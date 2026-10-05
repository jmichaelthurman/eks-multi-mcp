import asyncio
import base64
import urllib.parse

import pytest

from eks_multi_mcp.auth import AuthManager
from eks_multi_mcp.config import settings_from_dict
from eks_multi_mcp.server import EksMultiServer


def call(srv, tool_name, **args):
    return asyncio.run(srv.mcp.call_tool(tool_name, args))


def server(files, **extra):
    return EksMultiServer(settings_from_dict({**files, **extra}))


def test_tools_registered_with_annotations(files):
    tools = {t.name: t for t in asyncio.run(server(files).mcp.list_tools())}
    for name in ["list_targets", "doctor", "list_k8s_resources", "fleet_overview", "apply_yaml"]:
        assert name in tools
    assert tools["list_targets"].annotations.read_only_hint is True
    assert tools["apply_yaml"].annotations.destructive_hint is True
    assert "target" in tools["list_k8s_resources"].input_schema["required"]


def test_list_targets_offline(files):
    res = call(server(files), "list_targets", selector="env:prod")
    assert "web-prod-blue" in res.content[0].text


def test_writes_blocked_when_read_only(files):
    with pytest.raises(Exception, match="read-only"):
        call(server(files), "apply_yaml", target="dev", yaml_content="kind: x")


def test_protected_env_requires_confirm(files):
    srv = server(files, safety={"allow_write": True})
    with pytest.raises(Exception, match="confirm_cluster='web-prod-blue'"):
        call(srv, "manage_k8s_resource", target="prod-blue", operation="delete", kind="Pod", name="x")


def test_cluster_map_read_only_beats_allow_write(files):
    srv = server(files, safety={"allow_write": True},
                 clusters=[{"cluster_name": "web-dev-blue", "region": "us-east-1",
                            "account_id": "111111111111", "read_only": True}])
    with pytest.raises(Exception, match="read_only"):
        call(srv, "apply_yaml", target="dev", yaml_content="kind: x")


def test_sensitive_tools_gated(files):
    with pytest.raises(Exception, match="sensitive data access"):
        call(server(files), "get_pod_logs", target="dev", namespace="a", pod_name="b")


def test_eks_token_shape(files, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    srv = server(files)
    t = srv.registry.resolve("dev")
    t.read_profile = None  # use env credentials
    tok = AuthManager().eks_token(t)
    assert tok.startswith("k8s-aws-v1.") and "=" not in tok
    b = tok.removeprefix("k8s-aws-v1.")
    url = urllib.parse.urlparse(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode())
    q = urllib.parse.parse_qs(url.query)
    assert url.netloc == "sts.us-east-1.amazonaws.com"
    assert q["Action"] == ["GetCallerIdentity"]
    assert "x-k8s-aws-id" in q["X-Amz-SignedHeaders"][0]
