"""Extract EKS cluster references from kubeconfig files (no API calls, no exec)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

EKS_ARN = re.compile(r"^arn:aws[\w-]*:eks:([a-z0-9-]+):(\d{12}):cluster/(.+)$")
# https://<id>.<suffix>.<region>.eks.amazonaws.com
EKS_ENDPOINT = re.compile(r"^https://[A-Za-z0-9]+\.[a-z0-9]+\.([a-z0-9-]+)\.eks\.amazonaws\.com")


@dataclass
class KubeContext:
    name: str
    source: str
    cluster_entry: str
    server: str | None
    ca_data: str | None
    # From the cluster entry name (when it is an ARN)
    arn_region: str | None = None
    arn_account: str | None = None
    arn_cluster: str | None = None
    # From the user's exec block
    exec_command: str | None = None
    exec_cluster: str | None = None
    exec_region: str | None = None
    exec_profile: str | None = None
    exec_role_arn: str | None = None
    namespace: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_eks(self) -> bool:
        return bool(self.arn_cluster or self.exec_cluster or (self.server and EKS_ENDPOINT.match(self.server)))

    @property
    def cluster_name(self) -> str | None:
        return self.arn_cluster or self.exec_cluster

    @property
    def region(self) -> str | None:
        if self.arn_region:
            return self.arn_region
        if self.exec_region:
            return self.exec_region
        if self.server and (m := EKS_ENDPOINT.match(self.server)):
            return m.group(1)
        return None


def _arg(args: list[str], *names: str) -> str | None:
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def load_kube_contexts(paths: list[str]) -> list[KubeContext]:
    """Load contexts from each kubeconfig. Like kubectl, the first file to define a name wins."""
    contexts: dict[str, KubeContext] = {}
    for path in paths:
        p = Path(path)
        if not p.is_file():
            continue
        doc = yaml.safe_load(p.read_text()) or {}
        clusters = {c["name"]: c.get("cluster", {}) or {} for c in doc.get("clusters", []) or []}
        users = {u["name"]: u.get("user", {}) or {} for u in doc.get("users", []) or []}
        for c in doc.get("contexts", []) or []:
            name = c["name"]
            if name in contexts:
                continue
            ctx = c.get("context", {}) or {}
            centry = ctx.get("cluster", "")
            cl = clusters.get(centry, {})
            kc = KubeContext(
                name=name,
                source=str(p),
                cluster_entry=centry,
                server=cl.get("server"),
                ca_data=cl.get("certificate-authority-data"),
                namespace=ctx.get("namespace"),
            )
            if m := EKS_ARN.match(centry):
                kc.arn_region, kc.arn_account, kc.arn_cluster = m.groups()

            ex = (users.get(ctx.get("user", ""), {}) or {}).get("exec") or {}
            if ex:
                args = [str(a) for a in ex.get("args", []) or []]
                env = {e.get("name"): e.get("value") for e in ex.get("env", []) or [] if isinstance(e, dict)}
                kc.exec_command = ex.get("command")
                kc.exec_cluster = _arg(args, "--cluster-name", "--cluster-id", "-i")
                kc.exec_region = _arg(args, "--region") or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
                kc.exec_profile = _arg(args, "--profile") or env.get("AWS_PROFILE")
                kc.exec_role_arn = _arg(args, "--role-arn", "--role", "-r")
                # aws-iam-authenticator accepts an ARN for --cluster-id
                if kc.exec_cluster and (m := EKS_ARN.match(kc.exec_cluster)):
                    kc.exec_cluster = m.group(3)

            if kc.arn_cluster and kc.exec_cluster and kc.arn_cluster != kc.exec_cluster:
                kc.warnings.append(
                    f"exec block requests a token for '{kc.exec_cluster}' but the context points at "
                    f"'{kc.arn_cluster}'; the exec block is ignored, the token is minted for the real cluster"
                )
            if kc.arn_region and kc.exec_region and kc.arn_region != kc.exec_region:
                kc.warnings.append(f"exec --region {kc.exec_region} differs from the cluster ARN's {kc.arn_region}")
            contexts[name] = kc
    return list(contexts.values())
