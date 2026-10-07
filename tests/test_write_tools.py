"""apply_yaml and manage_k8s_resource: gates, human approval, what reaches the API server,
and response shape."""

import fakes
import pytest
from fakes import Approver, FakeDynamic, FakeResource, install_dynamic, make_server, result, tool_error

from eks_multi_mcp.server import PROMPT_DETAIL_CHARS, REQUEST_YAML_BEGIN, REQUEST_YAML_END


@pytest.fixture(autouse=True, params=["legacy", "auto"])
def client_mode(request, monkeypatch):
    """Run every write test over both protocols: elicitation/create mid-call (legacy) and
    InputRequiredResult round trips (2026-07-28, what Claude Code speaks)."""
    monkeypatch.setattr(fakes, "CLIENT_MODE", request.param)
    return request.param


CM ={"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm", "namespace": "app"}, "data": {"k": "v"}}
DELETE_CM = {"operation": "delete", "kind": "ConfigMap", "name": "cm", "namespace": "app"}


# Writes need an env from the cluster map, not one guessed from names.
DEV_ACCOUNT = {"111111111111": {"env": "dev"}}


@pytest.fixture
def writable(files, monkeypatch):
    srv = make_server(files, safety={"allow_write": True}, accounts=DEV_ACCOUNT)
    cm = FakeResource("ConfigMap")
    ns = FakeResource("Namespace", namespaced=False)
    dyn = FakeDynamic(cm, ns)
    modes = install_dynamic(monkeypatch, srv, dyn)
    return srv, dyn, cm, ns, modes


def assert_write_profile_only_after_approval(modes):
    """Validation may use the read client; the write client is built only after approval."""
    assert "write" in modes
    first_write = modes.index("write")
    assert all(m == "read" for m in modes[:first_write])
    assert all(m == "write" for m in modes[first_write:])


# ------------------------------------------------------------------ gates
@pytest.mark.parametrize("tool,args", [
    ("apply_yaml", {"yaml_content": "kind: x"}),
    ("manage_k8s_resource", DELETE_CM),
])
def test_write_tools_refuse_without_allow_write(files, monkeypatch, tool, args):
    srv = make_server(files)
    modes = install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    human = Approver()
    assert "read-only" in tool_error(srv, tool, human, target="dev", **args)
    assert modes == [] and human.prompts == []  # refused before any client or prompt


def test_agent_supplied_confirmation_cannot_replace_the_human(writable):
    """The old confirm_cluster argument is gone: passing it changes nothing, the human is still
    asked, and their refusal stands."""
    srv, _, cm, _, _ = writable
    human = Approver("decline")
    msg = tool_error(srv, "manage_k8s_resource", human, target="dev", confirm_cluster="web-dev-blue",
                     **DELETE_CM)
    assert "not approved" in msg and len(human.prompts) == 1 and cm.calls == []


