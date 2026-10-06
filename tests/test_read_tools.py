"""Read tools against in-memory Kubernetes and AWS doubles."""

from datetime import UTC, datetime
from types import SimpleNamespace

import kubernetes.client
import pytest
import yaml
from botocore.exceptions import ClientError
from fakes import (
    FakeAws,
    FakeDynamic,
    FakeResource,
    install_aws,
    install_dynamic,
    make_server,
    result,
    tool_error,
)
from kubernetes.client.exceptions import ApiException

from eks_multi_mcp.auth import CredentialError


def pod(name, ns, phase="Running", **extra):
    return {"metadata": {"name": name, "namespace": ns, "creationTimestamp": "t", "managedFields": [{"x": 1}]},
            "status": {"phase": phase}, **extra}


@pytest.fixture
def k8s(files, monkeypatch):
    srv = make_server(files)
    pods = FakeResource("Pod", items=[pod("a", "ns1"), pod("b", "ns2", "Pending")])
    secret = FakeResource("Secret", items=[{"metadata": {"name": "s", "namespace": "ns1"},
                                            "data": {"password": "aHVudGVyMg=="}, "stringData": {"t": "x"}}])
    secret_list = FakeResource("SecretList", base_kind="Secret", items=secret.items)
    nodes = FakeResource("Node", namespaced=False, items=[{"metadata": {"name": "n1"}}])
    deploy = FakeResource("Deployment", api_version="apps/v1", items=[{
        "metadata": {"name": "web", "namespace": "ns1", "labels": {"app": "web"}},
        "spec": {"replicas": 3},
        "status": {"replicas": 3, "readyReplicas": 1,
                   "conditions": [{"type": "Available", "status": "False"}, {"type": "Progressing",
                                                                            "status": "False"}]}}])
    dyn = FakeDynamic(pods, secret, secret_list, nodes, deploy)
    modes = install_dynamic(monkeypatch, srv, dyn)
    return SimpleNamespace(srv=srv, pods=pods, secret=secret, nodes=nodes, deploy=deploy, modes=modes)


# ------------------------------------------------------------------ Kubernetes reads
def test_list_namespaced_across_all_namespaces(k8s):
    out = result(k8s.srv, "list_k8s_resources", target="dev", kind="Pod")
    assert [(r["name"], r["namespace"], r["phase"]) for r in out["result"]] == [
        ("a", "ns1", "Running"), ("b", "ns2", "Pending")]
    assert out["count"] == 2 and out["truncated"] is False
    assert k8s.modes == ["read"]
    assert out["_target"]["profile"] == k8s.srv.registry.resolve("dev").read_profile


def test_list_passes_selectors_and_caps_limit(k8s):
    k8s.pods.continue_token = "more"
    out = result(k8s.srv, "list_k8s_resources", target="dev", kind="Pod", namespace="ns1",
                 label_selector="app=x", field_selector="status.phase=Running", limit=10_000)
    (_, kw), = k8s.pods.calls
    assert kw == {"name": None, "namespace": "ns1", "label_selector": "app=x",
                  "field_selector": "status.phase=Running", "limit": 500}
    assert out["truncated"] is True


def test_list_cluster_scoped_sends_no_namespace(k8s):
    result(k8s.srv, "list_k8s_resources", target="dev", kind="Node", namespace="ignored")
    assert k8s.nodes.calls[0][1]["namespace"] is None


def test_list_summarizes_readiness_and_failing_conditions(k8s):
    row, = result(k8s.srv, "list_k8s_resources", target="dev", kind="Deployment", api_version="apps/v1")["result"]
    assert row["ready"] == "1/3"
    assert row["not"] == ["Available"]  # Progressing is not a health condition
    assert row["labels"] == {"app": "web"}


def test_get_strips_managed_fields(k8s):
    out = result(k8s.srv, "get_k8s_resource", target="dev", kind="Pod", name="a", namespace="ns1", output="json")
    assert "managedFields" not in out["result"]["metadata"]
    y = result(k8s.srv, "get_k8s_resource", target="dev", kind="Pod", name="a", namespace="ns1")
    assert yaml.safe_load(y["result"])["metadata"]["name"] == "a"


@pytest.mark.parametrize("kind", ["Secret", "SecretList"])
def test_get_secret_is_redacted_by_default(k8s, kind):
    out = result(k8s.srv, "get_k8s_resource", target="dev", kind=kind, name="s", namespace="ns1", output="json")
    assert out["result"]["data"] == {"password": "<redacted>"}
    assert out["result"]["stringData"] == {"t": "<redacted>"}


