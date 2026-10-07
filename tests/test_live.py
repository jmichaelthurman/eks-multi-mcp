"""Opt-in end-to-end checks against real clusters, using your real config and SSO sessions.

    EKS_MULTI_MCP_LIVE_TARGET=<non-prod target> \\
    EKS_MULTI_MCP_LIVE_PROTECTED_TARGET=<prod target> \\
    .venv/bin/python -m pytest -m live -v -s

Every write is a server-side dry run, followed by a read proving nothing persisted, and
each one is shown to you on the terminal and sent only if you approve it there; without a
terminal (no -s, or CI) the write checks skip. The protected target only ever receives
write calls the server must refuse before building a client or showing a prompt.
Skipped entirely when EKS_MULTI_MCP_LIVE_TARGET is unset.
"""

import os
import uuid

import pytest
from fakes import Approver, call, result, tool_error

from eks_multi_mcp.config import load_settings
from eks_multi_mcp.registry import normalize_env
from eks_multi_mcp.server import EksMultiServer

TARGET = os.environ.get("EKS_MULTI_MCP_LIVE_TARGET")
PROTECTED = os.environ.get("EKS_MULTI_MCP_LIVE_PROTECTED_TARGET")

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not TARGET, reason="set EKS_MULTI_MCP_LIVE_TARGET to run live checks")]

NS = "default"
PROBE = "kube-root-ca.crt"  # exists in every namespace of every cluster


def _server(**safety) -> EksMultiServer:
    s = load_settings()
    s.allow_write = safety.get("allow_write", False)
    s.allow_sensitive_data_access = safety.get("allow_sensitive_data_access", False)
    return EksMultiServer(s)


@pytest.fixture(scope="module")
def srv():
    srv = _server(allow_write=True)
    t = srv.registry.resolve(TARGET)
    protected = {normalize_env(e) for e in srv.settings.protected_envs}
    if t.env and normalize_env(t.env) in protected:
        pytest.exit(f"EKS_MULTI_MCP_LIVE_TARGET '{TARGET}' is in protected env '{t.env}'; use a non-prod target")
    return srv


class TerminalHuman:
    """Approval prompt answered by the person running the tests, on their terminal."""

    def __init__(self):
        try:
            self.tty = open("/dev/tty", "r+")  # noqa: SIM115 - held for the session
        except OSError:
            pytest.skip("live write checks need a terminal to ask you for approval (run with -s)")

    async def __call__(self, context, params):
        import mcp_types as types

        props = params.requested_schema["properties"]
        self.tty.write("\n" + "=" * 70 + "\n" + params.message + "\n")
        if "cluster_name" in props:
            self.tty.write("cluster name> ")
            self.tty.flush()
            return types.ElicitResult(action="accept", content={"cluster_name": self.tty.readline().strip()})
        self.tty.write("approve? [y/N]> ")
        self.tty.flush()
        yes = self.tty.readline().strip().lower() in ("y", "yes")
        return types.ElicitResult(action="accept" if yes else "decline", content={"approve": True} if yes else None)


@pytest.fixture(scope="module")
def human():
    return TerminalHuman()


def _absent(srv, kind, name):
    assert "404" in tool_error(srv, "get_k8s_resource", target=TARGET, kind=kind, name=name, namespace=NS)


def test_access(srv):
    out = result(srv, "describe_target", target=TARGET)
    access = out["result"]["access"]
    assert access["ok"] is True, access
    assert access["kubernetes_auth"] == "ok"
    assert out["_target"]["cluster"] == srv.registry.resolve(TARGET).cluster_name


@pytest.mark.parametrize("tool,args", [
    ("describe_cluster", {}),
    ("list_nodegroups", {}),
    ("list_addons", {}),
    ("get_eks_insights", {"category": "UPGRADE_READINESS"}),
    ("list_api_versions", {}),
    ("list_k8s_resources", {"kind": "Namespace", "limit": 5}),
    ("list_k8s_resources", {"kind": "Deployment", "api_version": "apps/v1", "namespace": "kube-system"}),
    ("get_k8s_resource", {"kind": "ConfigMap", "name": PROBE, "namespace": NS}),
    ("get_k8s_events", {"limit": 5}),
])
def test_read_tools(srv, tool, args):
    out = result(srv, tool, target=TARGET, **args)
    assert out["_target"]["mode"] == "read"