# ------------------------------------------------------------------ human approval
@pytest.mark.parametrize("target", ["dev", "dev-main"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_client_without_prompt_support_cannot_write(writable, target, dry_run):
    srv, _, cm, _, modes = writable
    msg = tool_error(srv, "manage_k8s_resource", None, target=target, dry_run=dry_run, **DELETE_CM)
    assert "cannot show an approval prompt" in msg and "nothing was sent" in msg
    assert cm.calls == [] and "write" not in modes


@pytest.mark.parametrize("reply,expect", [
    ("decline", "not approved (decline)"),
    ("cancel", "not approved (cancel)"),
    ({"approve": False}, "not approved"),
    ({"something": "else"}, "malformed"),
])
def test_unprotected_write_needs_explicit_yes(writable, reply, expect):
    srv, _, cm, _, modes = writable
    human = Approver(reply)
    msg = tool_error(srv, "manage_k8s_resource", human, target="dev", dry_run=True, **DELETE_CM)
    assert expect in msg and "nothing was sent" in msg
    assert len(human.prompts) == 1
    assert cm.calls == [] and "write" not in modes


@pytest.mark.parametrize("dry_run", [True, False])
def test_unprotected_write_proceeds_after_approval(writable, dry_run):
    srv, _, cm, _, modes = writable
    human = Approver()
    out = result(srv, "manage_k8s_resource", human, target="dev", dry_run=dry_run, **DELETE_CM)
    assert out["_target"]["mode"] == "write"
    assert cm.calls[0][0] == "delete"
    assert human.schemas[0]["required"] == ["approve"]
    assert_write_profile_only_after_approval(modes)


def test_prompt_tells_the_human_exactly_what_will_happen(writable):
    srv, _, _, _, _ = writable
    human = Approver()
    result(srv, "manage_k8s_resource", human, target="dev", operation="patch", kind="ConfigMap", name="cm",
           namespace="app", body={"data": {"k": "changed"}}, dry_run=True)
    t = srv.registry.resolve("dev")
    prompt, = human.prompts
    assert prompt.startswith("DRY RUN (server-side, not persisted): patch v1 ConfigMap app/cm")
    assert f"Cluster: {t.cluster_name}" in prompt
    assert f"Account: {t.account_id}" in prompt and f"env: {t.env}" in prompt
    assert f"Write profile: {t.write_profile}" in prompt
    assert "k: changed" in prompt  # the body itself is shown


def test_real_write_is_labelled_as_such(writable):
    srv, *_ = writable
    human = Approver()
    result(srv, "manage_k8s_resource", human, target="dev", **DELETE_CM)
    assert human.prompts[0].startswith("WRITE: delete v1 ConfigMap app/cm")


def test_apply_prompt_lists_every_object_and_the_yaml(writable):
    srv, *_ = writable
    human = Approver()
    doc = "apiVersion: v1\nkind: Namespace\nmetadata: {name: n1}\n---\n" \
          "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c1, namespace: n1}\ndata: {k: v}\n"
    result(srv, "apply_yaml", human, target="dev", yaml_content=doc, force_conflicts=True)
    prompt, = human.prompts
    assert "server-side apply of 2 object(s) with force_conflicts" in prompt
    assert "  Namespace n1\n  ConfigMap n1/c1" in prompt
    assert "    data:\n      k: v" in prompt  # inside the indented request block


def test_oversized_write_is_refused_not_truncated(writable):
    """The review's repro: a large ConfigMap pushes a ClusterRoleBinding granting cluster-admin
    past any display cutoff. The human must see everything they approve, so the write is
    refused before any prompt rather than shown in part."""
    srv, dyn, *_ = writable
    dyn.add(FakeResource("ClusterRoleBinding", namespaced=False, api_version="rbac.authorization.k8s.io/v1"))
    human = Approver()
    doc = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: filler, namespace: app}\n"
           f"data: {{blob: {'x' * PROMPT_DETAIL_CHARS}}}\n---\n"
           "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata: {name: everyone-admin}\n"
           "roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: cluster-admin}\n"
           "subjects: [{apiGroup: rbac.authorization.k8s.io, kind: Group, name: 'system:authenticated'}]\n")
    msg = tool_error(srv, "apply_yaml", human, target="dev", yaml_content=doc)
    assert "too large to show in full for approval" in msg and "split it" in msg
    assert human.prompts == [] and dyn.applied == []


def test_oversized_manage_body_is_refused(writable):
    srv, _, cm, _, _ = writable
    human = Approver()
    body = {**CM, "data": {"blob": "x" * 20_000}}
    msg = tool_error(srv, "manage_k8s_resource", human, target="dev", operation="create", kind="ConfigMap",
                     name="cm", namespace="app", body=body)
    assert "too large to show in full for approval" in msg
    assert human.prompts == [] and cm.calls == []


@pytest.mark.parametrize("field,value", [
    ("name", "cm\nCluster: sandbox-throwaway  (us-east-1)"),  # the review's repro
    ("name", "cm Cluster: elsewhere"),
    ("name", "../cm"),
    ("name", ""),
    ("namespace", "app\nWrite profile: nobody"),
    ("namespace", "App"),
    ("kind", "ConfigMap\nCluster: x"),
    ("api_version", "v1\nCluster: x"),
])
def test_names_that_could_spoof_the_prompt_are_refused(writable, field, value):
    srv, _, cm, _, _ = writable
    human = Approver()
    args = {**DELETE_CM, "api_version": "v1", field: value}
    msg = tool_error(srv, "manage_k8s_resource", human, target="dev", **args)
    assert f"invalid {field.replace('_', '')}" in msg.replace("_", "")
    assert human.prompts == [] and cm.calls == []