def test_get_secret_unredacted_with_sensitive_access(files, monkeypatch):
    srv = make_server(files, safety={"allow_sensitive_data_access": True})
    install_dynamic(monkeypatch, srv, FakeDynamic(FakeResource("Secret", items=[
        {"metadata": {"name": "s", "namespace": "ns1"}, "data": {"password": "aHVudGVyMg=="}}])))
    out = result(srv, "get_k8s_resource", target="dev", kind="Secret", name="s", namespace="ns1", output="json")
    assert out["result"]["data"] == {"password": "aHVudGVyMg=="}


def test_unknown_kind_points_at_list_api_versions(k8s):
    assert "use list_api_versions" in tool_error(k8s.srv, "list_k8s_resources", target="dev", kind="Widget")


@pytest.mark.parametrize("status,hint", [(401, "not mapped to this cluster"), (403, "lacks this permission"),
                                         (404, "")])
def test_kubernetes_api_errors_are_explained(k8s, monkeypatch, status, hint):
    e = ApiException(status=status, reason="Nope")
    e.body = '{"message": "pods is forbidden"}'

    def boom(**kw):
        raise e

    monkeypatch.setattr(k8s.pods, "get", boom)
    msg = tool_error(k8s.srv, "list_k8s_resources", target="dev", kind="Pod")
    assert f"Kubernetes API {status} Nope: pods is forbidden" in msg
    assert hint in msg


def test_non_json_error_body_is_truncated(k8s, monkeypatch):
    e = ApiException(status=500, reason="Boom")
    e.body = "x" * 1000

    def boom(**kw):
        raise e

    monkeypatch.setattr(k8s.pods, "get", boom)
    assert len(tool_error(k8s.srv, "list_k8s_resources", target="dev", kind="Pod")) < 400


def test_credential_errors_become_tool_errors(files, monkeypatch):
    srv = make_server(files)

    def dynamic(*a, **k):
        raise CredentialError("the SSO session behind profile 'dev' is missing or expired")

    monkeypatch.setattr(srv.auth, "dynamic", dynamic)
    assert "SSO session" in tool_error(srv, "list_k8s_resources", target="dev", kind="Pod")


# ------------------------------------------------------------------ CoreV1-based reads
class FakeCore:
    def __init__(self, events=()):
        self.events = list(events)
        self.calls = []

    def list_namespaced_event(self, namespace, **kw):
        self.calls.append(("ns", namespace, kw))
        return SimpleNamespace(items=list(self.events))

    def list_event_for_all_namespaces(self, **kw):
        self.calls.append(("all", None, kw))
        return SimpleNamespace(items=list(self.events))

    def read_namespaced_pod_log(self, name, namespace, **kw):
        self.calls.append(("log", (name, namespace), kw))
        return "line1\nline2"


def ev(name, ts, type_="Normal"):
    return SimpleNamespace(
        last_timestamp=ts, event_time=None, first_timestamp=None, type=type_, reason="R", count=1, message="m",
        metadata=SimpleNamespace(namespace="ns1", creation_timestamp=None),
        involved_object=SimpleNamespace(kind="Pod", name=name))


@pytest.fixture
def core(files, monkeypatch):
    fc = FakeCore([ev("old", datetime(2026, 1, 1, tzinfo=UTC)), ev("none", None),
                   ev("new", datetime(2026, 2, 1, tzinfo=UTC), "Warning")])
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda api: fc)
    return fc


def _no_client(srv, monkeypatch):
    monkeypatch.setattr(srv.auth, "api_client", lambda t, mode="read": object())


def test_events_newest_first_with_filters(files, monkeypatch, core):
    srv = make_server(files)
    _no_client(srv, monkeypatch)
    out = result(srv, "get_k8s_events", target="dev", namespace="ns1", involved_kind="Pod",
                 involved_name="web", warnings_only=True, limit=2)
    assert core.calls == [("ns", "ns1", {"field_selector":
                                         "involvedObject.kind=Pod,involvedObject.name=web,type=Warning"})]
    assert [r["object"] for r in out["result"]] == ["Pod/new", "Pod/old"]
    assert out["count"] == 2


