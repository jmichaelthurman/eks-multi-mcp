"""MCP tool surface. Every tool takes an explicit `target`; there is no implicit default
cluster, so an answer can never silently come from the wrong account."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import anyio
import yaml
from botocore.exceptions import BotoCoreError, ClientError
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic.exceptions import DynamicApiError, ResourceNotFoundError
from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import NoBackChannelError
from mcp_types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ToolAnnotations,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, Field, ValidationError

from .auth import AuthManager, CredentialError, explain_aws_error
from .config import Settings
from .registry import Registry, Target, TargetNotFound, normalize_env

log = logging.getLogger("eks_multi_mcp")

INSTRUCTIONS = """\
Multi-account, multi-cluster Amazon EKS server.

- Every tool takes `target`: a cluster alias, kube context name, cluster ARN, or
  `<account|profile|env>/<cluster>`. Call `list_targets` first; never guess.
- Every response carries `_target` (account, cluster, profile). Check it before you
  rely on the answer, especially when comparing environments.
- Fleet tools (`fleet_*`) take a selector such as `env:dev`, `account:team-*`,
  `web-*`, or `*`.
- Reads use each account's read profile; writes use its write profile.
- Writes to a protected environment (always prd/prod/production) or to a target whose
  environment is unknown are forbidden; there is no way to approve them.
- Every other write, dry runs included, pauses for a human to approve it in the MCP
  client. Clients that cannot show that prompt cannot write.
- Run `doctor` when access fails: it pinpoints expired SSO, stale kube contexts and
  exec blocks pointed at the wrong account.
