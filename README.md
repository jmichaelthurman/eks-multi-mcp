# eks-multi-mcp

An Amazon EKS MCP server for AWS Organizations with **many accounts and many clusters per
account**. The official `awslabs.eks-mcp-server` is pinned to one `AWS_PROFILE` and one
cluster per process. This server builds a registry of every reachable cluster and takes an
explicit `target` on every call.

## Where targets come from

All sources merge, deduplicated by cluster (`account/region/name`); the cluster map wins on conflicts:

| Source | What it gives you |
|---|---|
| **kubeconfig** (`$KUBECONFIG` / `~/.kube/config`) | Every EKS context. Many contexts pointing at the same cluster become one target, and every context name works as an alias. |
| **`~/.aws/config`** | Maps account → profiles (SSO `sso_account_id` or `role_arn`). Picks a **read** profile and a **write** profile per account by role name, e.g. a `ReadOnlyAccess` profile for reads and an `AdministratorAccess` profile for writes. |
| **cluster map** (optional YAML) | Aliases, env labels, per-cluster profile pins, `role_arn` to assume, `read_only`, and clusters with no kube context at all. See [`examples/config.example.yaml`](examples/config.example.yaml). |
| `discover_clusters` tool / `discovery.aws: true` | `eks:ListClusters` in each account, which finds clusters nobody has added to kubeconfig yet. Discovered targets last until the server restarts; add them to the map to keep them. |

### How profiles and environments are chosen

For each cluster the **read profile** is, in order: a pin in the cluster map, the account's
`read_profile`, else the first profile for that account whose role name matches
`read_role_patterns` (default `ReadOnly`, `ViewOnly`). The **write profile** works the same
way with `write_role_patterns` (default `Admin`, `PowerUser`) and falls back to the read
profile when nothing matches. When several profiles qualify, the one kubeconfig already uses
for that cluster wins.

A cluster's **env** comes from the map (cluster, then account). Otherwise it is inferred
from a `dev` / `stg` / `prod` token in the cluster, account or profile name; `prd` and
`production` normalize to `prod`, `staging` to `stg`. An inferred env is used for display,
selectors and protection, but it never makes a cluster writable: set `env` in the map for
every account you want to write to.

### Why it never runs your kubeconfig exec blocks

The server mints EKS tokens itself, the same presigned `sts:GetCallerIdentity` that
`aws eks get-token` produces, using the profile resolved **from the cluster's account**.
That makes it immune to the usual kubeconfig traps, and `doctor` reports each one:

- the exec block sets no `AWS_PROFILE`, so kubectl silently uses the ambient profile
- the exec block's `AWS_PROFILE` belongs to a **different account** than the cluster ARN
- the exec block requests a token for a **different cluster** than the context
- a stale context for a deleted cluster (checked live against `eks:DescribeCluster`)

## Safety model

- **Read-only by default.** Write tools (`apply_yaml`, `manage_k8s_resource`) need `--allow-write`.
- Writes use the target's **write profile**; reads always use the **read profile**.
- **Writes to production are forbidden outright.** Protected envs are always `prd`, `prod` and
  `production`; `safety.protected_envs` can add more but never remove those. A target whose env
  is unknown, or only inferred from its name, is treated as protected (a name can say `dev`
  in a production account). These writes are refused before any client is built or any
  prompt is shown, and nothing can approve them.
- **Every other write needs a human to approve it, dry runs included.** Before anything uses
  the write profile, the server asks the client to show an MCP elicitation prompt: mid-call on
  older protocol versions, or as an input-required round trip on 2026-07-28 (what Claude Code
  speaks), where the echoed state is sealed and bound to the exact write and prompt. The prompt
  shows the operation, the full YAML or body, and the cluster, account, env and write profile
  both before and after it. The write goes ahead only if the user approves; the calling agent
  can't supply the approval.