@pytest.mark.parametrize("metadata", [
    r'{name: "cm\nCluster: elsewhere", namespace: app}',
    r'{name: cm, namespace: "app\nWrite profile: nobody"}',
])
def test_apply_names_that_could_spoof_the_prompt_are_refused(writable, metadata):
    srv, dyn, *_ = writable
    human = Approver()
    doc = f"apiVersion: v1\nkind: ConfigMap\nmetadata: {metadata}\n"
    msg = tool_error(srv, "apply_yaml", human, target="dev", yaml_content=doc)
    assert "invalid" in msg
    assert human.prompts == [] and dyn.applied == []


def test_rbac_style_names_are_allowed(writable):
    srv, dyn, *_ = writable
    dyn.add(FakeResource("ClusterRole", namespaced=False, api_version="rbac.authorization.k8s.io/v1"))
    result(srv, "manage_k8s_resource", Approver(), target="dev", operation="delete",
           api_version="rbac.authorization.k8s.io/v1", kind="ClusterRole", name="system:aggregate-to-view")


def _request_block(prompt: str) -> list[str]:
    lines = prompt.splitlines()
    start = lines.index(REQUEST_YAML_BEGIN)
    return lines[start + 1:lines.index(REQUEST_YAML_END, start)]


@pytest.mark.parametrize("tool", ["manage_k8s_resource", "apply_yaml"])
def test_body_text_cannot_pass_for_server_text(writable, tool):
    """Re-review repro: a top-level body key 'Cluster: ...' rendered as an unindented line
    that read like the server's own. Request YAML now sits between markers, every line
    indented, so nothing from the request can start a line in the prompt."""
    srv, *_ = writable
    human = Approver()
    fake = "Cluster: sandbox-throwaway  (us-east-1)"
    if tool == "manage_k8s_resource":
        result(srv, tool, human, target="dev", operation="create", kind="ConfigMap", name="cm", namespace="app",
               body={**CM, fake: "x"}, dry_run=True)
    else:
        result(srv, tool, human, target="dev", dry_run=True,
               yaml_content=f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: cm, namespace: app}}\n'{fake}': x\n")
    prompt, = human.prompts
    block = _request_block(prompt)
    assert block and all(line.startswith("    ") for line in block)
    assert not any(line.startswith("Cluster: sandbox") for line in prompt.splitlines())


def test_delete_has_no_request_block(writable):
    srv, *_ = writable
    human = Approver()
    result(srv, "manage_k8s_resource", human, target="dev", **DELETE_CM)
    assert REQUEST_YAML_BEGIN not in human.prompts[0]


@pytest.mark.parametrize("operation,body,field", [
    # Re-review repro: the prompt said app/harmless while the API would create something-else.
    ("create", {**CM, "metadata": {"name": "something-else", "namespace": "app"}}, "metadata.name"),
    ("replace", {**CM, "metadata": {"name": "something-else", "namespace": "app"}}, "metadata.name"),
    ("patch", {"metadata": {"name": "something-else"}}, "metadata.name"),
    ("create", {**CM, "metadata": {"name": "harmless", "namespace": "kube-system"}}, "metadata.namespace"),
    ("create", {**CM, "kind": "Secret", "metadata": {"name": "harmless", "namespace": "app"}}, "kind"),
    ("create", {**CM, "apiVersion": "v2", "metadata": {"name": "harmless", "namespace": "app"}}, "apiVersion"),
    ("create", {"apiVersion": "v1", "kind": "ConfigMap", "data": {}}, "metadata.name"),
])
def test_body_must_match_the_arguments(writable, operation, body, field):
    srv, _, cm, _, _ = writable
    human = Approver()
    msg = tool_error(srv, "manage_k8s_resource", human, target="dev", operation=operation, kind="ConfigMap",
                     name="harmless", namespace="app", body=body)
    assert f"body {field}" in msg and "nothing was sent" in msg
    assert human.prompts == [] and cm.calls == []


