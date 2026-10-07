import pytest

from eks_multi_mcp.config import settings_from_dict
from eks_multi_mcp.registry import Registry, TargetNotFound, infer_env


def reg(files, **extra):
    return Registry(settings_from_dict({**files, **extra}))


def test_contexts_dedupe_to_clusters(files):
    r = reg(files)
    names = sorted(t.cluster_name for t in r.targets.values())
    assert names == ["main", "main", "web-dev-blue", "web-prod-blue"]
    t = r.resolve("dev")
    assert t is r.resolve("dev-blue") is r.resolve("web-dev-blue")
    assert t.read_profile == "dev"  # kubeconfig usage breaks the tie with dev-legacy


def test_prod_read_and_write_profiles_split_by_role(files):
    t = reg(files).resolve("prod-blue")
    assert (t.read_profile, t.write_profile) == ("prod", "prod-admin")
    assert t.env == "prod"


def test_missing_and_wrong_exec_profile_are_flagged(files):
    t = reg(files).resolve("web-prod-blue")
    joined = " ".join(t.warnings)
    assert "sets no AWS_PROFILE" in joined
    assert "requests a token for 'other-cluster'" in joined
    assert "account 111111111111, but the cluster lives in 333333333333" in joined
    assert "dev" not in t.kube_profiles  # the wrong-account profile is never used


def test_same_cluster_name_in_two_accounts_is_disambiguated(files):
    r = reg(files)
    with pytest.raises(TargetNotFound, match="unknown target 'main'"):
        r.resolve("main")
    assert r.resolve("dev/main").account_id == "111111111111"
    assert r.resolve("444444444444/main").account_id == "444444444444"
    assert r.resolve("ops-main").read_profile == "ops"  # assume_role profile, account parsed from role_arn


def test_resolve_by_arn_and_unknown_hint(files):
    r = reg(files)
    assert r.resolve("arn:aws:eks:us-east-1:333333333333:cluster/web-prod-blue").alias == "web-prod-blue"
    with pytest.raises(TargetNotFound, match="Did you mean"):
        r.resolve("blue")


def test_selectors(files):
    r = reg(files)
    assert {t.cluster_name for t in r.select("env:prod")} == {"web-prod-blue"}
    assert {t.cluster_name for t in r.select("web-*")} == {"web-dev-blue", "web-prod-blue"}
    assert len(r.select("*")) == 4
    assert {t.account_id for t in r.select("account:dev")} == {"111111111111"}


def test_cluster_map_overrides_and_adds(files):
    r = reg(files, clusters=[
        {"cluster_name": "web-prod-blue", "region": "us-east-1", "account_id": "333333333333",
         "alias": "live", "read_only": True, "env": "production"},
        {"cluster_name": "batch", "region": "eu-west-1", "read_profile": "prod", "write_profile": "prod-admin"},
    ], accounts={"111111111111": {"name": "core-dev", "env": "dev"}})
    prod = r.resolve("live")
    assert prod is r.resolve("prod-blue")  # map entry merged with the kube context
    assert prod.read_only and prod.env == "production" and "cluster_map" in prod.sources
    batch = r.resolve("batch")
    assert batch.account_id == "333333333333" and batch.region == "eu-west-1"
    assert r.resolve("dev").account_name == "core-dev"


def test_exclude_contexts_and_kubeconfig_off(files):
    r = reg(files, discovery={"exclude_contexts": ["prod-*"]})
    assert any("prod-blue (excluded" in s for s in r.skipped_contexts)
    assert reg(files, kubeconfig=False).targets == {}


@pytest.mark.parametrize("name,env", [
    ("web-prod-use1-blue", "prod"), ("api-stg-use1", "stg"),
    ("x-production", "prod"), ("x-prd", "prod"), ("shared-use1-main", None),
])
def test_infer_env(name, env):
    assert infer_env(name) == env