def test_events_all_namespaces_without_filters(files, monkeypatch, core):
    srv = make_server(files)
    _no_client(srv, monkeypatch)
    out = result(srv, "get_k8s_events", target="dev")
    assert core.calls == [("all", None, {})]
    assert out["result"][-1]["time"] is None  # undated events sort last


def test_pod_logs(files, monkeypatch, core):
    srv = make_server(files, safety={"allow_sensitive_data_access": True})
    _no_client(srv, monkeypatch)
    out = result(srv, "get_pod_logs", target="dev", namespace="ns1", pod_name="p", container="c",
                 tail_lines=99_999, since_seconds=60, previous=True)
    assert out["result"] == "line1\nline2"
    assert core.calls == [("log", ("p", "ns1"), {"tail_lines": 5000, "previous": True, "container": "c",
                                                 "since_seconds": 60})]


def test_list_api_versions(files, monkeypatch):
    srv = make_server(files)
    _no_client(srv, monkeypatch)
    groups = SimpleNamespace(groups=[SimpleNamespace(versions=[SimpleNamespace(group_version="apps/v1")])])
    monkeypatch.setattr(kubernetes.client, "ApisApi", lambda api: SimpleNamespace(get_api_versions=lambda: groups))
    monkeypatch.setattr(kubernetes.client, "VersionApi",
                        lambda api: SimpleNamespace(get_code=lambda: SimpleNamespace(git_version="v1.31.2")))
    out = result(srv, "list_api_versions", target="dev")
    assert out["result"] == {"server_version": "v1.31.2", "api_versions": ["v1", "apps/v1"]}


# ------------------------------------------------------------------ EKS / AWS tools
def test_list_nodegroups_and_fargate(files, monkeypatch):
    srv = make_server(files)
    eks = FakeAws(
        paginators={"list_nodegroups": [{"nodegroups": ["ng1"]}],
                    "list_fargate_profiles": [{"fargateProfileNames": ["fp1"]}, {"fargateProfileNames": ["fp2"]}]},
        describe_nodegroup={"nodegroup": {"status": "ACTIVE", "version": "1.31", "scalingConfig": {"maxSize": 3},
                                          "health": {"issues": []}}})
    seen = install_aws(monkeypatch, srv, eks=eks)
    out = result(srv, "list_nodegroups", target="dev")
    assert out["result"]["nodegroups"][0]["name"] == "ng1"
    assert out["result"]["nodegroups"][0]["scaling"] == {"maxSize": 3}
    assert out["result"]["fargate_profiles"] == ["fp1", "fp2"]
    assert eks.paginators["list_nodegroups"].kwargs == {"clusterName": "web-dev-blue"}
    assert seen == [("eks", "read")]
    assert "fargate_profiles" not in result(srv, "list_nodegroups", target="dev", include_fargate=False)["result"]


def test_list_addons(files, monkeypatch):
    srv = make_server(files)
    install_aws(monkeypatch, srv, eks=FakeAws(
        paginators={"list_addons": [{"addons": ["vpc-cni"]}]},
        describe_addon={"addon": {"addonVersion": "v1.18", "status": "DEGRADED",
                                  "health": {"issues": [{"code": "X"}]}}}))
    a, = result(srv, "list_addons", target="dev")["result"]
    assert a == {"name": "vpc-cni", "version": "v1.18", "status": "DEGRADED", "issues": [{"code": "X"}],
                 "serviceAccountRoleArn": None}


def test_insights_list_with_category_and_detail(files, monkeypatch):
    srv = make_server(files)
    eks = FakeAws(paginators={"list_insights": [{"insights": [{"id": "i1", "lastRefreshTime":
                                                                datetime(2026, 1, 1, tzinfo=UTC)}]}]},
                  describe_insight={"insight": {"id": "i1", "recommendation": "upgrade"}})
    install_aws(monkeypatch, srv, eks=eks)
    out = result(srv, "get_eks_insights", target="dev", category="UPGRADE_READINESS")
    assert out["result"][0]["lastRefreshTime"].startswith("2026-01-01")
    assert eks.paginators["list_insights"].kwargs == {"clusterName": "web-dev-blue",
                                                      "filter": {"categories": ["UPGRADE_READINESS"]}}
    assert result(srv, "get_eks_insights", target="dev", insight_id="i1")["result"]["recommendation"] == "upgrade"