def test_cluster_scoped_body_must_not_name_a_namespace(writable):
    srv, _, _, ns, _ = writable
    msg = tool_error(srv, "manage_k8s_resource", Approver(), target="dev", operation="create", kind="Namespace",
                     name="n1", body={"apiVersion": "v1", "kind": "Namespace",
                                      "metadata": {"name": "n1", "namespace": "app"}})
    assert "body metadata.namespace" in msg and ns.calls == []


def test_prompt_shows_the_real_target_first_and_last(writable):
    """Body text can contain anything (a data value may read 'Cluster: other'), so the
    real target block closes the prompt too, right above the question."""
    srv, *_ = writable
    human = Approver()
    result(srv, "manage_k8s_resource", human, target="dev", operation="create", kind="ConfigMap", name="cm",
           namespace="app", body={**CM, "data": {"note": "Cluster: somewhere-else"}}, dry_run=True)
    t = srv.registry.resolve("dev")
    lines = human.prompts[0].splitlines()
    assert lines[1].startswith(f"Cluster: {t.cluster_name}")
    assert lines[-1] == "Approve this write?"
    assert lines[-4].startswith(f"Cluster: {t.cluster_name}") and lines[-2] == f"Write profile: {t.write_profile}"


def test_no_prompt_for_invalid_requests(writable):
    srv, _, cm, _, _ = writable
    human = Approver()
    tool_error(srv, "manage_k8s_resource", human, target="dev", operation="create", kind="ConfigMap", name="cm",
               namespace="app")
    tool_error(srv, "apply_yaml", human, target="dev", yaml_content="a: [unclosed")
    assert human.prompts == [] and cm.calls == []