- **The prompt is never partial or forged.** A write too large to show in full is refused,
  not cut off. Names, namespaces, kinds and API versions must be valid Kubernetes names, so
  they can't smuggle in fake prompt lines. The request's YAML sits between markers with
  every line indented, so it can't pass for the server's own text, and a body whose kind,
  API version, name or namespace disagrees with the operation shown is refused.
- **Fails closed.** If the client can't show an elicitation prompt, or the user declines,
  cancels, or answers anything else, nothing is sent to the cluster.
- Approval is only as good as the client: use a client that shows elicitation prompts to
  you, never one that answers them automatically. Production doesn't depend on this; it is
  never writable.
- The config file, including the safety policy, is read only at startup; `reload_config`
  re-reads kubeconfig and `~/.aws/config` only.
- `read_only: true` on an account or cluster in the map overrides `--allow-write`.
- Pod logs, CloudWatch logs and Secret values need `--allow-sensitive-data-access`.
  Without it, a Secret comes back as an allowlist only (name, namespace, labels, type and
  key names); annotations such as `last-applied-configuration`, which carry the values, are
  dropped.
- **Writes and sensitive access are stdio-only.** The HTTP transports have no client
  authentication, so any local process could connect and answer its own approval prompts.
  The server refuses to start with `--allow-write` or `--allow-sensitive-data-access` on them.
- **No default target.** Every response carries a `_target` stamp (account, cluster,
  profile, mode), so an answer can't silently come from the wrong account.

## Tools

| Group | Tools |
|---|---|
| Registry | `list_targets`, `describe_target`, `doctor`, `discover_clusters`, `reload_config` |
| EKS / AWS | `describe_cluster`, `list_nodegroups`, `list_addons`, `get_eks_insights`, `get_cloudwatch_logs`*, `get_cloudwatch_metrics`, `get_policies_for_role` |
| Kubernetes | `list_api_versions`, `list_k8s_resources`, `get_k8s_resource`, `get_k8s_events`, `get_pod_logs`*, `apply_yaml`†, `manage_k8s_resource`† |
| Fleet | `fleet_overview`, `fleet_list_k8s_resources` |

\* sensitive · † write

**Targets** can be an alias, any kube context name, a cluster ARN, or
`<account-id | account-name | profile | env>/<cluster>` (needed when two accounts share a
cluster name). **Selectors** for fleet tools and `list_targets` are comma-separated terms:
`env:prod`, `account:team-*`, `region:us-west-2`, `tag:color=blue`, `web-*`, `*`.

## Keeping your configuration private

Account IDs, profile and role names, cluster names and endpoints describe your
environment. **They never belong in this repository.** Every value under `examples/` and
`tests/` is a placeholder.

- Put your real settings in `~/.config/eks-multi-mcp/config.yaml` (outside the repo) or
  pass `--config`. `.gitignore` also blocks `config.yaml`, `*.local.yaml`, `kubeconfig*`.
- Enable the hooks once per clone: `git config core.hooksPath .githooks`
  - `pre-commit` runs `scripts/check_identifiers.py --staged` plus `gitleaks` on staged changes
  - `commit-msg` scans the commit message
  - `pre-push` scans exactly what is being pushed: every line added by every new commit
    (even if a later commit deletes it), commit messages, and author/committer identities
- Commit author and committer emails must match `git config identifiers.allowedEmail`
  (a regex; default GitHub noreply addresses). Your global git email and its domain are
  treated as private terms, so a work address can't leak through content or metadata.
- `check_identifiers.py` builds its denylist **at run time** from your own `~/.aws/config`
  and kubeconfig (account IDs, SSO role/session names, hyphenated profile names, cluster and
  context names, endpoint IDs). The list is never written to the repo. Add extra private
  terms, one per line, to `~/.config/eks-multi-mcp/denylist.txt`.
- Everywhere, including CI with no local config, any 12-digit number must be a placeholder
  such as `111111111111`.