@pytest.mark.parametrize("log_type,group", [
    ("application", "/aws/containerinsights/web-dev-blue/application"),
    ("control-plane", "/aws/eks/web-dev-blue/cluster"),
])
def test_cloudwatch_logs(files, monkeypatch, log_type, group):
    srv = make_server(files, safety={"allow_sensitive_data_access": True})
    logs = FakeAws(filter_log_events={"events": [{"timestamp": 0, "logStreamName": "s", "message": "hi"}]})
    install_aws(monkeypatch, srv, logs=logs)
    out = result(srv, "get_cloudwatch_logs", target="dev", log_type=log_type, filter_pattern="ERROR", limit=5000)
    assert out["log_group"] == group
    (_, kw), = logs.calls
    assert kw["logGroupName"] == group and kw["filterPattern"] == "ERROR" and kw["limit"] == 1000
    assert out["result"] == [{"ts": "1970-01-01T00:00:00+00:00", "stream": "s", "message": "hi"}]


def test_cloudwatch_logs_gated(files):
    assert "sensitive data access" in tool_error(make_server(files), "get_cloudwatch_logs", target="dev")


def test_cloudwatch_metrics_adds_cluster_dimension(files, monkeypatch):
    srv = make_server(files)
    t2, t1 = datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    cw = FakeAws(get_metric_statistics={"Datapoints": [{"Timestamp": t2, "Maximum": 2.0, "Unit": "Percent"},
                                                       {"Timestamp": t1, "Maximum": 1.0, "Unit": "Percent"}]})
    install_aws(monkeypatch, srv, cloudwatch=cw)
    out = result(srv, "get_cloudwatch_metrics", target="dev", metric_name="node_cpu_utilization", stat="Maximum",
                 dimensions={"NodeName": "n1"})
    assert out["dimensions"] == {"NodeName": "n1", "ClusterName": "web-dev-blue"}
    assert [p["Maximum"] for p in out["result"]] == [1.0, 2.0]
    result(srv, "get_cloudwatch_metrics", target="dev", metric_name="x", namespace="AWS/EC2")
    assert cw.calls[-1][1]["Dimensions"] == []


def test_policies_for_role(files, monkeypatch):
    srv = make_server(files)
    install_aws(monkeypatch, srv, iam=FakeAws(
        paginators={"list_attached_role_policies": [{"AttachedPolicies": [{"PolicyName": "p"}]}],
                    "list_role_policies": [{"PolicyNames": ["inline1"]}]},
        get_role={"Role": {"Arn": "arn:aws:iam::111111111111:role/r", "AssumeRolePolicyDocument": {"v": 1}}},
        get_role_policy={"PolicyDocument": {"Statement": []}}))
    out = result(srv, "get_policies_for_role", target="dev", role_name="r")["result"]
    assert out == {"arn": "arn:aws:iam::111111111111:role/r", "trust_policy": {"v": 1},
                   "attached": [{"PolicyName": "p"}], "inline": {"inline1": {"Statement": []}}}


def test_aws_client_errors_are_explained(files, monkeypatch):
    srv = make_server(files)

    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "ListAddons")

    install_aws(monkeypatch, srv, eks=FakeAws(paginators={"list_addons": [{"addons": ["a"]}]},
                                              describe_addon=denied))
    assert "AccessDeniedException: nope" in tool_error(srv, "list_addons", target="dev")


def test_describe_cluster_strips_ca_and_serializes_dates(files, monkeypatch):
    srv = make_server(files)
    install_aws(monkeypatch, srv, eks=FakeAws(describe_cluster={"cluster": {
        "name": "web-dev-blue", "createdAt": datetime(2026, 1, 1, tzinfo=UTC), "certificateAuthority": {"data": "x"}}}))
    out = result(srv, "describe_cluster", target="dev")["result"]
    assert "certificateAuthority" not in out and out["createdAt"].startswith("2026-01-01")


