"""Write approval over the 2026-07-28 protocol, driven one round at a time.

The server cannot send elicitation/create mid-call there, so a write tool first returns
an InputRequiredResult carrying the prompt and a sealed request_state, and the client
retries with the human's answer. These tests play a client that does not behave: one
that forges or replays the state, or answers without it, and check that nothing is written.
"""

import asyncio
import dataclasses

import mcp_types as types
import pytest
from fakes import Approver, FakeDynamic, FakeResource, install_dynamic, make_server

ARGS = {"target": "dev", "operation": "delete", "kind": "ConfigMap", "name": "cm", "namespace": "app",
        "dry_run": True}
APPROVED = {"approve": types.ElicitResult(action="accept", content={"approve": True})}


@pytest.fixture
def writable(files, monkeypatch):
    srv = make_server(files, safety={"allow_write": True})
    cm = FakeResource("ConfigMap")
    modes = install_dynamic(monkeypatch, srv, FakeDynamic(cm, FakeResource("Namespace", namespaced=False)))
    return srv, cm, modes


def rounds(srv, *steps):
    """Run tools/call once per step over one 2026-07-28 connection. A step is
    (arguments, input_responses, request_state); a callable request_state receives the
    previous round's result. Returns each round's result, or the exception it raised."""
    from mcp.client.client import Client

    async def run():
        out = []
        async with Client(srv.mcp, mode="auto", elicitation_callback=Approver()) as client:
            assert client.session.protocol_version == "2026-07-28"
            for args, responses, state in steps:
                if callable(state):
                    state = state(out[-1])
                try:
                    out.append(await client.session.call_tool(
                        "manage_k8s_resource", args, input_responses=responses, request_state=state,
                        allow_input_required=True))
                except Exception as e:  # noqa: BLE001 - a rejected round is a result here
                    out.append(e)
        return out

    return asyncio.run(run())


def echo(prev):
    return prev.request_state


def nothing_written(cm, modes):
    return "write" not in modes and not any(c[0] == "delete" for c in cm.calls)


def test_first_round_asks_and_writes_nothing(writable):
    srv, cm, modes = writable
    [first] = rounds(srv, (ARGS, None, None))
    assert isinstance(first, types.InputRequiredResult)
    prompt = first.input_requests["approve"]
    assert prompt.method == "elicitation/create"
    assert prompt.params.message.startswith("DRY RUN (server-side, not persisted): delete v1 ConfigMap app/cm")
    assert "approve" in prompt.params.requested_schema["properties"]
    assert first.request_state and "approval:v1:" not in first.request_state  # sealed, not readable
    assert nothing_written(cm, modes)


def test_approved_retry_writes(writable):
    srv, cm, _ = writable
    _, second = rounds(srv, (ARGS, None, None), (ARGS, APPROVED, echo))
    assert isinstance(second, types.CallToolResult) and not second.is_error
    assert [c[0] for c in cm.calls].count("delete") == 1
    assert cm.calls[-1][1]["dry_run"] == "All"


def test_forged_state_is_rejected(writable):
    srv, cm, modes = writable
    [only] = rounds(srv, (ARGS, APPROVED, "approval:v1:" + "0" * 64))
    assert "Invalid or expired requestState" in str(only)
    assert nothing_written(cm, modes)


def test_state_cannot_be_replayed_onto_another_write(writable):
    srv, cm, modes = writable
    other = {**ARGS, "name": "something-else"}
    _, second = rounds(srv, (ARGS, None, None), (other, APPROVED, echo))
    assert "Invalid or expired requestState" in str(second)
    assert nothing_written(cm, modes)


def test_state_cannot_turn_a_dry_run_into_a_real_write(writable):
    srv, cm, modes = writable
    _, second = rounds(srv, (ARGS, None, None), ({**ARGS, "dry_run": False}, APPROVED, echo))
    assert "Invalid or expired requestState" in str(second)
    assert nothing_written(cm, modes)


def test_answer_without_state_asks_again(writable):
    srv, cm, modes = writable
    [only] = rounds(srv, (ARGS, APPROVED, None))
    assert isinstance(only, types.InputRequiredResult)
    assert nothing_written(cm, modes)


def test_retry_without_answer_asks_again(writable):
    srv, cm, modes = writable
    _, second = rounds(srv, (ARGS, None, None), (ARGS, None, echo))
    assert isinstance(second, types.InputRequiredResult)
    assert nothing_written(cm, modes)


def test_target_changed_between_rounds_is_refused(writable, monkeypatch):
    """The arguments are the same, but the alias now resolves to a different write profile
    than the prompt showed (e.g. reload_config picked up an edited kubeconfig)."""
    srv, cm, modes = writable
    resolve = srv.registry.resolve

    def moved_after_first_round(prev):
        monkeypatch.setattr(srv.registry, "resolve",
                            lambda ref: dataclasses.replace(resolve(ref), write_profile="someone-else"))
        return prev.request_state

    _, second = rounds(srv, (ARGS, None, None), (ARGS, APPROVED, moved_after_first_round))
    assert second.is_error and "no longer matches the one that was approved" in second.content[0].text
    assert nothing_written(cm, modes)


@pytest.mark.parametrize("answer,expect", [
    (types.ElicitResult(action="decline"), "not approved (decline)"),
    (types.ElicitResult(action="cancel"), "not approved (cancel)"),
    (types.ElicitResult(action="accept", content={"approve": False}), "not approved;"),
    (types.ElicitResult(action="accept", content={}), "malformed"),
    (types.ElicitResult(action="accept", content={"approve": "maybe"}), "malformed"),
    (types.ListRootsResult(roots=[]), "malformed"),
])
def test_anything_but_explicit_approval_is_refused(writable, answer, expect):
    srv, cm, modes = writable
    _, second = rounds(srv, (ARGS, None, None), (ARGS, {"approve": answer}, echo))
    assert second.is_error and expect in second.content[0].text
    assert "nothing was sent to the cluster" in second.content[0].text
    assert nothing_written(cm, modes)