## Install and run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Clusters with private API
endpoints must be reachable from where the server runs (VPN or similar).

```bash
git clone git@github.com:jmichaelthurman/eks-multi-mcp.git && cd eks-multi-mcp
uv sync
uv run eks-multi-mcp targets               # resolved registry as a table
uv run eks-multi-mcp doctor                # config audit + live access check (exit 1 on problems)
uv run eks-multi-mcp doctor --no-access-check
uv run eks-multi-mcp serve                 # stdio MCP server (default command)
```

| Option | Purpose |
|---|---|
| `--config PATH` | Settings / cluster map. Default: `$EKS_MULTI_MCP_CONFIG`, then `~/.config/eks-multi-mcp/config.yaml`, then `~/.eks-multi-mcp.yaml`. |
| `--allow-write` | Enable `apply_yaml` and `manage_k8s_resource`. |
| `--allow-sensitive-data-access` | Enable pod and CloudWatch logs and unredacted Secrets. |
| `--kubeconfig PATH` | Kubeconfig to read (repeatable). Default: `$KUBECONFIG`, else `~/.kube/config`. |
| `--no-kubeconfig` | Use only the cluster map and AWS discovery. |
| `--selector SEL` | `targets` / `doctor`: limit to matching targets, e.g. `env:prod`. |
| `--no-access-check` | `doctor`: static checks only, no AWS or Kubernetes calls. |
| `--transport` | `stdio` (default), `streamable-http` or `sse`. The HTTP transports are unauthenticated and refuse writes and sensitive access. |
| `--log-level` | Server log level (default `WARNING`; logs go to stderr). |

### Claude Code

```bash
claude mcp add eks-multi -- uv run --directory /path/to/eks-multi-mcp eks-multi-mcp
# with writes enabled (non-prod only; each write still asks you to approve it):
claude mcp add eks-multi-rw -- uv run --directory /path/to/eks-multi-mcp eks-multi-mcp --allow-write
```

### Other MCP clients (JSON)

```json
{
  "mcpServers": {
    "eks-multi": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/eks-multi-mcp", "eks-multi-mcp",
               "--config", "/path/outside/the/repo/config.yaml"]
    }
  }
}
```

SSO sessions are yours to manage. When one has expired, tools and `doctor` say which
profile needs it instead of failing with a generic credentials error.

## Required IAM

- Read profile: `eks:DescribeCluster`, `eks:List*` (including `ListClusters` for discovery),
  `eks:Describe*` (node groups, add-ons, insights), `sts:GetCallerIdentity`, plus a Kubernetes RBAC mapping (access entry or
  `aws-auth`) for the role. Optional: `logs:FilterLogEvents`, `cloudwatch:GetMetricStatistics`,
  `iam:GetRole*`/`iam:List*RolePolicies`.
- Write profile: a Kubernetes access entry or RBAC mapping that grants the verbs you need.

## Development

```bash
uv run --group dev pytest -q --cov=eks_multi_mcp     # offline; never touches AWS or your config
uv run --group dev ruff check src tests scripts
uv run python scripts/check_identifiers.py --all
```

The offline suite replaces boto3 and the Kubernetes client with in-memory doubles
(`tests/fakes.py`) and hides `$HOME`, `~/.aws` and `$KUBECONFIG`, so it runs anywhere.

`tests/test_live.py` is an opt-in end-to-end run against real clusters with your own
config and SSO sessions. Every write in it is a server-side dry run followed by a read
proving nothing persisted, and each one asks you to approve it on the terminal (run it from
a real terminal, with `-s`; without one the write checks skip). The protected target only
receives calls the gate must refuse before any request is sent.

```bash
EKS_MULTI_MCP_LIVE_TARGET=<non-prod target> \
EKS_MULTI_MCP_LIVE_PROTECTED_TARGET=<prod target> \
uv run --group dev pytest -m live -v -s
```