# ------------------------------------------------------------------ fleet
def test_fleet_overview_isolates_failures(files, monkeypatch):
    srv = make_server(files)

    def describe(t, refresh=False):
        if t.alias == "web-prod-blue":
            raise CredentialError("credentials for profile 'prod' are expired or invalid (ExpiredToken)")
        if t.cluster_name == "main" and t.account_id == "444444444444":
            raise RuntimeError("socket closed")
        return {"version": "1.31", "status": "ACTIVE", "createdAt": datetime(2026, 1, 1, tzinfo=UTC),
                "resourcesVpcConfig": {"endpointPublicAccess": False}, "accessConfig": {"authenticationMode": "API"}}

    monkeypatch.setattr(srv.auth, "describe_cluster", describe)
    out = result(srv, "fleet_overview")
    by = {c["target"]: c for c in out["clusters"]}
    assert out["count"] == len(srv.registry.targets) == len(by)
    assert by["web-dev-blue"]["version"] == "1.31" and by["web-dev-blue"]["authMode"] == "API"
    assert "ExpiredToken" in by["web-prod-blue"]["error"]
    ops = next(c for c in out["clusters"] if c["account_id"] == "444444444444")
    assert ops["error"] == "RuntimeError: socket closed"


def test_fleet_overview_with_insight_counts(files, monkeypatch):
    srv = make_server(files)
    monkeypatch.setattr(srv.auth, "describe_cluster", lambda t, refresh=False: {"version": "1.31"})
    install_aws(monkeypatch, srv, eks=FakeAws(list_insights={"insights": [
        {"insightStatus": {"status": "ERROR"}}, {"insightStatus": {"status": "PASSING"}},
        {"insightStatus": {"status": "PASSING"}}]}))
    out = result(srv, "fleet_overview", selector="env:dev", include_insights=True)
    assert out["clusters"] and all(c["upgradeInsights"] == {"ERROR": 1, "WARNING": 0, "PASSING": 2, "UNKNOWN": 0}
                                   for c in out["clusters"])


def test_fleet_list_k8s_resources(files, monkeypatch):
    srv = make_server(files)
    dyn = FakeDynamic(FakeResource("Pod", items=[pod(f"p{i}", "ns") for i in range(5)]))

    def dynamic(t, mode="read"):
        if t.env == "prod":
            raise CredentialError("no credentials could be loaded for profile 'prod'")
        return dyn

    monkeypatch.setattr(srv.auth, "dynamic", dynamic)
    out = result(srv, "fleet_list_k8s_resources", kind="Pod", names_limit=2)
    by = {c["target"]: c for c in out["clusters"]}
    assert by["web-dev-blue"] == {**by["web-dev-blue"], "count": 5, "names": ["ns/p0", "ns/p1"], "truncated": True}
    assert "no credentials" in by["web-prod-blue"]["error"]


def test_fleet_timeout_reports_instead_of_hanging(files, monkeypatch):
    import threading

    from eks_multi_mcp import server as server_mod

    release = threading.Event()
    targets = make_server(files).registry.select("*")
    out = server_mod._fan_out(targets, lambda t: release.wait(5) or {"ok": True}, timeout=0.05, workers=len(targets))
    release.set()
    assert all(v == {"error": "timed out"} for v in out.values())
    assert set(out) == {t.alias for t in targets}


# ------------------------------------------------------------------ doctor / describe_target / probe
def _ident(account):
    return lambda t, mode="read": {"account": account, "arn": f"arn:aws:sts::{account}:assumed-role/x/y"}


def test_probe_flags_profile_in_wrong_account(files, monkeypatch):
    srv = make_server(files)
    monkeypatch.setattr(srv.auth, "caller_identity", _ident("999999999999"))
    out = result(srv, "describe_target", target="dev")["result"]
    assert out["access"]["ok"] is False
    assert "authenticates into account 999999999999, not 111111111111" in out["access"]["error"]


def test_probe_reports_stale_context(files, monkeypatch):
    srv = make_server(files)
    monkeypatch.setattr(srv.auth, "caller_identity", _ident("111111111111"))

    def gone(t, refresh=False):
        raise CredentialError("ResourceNotFoundException: No cluster found (via profile 'dev')")

    monkeypatch.setattr(srv.auth, "describe_cluster", gone)
    out = result(srv, "describe_target", target="dev")["result"]["access"]
    assert out == {**out, "ok": False, "error": "cluster not found in this account/region (stale kube context?)"}