def test_fleet_tools_cover_the_target(srv):
    t = srv.registry.resolve(TARGET)
    out = result(srv, "fleet_overview", selector=t.alias)
    assert [c["target"] for c in out["clusters"]] == [t.alias]
    assert "error" not in out["clusters"][0], out["clusters"][0]
    out = result(srv, "fleet_list_k8s_resources", kind="Namespace", selector=t.alias)
    assert out["clusters"][0]["count"] > 0


def test_secret_values_redacted_without_sensitive_access():
    ro = _server()
    secrets = result(ro, "list_k8s_resources", target=TARGET, kind="Secret", namespace="kube-system", limit=1)
    if not secrets["result"]:
        pytest.skip("no Secrets in kube-system")
    name = secrets["result"][0]["name"]
    got = result(ro, "get_k8s_resource", target=TARGET, kind="Secret", name=name, namespace="kube-system",
                 output="json")["result"]
    assert all(v == "<redacted>" for v in (got.get("data") or {}).values())


def test_read_only_server_refuses_writes():
    assert "read-only" in tool_error(_server(), "manage_k8s_resource", target=TARGET, operation="delete",
                                     kind="ConfigMap", name=PROBE, namespace=NS, dry_run=True)


def test_apply_yaml_dry_run_persists_nothing(srv, human):
    name = f"eks-multi-mcp-live-{uuid.uuid4().hex[:8]}"
    doc = f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {name}, namespace: {NS}}}\ndata: {{k: v}}\n"
    out = result(srv, "apply_yaml", human, target=TARGET, yaml_content=doc, dry_run=True)
    assert out["_target"]["mode"] == "write" and out["result"][0]["dry_run"] is True
    _absent(srv, "ConfigMap", name)


@pytest.mark.parametrize("op", ["create", "replace", "patch", "delete"])
def test_manage_dry_run_persists_nothing(srv, human, op):
    before = result(srv, "get_k8s_resource", target=TARGET, kind="ConfigMap", name=PROBE, namespace=NS,
                    output="json")["result"]
    name = PROBE if op != "create" else f"eks-multi-mcp-live-{uuid.uuid4().hex[:8]}"
    body = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NS},
            "data": {"eks-multi-mcp-live": "dry-run"}}
    if op == "replace":
        body["metadata"]["resourceVersion"] = before["metadata"]["resourceVersion"]
    if op == "patch":
        body = {"metadata": {"labels": {"eks-multi-mcp-live": "dry-run"}}}
    out = result(srv, "manage_k8s_resource", human, target=TARGET, operation=op, kind="ConfigMap", name=name,
                 namespace=NS, body=None if op == "delete" else body, dry_run=True)
    assert out["_target"]["mode"] == "write" and out["dry_run"] is True
    if op == "create":
        _absent(srv, "ConfigMap", name)
    else:
        after = result(srv, "get_k8s_resource", target=TARGET, kind="ConfigMap", name=PROBE, namespace=NS,
                       output="json")["result"]
        assert after["metadata"]["resourceVersion"] == before["metadata"]["resourceVersion"]
        assert after.get("data") == before.get("data")


@pytest.mark.skipif(not PROTECTED, reason="set EKS_MULTI_MCP_LIVE_PROTECTED_TARGET to check the prod gate")
@pytest.mark.parametrize("tool,args", [
    ("manage_k8s_resource", {"operation": "delete", "kind": "ConfigMap", "name": PROBE, "namespace": NS}),
    ("apply_yaml", {"yaml_content": f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {PROBE}, "
                                    f"namespace: {NS}}}\n"}),
])
def test_protected_target_is_never_written(srv, monkeypatch, tool, args):
    """Even with a client that approves everything, prod is refused before any client is
    built, any prompt is shown, or any request is sent."""

    def no_clients(*a, **k):
        raise AssertionError("a Kubernetes client was built for a protected target")

    monkeypatch.setattr(srv.auth, "dynamic", no_clients)
    monkeypatch.setattr(srv.auth, "api_client", no_clients)
    rubber_stamp = Approver()
    msg = tool_error(srv, tool, rubber_stamp, target=PROTECTED, dry_run=True, **args)
    assert "forbidden" in msg and "is protected" in msg
    assert rubber_stamp.prompts == []


def test_unknown_target_lists_suggestions(srv):
    assert "unknown target" in tool_error(srv, "describe_cluster", target="definitely-not-a-cluster")


def test_list_targets_includes_target(srv):
    res = call(srv, "list_targets")
    assert srv.registry.resolve(TARGET).alias in res.content[0].text
