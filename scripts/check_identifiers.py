#!/usr/bin/env python3
"""Block commits that would publish real environment identifiers.

The denylist is never stored in the repository. It is derived at run time from the
developer's own ~/.aws/config and kubeconfig (account IDs, SSO role and session names,
hyphenated profile names, cluster and context names, EKS endpoint IDs), plus an optional
private file of extra terms (one per line):

    ~/.config/eks-multi-mcp/denylist.txt      (or $EKS_MULTI_MCP_DENYLIST)

A generic rule also runs everywhere (including CI, where no local config exists): any
12-digit number must be an obvious placeholder such as 111111111111 or 123456789012.

Usage:
    scripts/check_identifiers.py --staged    # what is about to be committed (pre-commit hook)
    scripts/check_identifiers.py --all       # every tracked or committable file
    scripts/check_identifiers.py FILE...
Exit status is 1 when anything is found. Matches are printed masked.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eks_multi_mcp.aws_profiles import load_aws_profiles
from eks_multi_mcp.config import default_kubeconfig_paths
from eks_multi_mcp.kubeconfig import EKS_ENDPOINT, load_kube_contexts

ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
PLACEHOLDER_IDS = {"123456789012", "210987654321", "000000000000"} | {str(d) * 12 for d in range(10)}
# Too generic to flag on their own; they still count inside longer names.
COMMON_WORDS = {"default", "dev", "develop", "development", "test", "qa", "uat", "stg", "stage", "staging",
                "prd", "prod", "production", "sandbox", "admin", "readonly", "main", "data", "infra"}
SKIP_FILES = {"uv.lock"}


def private_terms() -> set[str]:
    terms: set[str] = set()
    aws_config = os.path.expanduser(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config"))
    for prof in load_aws_profiles(aws_config).values():
        if prof.account_id:
            terms.add(prof.account_id)
        if prof.role_name:
            terms.add(prof.role_name)
        if "-" in prof.name or any(c.isdigit() or c.isupper() for c in prof.name):
            terms.add(prof.name)
    try:
        text = Path(aws_config).read_text()
        terms.update(re.findall(r"^\s*\[sso-session\s+([^\]\s]+)\s*\]", text, re.MULTILINE))
        terms.update(re.findall(r"^\s*sso_start_url\s*=\s*(\S+)", text, re.MULTILINE))
    except OSError:
        pass

    for kc in load_kube_contexts([os.path.expanduser(p) for p in default_kubeconfig_paths()]):
        if not kc.is_eks:
            continue
        for t in (kc.arn_account, kc.cluster_name, kc.exec_cluster, kc.exec_profile):
            if t:
                terms.add(t)
        if "-" in kc.name:
            terms.add(kc.name)
        if kc.server and EKS_ENDPOINT.match(kc.server):
            terms.add(kc.server.split("//", 1)[1].split(".", 1)[0])

    extra = Path(os.path.expanduser(os.environ.get("EKS_MULTI_MCP_DENYLIST",
                                                   "~/.config/eks-multi-mcp/denylist.txt")))
    if extra.is_file():
        terms.update(x.strip() for x in extra.read_text().splitlines() if x.strip() and not x.startswith("#"))
    return {t for t in terms if len(t) >= 4 and t.lower() not in COMMON_WORDS and t not in PLACEHOLDER_IDS}


def compile_terms(terms: set[str]) -> re.Pattern | None:
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_-])(?:{alts})(?![A-Za-z0-9_-])", re.IGNORECASE)


def mask(s: str) -> str:
    return s[:3] + "*" * max(3, len(s) - 3)


def scan_text(name: str, text: str, pattern: re.Pattern | None) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if pattern:
            for m in pattern.finditer(line):
                hits.append(f"{name}:{n}: private identifier '{mask(m.group(0))}'")
        if Path(name).name not in SKIP_FILES:
            for m in ACCOUNT_ID.finditer(line):
                if m.group(0) not in PLACEHOLDER_IDS:
                    hits.append(f"{name}:{n}: 12-digit number '{mask(m.group(0))}' is not a placeholder "
                                f"(use 111111111111-style IDs)")
    return hits


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args(argv)

    pattern = compile_terms(private_terms())
    sources: list[tuple[str, str]] = []
    if args.staged:
        for f in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0"):
            if f:
                sources.append((f, git("show", f":{f}")))
    else:
        names = args.files or [f for f in (git("ls-files", "-z") + git("ls-files", "-z", "--others",
                                                                         "--exclude-standard")).split("\0") if f]
        for f in names:
            try:
                sources.append((f, Path(f).read_text(errors="replace")))
            except (OSError, IsADirectoryError):
                continue

    hits = [h for name, text in sources for h in scan_text(name, text, pattern)]
    # Commit messages are checked by the commit-msg hook via --files .git/COMMIT_EDITMSG.
    for h in hits:
        print(h, file=sys.stderr)
    if hits:
        print(f"\nblocked: {len(hits)} environment identifier(s) found. Replace them with placeholders; "
              "real values belong in ~/.config/eks-multi-mcp/config.yaml.", file=sys.stderr)
        return 1
    print(f"check_identifiers: {len(sources)} file(s) clean "
          f"({'local denylist active' if pattern else 'generic rules only'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