def test_probe_endpoint_drift_switches_to_live_endpoint(files, monkeypatch):
    srv = make_server(files)
    t = srv.registry.resolve("dev")
    monkeypatch.setattr(srv.auth, "caller_identity", _ident("111111111111"))
    monkeypatch.setattr(srv.auth, "describe_cluster", lambda t, refresh=False: {
        "status": "ACTIVE", "version": "1.31", "endpoint": "https://NEW.gr7.us-east-1.eks.amazonaws.com",
        "certificateAuthority": {"data": "TkVX"}})
    invalidated = []
    monkeypatch.setattr(srv.auth, "invalidate", lambda tt=None: invalidated.append(tt))
    monkeypatch.setattr(kubernetes.client, "VersionApi",
                        lambda api: SimpleNamespace(get_code=lambda: SimpleNamespace(git_version="v1.31")))
    monkeypatch.setattr(kubernetes.client, "CoreV1Api",
                        lambda api: SimpleNamespace(list_namespace=lambda limit: None))
    monkeypatch.setattr(srv.auth, "api_client", lambda tt, mode="read": object())
    out = result(srv, "describe_target", target="dev")["result"]["access"]
    assert out["ok"] is True and out["kubernetes_auth"] == "ok" and "differs" in out["warning"]
    assert t.endpoint == "https://NEW.gr7.us-east-1.eks.amazonaws.com" and t.ca_data == "TkVX"
    assert invalidated == [t]


def test_probe_unreachable_api_server(files, monkeypatch):
    srv = make_server(files)
    monkeypatch.setattr(srv.auth, "caller_identity", _ident("111111111111"))
    monkeypatch.setattr(srv.auth, "describe_cluster", lambda t, refresh=False: {"status": "ACTIVE"})

    def unreachable(t, mode="read"):
        raise ConnectionError("timed out")

    monkeypatch.setattr(srv.auth, "api_client", unreachable)
    out = result(srv, "describe_target", target="dev")["result"]["access"]
    assert out["ok"] is False and out["error"].startswith("cannot reach the API server: ConnectionError")


def test_doctor_counts_problems(files, monkeypatch):
    srv = make_server(files)

    def ident(t, mode="read"):
        if t.env == "prod":
            raise CredentialError("the SSO session behind profile 'prod' is missing or expired")
        return {"account": t.account_id, "arn": "arn"}

    monkeypatch.setattr(srv.auth, "caller_identity", ident)
    monkeypatch.setattr(srv.auth, "describe_cluster", lambda t, refresh=False: {"status": "ACTIVE"})
    out = result(srv, "doctor")
    by = {r["target"]: r for r in out["targets"]}
    assert by["web-dev-blue"]["access"]["ok"] is True
    assert "SSO session" in by["web-prod-blue"]["access"]["error"]
    assert out["targets_with_problems"] == sum(
        1 for r in out["targets"] if r["warnings"] or not r["access"]["ok"])
    assert out["targets_checked"] == len(srv.registry.targets)


def test_doctor_without_access_check_makes_no_calls(files, monkeypatch):
    srv = make_server(files)
    monkeypatch.setattr(srv.auth, "caller_identity", lambda *a, **k: pytest.fail("network call"))
    out = result(srv, "doctor", check_access=False)
    assert all("access" not in r for r in out["targets"])


# ------------------------------------------------------------------ discovery / reload
def test_discover_clusters_adds_new_targets(files, monkeypatch):
    srv = make_server(files)
    before = len(srv.registry.targets)

    class Sess:
        def __init__(self, profile):
            self.profile = profile

        def client(self, service, region_name):
            if region_name == "eu-west-1":
                _raise()
            names = ["web-dev-blue", "brand-new"] if self.profile == "dev" else []
            return FakeAws(paginators={"list_clusters": [{"clusters": names}]})

    def _raise():
        raise ClientError({"Error": {"Code": "UnrecognizedClientException", "Message": "bad"}}, "ListClusters")

    monkeypatch.setattr(srv.auth, "base_session", lambda p: Sess(p))
    out = result(srv, "discover_clusters", profiles=["dev"], regions=["us-east-1", "eu-west-1"])
    assert out["new_targets"] == ["brand-new"]
    assert out["queried"]["dev"]["us-east-1"] == ["web-dev-blue", "brand-new"]
    assert "UnrecognizedClientException" in out["queried"]["dev"]["eu-west-1"]["error"]
    assert len(srv.registry.targets) == before + 1
    new = srv.registry.resolve("brand-new")
    assert new.account_id == "111111111111" and new.read_profile


def test_reload_config_drops_cached_clients(files, monkeypatch):
    srv = make_server(files)
    calls = []
    monkeypatch.setattr(srv.auth, "invalidate", lambda t=None: calls.append(t))
    out = result(srv, "reload_config")
    assert out == {"targets": len(srv.registry.targets)} and calls == [None]
