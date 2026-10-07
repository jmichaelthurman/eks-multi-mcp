"""In-memory stand-ins for boto3 clients and the Kubernetes dynamic client.

They record every call so tests can assert what would have reached AWS or the API
server (which profile mode, which namespace, whether dry_run was sent).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from types import SimpleNamespace

from kubernetes.dynamic.exceptions import ResourceNotFoundError

from eks_multi_mcp.config import settings_from_dict
from eks_multi_mcp.server import EksMultiServer


def make_server(files, **extra) -> EksMultiServer:
    return EksMultiServer(settings_from_dict({**files, **extra}))


class Approver:
    """Plays the human at the MCP client's approval prompt and records what it was shown.

    reply: "approve" (types the cluster name shown in the prompt when one is asked for),
    "decline", "cancel", or a dict sent verbatim as the form content.
    """

    def __init__(self, reply="approve"):
        self.reply = reply
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    async def __call__(self, context, params):
        import mcp_types as types

        self.prompts.append(params.message)
        self.schemas.append(params.requested_schema)
        if self.reply in ("decline", "cancel"):
            return types.ElicitResult(action=self.reply)
        if isinstance(self.reply, dict):
            return types.ElicitResult(action="accept", content=self.reply)
        if "cluster_name" in params.requested_schema["properties"]:
            shown = re.search(r"^Cluster: (\S+)", params.message, re.MULTILINE).group(1)
            return types.ElicitResult(action="accept", content={"cluster_name": shown})
        return types.ElicitResult(action="accept", content={"approve": True})


# Protocol the test client speaks. "legacy" runs the initialize handshake, where the server
# can send elicitation/create mid-call. "auto" negotiates 2026-07-28 (what Claude Code
# speaks), where the server cannot, and approval rides InputRequiredResult round trips.
CLIENT_MODE = "legacy"


def call(srv, tool_name, approver=None, **args):
    """Call a tool through a real in-process MCP client. Without an approver the client
    declares no elicitation support, like a client that cannot prompt its user.
    A tool error is raised as ToolError with the server's message."""
    from mcp.client.client import Client
    from mcp.server.mcpserver.exceptions import ToolError

    async def run():
        kw = {"elicitation_callback": approver} if approver else {}
        async with Client(srv.mcp, mode=CLIENT_MODE, **kw) as client:
            return await client.call_tool(tool_name, args)

    res = asyncio.run(run())
    if res.is_error:
        raise ToolError(res.content[0].text.removeprefix(f"Error executing tool {tool_name}: "))
    return res


def result(srv, tool_name, approver=None, **args):
    """Decoded JSON result of a tool call."""
    return json.loads(call(srv, tool_name, approver, **args).content[0].text)


class Obj:
    """Minimal ResourceInstance: to_dict() plus attribute access to metadata."""

    def __init__(self, d: dict):
        self._d = d
        md = d.get("metadata") or {}
        self.metadata = SimpleNamespace(resourceVersion=md.get("resourceVersion"), name=md.get("name"))

    def to_dict(self) -> dict:
        return copy.deepcopy(self._d)


class FakeResource:
    def __init__(self, kind: str, namespaced: bool = True, api_version: str = "v1", items=None,
                 base_kind: str | None = None):
        self.kind = kind
        self.namespaced = namespaced
        self.api_version = api_version
        self.base_kind = base_kind
        self.items: list[dict] = items or []
        self.calls: list[tuple[str, dict]] = []
        self.continue_token: str | None = None
        self.delete_response: dict | None = None

    def get(self, name=None, namespace=None, **kw):
        self.calls.append(("get", {"name": name, "namespace": namespace, **kw}))
        if name is None:
            items = [i for i in self.items if namespace is None or i["metadata"].get("namespace") == namespace]
            return Obj({"items": items, "metadata": {"continue": self.continue_token}})
        for i in self.items:
            if i["metadata"]["name"] == name and (namespace is None or i["metadata"].get("namespace") == namespace):
                return Obj(i)
        raise AssertionError(f"fake has no {self.kind} {namespace}/{name}")

    def create(self, body, namespace=None, **kw):
        self.calls.append(("create", {"body": body, "namespace": namespace, **kw}))
        return Obj({**body, "metadata": {**body.get("metadata", {}), "creationTimestamp": "2026-01-01T00:00:00Z"}})

    def replace(self, body, name, namespace=None, **kw):
        self.calls.append(("replace", {"body": body, "name": name, "namespace": namespace, **kw}))
        return Obj(body)

    def patch(self, body, name, namespace=None, **kw):
        self.calls.append(("patch", {"body": body, "name": name, "namespace": namespace, **kw}))
        return Obj({"metadata": {"name": name, "namespace": namespace}, **body})

    def delete(self, name, namespace=None, **kw):
        self.calls.append(("delete", {"name": name, "namespace": namespace, **kw}))
        return Obj(self.delete_response or {"metadata": {"name": name, "namespace": namespace}})


class FakeDynamic:
    def __init__(self, *resources: FakeResource):
        self._by_key = {(r.api_version, r.kind): r for r in resources}
        self.applied: list[dict] = []
        outer = self

        class _Resources:
            def get(self, api_version, kind):
                try:
                    return outer._by_key[(api_version, kind)]
                except KeyError:
                    raise ResourceNotFoundError(f"no {api_version}/{kind}") from None

        self.resources = _Resources()

    def server_side_apply(self, res, body, name, namespace=None, **kw):
        self.applied.append({"kind": res.kind, "name": name, "namespace": namespace, **kw})
        return Obj({"metadata": {"name": name, "resourceVersion": "42"}})


def install_dynamic(monkeypatch, srv, dyn: FakeDynamic) -> list[str]:
    """Route srv.auth.dynamic to dyn; returns the list of modes ('read'/'write') requested."""
    modes: list[str] = []

    def dynamic(target, mode="read"):
        modes.append(mode)
        return dyn

    monkeypatch.setattr(srv.auth, "dynamic", dynamic)
    return modes


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages
        self.kwargs: dict | None = None

    def paginate(self, **kw):
        self.kwargs = kw
        return iter(self._pages)


class FakeAws:
    """A boto3 client double: attributes are canned methods, paginators are page lists."""

    def __init__(self, paginators: dict | None = None, **methods):
        self.paginators = {k: FakePaginator(v) for k, v in (paginators or {}).items()}
        self.calls: list[tuple[str, dict]] = []
        for name, fn in methods.items():
            setattr(self, name, self._record(name, fn))

    def _record(self, name, fn):
        def wrapper(**kw):
            self.calls.append((name, kw))
            return fn(**kw) if callable(fn) else fn
        return wrapper

    def get_paginator(self, name):
        return self.paginators[name]


def install_aws(monkeypatch, srv, **clients) -> list[tuple[str, str]]:
    """Route srv.auth.aws(target, service) to clients[service]; returns (service, mode) calls."""
    seen: list[tuple[str, str]] = []

    def aws(target, service, mode="read"):
        seen.append((service, mode))
        return clients[service]

    monkeypatch.setattr(srv.auth, "aws", aws)
    return seen


def tool_error(srv, tool_name, approver=None, **args) -> str:
    """Message of the error a call reports. Fails if the call succeeds, or if it crashed with
    an unmapped exception (the client then sees only a bare 'Error executing tool')."""
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        call(srv, tool_name, approver, **args)
    except ToolError as e:
        msg = str(e)
        assert msg != f"Error executing tool {tool_name}", "unmapped exception (no detail reached the client)"
        return msg
    raise AssertionError(f"{tool_name} succeeded; expected an error")