# ------------------------------------------------------------------ protected environments
@pytest.mark.parametrize("tool,args", [
    ("apply_yaml", {"yaml_content": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: cm, namespace: app}\n"}),
    ("manage_k8s_resource", DELETE_CM),
])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("human", [None, "approve", {"approve": True}, {"cluster_name": "web-prod-blue"}])
def test_protected_env_writes_are_forbidden_outright(writable, tool, args, dry_run, human):
    """No answer unlocks prod: refused before any client is built or any prompt is shown."""
    srv, dyn, cm, _, modes = writable
    approver = Approver(human) if human else None
    msg = tool_error(srv, tool, approver, target="prod-blue", dry_run=dry_run, **args)
    assert "writes to 'web-prod-blue' are forbidden" in msg and "is protected" in msg
    assert modes == [] and cm.calls == [] and dyn.applied == []
    assert approver is None or approver.prompts == []


@pytest.mark.parametrize("target", ["shop", "prod-blue", "333333333333/web-prod-blue", "web-prod-blue"])
def test_protected_env_forbidden_by_every_name(files, monkeypatch, target):
    srv = make_server(files, safety={"allow_write": True},
                      clusters=[{"cluster_name": "web-prod-blue", "region": "us-east-1",
                                 "account_id": "333333333333", "alias": "shop"}])
    modes = install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    assert "forbidden" in tool_error(srv, "manage_k8s_resource", Approver(), target=target, **DELETE_CM)
    assert modes == []


@pytest.mark.parametrize("env", ["prd", "prod", "PRD", "production", "Prod"])
@pytest.mark.parametrize("configured", [None, [], ["prod"], ["prd"], ["stg"]])
def test_production_cannot_be_unprotected_by_config(files, monkeypatch, env, configured):
    """Regression (spellings) and policy: config may add protected envs, never remove prod."""
    safety = {"allow_write": True}
    if configured is not None:
        safety["protected_envs"] = configured
    srv = make_server(files, safety=safety, accounts={"111111111111": {"env": env}})
    modes = install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    human = Approver()
    assert "is protected" in tool_error(srv, "manage_k8s_resource", human, target="dev", **DELETE_CM)
    assert modes == [] and human.prompts == []


def test_config_can_protect_more_envs(files, monkeypatch):
    srv = make_server(files, safety={"allow_write": True, "protected_envs": ["dev"]})
    install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    assert "env 'dev'" in tool_error(srv, "manage_k8s_resource", Approver(), target="dev", **DELETE_CM)


def test_unknown_env_is_forbidden(files, monkeypatch):
    """A target whose env can't be determined might be production, so it is treated as one."""
    srv = make_server(files, safety={"allow_write": True})
    t = srv.registry.resolve("ops-main")
    assert t.env is None
    modes = install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    human = Approver()
    msg = tool_error(srv, "manage_k8s_resource", human, target="ops-main", **DELETE_CM)
    assert "environment is unknown" in msg
    assert modes == [] and human.prompts == []


def test_unknown_env_writable_once_env_is_set(files, monkeypatch):
    srv = make_server(files, safety={"allow_write": True}, accounts={"444444444444": {"env": "sandbox"}})
    cm = FakeResource("ConfigMap")
    install_dynamic(monkeypatch, srv, FakeDynamic(cm))
    result(srv, "manage_k8s_resource", Approver(), target="ops-main", **DELETE_CM)
    assert cm.calls[0][0] == "delete"


@pytest.mark.parametrize("target,clusters", [
    # A prod-account cluster whose name merely looks like dev (the review's repro).
    ("dev-portal", [{"cluster_name": "dev-portal", "region": "us-east-1", "account_id": "333333333333"}]),
    # The kubeconfig-only dev target: its env is guessed from names, never configured.
    ("dev", []),
])
def test_guessed_env_is_not_writable(files, monkeypatch, target, clusters):
    """An env guessed from cluster, account or profile names is fine for display and
    selectors, but it is not evidence that a cluster is safe to write to."""
    srv = make_server(files, safety={"allow_write": True}, clusters=clusters)
    t = srv.registry.resolve(target)
    assert t.env == "dev" and not t.env_configured
    modes = install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    human = Approver()
    msg = tool_error(srv, "manage_k8s_resource", human, target=target, **DELETE_CM)
    assert "guessed from its name" in msg and "set env" in msg
    assert modes == [] and human.prompts == []


@pytest.mark.parametrize("config", [
    {"accounts": {"333333333333": {"env": "dev"}}},
    {"clusters": [{"cluster_name": "dev-portal", "region": "us-east-1", "account_id": "333333333333",
                   "env": "dev"}]},
])
def test_configured_env_is_writable(files, monkeypatch, config):
    clusters = config.get("clusters") or [{"cluster_name": "dev-portal", "region": "us-east-1",
                                           "account_id": "333333333333"}]
    srv = make_server(files, safety={"allow_write": True}, clusters=clusters,
                      **({"accounts": config["accounts"]} if "accounts" in config else {}))
    assert srv.registry.resolve("dev-portal").env_configured
    cm = FakeResource("ConfigMap")
    install_dynamic(monkeypatch, srv, FakeDynamic(cm))
    result(srv, "manage_k8s_resource", Approver(), target="dev-portal", **DELETE_CM)
    assert cm.calls[0][0] == "delete"


def test_guessed_prod_is_still_protected(files):
    """Inference still counts toward protection: a name that looks like prod is refused."""
    srv = make_server(files, safety={"allow_write": True})
    assert "is protected" in tool_error(srv, "manage_k8s_resource", Approver(), target="prod-blue", **DELETE_CM)


def test_account_read_only_beats_allow_write(files, monkeypatch):
    srv = make_server(files, safety={"allow_write": True}, accounts={"111111111111": {"read_only": True}})
    install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("ConfigMap")))
    assert "read_only" in tool_error(srv, "apply_yaml", Approver(), target="dev", yaml_content="kind: x")


def test_unknown_target_is_a_clean_error(writable):
    srv, *_ = writable
    assert "unknown target 'nope'" in tool_error(srv, "apply_yaml", Approver(), target="nope", yaml_content="kind: x")


