"""Command line entry point and settings loading."""

import json

import pytest

from eks_multi_mcp import cli
from eks_multi_mcp.config import load_settings, settings_from_dict


def test_load_settings_explicit_path_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError, match="config file not found"):
        load_settings(str(tmp_path / "missing.yaml"))


def test_load_settings_from_env_var(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("safety:\n  allow_write: true\n  protected_envs: [Live]\n")
    monkeypatch.setenv("EKS_MULTI_MCP_CONFIG", str(cfg))
    s = load_settings()
    assert s.source_path == str(cfg) and s.allow_write is True
    assert s.protected_envs == ["prd", "prod", "production", "live"]


def test_defaults_are_safe(tmp_path):
    s = load_settings()  # conftest points every default path at a missing file
    assert s.source_path is None
    assert s.allow_write is False and s.allow_sensitive_data_access is False
    assert {"prd", "prod", "production"} <= set(s.protected_envs)


def test_settings_from_dict_shapes():
    s = settings_from_dict({
        "kubeconfig": "~/k", "accounts": {111111111111: {"name": "a", "read_only": True}},
        "clusters": [{"cluster_name": "c", "region": "r", "account_id": 222222222222, "profile": "p"}],
    })
    assert s.kubeconfig_paths[0].endswith("/k") and not s.kubeconfig_paths[0].startswith("~")
    assert s.accounts["111111111111"].read_only is True
    assert s.clusters[0].account_id == "222222222222" and s.clusters[0].read_profile == "p"
    assert settings_from_dict({"kubeconfig": False}).use_kubeconfig is False


def _cli_env(files, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", files["kubeconfig"])
    monkeypatch.setenv("AWS_CONFIG_FILE", files["aws_config"])


def test_cli_targets_table(files, monkeypatch, capsys):
    _cli_env(files, monkeypatch)
    cli.main(["targets", "--selector", "env:prod"])
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert lines[0].split()[:2] == ["TARGET", "ENV"]
    assert any(line.startswith("web-prod-blue") for line in lines[1:])
    assert "1 targets" in out.err


def test_cli_doctor_exit_code_reflects_problems(files, monkeypatch, capsys):
    _cli_env(files, monkeypatch)
    with pytest.raises(SystemExit) as ex:
        cli.main(["doctor", "--no-access-check"])
    report = json.loads(capsys.readouterr().out)
    assert ex.value.code == (1 if report["targets_with_problems"] else 0)
    assert report["targets_with_problems"] > 0  # the fixture has contexts with no AWS_PROFILE


def test_cli_flags_reach_settings(files, monkeypatch):
    _cli_env(files, monkeypatch)
    seen = {}

    class Srv:
        def __init__(self, settings):
            seen["s"] = settings
            self.mcp = type("M", (), {"run": lambda self, transport: seen.setdefault("transport", transport)})()

    monkeypatch.setattr("eks_multi_mcp.server.EksMultiServer", Srv)
    cli.main(["--allow-write", "--allow-sensitive-data-access", "--no-kubeconfig"])
    s = seen["s"]
    assert s.allow_write and s.allow_sensitive_data_access and not s.use_kubeconfig
    assert seen["transport"] == "stdio"


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
@pytest.mark.parametrize("flags,config", [
    (["--allow-write"], ""),
    (["--allow-sensitive-data-access"], ""),
    ([], "safety:\n  allow_write: true\n"),
    ([], "safety:\n  allow_sensitive_data_access: true\n"),
])
def test_http_transports_refuse_write_and_sensitive_access(files, monkeypatch, tmp_path, transport, flags, config):
    """HTTP has no authentication: any local process could connect, act as the MCP client
    and answer its own approval prompts. So writes and sensitive reads are stdio-only,
    whether enabled by flag or by config file."""
    _cli_env(files, monkeypatch)
    if config:
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(config)
        flags = [*flags, "--config", str(cfg)]
    started = []
    monkeypatch.setattr("eks_multi_mcp.server.EksMultiServer", lambda s: started.append(s))
    with pytest.raises(SystemExit) as ex:
        cli.main(["--transport", transport, "--no-kubeconfig", *flags])
    assert "only over stdio" in str(ex.value) and started == []


def test_http_transport_read_only_is_allowed(files, monkeypatch):
    _cli_env(files, monkeypatch)
    seen = {}

    class Srv:
        def __init__(self, settings):
            seen["s"] = settings
            self.mcp = type("M", (), {"run": lambda self, transport: seen.setdefault("transport", transport)})()

    monkeypatch.setattr("eks_multi_mcp.server.EksMultiServer", Srv)
    cli.main(["--transport", "streamable-http", "--no-kubeconfig"])
    assert seen["transport"] == "streamable-http" and not seen["s"].allow_write


def test_cli_default_is_read_only_stdio(files, monkeypatch):
    _cli_env(files, monkeypatch)
    seen = {}

    class Srv:
        def __init__(self, settings):
            seen["s"] = settings
            self.mcp = type("M", (), {"run": lambda self, transport: seen.setdefault("transport", transport)})()

    monkeypatch.setattr("eks_multi_mcp.server.EksMultiServer", Srv)
    cli.main([])
    assert seen["s"].allow_write is False and seen["transport"] == "stdio"
