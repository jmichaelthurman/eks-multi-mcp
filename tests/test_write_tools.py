"""apply_yaml and manage_k8s_resource: gates, what reaches the API server, and response shape."""

import pytest
from fakes import FakeDynamic, FakeResource, install_dynamic, make_server, result, tool_error

CM = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm", "namespace": "app"}, "data": {"k": "v"}}


@pytest.fixture
def writable(files, monkeypatch):
    srv = make_server(files, safety={"allow_write": True})
    cm = FakeResource("ConfigMap")
    ns = FakeResource("Namespace", namespaced=False)
    dyn = FakeDynamic(cm, ns)
    modes = install_dynamic(monkeypatch, srv, dyn)
    return srv, dyn, cm, ns, modes


# ------------------------------------------------------------------ gates
@pytest.mark.parametrize("tool,args", [
    ("apply_yaml", {"yaml_content": "kind: x"}),
    ("manage_k8s_resource", {"operation": "delete", "kind": "ConfigMap", "name": "cm", "namespace": "app"}),
])
def test_write_tools_refuse_without_allow_write(files, monkeypatch, tool, args):
    srv = make_server(files)
    dyn = FakeDynamic(FakeResource("ConfigMap"))
    modes = install_dynamic(monkeypatch, srv, dyn)
    assert "read-only" in tool_error(srv, tool, target="dev", **args)
    assert modes == []  # refused before any client was built


@pytest.mark.parametrize("confirm", [None, "prod-blue", "web-prod-blue ", "WEB-PROD-BLUE"])
def test_protected_env_refuses_anything_but_the_exact_cluster_name(writable, confirm):
    srv, _, cm, _, modes = writable
    msg = tool_error(srv, "manage_k8s_resource", target="prod-blue", operation="delete", kind="ConfigMap",
                     name="cm", namespace="app", dry_run=True, confirm_cluster=confirm)
    assert "confirm_cluster='web-prod-blue'" in msg
    assert modes == [] and cm.calls == []


def test_protected_env_proceeds_with_exact_confirm(writable):
    srv, _, cm, _, _ = writable
    out = result(srv, "manage_k8s_resource", target="prod-blue", operation="delete", kind="ConfigMap",
                 name="cm", namespace="app", dry_run=True, confirm_cluster="web-prod-blue")
    assert out["_target"]["cluster"] == "web-prod-blue"
    assert out["_target"]["mode"] == "write"
    assert cm.calls == [("delete", {"name": "cm", "namespace": "app", "dry_run": "All"})]


@pytest.mark.parametrize("confirm", ["shop", "prod-blue", "333333333333/web-prod-blue"])
def test_protected_env_rejects_aliases_as_confirmation(files, monkeypatch, confirm):
    """The confirmation must be the real cluster name, never an alias that resolved to it."""
    srv = make_server(files, safety={"allow_write": True},
                      clusters=[{"cluster_name": "web-prod-blue", "region": "us-east-1",
                                 "account_id": "333333333333", "alias": "shop"}])
    assert srv.registry.resolve(confirm).cluster_name == "web-prod-blue"
    cm = FakeResource("ConfigMap")
    install_dynamic(monkeypatch, srv, FakeDynamic(cm))
    msg = tool_error(srv, "manage_k8s_resource", target=confirm, operation="delete", kind="ConfigMap",
                     name="cm", namespace="app", confirm_cluster=confirm)
    assert "confirm_cluster='web-prod-blue'" in msg and cm.calls == []


@pytest.mark.parametrize("env,protected", [
    ("prd", ["prod"]), ("prod", ["prd"]), ("PRD", ["prod"]), ("production", ["prd"]), ("Prod", ["PROD"]),
])
def test_protected_env_matches_across_spellings(files, monkeypatch, env, protected):
    """Regression: an explicit `env: prd` slipped past `protected_envs: [prod]`."""
    srv = make_server(files, safety={"allow_write": True, "protected_envs": protected},
                      accounts={"111111111111": {"env": env}})
    install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    msg = tool_error(srv, "manage_k8s_resource", target="dev", operation="delete", kind="ConfigMap",
                     name="cm", namespace="app")
    assert "protected env" in msg


def test_unprotected_env_needs_no_confirm(writable):
    srv, _, cm, _, _ = writable
    result(srv, "manage_k8s_resource", target="dev", operation="delete", kind="ConfigMap", name="cm",
           namespace="app")
    assert cm.calls[0][0] == "delete"


def test_account_read_only_beats_allow_write(files, monkeypatch):
    srv = make_server(files, safety={"allow_write": True}, accounts={"111111111111": {"read_only": True}})
    install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    assert "read_only" in tool_error(srv, "apply_yaml", target="dev", yaml_content="kind: x")


def test_unknown_target_is_a_clean_error(writable):
    srv, *_ = writable
    assert "unknown target 'nope'" in tool_error(srv, "apply_yaml", target="nope", yaml_content="kind: x")


