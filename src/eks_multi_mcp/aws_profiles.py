"""Parse ~/.aws/config into an account -> profiles index without making any API calls."""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

_ROLE_ARN = re.compile(r"^arn:aws[\w-]*:iam::(\d{12}):role/(?:.*/)?([^/]+)$")


@dataclass(frozen=True)
class AwsProfile:
    name: str
    account_id: str | None
    role_name: str | None
    region: str | None
    kind: str  # sso | assume_role | credential_process | static | unknown


def load_aws_profiles(path: str) -> dict[str, AwsProfile]:
    p = Path(path)
    if not p.is_file():
        return {}
    cp = configparser.RawConfigParser(strict=False)
    cp.read(p)

    sections: dict[str, dict[str, str]] = {}
    for section in cp.sections():
        if section == "default":
            name = "default"
        elif section.startswith("profile "):
            name = section[len("profile ") :].strip()
        else:
            continue  # sso-session, services, etc.
        sections[name] = {k: v.strip() for k, v in cp.items(section)}

    out: dict[str, AwsProfile] = {}
    for name, kv in sections.items():
        account_id = kv.get("sso_account_id")
        role_name = kv.get("sso_role_name")
        kind = "unknown"
        if account_id:
            kind = "sso"
        elif kv.get("role_arn"):
            kind = "assume_role"
            m = _ROLE_ARN.match(kv["role_arn"])
            if m:
                account_id, role_name = m.group(1), m.group(2)
        elif kv.get("credential_process"):
            kind = "credential_process"
        elif kv.get("aws_access_key_id"):
            kind = "static"
        out[name] = AwsProfile(
            name=name,
            account_id=account_id,
            role_name=role_name,
            region=kv.get("region"),
            kind=kind,
        )
    return out


def profiles_by_account(profiles: dict[str, AwsProfile], exclude: list[str]) -> dict[str, list[AwsProfile]]:
    idx: dict[str, list[AwsProfile]] = {}
    for prof in profiles.values():
        if prof.account_id and prof.name not in exclude:
            idx.setdefault(prof.account_id, []).append(prof)
    for lst in idx.values():
        lst.sort(key=lambda p: p.name)
    return idx


def pick_profile(candidates: list[AwsProfile], patterns: list[str]) -> AwsProfile | None:
    """First profile whose role name matches a pattern, in pattern priority order."""
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for prof in candidates:
            if prof.role_name and rx.search(prof.role_name):
                return prof
    return None


def pick_read_write(
    candidates: list[AwsProfile], read_patterns: list[str], write_patterns: list[str]
) -> tuple[AwsProfile | None, AwsProfile | None]:
    """Choose a read profile and a write profile for one account.

    Read prefers least privilege (read-role patterns), else anything that is *not*
    a write-role, else the first profile. Write prefers write-role patterns, else
    falls back to the read profile (so accounts with a single role still work).
    """
    if not candidates:
        return None, None
    write = pick_profile(candidates, write_patterns)
    read = pick_profile(candidates, read_patterns)
    if read is None:
        non_write = [c for c in candidates if c is not write]
        read = non_write[0] if non_write else candidates[0]
    return read, write or read
