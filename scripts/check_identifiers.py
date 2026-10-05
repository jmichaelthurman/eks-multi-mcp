#!/usr/bin/env python3
"""Block commits that would publish real environment identifiers.

The denylist is never stored in the repository. It is derived at run time from the
developer's own ~/.aws/config and kubeconfig (account IDs, SSO role and session names,
hyphenated profile names, cluster and context names, EKS endpoint IDs), plus an optional
private file of extra terms (one per line):

    ~/.config/eks-multi-mcp/denylist.txt      (or $EKS_MULTI_MCP_DENYLIST)

Your global git email (and its domain, unless it is a public mail provider) is added
too, so an employer address cannot leak through file content or commit metadata.

Generic rules also run everywhere (including CI, where no local config exists): any
12-digit number must be an obvious placeholder such as 111111111111 or 123456789012, and
in --push / --range mode every author and committer email must match
`git config identifiers.allowedEmail` (a regex; default: GitHub noreply addresses).

Usage:
    scripts/check_identifiers.py --staged    # what is about to be committed (pre-commit hook)
    scripts/check_identifiers.py --push      # commits being pushed; reads pre-push stdin
    scripts/check_identifiers.py --range A..B   # commits in a revision range
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
PUBLIC_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "icloud.com", "me.com",
               "yahoo.com", "proton.me", "protonmail.com", "users.noreply.github.com"}
DEFAULT_ALLOWED_EMAIL = r"@users\.noreply\.github\.com$"
ZERO_SHA = re.compile(r"^0+$")


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

    email = _git_config("--global", "user.email")
    if email and not email.endswith("@users.noreply.github.com"):
        terms.add(email)
        domain = email.rsplit("@", 1)[-1].lower()
        if domain not in PUBLIC_MAIL:
            terms.add(domain)
            terms.add(domain.split(".")[0])

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


def _git_config(*args: str) -> str | None:
    res = subprocess.run(["git", "config", *args], capture_output=True, text=True, check=False)
    return res.stdout.strip() or None


def push_ranges(stdin_lines: list[str]) -> list[str]:
    """Translate pre-push stdin (`<local ref> <local sha> <remote ref> <remote sha>`) into
    rev-list arguments that cover exactly the commits the remote does not have yet."""
    ranges = []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) != 4 or ZERO_SHA.match(parts[1]):  # malformed, or a branch deletion
            continue
        local, remote = parts[1], parts[3]
        if ZERO_SHA.match(remote):
            ranges.append(f"{local} --not --remotes")  # new branch: anything no remote has
        else:
            ranges.append(f"{remote}..{local}")
    return ranges


def scan_commits(rev_args: str, pattern: re.Pattern | None, allowed_email: re.Pattern) -> tuple[int, list[str]]:
    """Scan every commit in the range: added lines of its patch (so content added and later
    deleted is still caught), its message, and its author/committer identity."""
    commits = [c for c in git("rev-list", *rev_args.split()).split() if c]
    hits: list[str] = []
    for c in commits:
        short = c[:7]
        an, ae, cn, ce, *msg = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", c).split("\n")
        for role, email in (("author", ae), ("committer", ce)):
            if not allowed_email.search(email):
                hits.append(f"{short}: {role} email '{mask(email)}' does not match identifiers.allowedEmail")
        hits += scan_text(f"{short}:identity", f"{an}\n{cn}\n{ae}\n{ce}", pattern)
        hits += scan_text(f"{short}:message", "\n".join(msg), pattern)
        current = "?"
        added: dict[str, list[str]] = {}
        patch = git("show", "--format=", "--unified=0", "--no-color", "--no-ext-diff", "--no-renames", c)
        for line in patch.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("+") and not line.startswith("+++"):
                added.setdefault(current, []).append(line[1:])
        for path, lines in added.items():
            hits += scan_text(f"{short}:{path}", "\n".join(lines), pattern)
    return len(commits), hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--push", action="store_true", help="pre-push mode: read ref lines from stdin")
    ap.add_argument("--range", help="scan the commits in a revision range, e.g. origin/main..HEAD")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args(argv)

    pattern = compile_terms(private_terms())
    if args.push or args.range:
        allowed = re.compile(_git_config("identifiers.allowedEmail") or DEFAULT_ALLOWED_EMAIL, re.IGNORECASE)
        ranges = push_ranges(sys.stdin.read().splitlines()) if args.push else [args.range]
        total, hits = 0, []
        for rng in ranges:
            n, h = scan_commits(rng, pattern, allowed)
            total, hits = total + n, hits + h
        return report(hits, f"{total} commit(s)", pattern)

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
    return report(hits, f"{len(sources)} file(s)", pattern)


def report(hits: list[str], scope: str, pattern: re.Pattern | None) -> int:
    for h in hits:
        print(h, file=sys.stderr)
    if hits:
        print(f"\nblocked: {len(hits)} environment identifier(s) found. Replace them with placeholders; "
              "real values belong in ~/.config/eks-multi-mcp/config.yaml. For commit identity, set a "
              "repo-local user.email that matches identifiers.allowedEmail and re-author the commits "
              "(git rebase -r <base> --exec 'git commit --amend --no-edit --reset-author').", file=sys.stderr)
        return 1
    print(f"check_identifiers: {scope} clean "
          f"({'local denylist active' if pattern else 'generic rules only'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
