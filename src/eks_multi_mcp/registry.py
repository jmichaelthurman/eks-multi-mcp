"""Target registry: merges the explicit cluster map, kubeconfig contexts and AWS discovery
into one deduplicated set of clusters, each with a resolved read and write profile."""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from .aws_profiles import AwsProfile, load_aws_profiles, pick_read_write, profiles_by_account
from .config import ClusterSettings, Settings
from .kubeconfig import KubeContext, load_kube_contexts

_ENV_TOKEN = re.compile(
    r"(?:^|[-_.])(dev|develop|development|stg|stage|staging|qa|uat|test|sandbox|prd|prod|production)(?:[-_.]|$)",
    re.IGNORECASE,
)
_ENV_NORMAL = {"develop": "dev", "development": "dev", "stage": "stg", "staging": "stg", "prd": "prod",
               "production": "prod"}


def normalize_env(env: str | None) -> str:
    """Canonical spelling of an env name, so `prd` and `prod` compare equal."""
    e = (env or "").lower()
    return _ENV_NORMAL.get(e, e)


def infer_env(*names: str | None) -> str | None:
    for n in names:
        if n and (m := _ENV_TOKEN.search(n)):
            tok = m.group(1).lower()
            return _ENV_NORMAL.get(tok, tok)
    return None


@dataclass
class Target:
    cluster_name: str
    region: str
    account_id: str | None
    alias: str
    aliases: set[str] = field(default_factory=set)
    env: str | None = None
    account_name: str | None = None
    read_profile: str | None = None
    write_profile: str | None = None
    role_arn: str | None = None
    read_only: bool = False
    endpoint: str | None = None
    ca_data: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # profile names referenced by kubeconfig exec blocks for this cluster
    kube_profiles: list[str] = field(default_factory=list)
    explicit_read_profile: bool = False
    explicit_write_profile: bool = False

    @property
    def key(self) -> str:
        return f"{self.account_id or '?'}/{self.region}/{self.cluster_name}"

    @property
    def arn(self) -> str | None:
        if not self.account_id:
            return None
        return f"arn:aws:eks:{self.region}:{self.account_id}:cluster/{self.cluster_name}"

    def stamp(self, mode: str = "read") -> dict:
        """Identity block attached to every tool response, so output is never anonymous."""
        return {
            "target": self.alias,
            "cluster": self.cluster_name,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "region": self.region,
            "env": self.env,
            "profile": self.write_profile if mode == "write" else self.read_profile,
            "mode": mode,
        }

    def summary(self) -> dict:
        d = asdict(self)
        d["aliases"] = sorted(self.aliases)
        d["arn"] = self.arn
        d.pop("ca_data", None)
        d.pop("kube_profiles", None)
        d.pop("explicit_read_profile", None)
        d.pop("explicit_write_profile", None)
        return d


class TargetNotFound(LookupError):
    pass