"""

MAX_ITEMS = 500
SENSITIVE_KINDS = {"Secret"}
PROMPT_DETAIL_CHARS = 4000


class WriteApproval(BaseModel):
    approve: bool = Field(description="Approve this write")


APPROVAL_KEY = "approve"


class _AskForApproval(Exception):
    """Raised from a write tool's worker thread on a 2026-07-28 connection: the tool
    returns `result` (an InputRequiredResult) and the client retries with the answer."""

    def __init__(self, result: InputRequiredResult):
        super().__init__("approval required")
        self.result = result


class EksMultiServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.registry = Registry(settings)
        self.auth = AuthManager()
        self.mcp = MCPServer("eks-multi", instructions=INSTRUCTIONS, log_level=settings.log_level)
        self._register()

    # ------------------------------------------------------------ helpers
    def target(self, ref: str) -> Target:
        try:
            return self.registry.resolve(ref)
        except TargetNotFound as e:
            raise ToolError(str(e)) from e

    def require_sensitive(self, what: str) -> None:
        if not self.settings.allow_sensitive_data_access:
            raise ToolError(
                f"{what} requires sensitive data access; the server was started without "
                "--allow-sensitive-data-access"
            )

    def require_write(self, t: Target) -> None:
        """Hard gates, checked before any client is built or any prompt is shown."""
        if not self.settings.allow_write:
            raise ToolError("the server is read-only; restart it with --allow-write to enable writes")
        if t.read_only:
            raise ToolError(f"target '{t.alias}' is marked read_only in the cluster map")
        if not t.env:
            raise ToolError(f"writes to '{t.alias}' are forbidden: its environment is unknown, so it "
                            "could be production (set env for its account in the cluster map)")
        if self.is_protected(t):
            raise ToolError(f"writes to '{t.alias}' are forbidden: env '{t.env}' (account {t.account_id}) "
                            "is protected, and this server never writes to protected environments")

    def is_protected(self, t: Target) -> bool:
        protected = {normalize_env(e) for e in self.settings.protected_envs}
        return normalize_env(t.env) in protected

    def approve(self, ctx: Context, t: Target, action: str, detail: str, dry_run: bool) -> None:
        """Ask the human, through the MCP client, to approve one write. Called from a worker
        thread after require_write and validation, before anything uses the write profile. Fails closed:
        no prompt, no answer, or any answer but an explicit approval means no write.

        The calling agent never supplies the approval itself; it comes back from the client's
        elicitation prompt, which the client shows to its user."""
        refused = "nothing was sent to the cluster"
        try:
            can_prompt = ctx.session.check_client_capability(
                ClientCapabilities(elicitation=ElicitationCapability()))
        except Exception:  # noqa: BLE001 - no live session (e.g. called outside a client connection)
            can_prompt = False
        if not can_prompt:
            raise ToolError("writes need a human to approve them, but this MCP client cannot show an "
                            f"approval prompt (no elicitation support); {refused}")

        if self.is_protected(t):  # require_write already refused; never prompt for one
            raise ToolError(f"writes to protected env '{t.env}' are forbidden; {refused}")
        if len(detail) > PROMPT_DETAIL_CHARS:
            detail = detail[:PROMPT_DETAIL_CHARS] + f"\n... ({len(detail) - PROMPT_DETAIL_CHARS} more characters)"
        lines = [
            f"{'DRY RUN (server-side, not persisted)' if dry_run else 'WRITE'}: {action}",
            f"Cluster: {t.cluster_name}  ({t.region})",
            f"Account: {t.account_id} ({t.account_name or 'unnamed'})  env: {t.env or 'unknown'}",
            f"Write profile: {t.write_profile}",
            "",
            detail,
            "",
            "Approve this write?",
        ]
        message = "\n".join(lines)
        if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
            self._approve_by_round_trip(ctx, message, refused)
        else:
            try:
                answer = anyio.from_thread.run(ctx.elicit, message, WriteApproval)
            except NoBackChannelError as e:
                raise ToolError(f"this MCP connection cannot show an approval prompt; {refused}") from e
            except ValueError as e:
                raise ToolError(f"the approval response was malformed; {refused}") from e
            if answer.action != "accept":
                raise ToolError(f"the write was not approved ({answer.action}); {refused}")
            if answer.data.approve is not True:
                raise ToolError(f"the write was not approved; {refused}")
        log.warning("write approved by user: %s on %s (%s)", action, t.cluster_name, t.account_id)

    @staticmethod
    def _approve_by_round_trip(ctx: Context, message: str, refused: str) -> None:
        """2026-07-28 approval. The server cannot send elicitation/create mid-call, so the
        first round returns the prompt as an InputRequiredResult; the client shows it, then
        retries the same call with the human's answer.

        request_state carries a digest of the exact prompt. The SDK seals it (AES-GCM under
        a per-process key, bound to this tool and these arguments, 10-minute TTL), so the
        client can neither forge nor replay it, and the digest makes the retry fail if the
        target now resolves differently (e.g. after reload_config) from what was shown."""
        digest = "approval:v1:" + hashlib.sha256(message.encode()).hexdigest()
        state, responses = ctx.request_state, ctx.input_responses or {}
        if state is None or APPROVAL_KEY not in responses:
            prompt = ElicitRequest(params=ElicitRequestFormParams(
                message=message, requested_schema=render_elicitation_schema(WriteApproval)))
            raise _AskForApproval(InputRequiredResult(input_requests={APPROVAL_KEY: prompt}, request_state=digest))
        if state != digest:
            raise ToolError(f"the write no longer matches the one that was approved; {refused}")
        answer = responses[APPROVAL_KEY]
        if not isinstance(answer, ElicitResult):
            raise ToolError(f"the approval response was malformed; {refused}")
        if answer.action != "accept":
            raise ToolError(f"the write was not approved ({answer.action}); {refused}")
        try:
            approved = WriteApproval.model_validate(answer.content or {}).approve
        except ValidationError as e:
            raise ToolError(f"the approval response was malformed; {refused}") from e
        if approved is not True:
            raise ToolError(f"the write was not approved; {refused}")

    @staticmethod
    def wrap(t: Target, data: Any, mode: str = "read", **extra) -> dict:
        return {"_target": t.stamp(mode), **extra, "result": data}

    def _register(self) -> None:
        mcp = self.mcp

        read_only = ToolAnnotations(read_only_hint=True, open_world_hint=True)
        mutating = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

        def tool(fn=None, *, write: bool = False):
            """Register a sync implementation as an async tool run in a worker thread,
            mapping credential and API failures to readable tool errors."""
            if fn is None:
                return functools.partial(tool, write=write)

            @functools.wraps(fn)
            async def runner(*args, **kwargs):
                def call():
                    try:
                        return fn(*args, **kwargs)
                    except ToolError:
                        raise
                    except CredentialError as e:
                        raise ToolError(str(e)) from e
                    except (ApiException, DynamicApiError) as e:
                        raise ToolError(_k8s_error(e)) from e
                    except (BotoCoreError, ClientError) as e:
                        raise ToolError(explain_aws_error(e, None)) from e

                try:
                    return await anyio.to_thread.run_sync(call)
                except _AskForApproval as ask:
                    return ask.result

            return mcp.tool(annotations=mutating if write else read_only)(runner)

        # ================================================================ registry
        @tool
        def list_targets(selector: str | None = None) -> dict:
            """List the EKS clusters this server can reach, deduplicated across kube contexts.

            selector: optional filter, e.g. `env:prod`, `account:team-*`, `web-*`, `*`.
            """
            rows = []
            for t in self.registry.select(selector):
                rows.append({
                    "target": t.alias,
                    "cluster": t.cluster_name,
                    "env": t.env,
                    "account_id": t.account_id,
                    "account_name": t.account_name,
                    "region": t.region,
                    "read_profile": t.read_profile,
                    "write_profile": t.write_profile,
                    "read_only": t.read_only,
                    "aliases": sorted(t.aliases - {t.alias}),
                    "warnings": len(t.warnings),
                })
            return {
                "count": len(rows),
                "write_enabled": self.settings.allow_write,
                "sensitive_data_enabled": self.settings.allow_sensitive_data_access,
                "targets": rows,
            }

        @tool
        def describe_target(target: str, check_access: bool = True) -> dict:
            """Show how a target was resolved (sources, profiles, warnings) and, if check_access,
            verify the read profile's identity and that the cluster exists."""
            t = self.target(target)
            out: dict[str, Any] = {"resolution": t.summary()}
            if check_access:
                out["access"] = self._probe(t, k8s=True)
            return self.wrap(t, out)

        @tool
        def doctor(selector: str | None = None, check_access: bool = True) -> dict:
            """Audit configuration across targets: kube exec blocks that point at the wrong
            account or cluster, contexts with no AWS_PROFILE, unmapped accounts, ambiguous
            aliases and, if check_access, expired SSO sessions and clusters that no longer exist."""
            targets = self.registry.select(selector)
            report: dict[str, Any] = {
                "config_file": self.settings.source_path,
                "kubeconfig": self.settings.kubeconfig_paths if self.settings.use_kubeconfig else None,
                "aws_config": self.settings.aws_config_path,
                "ambiguous_aliases": {a: sorted(self.registry.targets[k].alias for k in ks)
                                      for a, ks in self.registry.ambiguous.items()},
                "skipped_contexts": self.registry.skipped_contexts,
            }
            probes: dict[str, dict] = {}
            if check_access:
                probes = _fan_out(targets, lambda t: self._probe(t, k8s=False))
            rows = []
            for t in targets:
                row = {"target": t.alias, "account": t.account_name or t.account_id, "env": t.env,
                       "read_profile": t.read_profile, "warnings": t.warnings}
                if check_access:
                    row["access"] = probes.get(t.alias)
                rows.append(row)
            problems = [r for r in rows if r["warnings"] or (r.get("access") or {}).get("ok") is False]
            report["targets_checked"] = len(rows)
            report["targets_with_problems"] = len(problems)
            report["targets"] = rows
            return report

        @tool
        def discover_clusters(profiles: list[str] | None = None, regions: list[str] | None = None) -> dict:
            """Find clusters missing from kubeconfig by calling eks:ListClusters.

            By default one profile per account (its read profile) is queried in that profile's
            region. New clusters become targets immediately (for this server process).
            """
            regs = regions or self.settings.discovery_regions or None
            if profiles:
                chosen = profiles
            else:
                chosen = []
                for acct, profs in self.registry.by_account.items():
                    known = [t.read_profile for t in self.registry.targets.values() if t.account_id == acct]
                    chosen.append(next((p for p in known if p), profs[0].name))
            results: dict[str, Any] = {}
            added: list[str] = []

            def scan(profile: str) -> dict:
                prof = self.registry.profiles.get(profile)
                sess = self.auth.base_session(profile)
                out: dict[str, Any] = {}
                for region in regs or [prof.region if prof and prof.region else "us-east-1"]:
                    try:
                        eks = sess.client("eks", region_name=region)
                        names = [n for page in eks.get_paginator("list_clusters").paginate()
                                 for n in page["clusters"]]
                        out[region] = names
                    except (BotoCoreError, ClientError) as e:
                        out[region] = {"error": explain_aws_error(e, profile)}
                return out

            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(scan, p): p for p in chosen}
                for f in as_completed(futs):
                    results[futs[f]] = f.result()

            for profile, by_region in results.items():
                acct = (self.registry.profiles.get(profile) or None)
                acct_id = acct.account_id if acct else None
                for region, names in by_region.items():
                    if isinstance(names, dict):
                        continue
                    for n in names:
                        before = set(self.registry.targets)
                        t = self.registry.add_discovered(profile, region, n, acct_id)
                        if t.key not in before:
                            added.append(t.alias)
            return {"queried": results, "new_targets": sorted(added)}

        @tool
        def reload_config() -> dict:
            """Re-read kubeconfig and ~/.aws/config, and drop cached clients. The config file
            (cluster map and safety policy) is read only at startup; restart to change it."""
            self.registry.reload()
            self.auth.invalidate()
            return {"targets": len(self.registry.targets)}

        # ================================================================ EKS / AWS
        @tool
        def describe_cluster(target: str) -> dict:
            """eks:DescribeCluster for one target (version, status, networking, logging, access config)."""
            t = self.target(target)
            c = self.auth.describe_cluster(t, refresh=True)
            c.pop("certificateAuthority", None)
            return self.wrap(t, _jsonable(c))

        @tool
        def list_nodegroups(target: str, include_fargate: bool = True) -> dict:
            """Managed node groups (with version, AMI, scaling, status) and Fargate profiles."""
            t = self.target(target)
            eks = self.auth.aws(t, "eks")
            groups = []
            for page in eks.get_paginator("list_nodegroups").paginate(clusterName=t.cluster_name):
                for ng in page["nodegroups"]:
                    d = eks.describe_nodegroup(clusterName=t.cluster_name, nodegroupName=ng)["nodegroup"]
                    groups.append({
                        "name": ng, "status": d.get("status"), "version": d.get("version"),
                        "releaseVersion": d.get("releaseVersion"), "amiType": d.get("amiType"),
                        "capacityType": d.get("capacityType"), "instanceTypes": d.get("instanceTypes"),
                        "scaling": d.get("scalingConfig"), "health": d.get("health", {}).get("issues"),
                        "labels": d.get("labels"), "taints": d.get("taints"),
                    })
            out: dict[str, Any] = {"nodegroups": groups}
            if include_fargate:
                out["fargate_profiles"] = [
                    p for page in eks.get_paginator("list_fargate_profiles").paginate(clusterName=t.cluster_name)
                    for p in page["fargateProfileNames"]
                ]
            return self.wrap(t, out)

        @tool
        def list_addons(target: str) -> dict:
            """EKS add-ons with versions, status and health issues."""
            t = self.target(target)
            eks = self.auth.aws(t, "eks")
            addons = []
            for page in eks.get_paginator("list_addons").paginate(clusterName=t.cluster_name):
                for a in page["addons"]:
                    d = eks.describe_addon(clusterName=t.cluster_name, addonName=a)["addon"]
                    addons.append({"name": a, "version": d.get("addonVersion"), "status": d.get("status"),
                                   "issues": d.get("health", {}).get("issues"),
                                   "serviceAccountRoleArn": d.get("serviceAccountRoleArn")})
            return self.wrap(t, addons)

        @tool
        def get_eks_insights(target: str, category: Literal["UPGRADE_READINESS", "CONFIGURATION"] | None = None,
                             insight_id: str | None = None) -> dict:
            """EKS cluster insights (upgrade readiness, misconfiguration). Pass insight_id for details."""
            t = self.target(target)
            eks = self.auth.aws(t, "eks")
            if insight_id:
                return self.wrap(t, _jsonable(eks.describe_insight(clusterName=t.cluster_name, id=insight_id)["insight"]))
            kw: dict[str, Any] = {"clusterName": t.cluster_name}
            if category:
                kw["filter"] = {"categories": [category]}
            items = [i for page in eks.get_paginator("list_insights").paginate(**kw) for i in page["insights"]]
            return self.wrap(t, _jsonable(items))

        @tool
        def get_cloudwatch_logs(target: str, log_type: Literal["application", "host", "dataplane", "performance",
                                                               "control-plane"] = "application",
                                filter_pattern: str = "", minutes: int = 30, limit: int = 100,
                                log_group: str | None = None) -> dict:
            """Search CloudWatch logs for a cluster (Container Insights groups, or the control plane).
            Requires sensitive data access."""
            self.require_sensitive("reading CloudWatch logs")
            t = self.target(target)
            group = log_group or (f"/aws/eks/{t.cluster_name}/cluster" if log_type == "control-plane"
                                  else f"/aws/containerinsights/{t.cluster_name}/{log_type}")
            logs = self.auth.aws(t, "logs")
            start = int((time.time() - minutes * 60) * 1000)
            kw: dict[str, Any] = {"logGroupName": group, "startTime": start, "limit": min(limit, 1000)}
            if filter_pattern:
                kw["filterPattern"] = filter_pattern
            events = logs.filter_log_events(**kw).get("events", [])
            rows = [{"ts": datetime.fromtimestamp(e["timestamp"] / 1000, UTC).isoformat(),
                     "stream": e.get("logStreamName"), "message": e.get("message")} for e in events]
            return self.wrap(t, rows, log_group=group)

        @tool
        def get_cloudwatch_metrics(target: str, metric_name: str, namespace: str = "ContainerInsights",
                                   dimensions: dict[str, str] | None = None, minutes: int = 60,
                                   period: int = 300, stat: str = "Average") -> dict:
            """CloudWatch metric datapoints. ClusterName is added to dimensions automatically
            for the ContainerInsights namespace."""
            t = self.target(target)
            dims = dict(dimensions or {})
            if namespace == "ContainerInsights":
                dims.setdefault("ClusterName", t.cluster_name)
            cw = self.auth.aws(t, "cloudwatch")
            end = datetime.now(UTC)
            resp = cw.get_metric_statistics(
                Namespace=namespace, MetricName=metric_name,
                Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
                StartTime=end - timedelta(minutes=minutes), EndTime=end, Period=period, Statistics=[stat],
            )
            pts = sorted(resp.get("Datapoints", []), key=lambda p: p["Timestamp"])
            return self.wrap(t, [{"ts": p["Timestamp"].isoformat(), stat: p.get(stat), "unit": p.get("Unit")}
                                 for p in pts], dimensions=dims)

        @tool
        def get_policies_for_role(target: str, role_name: str) -> dict:
            """Trust policy, attached managed policies and inline policies for an IAM role in the
            target's account (useful for IRSA / Pod Identity debugging)."""
            t = self.target(target)
            iam = self.auth.aws(t, "iam")
            role = iam.get_role(RoleName=role_name)["Role"]
            attached = [p for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name)
                        for p in page["AttachedPolicies"]]
            inline = {}
            for page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name):
                for name in page["PolicyNames"]:
                    inline[name] = iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]
            return self.wrap(t, _jsonable({"arn": role["Arn"], "trust_policy": role["AssumeRolePolicyDocument"],
                                           "attached": attached, "inline": inline}))

        # ================================================================ Kubernetes
        @tool
        def list_api_versions(target: str) -> dict:
            """API group versions served by the cluster."""
            t = self.target(target)
            from kubernetes import client as kc
            api = self.auth.api_client(t)
            groups = kc.ApisApi(api).get_api_versions()
            versions = ["v1"] + [v.group_version for g in groups.groups for v in g.versions]
            ver = kc.VersionApi(api).get_code()
            return self.wrap(t, {"server_version": ver.git_version, "api_versions": versions})

        @tool
        def list_k8s_resources(target: str, kind: str, api_version: str = "v1", namespace: str | None = None,
                               label_selector: str | None = None, field_selector: str | None = None,
                               limit: int = 200) -> dict:
            """List Kubernetes resources of one kind (summaries: name, namespace, age, status).

            namespace=None lists across all namespaces for namespaced kinds.
            """
            t = self.target(target)
            res = self._resource(t, api_version, kind)
            kw = _selectors(label_selector, field_selector)
            kw["limit"] = min(limit, MAX_ITEMS)
            objs = res.get(namespace=namespace, **kw) if res.namespaced else res.get(**kw)
            items = [_summarize(o) for o in objs.to_dict().get("items", [])]
            more = bool((objs.to_dict().get("metadata") or {}).get("continue"))
            return self.wrap(t, items, count=len(items), truncated=more)

        @tool
        def get_k8s_resource(target: str, kind: str, name: str, api_version: str = "v1",
                             namespace: str | None = None, output: Literal["yaml", "json"] = "yaml") -> dict:
            """Get one Kubernetes resource in full (managedFields stripped). Secret values are
            redacted unless sensitive data access is enabled."""
            t = self.target(target)
            res = self._resource(t, api_version, kind)
            obj = (res.get(name=name, namespace=namespace) if res.namespaced else res.get(name=name)).to_dict()
            (obj.get("metadata") or {}).pop("managedFields", None)
            kinds = {kind, res.kind, getattr(res, "base_kind", None)}
            if kinds & SENSITIVE_KINDS and not self.settings.allow_sensitive_data_access:
                for k in ("data", "stringData"):
                    if obj.get(k):
                        obj[k] = {key: "<redacted>" for key in obj[k]}
            body = yaml.safe_dump(obj, sort_keys=False) if output == "yaml" else obj
            return self.wrap(t, body)

        @tool
        def get_k8s_events(target: str, namespace: str | None = None, involved_kind: str | None = None,
                           involved_name: str | None = None, warnings_only: bool = False,
                           limit: int = 100) -> dict:
            """Recent Kubernetes events, newest first, optionally scoped to one object."""
            t = self.target(target)
            from kubernetes import client as kc
            core = kc.CoreV1Api(self.auth.api_client(t))
            fs = []
            if involved_kind:
                fs.append(f"involvedObject.kind={involved_kind}")
            if involved_name:
                fs.append(f"involvedObject.name={involved_name}")
            if warnings_only:
                fs.append("type=Warning")
            kw = {"field_selector": ",".join(fs)} if fs else {}
            ev = (core.list_namespaced_event(namespace, **kw) if namespace
                  else core.list_event_for_all_namespaces(**kw)).items

            def when(e):
                return e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp

            ev.sort(key=lambda e: when(e) or datetime.min.replace(tzinfo=UTC), reverse=True)
            rows = [{"time": (when(e).isoformat() if when(e) else None), "type": e.type, "reason": e.reason,
                     "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                     "namespace": e.metadata.namespace, "count": e.count, "message": e.message}
                    for e in ev[:limit]]
            return self.wrap(t, rows, count=len(rows))

        @tool
        def get_pod_logs(target: str, namespace: str, pod_name: str, container: str | None = None,
                         tail_lines: int = 200, since_seconds: int | None = None, previous: bool = False) -> dict:
            """Container logs for a pod. Requires sensitive data access."""
            self.require_sensitive("reading pod logs")
            t = self.target(target)
            from kubernetes import client as kc
            core = kc.CoreV1Api(self.auth.api_client(t))
            kw: dict[str, Any] = {"tail_lines": min(tail_lines, 5000), "previous": previous}
            if container:
                kw["container"] = container
            if since_seconds:
                kw["since_seconds"] = since_seconds
            return self.wrap(t, core.read_namespaced_pod_log(pod_name, namespace, **kw))

        @tool(write=True)
        def apply_yaml(ctx: Context, target: str, yaml_content: str, namespace: str | None = None,
                       dry_run: bool = False, force_conflicts: bool = False) -> dict:
            """Server-side apply one or more YAML documents, authenticated with the target's
            write profile. Requires --allow-write and a human's approval in the MCP client.
            Forbidden in protected envs (prd/prod/production) and when the env is unknown."""
            t = self.target(target)
            self.require_write(t)
            try:
                docs = [d for d in yaml.safe_load_all(yaml_content) if d]
            except yaml.YAMLError as e:
                raise ToolError(f"invalid YAML: {e}") from e
            if not docs:
                raise ToolError("no YAML documents found")
            # Resolve and validate every document before applying any, so a bad document
            # late in the stream cannot leave the earlier ones half-applied.
            plan = []
            for i, d in enumerate(docs, 1):
                if not isinstance(d, dict):
                    raise ToolError(f"document {i} is not a mapping")
                md = d.get("metadata") or {}
                if not (d.get("apiVersion") and d.get("kind") and md.get("name")):
                    raise ToolError(f"document {i} needs apiVersion, kind and metadata.name")
                res = self._resource(t, d["apiVersion"], d["kind"])
                ns = _namespace(res, md.get("namespace") or namespace, f"document {i} ({d['kind']})")
                plan.append((d, ns))
            objects = "\n".join(f"  {d['kind']} {(ns + '/') if ns else ''}{d['metadata']['name']}" for d, ns in plan)
            self.approve(ctx, t, f"server-side apply of {len(plan)} object(s)"
                         + (" with force_conflicts" if force_conflicts else ""),
                         f"{objects}\n\n" + yaml.safe_dump_all([d for d, _ in plan], sort_keys=False), dry_run)
            dyn = self.auth.dynamic(t, "write")
            results = []
            for d, ns in plan:
                res = self._resource(t, d["apiVersion"], d["kind"], mode="write")
                kw: dict[str, Any] = {"field_manager": "eks-multi-mcp", "force_conflicts": force_conflicts}
                if dry_run:
                    kw["dry_run"] = "All"
                out = dyn.server_side_apply(res, body=d, name=d["metadata"]["name"], namespace=ns, **kw)
                results.append({"kind": d["kind"], "name": d["metadata"]["name"], "namespace": ns,
                                "resourceVersion": out.metadata.resourceVersion, "dry_run": dry_run})
            return self.wrap(t, results, mode="write")

        @tool(write=True)
        def manage_k8s_resource(ctx: Context, target: str,
                                operation: Literal["create", "replace", "patch", "delete"],
                                kind: str, name: str, api_version: str = "v1", namespace: str | None = None,
                                body: dict | None = None, dry_run: bool = False) -> dict:
            """Create, replace, merge-patch or delete one resource with the target's write profile.
            Requires --allow-write and a human's approval in the MCP client. Forbidden in
            protected envs (prd/prod/production) and when the env is unknown."""
            t = self.target(target)
            self.require_write(t)
            ns = _namespace(self._resource(t, api_version, kind), namespace, kind)
            if operation != "delete" and body is None:
                raise ToolError(f"'{operation}' needs a body")
            self.approve(ctx, t, f"{operation} {api_version} {kind} {(ns + '/') if ns else ''}{name}",
                         "" if body is None else yaml.safe_dump(body, sort_keys=False), dry_run)
            res = self._resource(t, api_version, kind, mode="write")
            kw: dict[str, Any] = {"dry_run": "All"} if dry_run else {}
            if operation == "delete":
                out = res.delete(name=name, namespace=ns, **kw)
            else:
                if operation == "create":
                    out = res.create(body=body, namespace=ns, **kw)
                elif operation == "replace":
                    out = res.replace(body=body, name=name, namespace=ns, **kw)
                else:
                    out = res.patch(body=body, name=name, namespace=ns,
                                    content_type="application/merge-patch+json", **kw)
            d = out.to_dict() if hasattr(out, "to_dict") else out
            if isinstance(d, dict) and d.get("kind") == "Status":
                # Deletes may answer with a Status instead of the object.
                d = {"status": d.get("status"), "details": d.get("details"), "message": d.get("message")}
            elif isinstance(d, dict) and "metadata" in d:
                d = _summarize(d)
            return self.wrap(t, d, mode="write",
                             operation=operation, dry_run=dry_run)

        # ================================================================ fleet
        @tool
        def fleet_overview(selector: str = "*", include_insights: bool = False) -> dict:
            """Version, status, platform version and endpoint access for many clusters at once
            (optionally with upgrade-insight counts). Unreachable targets are reported, not fatal."""
            def one(t: Target) -> dict:
                c = self.auth.describe_cluster(t, refresh=True)
                vpc = c.get("resourcesVpcConfig", {})
                row = {"version": c.get("version"), "status": c.get("status"),
                       "platformVersion": c.get("platformVersion"),
                       "endpointPublic": vpc.get("endpointPublicAccess"),
                       "endpointPrivate": vpc.get("endpointPrivateAccess"),
                       "authMode": (c.get("accessConfig") or {}).get("authenticationMode"),
                       "upgradePolicy": (c.get("upgradePolicy") or {}).get("supportType"),
                       "createdAt": c.get("createdAt").isoformat() if c.get("createdAt") else None}
                if include_insights:
                    eks = self.auth.aws(t, "eks")
                    ins = eks.list_insights(clusterName=t.cluster_name,
                                            filter={"categories": ["UPGRADE_READINESS"]})["insights"]
                    row["upgradeInsights"] = {s: sum(1 for i in ins if i.get("insightStatus", {}).get("status") == s)
                                              for s in ("ERROR", "WARNING", "PASSING", "UNKNOWN")}
                return row

            targets = self.registry.select(selector)
            res = _fan_out(targets, one)
            return {"count": len(targets), "clusters": [
                {**t.stamp(), **res[t.alias]} for t in targets]}

        @tool
        def fleet_list_k8s_resources(kind: str, selector: str = "*", api_version: str = "v1",
                                     namespace: str | None = None, label_selector: str | None = None,
                                     field_selector: str | None = None, names_limit: int = 50) -> dict:
            """List one resource kind across many clusters, e.g. find which clusters run a
            Deployment or compare CRDs across environments. Returns count + names per cluster."""
            def one(t: Target) -> dict:
                res = self._resource(t, api_version, kind)
                kw = _selectors(label_selector, field_selector)
                objs = (res.get(namespace=namespace, **kw) if res.namespaced else res.get(**kw)).to_dict()
                items = objs.get("items", [])
                names = [((i["metadata"].get("namespace") + "/") if i["metadata"].get("namespace") else "")
                         + i["metadata"]["name"] for i in items]
                return {"count": len(items), "names": names[:names_limit], "truncated": len(names) > names_limit}

            targets = self.registry.select(selector)
            res = _fan_out(targets, one)
            return {"kind": kind, "count": len(targets),
                    "clusters": [{**t.stamp(), **res[t.alias]} for t in targets]}

    # ------------------------------------------------------------ internals
    def _resource(self, t: Target, api_version: str, kind: str, mode: str = "read"):
        try:
            return self.auth.dynamic(t, mode).resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError as e:
            raise ToolError(f"{t.alias}: no resource kind '{kind}' in {api_version} "
                            "(use list_api_versions to check)") from e

    def _probe(self, t: Target, k8s: bool) -> dict:
        out: dict[str, Any] = {"ok": False}
        try:
            ident = self.auth.caller_identity(t)
            out["identity"] = ident["arn"]
            if t.account_id and ident["account"] != t.account_id:
                out["error"] = (f"profile '{t.read_profile}' authenticates into account {ident['account']}, "
                                f"not {t.account_id}")
                return out
        except CredentialError as e:
            out["error"] = str(e)
            return out
        try:
            c = self.auth.describe_cluster(t, refresh=True)
            out.update(cluster_status=c.get("status"), version=c.get("version"))
            if t.endpoint and c.get("endpoint") and t.endpoint.rstrip("/") != c["endpoint"].rstrip("/"):
                out["warning"] = "kubeconfig endpoint differs from the live cluster endpoint; using the live one"
                t.endpoint, t.ca_data = c["endpoint"], c["certificateAuthority"]["data"]
                self.auth.invalidate(t)
        except CredentialError as e:
            msg = str(e)
            out["error"] = ("cluster not found in this account/region (stale kube context?)"
                            if "ResourceNotFoundException" in msg else msg)
            return out
        if k8s:
            try:
                from kubernetes import client as kc
                out["kubernetes_version"] = kc.VersionApi(self.auth.api_client(t)).get_code().git_version
                kc.CoreV1Api(self.auth.api_client(t)).list_namespace(limit=1)
                out["kubernetes_auth"] = "ok"
            except (ApiException, DynamicApiError) as e:
                out["error"] = _k8s_error(e)
                return out
            except Exception as e:  # noqa: BLE001 - network: private endpoint without VPN, etc.
                out["error"] = f"cannot reach the API server: {type(e).__name__}: {e}"
                return out
        out["ok"] = True
        return out


# ------------------------------------------------------------------ utilities
def _fan_out(targets: list[Target], fn, workers: int = 8, timeout: float = 60) -> dict[str, dict]:
    out: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    futs = {pool.submit(fn, t): t for t in targets}
    try:
        for f in as_completed(futs, timeout=timeout * max(1, len(targets) // workers + 1)):
            t = futs[f]
            try:
                out[t.alias] = f.result()
            except ToolError as e:
                out[t.alias] = {"error": str(e)}
            except CredentialError as e:
                out[t.alias] = {"error": str(e)}
            except (ApiException, DynamicApiError) as e:
                out[t.alias] = {"error": _k8s_error(e)}
            except Exception as e:  # noqa: BLE001 - one bad cluster must not sink the fleet call
                out[t.alias] = {"error": f"{type(e).__name__}: {e}"}
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for t in targets:
        out.setdefault(t.alias, {"error": "timed out"})
    return out


def _k8s_error(e: Exception) -> str:
    status = getattr(e, "status", None)
    reason = getattr(e, "reason", "")
    body = getattr(e, "body", None)
    msg = ""
    if body:
        try:
            msg = json.loads(body).get("message", "")
        except (ValueError, TypeError, AttributeError):
            msg = str(body)[:300]
    hint = {401: " (token rejected: the IAM principal is not mapped to this cluster)",
            403: " (RBAC: the IAM principal lacks this permission)"}.get(status, "")
    return f"Kubernetes API {status} {reason}: {msg}{hint}".strip()


def _namespace(res, namespace: str | None, what: str) -> str | None:
    """Namespace to send for a write: required for namespaced kinds, dropped for cluster-scoped."""
    if not res.namespaced:
        return None
    if not namespace:
        raise ToolError(f"{what} is namespaced; pass a namespace")
    return namespace


def _selectors(label_selector: str | None, field_selector: str | None) -> dict:
    kw: dict[str, Any] = {}
    if label_selector:
        kw["label_selector"] = label_selector
    if field_selector:
        kw["field_selector"] = field_selector
    return kw


def _summarize(o: dict) -> dict:
    md = o.get("metadata") or {}
    st = o.get("status") or {}
    row: dict[str, Any] = {"name": md.get("name")}
    if md.get("namespace"):
        row["namespace"] = md["namespace"]
    row["created"] = md.get("creationTimestamp")
    if isinstance(st, dict):
        if "phase" in st:
            row["phase"] = st["phase"]
        if "replicas" in st or "readyReplicas" in st:
            row["ready"] = f"{st.get('readyReplicas', 0)}/{(o.get('spec') or {}).get('replicas', st.get('replicas'))}"
        conds = [c for c in st.get("conditions") or [] if isinstance(c, dict)]
        bad = [c["type"] for c in conds if c.get("status") == "False" and c.get("type") in
               {"Ready", "Available", "Healthy", "Synced"}]
        if bad:
            row["not"] = bad
    if md.get("labels"):
        row["labels"] = md["labels"]
    return row


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)))
