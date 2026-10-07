"""AuthManager: profile choice per mode, role assumption, caching, client construction."""

import base64
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    UnauthorizedSSOTokenError,
)
from fakes import make_server

from eks_multi_mcp import auth as auth_mod
from eks_multi_mcp.auth import AuthManager, CredentialError, explain_aws_error


@pytest.fixture
def target(files):
    t = make_server(files).registry.resolve("prod-blue")
    assert (t.read_profile, t.write_profile) == ("prod", "prod-admin")
    return t


@pytest.mark.parametrize("exc,expect", [
    (UnauthorizedSSOTokenError(), "SSO session behind profile 'p' is missing or expired"),
    (ProfileNotFound(profile="p"), "profile 'p' is not defined"),
    (NoCredentialsError(), "no credentials could be loaded for profile 'p'"),
    (ClientError({"Error": {"Code": "ExpiredToken", "Message": "x"}}, "Op"), "expired or invalid (ExpiredToken)"),
    (ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Op"), "AccessDenied: no (via profile 'p')"),
    (ValueError("odd"), "ValueError: odd (via profile 'p')"),
])
def test_explain_aws_error(exc, expect):
    assert expect in explain_aws_error(exc, "p")


def test_explain_aws_error_without_profile():
    assert "default credential chain" in explain_aws_error(NoCredentialsError(), None)


def test_session_uses_read_or_write_profile(target, monkeypatch):
    am = AuthManager()
    made = []
    monkeypatch.setattr(auth_mod.boto3, "Session", lambda **kw: made.append(kw) or SimpleNamespace(**kw))
    assert am.session(target, "read").profile_name == "prod"
    assert am.session(target, "write").profile_name == "prod-admin"
    am.session(target, "read")
    assert made == [{"profile_name": "prod"}, {"profile_name": "prod-admin"}]  # cached per profile


def test_unknown_profile_is_a_credential_error(target):
    target.read_profile = "does-not-exist"
    with pytest.raises(CredentialError, match="'does-not-exist' is not defined"):
        AuthManager().session(target)


class FakeSts:
    def __init__(self, expires_in: timedelta):
        self.assumed = []
        self.expires_in = expires_in

    def assume_role(self, RoleArn, RoleSessionName):
        self.assumed.append(RoleArn)
        return {"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t",
                                "Expiration": datetime.now(UTC) + self.expires_in}}


def test_role_assumption_is_cached_until_near_expiry(target, monkeypatch):
    am = AuthManager()
    target.role_arn = "arn:aws:iam::333333333333:role/EksAdmin"
    sts = FakeSts(timedelta(hours=1))
    monkeypatch.setattr(am, "base_session", lambda p: SimpleNamespace(client=lambda *a, **k: sts))
    s1 = am.session(target)
    assert am.session(target) is s1
    assert sts.assumed == [target.role_arn]
    sts.expires_in = timedelta(minutes=1)
    am._assumed.clear()
    am.session(target)
    am.session(target)  # under 5 minutes left: assume again
    assert len(sts.assumed) == 3


def test_role_assumption_failure_is_explained(target, monkeypatch):
    am = AuthManager()
    target.role_arn = "arn:aws:iam::333333333333:role/EksAdmin"

    def deny(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "not trusted"}}, "AssumeRole")

    monkeypatch.setattr(am, "base_session", lambda p: SimpleNamespace(
        client=lambda *a, **k: SimpleNamespace(assume_role=deny)))
    with pytest.raises(CredentialError, match="assume_role .*EksAdmin failed: AccessDenied: not trusted"):
        am.session(target)


def test_caller_identity_maps_errors(target, monkeypatch):
    am = AuthManager()

    def expired():
        raise ClientError({"Error": {"Code": "ExpiredTokenException", "Message": "x"}}, "GetCallerIdentity")

    monkeypatch.setattr(am, "aws", lambda t, s, mode="read": SimpleNamespace(get_caller_identity=expired))
    with pytest.raises(CredentialError, match="profile 'prod' are expired"):
        am.caller_identity(target)
    monkeypatch.setattr(am, "aws", lambda t, s, mode="read": SimpleNamespace(
        get_caller_identity=lambda: {"Account": "333333333333", "Arn": "arn:x"}))
    assert am.caller_identity(target) == {"account": "333333333333", "arn": "arn:x"}


