"""Credentials and clients: boto3 sessions per profile, EKS bearer tokens, Kubernetes clients.

Tokens are minted in-process from the resolved profile (the same presigned
sts:GetCallerIdentity that `aws eks get-token` produces), so the kubeconfig exec
block is never executed — a misconfigured exec block cannot send a request to the
wrong account.
"""

from __future__ import annotations

import base64
import copy
import os
import stat
import tempfile
import threading
import time
from dataclasses import dataclass

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    SSOError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from botocore.signers import RequestSigner
from kubernetes import client as k8s_client
from kubernetes.dynamic import DynamicClient

from .registry import Target

TOKEN_TTL = 14 * 60  # EKS accepts tokens for 15 minutes
TOKEN_REFRESH_MARGIN = 120
BOTO_CFG = BotoConfig(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=10, read_timeout=30)


class CredentialError(RuntimeError):
    pass


def explain_aws_error(exc: Exception, profile: str | None) -> str:
    p = f"profile '{profile}'" if profile else "the default credential chain"
    if isinstance(exc, (UnauthorizedSSOTokenError, TokenRetrievalError, SSOError)):
        return f"the SSO session behind {p} is missing or expired"
    if isinstance(exc, ProfileNotFound):
        return f"{p} is not defined in the AWS config"
    if isinstance(exc, NoCredentialsError):
        return f"no credentials could be loaded for {p}"
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = err.get("Code", "")
        if code in {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId"}:
            return f"credentials for {p} are expired or invalid ({code})"
        return f"{code}: {err.get('Message', str(exc))} (via {p})"
    return f"{type(exc).__name__}: {exc} (via {p})"


@dataclass
class _Assumed:
    session: boto3.Session
    expires: float


class AuthManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, boto3.Session] = {}
        self._assumed: dict[tuple[str | None, str], _Assumed] = {}
        self._clusters: dict[str, dict] = {}
        self._api: dict[tuple[str, str], tuple[k8s_client.ApiClient, DynamicClient | None]] = {}
        self._ca_dir = tempfile.mkdtemp(prefix="eks-multi-mcp-ca-")
        os.chmod(self._ca_dir, stat.S_IRWXU)

    # ------------------------------------------------------------- AWS
    def base_session(self, profile: str | None) -> boto3.Session:
        key = profile or "<default>"
        with self._lock:
            if key not in self._sessions:
                try:
                    self._sessions[key] = boto3.Session(profile_name=profile) if profile else boto3.Session()
                except ProfileNotFound as e:
                    raise CredentialError(explain_aws_error(e, profile)) from e
            return self._sessions[key]

    def session(self, target: Target, mode: str = "read") -> boto3.Session:
        profile = target.write_profile if mode == "write" else target.read_profile
        base = self.base_session(profile)
        if not target.role_arn:
            return base
        key = (profile, target.role_arn)
        with self._lock:
            hit = self._assumed.get(key)
            if hit and hit.expires - time.time() > 300:
                return hit.session
            try:
                creds = base.client("sts", region_name=target.region, config=BOTO_CFG).assume_role(
                    RoleArn=target.role_arn, RoleSessionName="eks-multi-mcp"
                )["Credentials"]
            except (BotoCoreError, ClientError) as e:
                raise CredentialError(f"assume_role {target.role_arn} failed: {explain_aws_error(e, profile)}") from e
            sess = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
            self._assumed[key] = _Assumed(sess, creds["Expiration"].timestamp())
            return sess

    def aws(self, target: Target, service: str, mode: str = "read"):
        return self.session(target, mode).client(service, region_name=target.region, config=BOTO_CFG)

    def caller_identity(self, target: Target, mode: str = "read") -> dict:
        profile = target.write_profile if mode == "write" else target.read_profile
        try:
            ident = self.aws(target, "sts", mode).get_caller_identity()
        except (BotoCoreError, ClientError) as e:
            raise CredentialError(explain_aws_error(e, profile)) from e
        return {"account": ident["Account"], "arn": ident["Arn"]}

    def describe_cluster(self, target: Target, refresh: bool = False) -> dict:
        """Cached eks:DescribeCluster. Returns a copy so callers can't corrupt the cache."""
        with self._lock:
            if not refresh and target.key in self._clusters:
                return copy.deepcopy(self._clusters[target.key])
        try:
            c = self.aws(target, "eks").describe_cluster(name=target.cluster_name)["cluster"]
        except (BotoCoreError, ClientError) as e:
            raise CredentialError(explain_aws_error(e, target.read_profile)) from e
        with self._lock:
            self._clusters[target.key] = c
        return copy.deepcopy(c)

    # ------------------------------------------------------------- EKS token
    def eks_token(self, target: Target, mode: str = "read") -> str:
        sess = self.session(target, mode)
        creds = sess.get_credentials()
        if creds is None:
            profile = target.write_profile if mode == "write" else target.read_profile
            raise CredentialError(f"no credentials could be loaded for profile '{profile}'")
        try:
            sts = sess.client("sts", region_name=target.region, config=BOTO_CFG)
            signer = RequestSigner(
                sts.meta.service_model.service_id, target.region, "sts", "v4", creds, sess.events
            )
            url = signer.generate_presigned_url(
                {
                    "method": "GET",
                    "url": f"https://sts.{target.region}.amazonaws.com/"
                    "?Action=GetCallerIdentity&Version=2011-06-15",
                    "body": {},
                    "headers": {"x-k8s-aws-id": target.cluster_name},
                    "context": {},
                },
                region_name=target.region,
                expires_in=60,
                operation_name="",
            )
        except (BotoCoreError, ClientError) as e:
            raise CredentialError(explain_aws_error(e, target.read_profile)) from e
        return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

    # ------------------------------------------------------------- Kubernetes
    def _endpoint_and_ca(self, target: Target) -> tuple[str, str]:
        """Prefer the live endpoint from eks:DescribeCluster (catches stale kube contexts for
        deleted clusters fast); fall back to kubeconfig values if DescribeCluster is denied."""
        try:
            c = self.describe_cluster(target)
            return c["endpoint"], c["certificateAuthority"]["data"]
        except CredentialError as e:
            if "ResourceNotFoundException" in str(e):
                raise CredentialError(
                    f"cluster '{target.cluster_name}' no longer exists in account {target.account_id} "
                    f"({target.region}); its kube context is stale"
                ) from e
            if target.endpoint and target.ca_data:
                return target.endpoint, target.ca_data
            raise

    def api_client(self, target: Target, mode: str = "read") -> k8s_client.ApiClient:
        return self._client_pair(target, mode)[0]

    def dynamic(self, target: Target, mode: str = "read") -> DynamicClient:
        api, dyn = self._client_pair(target, mode)
        if dyn is None:
            dyn = DynamicClient(api)
            with self._lock:
                self._api[(target.key, mode)] = (api, dyn)
        return dyn

    def _client_pair(self, target: Target, mode: str):
        key = (target.key, mode)
        with self._lock:
            if key in self._api:
                return self._api[key]

        endpoint, ca = self._endpoint_and_ca(target)
        ca_path = os.path.join(self._ca_dir, f"{abs(hash(target.key))}.crt")
        with open(ca_path, "wb") as fh:
            fh.write(base64.b64decode(ca))

        cfg = k8s_client.Configuration()
        cfg.host = endpoint
        cfg.ssl_ca_cert = ca_path
        cfg.verify_ssl = True
        cfg.retries = 1
        state = {"exp": 0.0}

        def refresh(conf: k8s_client.Configuration) -> None:
            if time.time() < state["exp"] - TOKEN_REFRESH_MARGIN:
                return
            # kubernetes-client >=36 reads api_key['BearerToken'] (falling back to the
            # legacy 'authorization' key) but only applies a prefix stored under
            # 'BearerToken', so embed the scheme in the value and set both keys.
            bearer = "Bearer " + self.eks_token(target, mode)
            conf.api_key["BearerToken"] = bearer
            conf.api_key["authorization"] = bearer
            state["exp"] = time.time() + TOKEN_TTL

        refresh(cfg)
        cfg.refresh_api_key_hook = refresh
        api = k8s_client.ApiClient(cfg)
        with self._lock:
            self._api[key] = (api, None)
        return api, None

    def invalidate(self, target: Target | None = None) -> None:
        with self._lock:
            if target is None:
                self._api.clear()
                self._clusters.clear()
                self._sessions.clear()
                self._assumed.clear()
            else:
                for k in [k for k in self._api if k[0] == target.key]:
                    del self._api[k]
                self._clusters.pop(target.key, None)