# ------------------------------------------------------------------ manage_k8s_resource
@pytest.mark.parametrize("op", ["create", "replace", "patch", "delete"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_manage_operations_use_write_client_and_forward_dry_run(writable, op, dry_run):
    srv, _, cm, _, modes = writable
    out = result(srv, "manage_k8s_resource", Approver(), target="dev", operation=op, kind="ConfigMap", name="cm",
                 namespace="app", body=None if op == "delete" else CM, dry_run=dry_run)
    assert_write_profile_only_after_approval(modes)
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
    assert "needs a body" in tool_error(srv, "manage_k8s_resource", Approver(), target="dev", operation=op,
                                        kind="ConfigMap", name="cm", namespace="app")
    assert cm.calls == []


def test_manage_namespaced_kind_requires_namespace(writable):
    srv, _, cm, _, _ = writable
    msg = tool_error(srv, "manage_k8s_resource", Approver(), target="dev", operation="delete", kind="ConfigMap", name="cm")
    assert "namespaced; pass a namespace" in msg
    assert cm.calls == []


def test_manage_cluster_scoped_kind_drops_namespace(writable):
    srv, _, _, ns, _ = writable
    result(srv, "manage_k8s_resource", Approver(), target="dev", operation="delete", kind="Namespace", name="x",
           namespace="ignored")
    assert ns.calls == [("delete", {"name": "x", "namespace": None})]


def test_manage_delete_status_response_reports_details(writable):
    """Regression: a delete answered with a Status object came back as name=None."""
    srv, _, cm, _, _ = writable
    cm.delete_response = {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Success",
                          "details": {"name": "cm", "kind": "configmaps", "uid": "u1"}}
    out = result(srv, "manage_k8s_resource", Approver(), target="dev", operation="delete", kind="ConfigMap", name="cm",
                 namespace="app", dry_run=True)
    assert out["result"]["status"] == "Success"
    assert out["result"]["details"]["name"] == "cm"


def test_manage_create_returns_summary(writable):
    srv, *_ = writable
    out = result(srv, "manage_k8s_resource", Approver(), target="dev", operation="create", kind="ConfigMap", name="cm",
                 namespace="app", body=CM)
    assert out["result"] == {"name": "cm", "namespace": "app", "created": "2026-01-01T00:00:00Z"}


def test_manage_unknown_kind(writable):
    srv, *_ = writable
    msg = tool_error(srv, "manage_k8s_resource", Approver(), target="dev", operation="delete", kind="Widget", name="w",
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
    out = result(srv, "apply_yaml", Approver(), target="dev", yaml_content=MULTI, namespace="fallback", dry_run=dry_run)
    assert_write_profile_only_after_approval(modes)
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
    result(srv, "apply_yaml", Approver(), target="dev", yaml_content=MULTI, namespace="x", force_conflicts=True)
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
    assert expect in tool_error(srv, "apply_yaml", Approver(), target="dev", yaml_content=doc)
    assert dyn.applied == []


def test_apply_validates_all_documents_before_applying_any(writable):
    """Regression: a bad document late in the stream left earlier ones applied."""
    srv, dyn, *_ = writable
    doc = MULTI + "apiVersion: v1\nkind: Widget\nmetadata: {name: w}\n"
    assert "no resource kind 'Widget'" in tool_error(srv, "apply_yaml", Approver(), target="dev", yaml_content=doc,
                                                      namespace="x")
    assert dyn.applied == []


def test_apply_invalid_yaml_is_a_tool_error(writable):
    srv, dyn, *_ = writable
    tool_error(srv, "apply_yaml", Approver(), target="dev", yaml_content="a: [unclosed")
    assert dyn.applied == []


def test_approval_prompt_is_never_shown_for_a_protected_target(files):
    """Backstop: even if a future tool forgets require_write, approve() refuses prod and never
    puts a prod write in front of the human."""
    from types import SimpleNamespace

    from mcp.server.mcpserver.exceptions import ToolError

    srv = make_server(files, safety={"allow_write": True})

    def elicit(*a, **k):
        raise AssertionError("prompted for a protected target")

    ctx = SimpleNamespace(session=SimpleNamespace(check_client_capability=lambda cap: True), elicit=elicit)
    with pytest.raises(ToolError, match="protected env 'prod' are forbidden"):
        srv.approve(ctx, srv.registry.resolve("prod-blue"), "delete v1 ConfigMap app/cm", "", True)
