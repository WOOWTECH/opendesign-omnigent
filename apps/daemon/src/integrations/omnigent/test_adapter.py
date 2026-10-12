"""Offline boundary tests. Never import SDK runtime or inspect live credentials."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

import adapter

ROOT = Path(__file__).resolve().parent
SERVER = "https://omnigent.example"
HOST = {"id": "host_local", "name": "Local", "manager": "kit", "managerRunning": True,
        "managed": True, "state": "running", "connected": False}


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Auth:
    def __init__(self):
        self.saved = []
        self.cleared = []
        self.refresh = []
    def token(self, server, *, refresh=True):
        self.refresh.append(refresh)
        return "FAKE_IN_MEMORY_TOKEN"
    def save(self, server, payload):
        self.saved.append(server)
    def clear(self, server):
        self.cleared.append(server)


class Service:
    def __init__(self):
        self.calls = []
        self.mode = "accounts"
        self.sentinel = True
        self.item_error = None
        self.message_error = None
        self.stop_error = None
        self.runner_online = False
        self.stop_snapshots = 0
        self.offline = False
        self.auth = None
        self.agents = [{"id": f"ag_{key}", "name": spec["agent"], "harness": spec["harness"],
                        "api_key": "EXTRA_SECRET"} for key, spec in adapter.ENGINES.items()]
        self.models = {key: [{"id": "anthropic/model-a", "model": "model-a", "providerID": "anthropic",
                              "displayName": "Model A", "isDefault": True, "token": "EXTRA_SECRET"}]
                       for key in adapter.ENGINES}
        self.hosts_override = None
    def auth_probe(self):
        return self.mode
    def hold_token(self, value):
        self.held_token = value
    def close(self):
        pass
    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload, kwargs))
        if path == "/auth/login":
            return {"token": "FAKE_LOGIN_TOKEN", "user": {"id": "user_fake"}, "expires_in": 3600}
        if path == "/v1/me":
            return {"id": "user_fake", "token": "EXTRA_SECRET"}
        if path == "/v1/hosts":
            if self.hosts_override is not None:
                return self.hosts_override
            rows = [{"host_id": "host_local", "name": "Local", "status": "offline" if self.offline else "online",
                     "configured_harnesses": {spec["harness"]: True for spec in adapter.ENGINES.values()},
                     "auth_token": "EXTRA_SECRET"},
                    {"host_id": "host_other", "status": "online", "auth_token": "EXTRA_SECRET"}]
            if self.auth and self.auth.cleared:
                rows[0]["status"] = "offline"
            return {"hosts": rows}
        if path == "/v1/harnesses":
            return {"data": {"opencode-native": {"key": "opencode-native", "label": "OpenCode", "secret": "EXTRA_SECRET"}}}
        if path.startswith("/v1/agents?"):
            return {"data": self.agents, "has_more": False}
        if "/harnesses/" in path:
            harness = path.split("/harnesses/")[1].split("/")[0]
            key = next(k for k, spec in adapter.ENGINES.items() if spec["harness"] == harness)
            return {"models": self.models[key], "error": "EXTRA_SECRET"}
        if method == "POST" and path == "/v1/sessions":
            return {"id": "conv_owned", "token": "EXTRA_SECRET"}
        if path.endswith("/events"):
            if payload["type"] == "message" and self.message_error:
                raise self.message_error
            if payload["type"] == "stop_session" and self.stop_error:
                raise self.stop_error
            return {"ok": True}
        if "/items?" in path:
            if self.item_error:
                return {"data": [{"type": "error", "code": self.item_error, "message": "EXTRA_SECRET"}]}
            return {"data": [{"role": "assistant", "type": "message", "content": [
                {"type": "output_text", "text": adapter.SENTINEL}]}] if self.sentinel else []}
        if path == "/v1/sessions/conv_owned":
            self.stop_snapshots += 1
            return {"id": "conv_owned", "runner_online": self.runner_online, "status": "idle"}
        raise AssertionError((method, path))


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT, prefix=".test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.auth = Auth()
        self.service = Service()
        self.service.auth = self.auth
        self.clock = Clock()
        self.inventory = {key: {"installed": key in ("claude", "opencode"), "version": "1.2.3"}
                          for key in adapter.ENGINES}
    def request(self, **extra):
        return {"server": SERVER, "configHome": str(self.base / "config"), "dataDir": str(self.base / "data"),
                "stateDir": str(self.base / "state"), "workspace": str(self.base),
                "engineInventory": self.inventory, **extra}
    def make(self, **extra):
        instance = adapter.Adapter(self.request(**extra), auth=self.auth,
                                   client_factory=lambda *_: self.service, clock=self.clock, sleeper=self.clock.sleep,
                                   probe_submit=lambda _: False)
        instance.local_host = lambda: dict(HOST)
        instance.config = lambda: {"server": SERVER, "host": {"host_id": "host_local"}}
        self.addCleanup(instance.close)
        return instance
    def assert_code(self, code, function):
        with self.assertRaises(adapter.AdapterError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)

    def test_probe_journal_records_only_safe_owned_metadata_and_removes_stopped(self):
        instance = self.make()
        record = {'server': SERVER, 'hostId': 'host_local', 'title': 'OpenDesign connection check · claude', 'engine': 'claude'}
        instance.record_probe('probe_a', record)
        self.assertEqual({'probe_a': record}, instance.probe_journal())
        self.assertEqual(0o600, (instance.state_dir / 'owned-probes.json').stat().st_mode & 0o777)
        instance.record_probe('probe_a', None)
        self.assertEqual({}, instance.probe_journal())

    def test_orphan_probe_cleanup_requires_exact_server_host_and_title(self):
        instance = self.make()
        record = {'server': SERVER, 'hostId': 'host_local', 'title': 'OpenDesign connection check · claude', 'engine': 'claude'}
        instance.record_probe('probe_a', record)
        self.service.request = Mock(return_value={'id': 'probe_a', 'host_id': 'other_host', 'title': record['title'], 'runner_online': False})
        self.assert_code('INTERNAL_ERROR', lambda: instance.recover_probes(self.service, SERVER, 'host_local'))
        self.assertIn('probe_a', instance.probe_journal())
        self.assertEqual(1, self.service.request.call_count)

    def test_orphan_probe_ignores_other_server_and_recovers_owned_stopped_session(self):
        instance = self.make()
        record = {'server': SERVER, 'hostId': 'host_local', 'title': 'OpenDesign connection check · claude', 'engine': 'claude'}
        instance.record_probe('probe_a', record)
        instance.recover_probes(self.service, 'https://another.example', 'host_local')
        self.assertEqual([], self.service.calls)
        self.service.request = Mock(return_value={'id': 'probe_a', 'host_id': 'host_local', 'title': record['title'], 'runner_online': False})
        instance.recover_probes(self.service, SERVER, 'host_local')
        self.assertEqual({}, instance.probe_journal())
        self.assertEqual(1, self.service.request.call_count)

    def test_server_normalization_and_rejected_origins(self):
        self.assertEqual(SERVER, adapter.normalize_server(" HTTPS://Omnigent.Example/// "))
        self.assertEqual("http://[::1]:8080", adapter.normalize_server("http://[::1]:8080/"))
        for bad in ["http://remote.example", "https://u:p@example.com", "https://example.com/?token=fake",
                    "https://example.com/#token", "https://example.com/\n", "file:///etc/passwd", "https://example.com:bad"]:
            with self.subTest(bad=bad):
                self.assert_code("INVALID_SERVER", lambda: adapter.normalize_server(bad))

    def test_catalog_uses_official_opencode_contract_and_omits_secrets(self):
        result = self.make().catalog()
        row = result["engines"]["opencode"]
        self.assertTrue(row["ready"])
        self.assertEqual("opencode-native", row["harness"])
        self.assertEqual("ag_opencode", row["agentId"])
        self.assertEqual("anthropic/model-a", row["models"][0]["id"])
        self.assertNotIn("EXTRA_SECRET", json.dumps(result))
        self.assertNotIn("providerID", row["models"][0])
        queried = [call[1] for call in self.service.calls if "/harnesses/" in call[1]]
        self.assertEqual(2, len(queried))
        self.assertFalse(result["engines"]["codex"]["installed"])
        self.assertFalse(result["engines"]["codex"]["ready"])

    def test_missing_builtin_agent_does_not_guess_custom_agent(self):
        self.service.agents = [{"id": "ag_custom", "name": "custom", "harness": "opencode-native"}]
        self.assert_code("MISSING_AGENT", self.make(engine="opencode").verify)
        self.assertFalse(any(c[1] == "/v1/sessions" for c in self.service.calls))

    def test_missing_engines_never_create_sessions(self):
        for key in ("codex", "pi"):
            with self.subTest(engine=key):
                self.assert_code("ENGINE_NOT_INSTALLED", self.make(engine=key).verify)
        self.assertEqual([], self.service.calls)

    def test_installed_optional_engines_can_verify(self):
        for key in ("codex", "pi"):
            with self.subTest(engine=key):
                self.inventory[key]["installed"] = True
                result = self.make(engine=key, model="anthropic/model-a").verify()
                self.assertTrue(result["ok"])
                self.assertEqual("stopped", result["cleanup"])

    def test_opencode_verifies_real_assistant_sentinel_and_owned_runner_stop(self):
        result = self.make(engine="opencode", model="anthropic/model-a").verify()
        self.assertTrue(result["ok"])
        self.assertTrue(result["sentinelVerified"])
        self.assertEqual("stopped", result["cleanup"])
        create = next(c[2] for c in self.service.calls if c[1] == "/v1/sessions")
        self.assertEqual("opencode-native", create["harness_override"])
        self.assertEqual("anthropic/model-a", create["model_override"])
        self.assertNotIn("terminal_launch_args", create)
        events = [c for c in self.service.calls if c[1].endswith("/events")]
        self.assertEqual(["message", "stop_session"], [c[2]["type"] for c in events])
        self.assertEqual(adapter.PROBE_PROMPT, events[0][2]["data"]["content"][0]["text"])
        self.assertIs(False, events[-1][3]["refresh_auth"])
        self.assertEqual(1, self.service.stop_snapshots)

    def test_claude_probe_permission_args_and_settle(self):
        result = self.make(engine="claude").verify()
        self.assertTrue(result["ok"])
        create = next(c[2] for c in self.service.calls if c[1] == "/v1/sessions")
        self.assertEqual(["--permission-mode", "default"], create["terminal_launch_args"])
        self.assertEqual(3.0, self.clock.now)

    def test_missing_or_partial_model_id_rejected_before_session(self):
        for value in ("model-a", "anthropic/unknown", ["anthropic/model-a"]):
            self.assert_code("INVALID_MODEL", self.make(engine="opencode", model=value).verify)
        self.assertFalse(any(c[1] == "/v1/sessions" for c in self.service.calls))

    def test_long_exact_model_identifier_is_preserved(self):
        model = "provider/" + "a" * 300
        self.service.models["opencode"] = [{"id": model}]
        self.assertTrue(self.make(engine="opencode", model=model).verify()["ok"])
        create = next(c[2] for c in self.service.calls if c[1] == "/v1/sessions")
        self.assertEqual(model, create["model_override"])

    def test_model_body_error_is_sanitized(self):
        self.service.models["opencode"] = None
        result = self.make().catalog()["engines"]["opencode"]
        self.assertFalse(result["ready"])
        self.assertEqual("INVALID_RESPONSE", result["modelError"])

    def test_user_and_tool_sentinel_or_extra_output_is_not_a_pass(self):
        for role, text in [("user", adapter.SENTINEL), ("tool", adapter.SENTINEL),
                           ("assistant", adapter.SENTINEL + " extra")]:
            items = [{"role": role, "type": "message", "content": [{"type": "text", "text": text}]}]
            self.assertFalse(adapter.Adapter.assistant_sentinel(items))

    def test_probe_timeout_is_bounded_and_stops_session(self):
        self.service.sentinel = False
        result = self.make(engine="opencode", timeoutSeconds=5).verify()
        self.assertFalse(result["ok"])
        self.assertEqual("stopped", result["cleanup"])
        self.assertLessEqual(self.clock.now, 5)

    def test_claude_start_timeout_still_stops_session(self):
        result = self.make(engine="claude", timeoutSeconds=1).verify()
        self.assertFalse(result["ok"])
        self.assertEqual("NATIVE_START_TIMEOUT", result["reason"])
        self.assertEqual("stopped", result["cleanup"])

    def test_send_error_still_stops_own_session(self):
        self.service.message_error = adapter.AdapterError("needs_login", "safe")
        result = self.make(engine="opencode").verify()
        self.assertEqual("NEEDS_LOGIN", result["reason"])
        self.assertEqual("stopped", result["cleanup"])

    def test_sigterm_cancellation_still_checks_owned_runner_cleanup(self):
        instance = self.make(engine="opencode")
        request = self.service.request
        def interrupt(method, path, payload=None, **kwargs):
            if "/items?" in path:
                os.kill(os.getpid(), signal.SIGTERM)
            return request(method, path, payload, **kwargs)
        self.service.request = interrupt
        output = io.StringIO()
        with patch.object(adapter, "dispatch", side_effect=lambda *_: instance.verify()), \
                patch.object(sys, "argv", ["adapter.py"]), \
                patch.object(sys, "stdin", io.StringIO('{"action":"verify"}')), contextlib.redirect_stdout(output):
            adapter.main()
        response = json.loads(output.getvalue())
        self.assertTrue(response["ok"])
        self.assertFalse(response["data"]["ok"])
        self.assertEqual("OPERATION_CANCELLED", response["data"]["reason"])
        self.assertEqual("stopped", response["data"]["cleanup"])

    def test_arbitrary_error_codes_do_not_escape(self):
        self.service.item_error = "EXTRA_SECRET"
        result = self.make(engine="opencode").verify()
        self.assertEqual("ENGINE_FAILED", result["reason"])
        self.assertNotIn("EXTRA_SECRET", json.dumps(result))

    def test_acknowledged_stop_without_disconnected_runner_is_failure(self):
        self.service.runner_online = True
        result = self.make(engine="opencode").verify()
        self.assertFalse(result["ok"])
        self.assertTrue(result["sentinelVerified"])
        self.assertEqual("stop_unconfirmed", result["cleanup"])
        self.assertEqual("STOP_UNCONFIRMED", result["reason"])
        self.assertEqual(20, self.clock.now)

    def test_stop_failure_does_not_overstate_verification(self):
        self.service.stop_error = adapter.AdapterError("server_unreachable", "safe")
        result = self.make(engine="opencode").verify()
        self.assertFalse(result["ok"])
        self.assertTrue(result["sentinelVerified"])
        self.assertEqual("stop_failed", result["cleanup"])

    def test_login_only_persists_official_token_after_identity_check(self):
        instance = self.make(username="fake-user", password="FAKE_TRANSIENT_PASSWORD")
        result = instance.login()
        self.assertTrue(result["authenticated"])
        self.assertEqual([SERVER], self.auth.saved)
        self.assertNotIn("password", instance.request)
        self.assertNotIn("FAKE_TRANSIENT_PASSWORD", json.dumps(result))
        self.assertEqual(["/auth/login", "/v1/me"], [c[1] for c in self.service.calls])
        self.assertTrue(self.service.calls[0][2]["issue_refresh"])
        self.assertEqual([], list(self.base.rglob("*")))

    def test_browser_auth_does_not_post_or_persist_password(self):
        self.service.mode = "oidc"
        instance = self.make(username="fake-user", password="FAKE_TRANSIENT_PASSWORD")
        self.assert_code("NEEDS_BROWSER_AUTH", instance.login)
        self.assertNotIn("password", instance.request)
        self.assertEqual([], self.service.calls)
        self.assertEqual([], self.auth.saved)

    def test_connect_switches_via_existing_manager_without_signals(self):
        instance = self.make(server="https://new.example", username="fake-user", password="fake-password")
        merged = []
        instance.merge_config = merged.append
        with patch.dict(os.environ, {"OG_SERVER_URL": SERVER}), patch.object(adapter.os, "kill") as kill:
            result = instance.connect()
        self.assertTrue(result["host"]["connected"])
        self.assertEqual(["https://new.example"], merged)
        kill.assert_not_called()
        self.assertEqual(["https://new.example"], self.auth.saved)

    def test_absent_manager_does_not_launch_or_log_in(self):
        instance = self.make(password="fake-password", username="fake-user")
        instance.local_host = lambda: {**HOST, "managerRunning": False}
        with patch.object(adapter.subprocess, "Popen") as popen:
            self.assert_code("KIT_NOT_RUNNING", instance.connect)
        popen.assert_not_called()
        self.assertEqual([], self.auth.saved)

    def test_disconnect_only_clears_current_server_and_retains_manager(self):
        instance = self.make()
        with patch.object(adapter.os, "kill") as kill:
            result = instance.disconnect()
        self.assertEqual([SERVER], self.auth.cleared)
        self.assertTrue(result["registrationRetained"])
        self.assertTrue(result["managerRetained"])
        self.assertFalse(result["host"]["connected"])
        self.assertTrue(all(c[0] == "GET" for c in self.service.calls))
        kill.assert_not_called()
        self.assertEqual([False], self.auth.refresh)

    def test_disconnect_rejects_wrong_server_before_clearing(self):
        self.assert_code("HOST_SERVER_MISMATCH", self.make(server="https://other.example").disconnect)
        self.assertEqual([], self.auth.cleared)

    def test_status_rejects_malformed_hosts_and_sanitizes_extra_fields(self):
        self.service.hosts_override = {"hosts": "EXTRA_SECRET"}
        result = self.make().status()
        self.assertEqual("INVALID_RESPONSE", result["connectivityError"])
        self.assertNotIn("EXTRA_SECRET", json.dumps(result))

    def test_inventory_never_installs_and_missing_binaries_stay_missing(self):
        instance = self.make()
        instance.request.pop("engineInventory")
        with patch.object(adapter.shutil, "which", return_value=None), patch.object(adapter.subprocess, "run") as run:
            result = instance.engine_inventory()
        self.assertTrue(all(not row["installed"] for row in result.values()))
        run.assert_not_called()

    def test_inventory_version_probe_runs_once_per_invocation(self):
        instance = self.make()
        instance.request.pop("engineInventory")
        with patch.object(adapter.shutil, "which", return_value="/fake/engine"), \
                patch.object(adapter.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="v1.2.3")) as run:
            first = instance.engine_inventory()
            second = instance.engine_inventory()
        self.assertEqual(4, run.call_count)
        self.assertEqual(first, second)
        first["claude"]["installed"] = False
        self.assertTrue(instance.engine_inventory()["claude"]["installed"])

    def test_unavailable_and_invalid_engine_types_are_safe(self):
        for key in ("unknown", [], None):
            self.assert_code("INVALID_ENGINE", self.make(engine=key).verify)

    def test_unowned_probe_cannot_send_enter(self):
        instance = self.make()
        with patch.object(adapter.subprocess, "run") as run:
            self.assertFalse(instance.submit_owned_probe_draft("conv_someone_else"))
        run.assert_not_called()

    def test_claude_recovery_requires_stable_owned_draft_and_no_dialog(self):
        root = self.base / "bridge"
        directory = root / "terminal"
        directory.mkdir(parents=True)
        (directory / "tmux.json").write_text(json.dumps({"socket_path": "/fake/socket", "tmux_target": "pane"}))
        bridge = types.ModuleType("omnigent.harnesses.claude_native.bridge")
        bridge._BRIDGE_ROOT = str(root)
        bridge.read_active_session_id = Mock(return_value="conv_owned")
        bridge.has_pending_user_prompt = Mock(return_value=False)
        bridge._terminal_dialog_headline = Mock(return_value=None)
        bridge.auto_mode_billing_notice_visible = Mock(return_value=False)
        bridge._history_search_footer_shown = Mock(return_value=False)
        native = types.ModuleType("omnigent.harnesses.claude_native")
        native.bridge = bridge
        rule = "─" * 50
        pane = rule + "\n❯ \n" + adapter.PROBE_PROMPT + "\n" + rule
        instance = self.make()
        instance._owned_probes.add("conv_owned")
        with patch.dict(sys.modules, {"omnigent.harnesses.claude_native": native}), \
                patch.object(adapter.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=pane)) as run, \
                patch.object(adapter.time, "sleep"):
            self.assertTrue(instance.submit_owned_probe_draft("conv_owned"))
            self.assertEqual(4, run.call_count)
            self.assertEqual("Enter", run.call_args.args[0][-1])
            run.reset_mock()
            bridge.has_pending_user_prompt.return_value = True
            self.assertFalse(instance.submit_owned_probe_draft("conv_owned"))
            run.assert_not_called()
            bridge.has_pending_user_prompt.return_value = False
            bridge._terminal_dialog_headline.return_value = "dialog"
            self.assertFalse(instance.submit_owned_probe_draft("conv_owned"))
            self.assertEqual(1, run.call_count)
            self.assertEqual("capture-pane", run.call_args.args[0][3])

    def test_claude_recovery_never_submits_changed_or_extra_draft(self):
        root = self.base / "bridge"
        directory = root / "terminal"
        directory.mkdir(parents=True)
        (directory / "tmux.json").write_text(json.dumps({"socket_path": "/fake/socket", "tmux_target": "pane"}))
        bridge = types.SimpleNamespace(_BRIDGE_ROOT=str(root), read_active_session_id=lambda _: "conv_owned",
                                       has_pending_user_prompt=lambda _: False, _terminal_dialog_headline=lambda _: None,
                                       auto_mode_billing_notice_visible=lambda _: False, _history_search_footer_shown=lambda _: False)
        native = types.ModuleType("omnigent.harnesses.claude_native")
        native.bridge = bridge
        rule = "─" * 50
        pane = rule + "\n❯ " + adapter.PROBE_PROMPT + "\n" + rule
        instance = self.make()
        instance._owned_probes.add("conv_owned")
        responses = [types.SimpleNamespace(returncode=0, stdout=pane),
                     types.SimpleNamespace(returncode=0, stdout=pane.replace(adapter.PROBE_PROMPT, "user's own draft"))]
        with patch.dict(sys.modules, {"omnigent.harnesses.claude_native": native}), \
                patch.object(adapter.subprocess, "run", side_effect=responses) as run, patch.object(adapter.time, "sleep"):
            self.assertFalse(instance.submit_owned_probe_draft("conv_owned"))
        self.assertEqual(2, run.call_count)

    def test_merge_preserves_identity_and_open_code_binding_without_subscription_guess(self):
        import yaml
        provider_config = types.ModuleType("omnigent.onboarding.provider_config")
        provider_config.load_providers = lambda config: {name: types.SimpleNamespace(**value)
                                                       for name, value in config.get("providers", {}).items()}
        provider_config.get_default_provider = lambda *_: None
        instance = self.make(server="https://new.example")
        instance.config = types.MethodType(adapter.Adapter.config, instance)
        instance.config_home.mkdir()
        path = instance.config_home / "config.yaml"
        original = {"server": SERVER, "host": {"host_id": "host_local", "name": "Existing"},
                    "harness": {"opencode-native": {"command": "/custom/opencode", "custom": "keep"}},
                    "providers": {"custom-provider": {"kind": "api", "cli": "custom"}},
                    "custom": {"setting": "keep"}}
        path.write_text(yaml.safe_dump(original))
        with patch.dict(sys.modules, {"omnigent.onboarding.provider_config": provider_config}), \
                patch.object(adapter.shutil, "which", return_value=None):
            instance.merge_config("https://new.example")
        result = yaml.safe_load(path.read_text())
        self.assertEqual("https://new.example", result["server"])
        self.assertEqual(original["host"], result["host"])
        self.assertEqual(original["harness"], result["harness"])
        self.assertEqual(original["custom"], result["custom"])
        self.assertNotIn("opencode-subscription", result["providers"])
        self.assertNotIn("codex-subscription", result["providers"])
        self.assertNotIn("pi-subscription", result["providers"])
        self.assertIn("claude-subscription", result["providers"])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_invalid_config_is_not_overwritten(self):
        instance = self.make()
        instance.config = types.MethodType(adapter.Adapter.config, instance)
        instance.config_home.mkdir()
        path = instance.config_home / "config.yaml"
        path.write_text("[broken YAML")
        self.assert_code("INVALID_CONFIG", instance.config)
        self.assertEqual("[broken YAML", path.read_text())

    def test_exact_claude_draft_excludes_history_partial_and_extra_text(self):
        rule = "─" * 50
        valid = rule + "\n❯ \n" + adapter.PROBE_PROMPT + "\n" + rule
        self.assertTrue(adapter.Adapter.exact_probe_draft(valid))
        for text in [adapter.PROBE_PROMPT, valid.replace(adapter.PROBE_PROMPT, "partial"),
                     valid.replace(adapter.PROBE_PROMPT, adapter.PROBE_PROMPT + " extra"),
                     adapter.PROBE_PROMPT + "\n" + rule + "\n❯ \n" + rule]:
            self.assertFalse(adapter.Adapter.exact_probe_draft(text))

    def test_mutation_lock_busy_and_environment_restored(self):
        request = self.request(action="login")
        state = Path(request["stateDir"])
        state.mkdir()
        before = {key: os.environ.get(key) for key in ("OMNIGENT_CONFIG_HOME", "OMNIGENT_DATA_DIR")}
        with (state / "operation.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_code("BUSY", lambda: adapter.dispatch(request, auth=self.auth, client_factory=lambda *_: self.service))
        self.assertEqual(before, {key: os.environ.get(key) for key in before})

    def test_cli_rejects_argv_and_invalid_json_without_raw_input(self):
        for args, content, code in [(["FAKE_ARGV_PASSWORD"], "{}", "STDIN_REQUIRED"),
                                    ([], "FAKE_RAW_PASSWORD", "INVALID_JSON"), ([], "[1]", "INVALID_REQUEST")]:
            result = subprocess.run([sys.executable, str(ROOT / "adapter.py"), *args],
                                    input=content, capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode)
            self.assertEqual("", result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(code, response["error"]["code"])
            self.assertNotIn("FAKE_", result.stdout)


class OfficialAuthTests(unittest.TestCase):
    def setUp(self):
        self.sdk = types.ModuleType("omnigent.cli_auth")
        self.sdk._token_file_lock = contextlib.nullcontext
        self.sdk.store_token = Mock()
        self.sdk.load_token = Mock(return_value=None)
        self.sdk.refresh_stored_token = Mock(return_value="FAKE_REFRESHED_TOKEN")
        self.sdk.clear_token = Mock()
        self.package = types.ModuleType("omnigent")
        self.package.__path__ = []
        self.context = patch.dict(sys.modules, {"omnigent": self.package, "omnigent.cli_auth": self.sdk})
        self.context.start()
        self.addCleanup(self.context.stop)
    def test_store_delegates_exact_official_shape_and_expiry(self):
        with patch.object(adapter.time, "time", return_value=10):
            adapter.OfficialAuth.save(SERVER, {"token": "FAKE_TOKEN", "user": {"id": "user_fake"},
                                               "expires_in": 30, "refresh_token": "FAKE_REFRESH"})
        self.sdk.store_token.assert_called_once_with(SERVER, "FAKE_TOKEN", "user_fake", 40, refresh_token="FAKE_REFRESH")
    def test_refresh_is_official_bounded_and_readonly_never_refreshes(self):
        self.assertEqual("FAKE_REFRESHED_TOKEN", adapter.OfficialAuth.token(SERVER))
        self.sdk.refresh_stored_token.assert_called_once_with(SERVER, timeout=10.0)
        self.sdk.refresh_stored_token.reset_mock()
        self.assertIsNone(adapter.OfficialAuth.token(SERVER, refresh=False))
        self.sdk.refresh_stored_token.assert_not_called()
    def test_clear_delegates_only_selected_server(self):
        adapter.OfficialAuth.clear(SERVER)
        self.sdk.clear_token.assert_called_once_with(SERVER)
    def test_invalid_login_shapes_never_store(self):
        for payload in [{"token": "fake", "user": {}}, {"token": "fake", "user": {"id": "fake"}, "expires_in": True},
                        {"token": "fake", "user": {"id": "fake"}, "expires_in": float("nan")},
                        {"token": "fake", "user": {"id": "fake"}, "refresh_token": {"secret": "fake"}}]:
            with self.assertRaises(adapter.AdapterError):
                adapter.OfficialAuth.save(SERVER, payload)
        self.sdk.store_token.assert_not_called()


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.auth = Auth()
        self.http = Mock()
        self.httpx = types.ModuleType("httpx")
        self.httpx.HTTPError = type("HTTPError", (Exception,), {})
        self.httpx.Client = Mock(return_value=self.http)
        modules = patch.dict(sys.modules, {"httpx": self.httpx})
        modules.start()
        self.addCleanup(modules.stop)
        self.client = adapter.Client(SERVER, self.auth, read_only=True)
        self.addCleanup(self.client.close)
    def test_redirects_never_forward_credentials_and_bodies_are_not_echoed(self):
        for status, code in [(302, "HTTP_302"), (401, "NEEDS_LOGIN"), (403, "FORBIDDEN"), (500, "HTTP_500")]:
            self.http.request.return_value = types.SimpleNamespace(status_code=status, text="EXTRA_SECRET")
            with self.assertRaises(adapter.AdapterError) as caught:
                self.client.request("POST", "/auth/login", {"password": "FAKE_PASSWORD"}, authenticated=False)
            self.assertEqual(code, caught.exception.code)
            self.assertNotIn("EXTRA_SECRET", caught.exception.message)
            self.assertIs(False, self.http.request.call_args.kwargs["follow_redirects"])
            self.assertNotIn("Authorization", self.http.request.call_args.kwargs["headers"])
    def test_readonly_auth_and_held_token_never_refresh_or_repopulate_store(self):
        self.http.request.return_value = types.SimpleNamespace(status_code=200, json=lambda: {})
        self.client.request("GET", "/v1/me")
        self.assertEqual([False], self.auth.refresh)
        self.client.hold_token("FAKE_HELD_TOKEN")
        self.client.request("GET", "/v1/me", refresh_auth=False)
        self.assertEqual([False], self.auth.refresh)
        self.assertEqual("Bearer FAKE_HELD_TOKEN", self.http.request.call_args.kwargs["headers"]["Authorization"])
        self.assertEqual([], self.auth.saved)
    def test_transport_exception_and_invalid_json_are_safe(self):
        self.http.request.side_effect = self.httpx.HTTPError("EXTRA_SECRET")
        with self.assertRaises(adapter.AdapterError) as caught:
            self.client.request("GET", "/v1/me")
        self.assertEqual("SERVER_UNREACHABLE", caught.exception.code)
        self.assertNotIn("EXTRA_SECRET", caught.exception.message)
        self.http.request.side_effect = None
        self.http.request.return_value = types.SimpleNamespace(status_code=200, json=lambda: ["EXTRA_SECRET"])
        with self.assertRaises(adapter.AdapterError) as caught:
            self.client.request("GET", "/v1/me")
        self.assertEqual("INVALID_RESPONSE", caught.exception.code)
    def test_unknown_exception_and_error_code_are_sanitized_on_wire(self):
        for error in [RuntimeError("EXTRA_SECRET"), adapter.AdapterError("EXTRA_SECRET", "FAKE_PASSWORD")]:
            output = io.StringIO()
            with patch.object(adapter, "dispatch", side_effect=error), patch.object(sys, "argv", ["adapter.py"]), \
                    patch.object(sys, "stdin", io.StringIO('{"action":"status"}')), contextlib.redirect_stdout(output):
                adapter.main()
            response = json.loads(output.getvalue())
            self.assertEqual("INTERNAL_ERROR", response["error"]["code"])
            self.assertNotIn("EXTRA_SECRET", output.getvalue())
            self.assertNotIn("FAKE_PASSWORD", output.getvalue())


if __name__ == "__main__":
    unittest.main()
