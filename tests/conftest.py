import pytest
import yaml

AWS_CONFIG = """
[profile dev]
sso_session = s
sso_account_id = 111111111111
sso_role_name = DeveloperAccess
region = us-east-1
[profile dev-legacy]
sso_session = s
sso_account_id = 111111111111
sso_role_name = DeveloperAccess
region = us-east-1
[profile prod]
sso_session = s
sso_account_id = 333333333333
sso_role_name = ReadOnlyAccess
region = us-east-1
[profile prod-admin]
sso_session = s2
sso_account_id = 333333333333
sso_role_name = AdministratorAccess
region = us-east-1
[profile ops]
role_arn = arn:aws:iam::444444444444:role/OpsAdmin
source_profile = dev
[sso-session s]
sso_region = us-east-1
"""


def _ctx(name, account, cluster, exec_cluster=None, profile=None, region="us-east-1"):
    arn = f"arn:aws:eks:{region}:{account}:cluster/{cluster}"
    exec_ = {
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "command": "aws",
        "args": ["--region", region, "eks", "get-token", "--cluster-name", exec_cluster or cluster],
    }
    if profile:
        exec_["env"] = [{"name": "AWS_PROFILE", "value": profile}]
    return (
        {"name": name, "context": {"cluster": arn, "user": f"u-{name}"}},
        {"name": arn, "cluster": {"server": f"https://{abs(hash(arn)) % 10**8:08d}.gr7.{region}.eks.amazonaws.com",
                                  "certificate-authority-data": "Q0E="}},
        {"name": f"u-{name}", "user": {"exec": exec_}},
    )


@pytest.fixture(autouse=True)
def isolated_aws(tmp_path, monkeypatch):
    """Tests must never reach a real account: hide the developer's AWS config and credentials."""
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "no-kubeconfig"))
    monkeypatch.setenv("EKS_MULTI_MCP_DENYLIST", str(tmp_path / "no-denylist"))
    monkeypatch.setenv("EKS_MULTI_MCP_CONFIG", str(tmp_path / "no-config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def files(tmp_path):
    entries = [
        _ctx("dev-blue", "111111111111", "web-dev-blue", profile="dev"),
        _ctx("dev", "111111111111", "web-dev-blue", profile="dev"),
        _ctx("prod-blue", "333333333333", "web-prod-blue"),  # no AWS_PROFILE
        _ctx("prod-bad", "333333333333", "web-prod-blue", exec_cluster="other-cluster", profile="dev"),
        _ctx("ops-main", "444444444444", "main"),
        _ctx("dev-main", "111111111111", "main", profile="dev"),
    ]
    clusters = list({e[1]["name"]: e[1] for e in entries}.values())
    kc = tmp_path / "kubeconfig"
    kc.write_text(yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config",
        "contexts": [e[0] for e in entries], "clusters": clusters, "users": [e[2] for e in entries],
    }))
    aws = tmp_path / "aws_config"
    aws.write_text(AWS_CONFIG)
    return {"kubeconfig": str(kc), "aws_config": str(aws)}
