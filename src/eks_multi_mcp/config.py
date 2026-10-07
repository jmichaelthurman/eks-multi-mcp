"""Server settings: the optional YAML file that layers explicit intent over discovery.

Everything here is optional. With no config file at all the server still discovers
targets from kubeconfig + ~/.aws/config and runs read-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Writes to these envs are always forbidden; config can add envs to the list, never remove these.
ALWAYS_PROTECTED_ENVS = ("prd", "prod", "production")

DEFAULT_CONFIG_PATHS = (
    "~/.config/eks-multi-mcp/config.yaml",
    "~/.eks-multi-mcp.yaml",
)


@dataclass
class AccountSettings:
    account_id: str
    name: str | None = None
    env: str | None = None
    read_profile: str | None = None
    write_profile: str | None = None
    read_only: bool = False


@dataclass
class ClusterSettings:
    """One explicit entry in the account/cluster map."""

    cluster_name: str
    region: str
    account_id: str | None = None
    alias: str | None = None
    aliases: list[str] = field(default_factory=list)
    env: str | None = None
    read_profile: str | None = None
    write_profile: str | None = None
    role_arn: str | None = None  # optional role to assume on top of the profile
    read_only: bool = False
    endpoint: str | None = None
    ca_data: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class Settings:
    kubeconfig_paths: list[str] = field(default_factory=list)
    aws_config_path: str = "~/.aws/config"
    use_kubeconfig: bool = True
    # Call eks:ListClusters in every eligible profile at startup. Off by default
    # because it costs one API call per profile/region and requires live SSO.
    aws_discovery: bool = False
    discovery_regions: list[str] = field(default_factory=list)

    # Profile selection when a target does not pin one explicitly.
    read_role_patterns: list[str] = field(default_factory=lambda: ["ReadOnly", "ViewOnly"])
    write_role_patterns: list[str] = field(default_factory=lambda: ["Admin", "PowerUser"])
    exclude_profiles: list[str] = field(default_factory=list)

    # Safety policy.
    allow_write: bool = False
    allow_sensitive_data_access: bool = False
    protected_envs: list[str] = field(default_factory=lambda: list(ALWAYS_PROTECTED_ENVS))
    exclude_contexts: list[str] = field(default_factory=list)  # glob patterns

    accounts: dict[str, AccountSettings] = field(default_factory=dict)
    clusters: list[ClusterSettings] = field(default_factory=list)

    log_level: str = "WARNING"
    source_path: str | None = None


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def default_kubeconfig_paths() -> list[str]:
    env = os.environ.get("KUBECONFIG")
    if env:
        return [p for p in env.split(os.pathsep) if p]
    return ["~/.kube/config"]


def load_settings(path: str | None = None) -> Settings:
    candidates = [path] if path else [os.environ.get("EKS_MULTI_MCP_CONFIG"), *DEFAULT_CONFIG_PATHS]
    raw: dict[str, Any] = {}
    source = None
    for c in candidates:
        if c and Path(_expand(c)).is_file():
            source = _expand(c)
            raw = yaml.safe_load(Path(source).read_text()) or {}
            break
    if path and source is None:
        raise FileNotFoundError(f"config file not found: {path}")
    return settings_from_dict(raw, source)


def settings_from_dict(raw: dict[str, Any], source: str | None = None) -> Settings:
    s = Settings(source_path=source)

    kc = raw.get("kubeconfig", None)
    if kc is False:
        s.use_kubeconfig = False
    elif isinstance(kc, str):
        s.kubeconfig_paths = [kc]
    elif isinstance(kc, list):
        s.kubeconfig_paths = list(kc)
    if not s.kubeconfig_paths:
        s.kubeconfig_paths = default_kubeconfig_paths()
    s.kubeconfig_paths = [_expand(p) for p in s.kubeconfig_paths]

    s.aws_config_path = _expand(raw.get("aws_config", os.environ.get("AWS_CONFIG_FILE", s.aws_config_path)))

    disc = raw.get("discovery", {}) or {}
    s.aws_discovery = bool(disc.get("aws", s.aws_discovery))
    s.discovery_regions = list(disc.get("regions", s.discovery_regions))
    s.exclude_contexts = list(disc.get("exclude_contexts", s.exclude_contexts))

    prefs = raw.get("profiles", {}) or {}
    s.read_role_patterns = list(prefs.get("read_role_patterns", s.read_role_patterns))
    s.write_role_patterns = list(prefs.get("write_role_patterns", s.write_role_patterns))
    s.exclude_profiles = list(prefs.get("exclude", s.exclude_profiles))

    safety = raw.get("safety", {}) or {}
    s.allow_write = bool(safety.get("allow_write", s.allow_write))
    s.allow_sensitive_data_access = bool(
        safety.get("allow_sensitive_data_access", s.allow_sensitive_data_access)
    )
    configured = [str(e).lower() for e in safety.get("protected_envs") or []]
    s.protected_envs = list(dict.fromkeys([*ALWAYS_PROTECTED_ENVS, *configured]))

    for acct_id, a in (raw.get("accounts", {}) or {}).items():
        a = a or {}
        acct_id = str(acct_id)
        s.accounts[acct_id] = AccountSettings(
            account_id=acct_id,
            name=a.get("name"),
            env=a.get("env"),
            read_profile=a.get("read_profile"),
            write_profile=a.get("write_profile"),
            read_only=bool(a.get("read_only", False)),
        )

    for c in raw.get("clusters", []) or []:
        s.clusters.append(
            ClusterSettings(
                cluster_name=c["cluster_name"],
                region=c["region"],
                account_id=str(c["account_id"]) if c.get("account_id") else None,
                alias=c.get("alias"),
                aliases=list(c.get("aliases", [])),
                env=c.get("env"),
                read_profile=c.get("read_profile") or c.get("profile"),
                write_profile=c.get("write_profile"),
                role_arn=c.get("role_arn"),
                read_only=bool(c.get("read_only", False)),
                endpoint=c.get("endpoint"),
                ca_data=c.get("ca_data"),
                tags=dict(c.get("tags", {})),
            )
        )
    return s
