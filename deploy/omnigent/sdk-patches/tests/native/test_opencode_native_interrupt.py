# SPDX-License-Identifier: Apache-2.0
# New regression tests authored for the native OpenCode interrupt patch.
# Source: tests/runner/test_opencode_native_interrupt.py in fix/od244-opencode-quota.
# See PROVENANCE.json; no SDK module replacement is performed by this file.

"""OpenCode interrupt must abort the resident server and confirm durable idle.

HTTP transports are local fakes; the runner dispatcher, bridge reader and
installed OpenCodeClient request/abort methods are exercised unchanged.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses.opencode_native import bridge, client
from omnigent.runner.native import interrupt as interrupt_mod


@pytest.fixture
async def native_stop(tmp_path, monkeypatch):
    bridge_id = "bridge-owned-not-runner-id"
    directory = tmp_path / bridge_id
    directory.mkdir()
    state = {
        "session_id": "conv_owned",
        "opencode_session_id": "ses_owned",
        "server_base_url": "http://127.0.0.1:45123",
        "auth_secret": "test-only-secret",
        "active_message_id": "msg_owned",
        "status": "busy",
        "workspace": "/test-workspace",
    }
    (directory / "state.json").write_text(json.dumps(state))
    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda value: tmp_path / value)
    observed = {
        "native": [],
        "posts": [],
        "published": [],
        "status": "running",
        "statuses": [{"ses_owned": {"type": "retry"}}, {}],
        "abort": True,
        "bridge": directory,
        "state": state,
        "native_error": None,
        "ap_status": 204,
        "before_status": None,
    }

    def native_http(request):
        observed["native"].append((request.method, request.url.path))
        if observed["native_error"]:
            raise httpx.ConnectError("sensitive error must not escape", request=request)
        assert (
            request.headers["authorization"]
            == bridge.auth_headers_for_secret("test-only-secret")["Authorization"]
        )
        assert request.headers["x-opencode-directory"] == "/test-workspace"
        if request.method == "POST":
            assert request.url.path == "/session/ses_owned/abort"
            return httpx.Response(200, json=observed["abort"])
        assert request.url.path == "/session/status"
        assert observed["status"] == "running", "published idle before native confirmation"
        if observed["before_status"]:
            observed["before_status"]()
        statuses = observed["statuses"]
        return httpx.Response(200, json=statuses.pop(0) if len(statuses) > 1 else statuses[0])

    def ap_http(request):
        if request.method == "GET":
            assert request.url.path == "/v1/sessions/conv_owned/labels"
            return httpx.Response(
                200, json={"labels": {bridge.OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY: bridge_id}}
            )
        assert request.url.path == "/v1/sessions/conv_owned/events"
        event = json.loads(request.content)
        observed["posts"].append(event)
        if event["type"] == "external_session_status":
            if observed["ap_status"] == 204:
                observed["status"] = event["data"]["status"]
        return httpx.Response(observed["ap_status"])

    real_client = client.OpenCodeClient
    clients = []

    def create_client(base_url, **kwargs):
        transport_client = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(native_http)
        )
        clients.append(transport_client)
        return real_client(base_url, client=transport_client, **kwargs)

    monkeypatch.setattr(client, "OpenCodeClient", create_client)
    ap = httpx.AsyncClient(base_url="http://ap.test", transport=httpx.MockTransport(ap_http))

    async def no_codex(*args, **kwargs):
        return None

    runner = interrupt_mod.NativeInterruptRunner(
        server_client=ap,
        resource_registry=SimpleNamespace(),
        publish_event=lambda *args: observed["published"].append(args),
        mark_subagent_terminal_and_wake=lambda *args, **kwargs: SimpleNamespace(delivered=True),
        session_sub_agent_names={},
        codex_bridge_state_for_session=no_codex,
        client_safe_error_detail=lambda *args, **kwargs: "safe-error",
        logger=logging.getLogger("test.opencode-interrupt"),
    )
    yield runner, observed
    await ap.aclose()
    for transport_client in clients:
        await transport_client.aclose()
    for timer in getattr(runner, "_pending_interrupt_timers", {}).values():
        timer.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["interrupt", "stop"])
async def test_opencode_quota_interrupt_aborts_and_confirms_idle(native_stop, action):
    runner, observed = native_stop
    response = await getattr(runner, action)("opencode-native", "conv_owned")
    assert ("POST", "/session/ses_owned/abort") in observed["native"], (
        "native retries still running: no abort reached the resident server"
    )
    assert response is not None and response.status_code == 204
    assert observed["native"] == [
        ("POST", "/session/ses_owned/abort"),
        ("GET", "/session/status"),
        ("GET", "/session/status"),
    ]
    assert observed["status"] == "idle", "reload must see persisted idle, not optimistic UI only"
    assert observed["posts"][-1]["data"]["turn_outcome"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["busy", "retry"])
async def test_abort_ack_does_not_mean_idle(native_stop, monkeypatch, kind):
    runner, observed = native_stop
    observed["statuses"] = [{"ses_owned": {"type": kind}}]
    monkeypatch.setattr(interrupt_mod, "_OPENCODE_INTERRUPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(interrupt_mod, "_OPENCODE_INTERRUPT_POLL_S", 0.001)
    response = await runner.interrupt("opencode-native", "conv_owned")
    assert response.status_code == 503
    assert observed["status"] == "running"
    assert observed["posts"] == observed["published"] == []
    assert observed["native"][0] == ("POST", "/session/ses_owned/abort")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses", [{}, {"ses_owned": {"type": "idle"}}, {"ses_other": {"type": "busy"}}]
)
async def test_idle_or_absent_owned_entry_is_confirmed(native_stop, statuses):
    runner, observed = native_stop
    observed["statuses"] = [statuses]
    observed["abort"] = False  # Already stopped is idempotent, but still checked.
    response = await runner.interrupt("opencode-native", "conv_owned")
    assert response.status_code == 204
    assert observed["status"] == "idle"
    assert observed["native"][-1] == ("GET", "/session/status")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses", [None, [], {"ses_owned": None}, {"ses_owned": {"type": "unknown"}}]
)
async def test_invalid_status_cannot_confirm_stop(native_stop, statuses):
    runner, observed = native_stop
    observed["statuses"] = [statuses]
    response = await runner.interrupt("opencode-native", "conv_owned")
    assert response.status_code == 503
    assert observed["status"] == "running"
    assert observed["posts"] == observed["published"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "owner", "remote", "native_id"])
async def test_invalid_bridge_cannot_abort_another_session(native_stop, change):
    runner, observed = native_stop
    path = observed["bridge"] / "state.json"
    if change == "missing":
        path.unlink()
    else:
        state = observed["state"]
        key, value = {
            "owner": ("session_id", "conv_other"),
            "remote": ("server_base_url", "https://provider.test"),
            "native_id": ("opencode_session_id", "ses_other/abort"),
        }[change]
        state[key] = value
        path.write_text(json.dumps(state))
    response = await runner.interrupt("opencode-native", "conv_owned")
    assert response.status_code == 503
    assert observed["native"] == observed["posts"] == observed["published"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["session_id", "active_message_id"])
async def test_bridge_reassignment_during_abort_cannot_settle_new_turn(native_stop, change):
    runner, observed = native_stop
    observed["statuses"] = [{}]

    def reassign():
        observed["state"][change] = "new-owner-or-turn"
        (observed["bridge"] / "state.json").write_text(json.dumps(observed["state"]))

    observed["before_status"] = reassign
    response = await runner.interrupt("opencode-native", "conv_owned")
    assert response.status_code == 503
    assert observed["posts"] == observed["published"] == []


@pytest.mark.asyncio
async def test_native_transport_failure_is_not_success_or_credential_leak(native_stop, caplog):
    runner, observed = native_stop
    observed["native_error"] = True
    response = await runner.interrupt("opencode-native", "conv_owned")
    assert response.status_code == 503
    assert observed["posts"] == observed["published"] == []
    assert "sensitive error" not in response.body.decode() + caplog.text
    assert "test-only-secret" not in response.body.decode() + caplog.text


@pytest.mark.asyncio
async def test_ap_persistence_failure_is_not_success(native_stop):
    runner, observed = native_stop
    observed["statuses"] = [{}]
    observed["ap_status"] = 503
    response = await runner.interrupt("opencode-native", "conv_owned")
    assert response.status_code == 503
    assert observed["status"] == "running"
    assert observed["published"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "claude-sdk", None])
async def test_unhandled_harness_fallback_unchanged(native_stop, harness):
    runner, observed = native_stop
    assert await runner.interrupt(harness, "conv_owned") is None
    assert await runner.stop(harness, "conv_owned") is None
    assert observed["native"] == observed["posts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["interrupt", "stop"])
async def test_codex_no_bridge_noop_unchanged(native_stop, action):
    runner, observed = native_stop
    response = await getattr(runner, action)("codex-native", "conv_owned")
    assert response.status_code == 204
    assert observed["native"] == observed["posts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["interrupt", "stop"])
async def test_pi_still_uses_its_native_queue(native_stop, monkeypatch, action):
    from omnigent.harnesses.pi_native import bridge as pi_bridge

    runner, observed = native_stop
    calls = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: conv)
    monkeypatch.setattr(pi_bridge, "enqueue_interrupt", lambda directory: calls.append(directory))
    response = await getattr(runner, action)("pi-native", "conv_owned")
    assert response.status_code == 204
    assert calls == ["conv_owned"]
    assert observed["native"] == observed["posts"] == []


@pytest.mark.asyncio
async def test_claude_still_uses_its_native_bridge(native_stop, monkeypatch):
    from omnigent.harnesses.claude_native import bridge as claude_bridge

    runner, observed = native_stop
    calls = []

    async def bridge_id(**kwargs):
        return "claude-owned"

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda value: value)
    monkeypatch.setattr(
        claude_bridge,
        "inject_interrupt",
        lambda directory, *, timeout_s: calls.append((directory, timeout_s)),
    )
    response = await runner.interrupt("claude-native", "conv_owned")
    assert response.status_code == 204
    assert calls == [("claude-owned", 1.0)]
    assert observed["native"] == observed["posts"] == []