class Registry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profiles: dict[str, AwsProfile] = {}
        self.by_account: dict[str, list[AwsProfile]] = {}
        self.targets: dict[str, Target] = {}
        self.ambiguous: dict[str, set[str]] = {}
        self.skipped_contexts: list[str] = []
        self.global_warnings: list[str] = []
        self.reload()

    # ---------------------------------------------------------------- build
    def reload(self) -> None:
        s = self.settings
        self.profiles = load_aws_profiles(s.aws_config_path)
        self.by_account = profiles_by_account(self.profiles, s.exclude_profiles)
        self.targets = {}
        self.skipped_contexts = []
        self.global_warnings = []

        for c in s.clusters:
            self._add_explicit(c)
        if s.use_kubeconfig:
            for kc in load_kube_contexts(s.kubeconfig_paths):
                self._add_kube_context(kc)
        self._finalize()

    def _find(self, account_id: str | None, region: str, cluster: str) -> Target | None:
        if account_id:
            t = self.targets.get(f"{account_id}/{region}/{cluster}")
            if t:
                return t
        # Match account-less entries (or account-less lookups) on region+name when unambiguous.
        matches = [
            t for t in self.targets.values()
            if t.region == region and t.cluster_name == cluster
            and (account_id is None or t.account_id is None or t.account_id == account_id)
        ]
        return matches[0] if len(matches) == 1 else None

    def _rekey(self, t: Target, old_key: str) -> None:
        if old_key != t.key:
            self.targets.pop(old_key, None)
            self.targets[t.key] = t

    def _add_explicit(self, c: ClusterSettings) -> None:
        account_id = c.account_id
        if not account_id and c.read_profile and c.read_profile in self.profiles:
            account_id = self.profiles[c.read_profile].account_id
        t = Target(
            cluster_name=c.cluster_name,
            region=c.region,
            account_id=account_id,
            alias=c.alias or c.cluster_name,
            env=c.env,
            read_profile=c.read_profile,
            write_profile=c.write_profile,
            role_arn=c.role_arn,
            read_only=c.read_only,
            endpoint=c.endpoint,
            ca_data=c.ca_data,
            tags=dict(c.tags),
            sources=["cluster_map"],
            explicit_read_profile=bool(c.read_profile),
            explicit_write_profile=bool(c.write_profile),
        )
        t.aliases.update({t.alias, c.cluster_name, *c.aliases})
        self.targets[t.key] = t

    def _add_kube_context(self, kc: KubeContext) -> None:
        if any(fnmatch.fnmatch(kc.name, pat) for pat in self.settings.exclude_contexts):
            self.skipped_contexts.append(f"{kc.name} (excluded by config)")
            return
        if not kc.is_eks or not kc.cluster_name or not kc.region:
            self.skipped_contexts.append(f"{kc.name} (not an EKS context)")
            return

        account_id = kc.arn_account
        exec_prof = self.profiles.get(kc.exec_profile) if kc.exec_profile else None
        if not account_id and exec_prof:
            account_id = exec_prof.account_id

        t = self._find(account_id, kc.region, kc.cluster_name)
        if t is None:
            t = Target(cluster_name=kc.cluster_name, region=kc.region, account_id=account_id,
                       alias=kc.cluster_name)
            t.aliases.add(kc.cluster_name)
            self.targets[t.key] = t
        elif t.account_id is None and account_id:
            old = t.key
            t.account_id = account_id
            self._rekey(t, old)

        t.aliases.add(kc.name)
        t.sources.append(f"kubeconfig:{kc.name}")
        t.endpoint = t.endpoint or kc.server
        t.ca_data = t.ca_data or kc.ca_data
        for w in kc.warnings:
            t.warnings.append(f"context '{kc.name}': {w}")

        if kc.exec_role_arn and not t.role_arn:
            t.role_arn = kc.exec_role_arn

        if kc.exec_profile:
            if kc.exec_profile not in self.profiles:
                t.warnings.append(
                    f"context '{kc.name}': exec AWS_PROFILE '{kc.exec_profile}' is not defined in "
                    f"{self.settings.aws_config_path}"
                )
            elif exec_prof and exec_prof.account_id and t.account_id and exec_prof.account_id != t.account_id:
                t.warnings.append(
                    f"context '{kc.name}': exec AWS_PROFILE '{kc.exec_profile}' is account "
                    f"{exec_prof.account_id}, but the cluster lives in {t.account_id}; ignoring that profile"
                )
            else:
                t.kube_profiles.append(kc.exec_profile)
        elif kc.exec_command:
            t.warnings.append(
                f"context '{kc.name}': exec block sets no AWS_PROFILE, so kubectl inherits whatever "
                f"profile is ambient; this server resolves the profile from the account instead"
            )

    def add_discovered(self, profile: str, region: str, cluster: str, account_id: str | None) -> Target:
        t = self._find(account_id, region, cluster)
        if t is None:
            t = Target(cluster_name=cluster, region=region, account_id=account_id, alias=cluster)
            t.aliases.add(cluster)
            self.targets[t.key] = t
        t.sources.append(f"aws_discovery:{profile}")
        t.kube_profiles.append(profile)
        self._finalize()
        return t

    def _finalize(self) -> None:
        s = self.settings
        for t in self.targets.values():
            acct = s.accounts.get(t.account_id or "")
            candidates = list(self.by_account.get(t.account_id or "", []))
            kube_counts = Counter(t.kube_profiles)
            # Tie-break: profiles the kubeconfig already uses for this cluster, most-used first.
            candidates.sort(key=lambda p: (-kube_counts.get(p.name, 0), p.name.lower()))
            auto_read, auto_write = pick_read_write(candidates, s.read_role_patterns, s.write_role_patterns)

            if not t.explicit_read_profile:
                t.read_profile = (
                    (acct.read_profile if acct else None)
                    or (auto_read.name if auto_read else None)
                    or (kube_counts.most_common(1)[0][0] if kube_counts else None)
                )
            if not t.explicit_write_profile:
                t.write_profile = (
                    (acct.write_profile if acct else None)
                    or (auto_write.name if auto_write else None)
                    or t.read_profile
                )
            if acct:
                t.account_name = t.account_name or acct.name
                t.read_only = t.read_only or acct.read_only
                t.env = t.env or acct.env
            if not t.account_name and candidates:
                t.account_name = min((p.name for p in candidates), key=lambda n: (len(n), n))
            t.env = (t.env or infer_env(t.cluster_name, t.account_name, t.read_profile) or "").lower() or None
            if not t.read_profile:
                t.warnings.append(
                    f"no AWS profile in {s.aws_config_path} maps to account {t.account_id or '(unknown)'}; "
                    "add one or pin read_profile in the cluster map"
                )
            t.sources = list(dict.fromkeys(t.sources))
            t.warnings = list(dict.fromkeys(t.warnings))

        # A cluster name used in two accounts must not alias silently to one of them.
        name_counts = Counter(t.cluster_name for t in self.targets.values())
        for t in self.targets.values():
            if name_counts[t.cluster_name] > 1 and t.alias == t.cluster_name:
                t.aliases.discard(t.cluster_name)
                t.alias = f"{t.account_name or t.account_id}/{t.cluster_name}"
                t.aliases.add(t.alias)

        self.ambiguous = {}
        owners: dict[str, set[str]] = {}
        for t in self.targets.values():
            for a in t.aliases:
                owners.setdefault(a, set()).add(t.key)
        self.ambiguous = {a: keys for a, keys in owners.items() if len(keys) > 1}
        self._alias_index = {a: next(iter(keys)) for a, keys in owners.items() if len(keys) == 1}

    # -------------------------------------------------------------- resolve
    def resolve(self, ref: str) -> Target:
        ref = (ref or "").strip()
        if not ref:
            raise TargetNotFound("a target is required; call list_targets to see the options")
        if ref in self.targets:
            return self.targets[ref]
        if ref in self.ambiguous:
            opts = sorted(self.targets[k].alias for k in self.ambiguous[ref])
            raise TargetNotFound(f"'{ref}' is ambiguous; it matches {opts}")
        if ref in self._alias_index:
            return self.targets[self._alias_index[ref]]
        for t in self.targets.values():
            if ref == t.arn:
                return t
        if "/" in ref:
            scope, name = ref.rsplit("/", 1)
            hits = [
                t for t in self.targets.values()
                if t.cluster_name == name
                and scope in {t.account_id, t.account_name, t.read_profile, t.write_profile, t.env}
            ]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise TargetNotFound(f"'{ref}' is ambiguous; it matches {sorted(h.alias for h in hits)}")
        close = sorted(a for a in self._alias_index if ref.lower() in a.lower())[:10]
        hint = f" Did you mean one of {close}?" if close else " Call list_targets to see the options."
        raise TargetNotFound(f"unknown target '{ref}'.{hint}")

    def select(self, selector: str | None) -> list[Target]:
        """Select several targets.

        Syntax: comma-separated terms; each term is `env:<glob>`, `account:<id|name glob>`,
        `region:<glob>`, `tag:<k>=<v>`, `*` or a target name/alias glob.
        """
        targets = sorted(self.targets.values(), key=lambda t: (t.env or "", t.account_name or "", t.alias))
        if not selector or selector.strip() in {"*", "all"}:
            return targets
        out: dict[str, Target] = {}
        for term in (x.strip() for x in selector.split(",") if x.strip()):
            kind, _, val = term.partition(":") if ":" in term else ("name", "", term)
            for t in targets:
                if kind == "env":
                    ok = fnmatch.fnmatch(t.env or "", val)
                elif kind == "account":
                    ok = any(fnmatch.fnmatch(x or "", val) for x in (t.account_id, t.account_name))
                elif kind == "region":
                    ok = fnmatch.fnmatch(t.region, val)
                elif kind == "tag":
                    k, _, v = val.partition("=")
                    ok = fnmatch.fnmatch(t.tags.get(k, ""), v or "*") and k in t.tags
                else:
                    ok = any(fnmatch.fnmatch(a, term) for a in t.aliases | {t.alias, t.cluster_name})
                if ok:
                    out[t.key] = t
        return list(out.values())
