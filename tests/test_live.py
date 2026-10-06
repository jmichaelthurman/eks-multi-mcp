"""Opt-in end-to-end checks against real clusters, using your real config and SSO sessions.

    EKS_MULTI_MCP_LIVE_TARGET=<non-prod target> \\
    EKS_MULTI_MCP_LIVE_PROTECTED_TARGET=<prod target> \\
    .venv/bin/python -m pytest -m live -v

Every write is a server-side dry run, and each one is followed by a read proving nothing
persisted. The protected target only ever receives calls the gate must refuse before any
API request is made. Skipped entirely when EKS_MULTI_MCP_LIVE_TARGET is unset.
"""

import os
import uuid

import pytest
from fakes import call, result, tool_error

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


def test_apply_yaml_dry_run_persists_nothing(srv):
    name = f"eks-multi-mcp-live-{uuid.uuid4().hex[:8]}"
    doc = f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {name}, namespace: {NS}}}\ndata: {{k: v}}\n"
    out = result(srv, "apply_yaml", target=TARGET, yaml_content=doc, dry_run=True)
    assert out["_target"]["mode"] == "write" and out["result"][0]["dry_run"] is True
    _absent(srv, "ConfigMap", name)


@pytest.mark.parametrize("op", ["create", "replace", "patch", "delete"])
def test_manage_dry_run_persists_nothing(srv, op):
    before = result(srv, "get_k8s_resource", target=TARGET, kind="ConfigMap", name=PROBE, namespace=NS,
                    output="json")["result"]
    name = PROBE if op != "create" else f"eks-multi-mcp-live-{uuid.uuid4().hex[:8]}"
    body = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NS},
            "data": {"eks-multi-mcp-live": "dry-run"}}
    if op == "replace":
        body["metadata"]["resourceVersion"] = before["metadata"]["resourceVersion"]
    if op == "patch":
        body = {"metadata": {"labels": {"eks-multi-mcp-live": "dry-run"}}}
    out = result(srv, "manage_k8s_resource", target=TARGET, operation=op, kind="ConfigMap", name=name,
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
@pytest.mark.parametrize("confirm", [None, "wrong-cluster", PROTECTED])
def test_protected_gate_refuses_before_any_request(srv, monkeypatch, confirm):
    t = srv.registry.resolve(PROTECTED)
    if confirm == t.cluster_name:
        pytest.skip("the protected target was given by its cluster name; alias check not applicable")

    def no_clients(*a, **k):
        raise AssertionError("the gate let a request through to the cluster")

    monkeypatch.setattr(srv.auth, "dynamic", no_clients)
    msg = tool_error(srv, "manage_k8s_resource", target=PROTECTED, operation="delete", kind="ConfigMap",
                     name=PROBE, namespace=NS, dry_run=True, confirm_cluster=confirm)
    assert f"confirm_cluster='{t.cluster_name}'" in msg


def test_unknown_target_lists_suggestions(srv):
    assert "unknown target" in tool_error(srv, "describe_cluster", target="definitely-not-a-cluster")


def test_list_targets_includes_target(srv):
    res = call(srv, "list_targets")
    assert srv.registry.resolve(TARGET).alias in res.content[0].text