def test_describe_cluster_caches_and_refreshes(target, monkeypatch):
    am = AuthManager()
    calls = []
    monkeypatch.setattr(am, "aws", lambda t, s, mode="read": SimpleNamespace(
        describe_cluster=lambda name: calls.append(name) or {"cluster": {"n": len(calls)}}))
    assert am.describe_cluster(target) == {"n": 1}
    assert am.describe_cluster(target) == {"n": 1}
    assert am.describe_cluster(target, refresh=True) == {"n": 2}
    assert calls == ["web-prod-blue", "web-prod-blue"]


def test_eks_token_without_credentials(target, monkeypatch):
    am = AuthManager()
    monkeypatch.setattr(am, "session", lambda t, mode="read": SimpleNamespace(get_credentials=lambda: None))
    with pytest.raises(CredentialError, match="profile 'prod-admin'"):
        am.eks_token(target, "write")


@pytest.fixture
def no_network(target, monkeypatch):
    """AuthManager whose endpoint lookup and token minting are canned."""
    am = AuthManager()
    tokens = []
    monkeypatch.setattr(am, "describe_cluster", lambda t, refresh=False: {
        "endpoint": "https://live.example", "certificateAuthority": {"data": base64.b64encode(b"CA").decode()}})
    monkeypatch.setattr(am, "eks_token", lambda t, mode="read": tokens.append(mode) or f"tok-{mode}")
    monkeypatch.setattr(auth_mod, "DynamicClient", lambda api: SimpleNamespace(api=api))  # skips discovery
    return am, tokens


def test_client_built_with_live_endpoint_ca_and_bearer(target, no_network):
    am, tokens = no_network
    api = am.api_client(target, "write")
    cfg = api.configuration
    assert cfg.host == "https://live.example" and cfg.verify_ssl is True
    assert Path(cfg.ssl_ca_cert).read_bytes() == b"CA"
    assert oct(os.stat(os.path.dirname(cfg.ssl_ca_cert)).st_mode & 0o777) == "0o700"
    assert cfg.api_key["BearerToken"] == "Bearer tok-write"
    assert tokens == ["write"]


def test_clients_cached_per_mode_and_invalidated(target, no_network):
    am, _ = no_network
    r1, w1 = am.api_client(target, "read"), am.api_client(target, "write")
    assert r1 is not w1
    assert am.api_client(target, "read") is r1
    assert am.dynamic(target, "read") is am.dynamic(target, "read")
    am.invalidate(target)
    assert am.api_client(target, "read") is not r1
    am.invalidate()
    assert am._api == {} and am._clusters == {}


def test_token_refreshes_near_expiry(target, no_network, monkeypatch):
    am, tokens = no_network
    cfg = am.api_client(target).configuration
    cfg.refresh_api_key_hook(cfg)
    assert tokens == ["read"]  # still fresh
    monkeypatch.setattr(auth_mod.time, "time", lambda: 10**12)
    cfg.refresh_api_key_hook(cfg)
    assert tokens == ["read", "read"]


def test_endpoint_falls_back_to_kubeconfig_when_describe_denied(target, monkeypatch):
    am = AuthManager()

    def denied(t, refresh=False):
        raise CredentialError("AccessDeniedException: no eks:DescribeCluster (via profile 'prod')")

    monkeypatch.setattr(am, "describe_cluster", denied)
    assert am._endpoint_and_ca(target) == (target.endpoint, target.ca_data)
    target.endpoint = None
    with pytest.raises(CredentialError, match="AccessDenied"):
        am._endpoint_and_ca(target)


def test_deleted_cluster_reported_as_stale_context(target, monkeypatch):
    am = AuthManager()

    def gone(t, refresh=False):
        raise CredentialError("ResourceNotFoundException: No cluster found")

    monkeypatch.setattr(am, "describe_cluster", gone)
    with pytest.raises(CredentialError, match="no longer exists in account 333333333333.*stale"):
        am._endpoint_and_ca(target)