# ------------------------------------------------------------------ manage_k8s_resource
@pytest.mark.parametrize("op", ["create", "replace", "patch", "delete"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_manage_operations_use_write_client_and_forward_dry_run(writable, op, dry_run):
    srv, _, cm, _, modes = writable
    out = result(srv, "manage_k8s_resource", target="dev", operation=op, kind="ConfigMap", name="cm",
                 namespace="app", body=None if op == "delete" else CM, dry_run=dry_run)
    assert set(modes) == {"write"}
    (verb, kw), = cm.calls
    assert verb == op
    assert kw["namespace"] == "app"
    assert ("dry_run" in kw) is dry_run and kw.get("dry_run", "All") == "All"
    if op == "patch":
        assert kw["content_type"] == "application/merge-patch+json"
    if op in ("replace", "patch", "delete"):
        assert kw["name"] == "cm"
    assert out["operation"] == op and out["dry_run"] is dry_run
    assert out["_target"]["profile"] == srv.registry.resolve("dev").write_profile


@pytest.mark.parametrize("op", ["create", "replace", "patch"])
def test_manage_requires_body(writable, op):
    srv, _, cm, _, _ = writable
    assert "needs a body" in tool_error(srv, "manage_k8s_resource", target="dev", operation=op,
                                        kind="ConfigMap", name="cm", namespace="app")
    assert cm.calls == []


def test_manage_namespaced_kind_requires_namespace(writable):
    srv, _, cm, _, _ = writable
    msg = tool_error(srv, "manage_k8s_resource", target="dev", operation="delete", kind="ConfigMap", name="cm")
    assert "namespaced; pass a namespace" in msg
    assert cm.calls == []


def test_manage_cluster_scoped_kind_drops_namespace(writable):
    srv, _, _, ns, _ = writable
    result(srv, "manage_k8s_resource", target="dev", operation="delete", kind="Namespace", name="x",
           namespace="ignored")
    assert ns.calls == [("delete", {"name": "x", "namespace": None})]


def test_manage_delete_status_response_reports_details(writable):
    """Regression: a delete answered with a Status object came back as name=None."""
    srv, _, cm, _, _ = writable
    cm.delete_response = {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Success",
                          "details": {"name": "cm", "kind": "configmaps", "uid": "u1"}}
    out = result(srv, "manage_k8s_resource", target="dev", operation="delete", kind="ConfigMap", name="cm",
                 namespace="app", dry_run=True)
    assert out["result"]["status"] == "Success"
    assert out["result"]["details"]["name"] == "cm"


def test_manage_create_returns_summary(writable):
    srv, *_ = writable
    out = result(srv, "manage_k8s_resource", target="dev", operation="create", kind="ConfigMap", name="cm",
                 namespace="app", body=CM)
    assert out["result"] == {"name": "cm", "namespace": "app", "created": "2026-01-01T00:00:00Z"}


def test_manage_unknown_kind(writable):
    srv, *_ = writable
    msg = tool_error(srv, "manage_k8s_resource", target="dev", operation="delete", kind="Widget", name="w",
                     namespace="app")
    assert "no resource kind 'Widget'" in msg


# ------------------------------------------------------------------ apply_yaml
MULTI = """
apiVersion: v1
kind: Namespace
metadata: {name: app, namespace: should-be-dropped}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: a, namespace: explicit}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: b}
---
"""


@pytest.mark.parametrize("dry_run", [True, False])
def test_apply_multi_document(writable, dry_run):
    srv, dyn, _, _, modes = writable
    out = result(srv, "apply_yaml", target="dev", yaml_content=MULTI, namespace="fallback", dry_run=dry_run)
    assert set(modes) == {"write"}
    assert [(a["kind"], a["name"], a["namespace"]) for a in dyn.applied] == [
        ("Namespace", "app", None),  # cluster-scoped: metadata.namespace ignored
        ("ConfigMap", "a", "explicit"),  # document namespace wins
        ("ConfigMap", "b", "fallback"),  # tool argument fills the gap
    ]
    for a in dyn.applied:
        assert a["field_manager"] == "eks-multi-mcp" and a["force_conflicts"] is False
        assert ("dry_run" in a) is dry_run
    assert [r["resourceVersion"] for r in out["result"]] == ["42"] * 3
    assert all(r["dry_run"] is dry_run for r in out["result"])


def test_apply_forwards_force_conflicts(writable):
    srv, dyn, *_ = writable
    result(srv, "apply_yaml", target="dev", yaml_content=MULTI, namespace="x", force_conflicts=True)
    assert all(a["force_conflicts"] is True for a in dyn.applied)


@pytest.mark.parametrize("doc,expect", [
    ("", "no YAML documents"),
    ("---\n---\n", "no YAML documents"),
    ("- a\n- b\n", "document 1 is not a mapping"),
    ("kind: ConfigMap\nmetadata: {name: a}\n", "needs apiVersion, kind and metadata.name"),
    ("apiVersion: v1\nkind: ConfigMap\n", "needs apiVersion, kind and metadata.name"),
    ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n", "namespaced; pass a namespace"),
    ("apiVersion: v1\nkind: Widget\nmetadata: {name: a}\n", "no resource kind 'Widget'"),
])
def test_apply_rejects_bad_documents_cleanly(writable, doc, expect):
    srv, dyn, *_ = writable
    assert expect in tool_error(srv, "apply_yaml", target="dev", yaml_content=doc)
    assert dyn.applied == []


def test_apply_validates_all_documents_before_applying_any(writable):
    """Regression: a bad document late in the stream left earlier ones applied."""
    srv, dyn, *_ = writable
    doc = MULTI + "apiVersion: v1\nkind: Widget\nmetadata: {name: w}\n"
    assert "no resource kind 'Widget'" in tool_error(srv, "apply_yaml", target="dev", yaml_content=doc,
                                                      namespace="x")
    assert dyn.applied == []


def test_apply_invalid_yaml_is_a_tool_error(writable):
    srv, dyn, *_ = writable
    tool_error(srv, "apply_yaml", target="dev", yaml_content="a: [unclosed")
    assert dyn.applied == []
