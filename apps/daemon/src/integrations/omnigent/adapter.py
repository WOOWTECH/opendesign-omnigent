#!/usr/bin/env python3
"""Standalone OpenDesign JSON boundary around the installed Omnigent client.

Passwords enter over stdin and are never persisted by this adapter. Omnigent
owns token storage and refresh. Only this machine's host and this invocation's
verification session are used; no other host, agent, or conversation is edited.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit, urlunsplit

ENGINES = {
    "claude": {"harness": "claude-native", "agent": "claude-native-ui", "family": "anthropic"},
    "opencode": {"harness": "opencode-native", "agent": "opencode-native-ui", "family": None},
    "codex": {"harness": "codex-native", "agent": "codex-native-ui", "family": "openai"},
    "pi": {"harness": "pi-native", "agent": "pi-native-ui", "family": "pi"},
}
SENTINEL = "OMNIGENT_CONNECTION_OK"
PROBE_PROMPT = f"Reply exactly {SENTINEL}. Do not use tools or modify any files."
SAFE_ERROR_CODES = frozenset({
    "NEEDS_SERVER", "INVALID_SERVER", "INVALID_LOGIN_RESPONSE", "SERVER_UNREACHABLE",
    "NEEDS_LOGIN", "FORBIDDEN", "INVALID_RESPONSE", "UNSUPPORTED_AUTH", "INVALID_PATH",
    "INVALID_CONFIG", "INVALID_TIMEOUT", "AGENT_CATALOG_TOO_LARGE", "INVALID_PROVIDERS",
    "KIT_NOT_RUNNING", "NEEDS_BROWSER_AUTH", "NEEDS_RESET_ID",
    "HOST_CONNECT_TIMEOUT", "HOST_SERVER_MISMATCH", "MISSING_HOST_ID", "HOST_DISCONNECT_TIMEOUT",
    "INVALID_ENGINE", "ENGINE_NOT_INSTALLED", "HOST_OFFLINE", "MISSING_AGENT", "ENGINE_NOT_READY",
    "MODELS_UNAVAILABLE", "INVALID_MODEL", "VERIFY_TIMEOUT", "INVALID_SESSION_RESPONSE",
    "INVALID_REQUEST", "INVALID_ACTION", "BUSY", "STDIN_REQUIRED", "REQUEST_TOO_LARGE",
    "HOST_OWNERSHIP_UNVERIFIED", "HOST_SHUTDOWN_TIMEOUT",
    "OPERATION_TIMEOUT", "OPERATION_CANCELLED", "INVALID_JSON", "MISSING_DEPENDENCY", "INTERNAL_ERROR",
})


class AdapterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code.upper()
        self.message = message


def normalize_server(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError("needs_server", "請提供 Omnigent server 網址。")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AdapterError("invalid_server", "Server 網址包含不支援的字元。")
    value = value.strip()
    if any(ord(c) < 33 for c in value) or "\\" in value:
        raise AdapterError("invalid_server", "Server 網址包含不支援的字元。")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AdapterError("invalid_server", "Server 網址格式不正確。") from None
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise AdapterError("invalid_server", "請使用沒有帳號密碼的 HTTP 或 HTTPS server 網址。")
    if parsed.query or parsed.fragment:
        raise AdapterError("invalid_server", "Server 網址不可包含查詢參數或片段。")
    if parsed.scheme == "http" and parsed.hostname.lower() not in ("localhost", "127.0.0.1", "::1"):
        raise AdapterError("invalid_server", "遠端 Omnigent server 必須使用 HTTPS。")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port:
        host += f":{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", ""))


def safe_text(value: object, limit: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:limit]


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def bounded_call(seconds: float = 15.0):
    """Linux CLI boundary: cap official token-file lock and refresh waits.

    The adapter is invoked in a dedicated process/main thread, not a shared
    threaded Python service. Preserve any earlier alarm deadline.
    """
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(_signum, _frame):
        raise AdapterError("operation_timeout", "Omnigent 操作超過等待時間。")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous_timer[0]) if previous_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


class OfficialAuth:
    """Use Omnigent's stored credential shape, locks, and refresh implementation."""

    @staticmethod
    def token(server: str, *, refresh: bool = True) -> str | None:
        from omnigent.cli_auth import load_token, refresh_stored_token

        with bounded_call():
            token = load_token(server, min_remaining_seconds=90.0)
            if not token and refresh:
                token = refresh_stored_token(server, timeout=10.0)
            return token or load_token(server)

    @staticmethod
    def save(server: str, payload: dict) -> None:
        from omnigent.cli_auth import _token_file_lock, store_token

        token = payload.get("token")
        user = payload.get("user")
        user_id = user.get("id") if isinstance(user, dict) else None
        if not isinstance(token, str) or not token or not isinstance(user_id, str) or not user_id:
            raise AdapterError("invalid_login_response", "Server 登入回應不符合 Omnigent 合約。")
        expires = payload.get("expires_in", 8 * 3600)
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= 0:
            raise AdapterError("invalid_login_response", "Server 登入憑證的有效時間不正確。")
        refresh_token = payload.get("refresh_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise AdapterError("invalid_login_response", "Server 更新憑證回應不正確。")
        with bounded_call(), _token_file_lock():
            store_token(server, token, user_id, time.time() + expires, refresh_token=refresh_token)

    @staticmethod
    def clear(server: str) -> None:
        from omnigent.cli_auth import _token_file_lock, clear_token

        with bounded_call(), _token_file_lock():
            clear_token(server)


class Client:
    def __init__(self, server: str, auth: OfficialAuth, *, read_only: bool = False, transport=None):
        import httpx
        self.server = server
        self.auth = auth
        self.read_only = read_only
        self._held_token = None
        self._http = httpx.Client(follow_redirects=False, transport=transport)

    def close(self) -> None:
        self._http.close()

    def hold_token(self, token: str) -> None:
        # A logout check needs an in-memory bearer after the local entry is
        # removed. Never refresh or write it back to the credential store.
        self._held_token = token

    def request(self, method: str, path: str, payload: dict | None = None, *, authenticated: bool = True,
                timeout_seconds: float = 15.0, refresh_auth: bool | None = None) -> dict:
        import httpx

        headers = {"Accept": "application/json"}
        if authenticated:
            token = self._held_token or self.auth.token(self.server, refresh=not self.read_only if refresh_auth is None else refresh_auth)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        try:
            # Redirects never forward the entered password or a stored bearer
            # to an unexpected origin. A proxy/TLS error is reported generically.
            response = self._http.request(method, f"{self.server}{path}", json=payload, headers=headers,
                                     timeout=timeout_seconds, follow_redirects=False)
        except httpx.HTTPError:
            raise AdapterError("server_unreachable", "無法連線 Omnigent server，請檢查網址、網路與 TLS。") from None
        if response.status_code == 401:
            raise AdapterError("needs_login", "請登入此 Omnigent server；原有登入可能已到期。")
        if response.status_code == 403:
            raise AdapterError("forbidden", "目前帳號沒有此 Omnigent 資源的權限。")
        if not 200 <= response.status_code < 300:
            raise AdapterError(f"http_{response.status_code}", f"Omnigent server 回傳 HTTP {response.status_code}。")
        try:
            body = response.json()
        except ValueError:
            raise AdapterError("invalid_response", "Server 回應並非有效的 Omnigent JSON。") from None
        if not isinstance(body, dict):
            raise AdapterError("invalid_response", "Server 回應格式不符合 Omnigent 合約。")
        return body

    def auth_probe(self) -> str:
        import httpx

        try:
            response = self._http.get(f"{self.server}/v1/me", timeout=15.0, follow_redirects=False)
        except httpx.HTTPError:
            raise AdapterError("server_unreachable", "無法連線 Omnigent server，請檢查網址、網路與 TLS。") from None
        if response.status_code == 200:
            return "header"
        if response.status_code != 401:
            raise AdapterError("unsupported_auth", "Server 沒有回傳 Omnigent 登入模式，請確認 API 網址。")
        try:
            body = response.json()
        except ValueError:
            raise AdapterError("unsupported_auth", "Server 沒有回傳 Omnigent 登入模式。") from None
        return "accounts" if isinstance(body, dict) and body.get("login_url") == "/login" else "oidc"


class Adapter:
    def __init__(self, request: dict, *, auth=None, client_factory=None, sleeper=time.sleep, clock=time.monotonic, probe_submit=None):
        self.request = request
        self.home = Path(os.environ.get("HOME", str(Path.home())))
        self.config_home = Path(request.get("configHome") or os.environ.get("OMNIGENT_CONFIG_HOME") or self.home / ".omnigent")
        self.data_dir = Path(request.get("dataDir") or os.environ.get("OMNIGENT_DATA_DIR") or self.config_home)
        self.state_dir = Path(request.get("stateDir") or self.data_dir / "opendesign-adapter")
        self.workspace = Path(request.get("workspace") or self.home)
        for path in (self.config_home, self.data_dir, self.state_dir, self.workspace):
            if not path.is_absolute():
                raise AdapterError("invalid_path", "Omnigent 設定與工作區必須使用絕對路徑。")
        # The official modules resolve these at call time, so every invocation
        # shares the resident host's token store and configuration.
        os.environ["OMNIGENT_CONFIG_HOME"] = str(self.config_home)
        os.environ["OMNIGENT_DATA_DIR"] = str(self.data_dir)
        self.auth = auth or OfficialAuth()
        factory = client_factory or (lambda server, readonly=False: Client(server, self.auth, read_only=readonly))
        self._clients = []
        def tracked_client(server, readonly=False):
            client = factory(server, readonly)
            self._clients.append(client)
            return client
        self.client_factory = tracked_client
        self.sleep = sleeper
        self.clock = clock
        self.probe_submit = probe_submit or self.submit_owned_probe_draft
        self._owned_probes: set[str] = set()
        self._deadline: float | None = None
        self._inventory: dict | None = None

    def close(self) -> None:
        for client in self._clients:
            close = getattr(client, "close", None)
            if close:
                close()

    def probe_journal(self) -> dict:
        path = self.state_dir / 'owned-probes.json'
        if not path.exists():
            return {}
        try:
            journal = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(journal, dict):
                raise ValueError()
            return journal
        except (OSError, ValueError):
            raise AdapterError('internal_error', '驗證對話紀錄無法讀取，請先檢查私有整合狀態。') from None

    def record_probe(self, session_id: str, record: dict | None) -> None:
        journal = self.probe_journal()
        if record is None:
            journal.pop(session_id, None)
        else:
            journal[session_id] = record
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile('w', dir=self.state_dir, delete=False, encoding='utf-8') as handle:
            temporary = Path(handle.name)
            try:
                os.chmod(temporary, 0o600)
                json.dump(journal, handle)
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(temporary, self.state_dir / 'owned-probes.json')
            finally:
                temporary.unlink(missing_ok=True)

    def recover_probes(self, client, server: str, host_id: str) -> None:
        for session_id, record in self.probe_journal().items():
            if not isinstance(record, dict) or record.get('server') != server or record.get('hostId') != host_id:
                continue
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', session_id):
                raise AdapterError('internal_error', '驗證對話紀錄格式不正確。')
            snapshot = self.api(client, 'GET', f'/v1/sessions/{quote(session_id, safe="")}')
            if snapshot.get('id') != session_id or snapshot.get('host_id') != host_id or snapshot.get('title') != record.get('title'):
                raise AdapterError('internal_error', '無法確認中斷測試對話的所有權，請先檢查此 host。')
            if snapshot.get('runner_online') is not False:
                self.api(client, 'POST', f'/v1/sessions/{quote(session_id, safe="")}/events', {'type': 'stop_session', 'data': {}}, refresh_auth=False)
                until = self.clock() + 20.0
                while self.clock() < until:
                    snapshot = self.api(client, 'GET', f'/v1/sessions/{quote(session_id, safe="")}', refresh_auth=False)
                    if snapshot.get('id') == session_id and snapshot.get('runner_online') is False:
                        break
                    self.sleep(1)
                if snapshot.get('runner_online') is not False:
                    raise AdapterError('internal_error', '中斷的測試對話尚未停止，請稍後重試。')
            self.record_probe(session_id, None)

    def config(self) -> dict:
        import yaml

        path = self.config_home / "config.yaml"
        if not path.exists():
            return {}
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raise AdapterError("invalid_config", "現有 Omnigent config.yaml 無法讀取，請先修正設定。") from None
        if config is None:
            return {}
        if not isinstance(config, dict):
            raise AdapterError("invalid_config", "現有 Omnigent config.yaml 必須是 YAML mapping。")
        return config

    def server(self) -> str:
        return normalize_server(self.request.get("server") or self.config().get("server"))

    def timeout(self, default=120, maximum=180) -> float:
        raw = self.request.get("timeoutSeconds", default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise AdapterError("invalid_timeout", "等待時間格式不正確。")
        return max(1.0, min(float(raw), maximum))

    def local_host(self) -> dict:
        host = self.config().get("host")
        host = host if isinstance(host, dict) else {}
        status_path = Path(os.environ.get("OG_STATUS_FILE") or Path(os.environ.get("OG_STATE_DIR") or self.data_dir / "omnigent-host") / "status.json")
        status = read_json(status_path)
        supervisor_pid = status.get("supervisor_pid")
        kit_running = self.process_matches(supervisor_pid, "supervisor.sh")
        if not kit_running:
            try:
                with (status_path.parent / "supervisor.lock").open("r") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    except BlockingIOError:
                        kit_running = True
            except OSError:
                pass
        # A local status file says the process is running; only catalog() can
        # claim that the server actually sees it online.
        return {"id": safe_text(host.get("host_id")), "name": safe_text(host.get("name")),
                "manager": "kit" if kit_running else "unmanaged", "managerRunning": kit_running,
                "managed": kit_running,
                "state": safe_text(status.get("state")) or "unknown", "connected": False}

    @staticmethod
    def process_matches(pid, marker: str) -> bool:
        try:
            pid = int(pid)
            if pid <= 1:
                return False
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            return marker.encode() in command
        except (TypeError, ValueError, OSError):
            return False

    def engine_inventory(self) -> dict:
        if self._inventory is not None:
            return copy.deepcopy(self._inventory)
        engines = {}
        for key, spec in ENGINES.items():
            inventory = self.request.get("engineInventory", {})
            entry = inventory.get(key) if isinstance(inventory, dict) else None
            if isinstance(entry, dict) and isinstance(entry.get("installed"), bool):
                engines[key] = {"key": key, "harness": spec["harness"], "installed": entry["installed"],
                                "version": safe_text(entry.get("version")), "configured": None, "ready": False}
                explicit = entry.get("path")
                if isinstance(explicit, str) and Path(explicit).is_absolute() and safe_text(explicit, 4096) == explicit:
                    engines[key]["path"] = explicit
                continue
            binary = shutil.which(key)
            version_text = None
            if binary:
                try:
                    probe = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
                    match = re.search(r"\d+\.\d+\.\d+(?:[-.][\w]+)*", probe.stdout)
                    if probe.returncode == 0 and match:
                        version_text = match.group(0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            engines[key] = {"key": key, "harness": spec["harness"], "installed": bool(binary),
                            "version": version_text, "path": binary, "configured": None, "ready": False}
        self._inventory = engines
        return copy.deepcopy(engines)

    def status(self) -> dict:
        try:
            version = importlib.metadata.version("omnigent")
        except importlib.metadata.PackageNotFoundError:
            version = None
        host = self.local_host()
        engines = self.engine_inventory()
        try:
            server = self.server()
        except AdapterError as error:
            if error.code != "NEEDS_SERVER":
                raise
            server = None
        connectivity_error = None
        if server and host["id"] and not self.request.get("localOnly", False):
            try:
                hosts = self.api(self.client_factory(server, True), "GET", "/v1/hosts").get("hosts")
                if not isinstance(hosts, list):
                    raise AdapterError("invalid_response", "Omnigent hosts 清單格式不正確。")
                matching = next((row for row in hosts if isinstance(row, dict) and row.get("host_id") == host["id"]), None)
                host["connected"] = bool(matching and matching.get("status") == "online")
                readiness = matching.get("configured_harnesses", {}) if matching else {}
                for key, spec in ENGINES.items():
                    available = readiness.get(spec["harness"], readiness.get(key)) if isinstance(readiness, dict) else None
                    if available not in (True, False, "binary-missing", "needs-auth", "version-too-low", None):
                        available = None
                    engines[key]["configured"] = available
                    engines[key]["ready"] = engines[key]["installed"] and host["connected"] and available is True
            except AdapterError as error:
                connectivity_error = error.code
        return {"server": server, "version": version, "installed": bool(version), "host": host,
                "engines": engines, "workspace": str(self.workspace), "connectivityError": connectivity_error}

    def api(self, client, method: str, path: str, payload=None, **kwargs) -> dict:
        seconds = kwargs.pop("timeout_seconds", 15.0)
        if self._deadline is not None:
            remaining = self._deadline - self.clock()
            if remaining <= 0:
                raise AdapterError("operation_timeout", "Omnigent 操作超過等待時間。")
            seconds = min(seconds, remaining)
        with bounded_call(seconds):
            return client.request(method, path, payload, timeout_seconds=seconds, **kwargs)

    def all_agents(self, client) -> list[dict]:
        agents = []
        after = None
        for _ in range(10):
            path = "/v1/agents?limit=100&order=asc"
            if after:
                path += "&after=" + quote(after, safe="")
            page = self.api(client, "GET", path)
            rows = page.get("data")
            if not isinstance(rows, list):
                raise AdapterError("invalid_response", "Omnigent agents 清單格式不正確。")
            agents.extend(row for row in rows if isinstance(row, dict))
            if not page.get("has_more"):
                return agents
            after = page.get("last_id")
            if not isinstance(after, str) or not after:
                break
        raise AdapterError("agent_catalog_too_large", "Omnigent agents 清單過大或分頁格式不正確。")

    def resume(self) -> dict:
        # Only the daemon's reconciled GUI approval authorizes this mutation.
        # Plain status/catalog calls never renew credentials or start a host.
        if self.request.get("allowManagedHost") is not True or self.request.get("localOnly", False):
            raise AdapterError("needs_login", "請先在 GUI 完成本機連線設定。")
        private_home = self.state_dir.resolve() / "host"
        if self.config_home.resolve() != private_home or self.data_dir.resolve() != private_home:
            raise AdapterError("host_ownership_unverified", "自動恢復僅能使用此整合的私有 host 設定。")
        server = self.server()
        if not self.auth.token(server, refresh=True):
            raise AdapterError("needs_login", "原有登入已無法更新，請重新登入。")
        self.ensure_host(server)
        return self.catalog()

    def catalog(self, *, client=None, read_only=True, engine_key=None) -> dict:
        server = self.server()
        client = client or self.client_factory(server, read_only)
        host = self.local_host()
        hosts = self.api(client, "GET", "/v1/hosts").get("hosts")
        if not isinstance(hosts, list):
            raise AdapterError("invalid_response", "Omnigent hosts 清單格式不正確。")
        matching = next((row for row in hosts if isinstance(row, dict) and host["id"] and row.get("host_id") == host["id"]), None)
        host["connected"] = bool(matching and matching.get("status") == "online")
        if matching:
            host["name"] = safe_text(matching.get("name")) or host["name"]
        readiness = matching.get("configured_harnesses") if matching else {}
        readiness = readiness if isinstance(readiness, dict) else {}
        harness_catalog = self.api(client, "GET", "/v1/harnesses").get("data") if engine_key is None else []
        agents = self.all_agents(client)
        inventory = self.engine_inventory()
        engines = {}
        for key, spec in ENGINES.items():
            if engine_key is not None and key != engine_key:
                continue
            agent = next((row for row in agents if row.get("name") == spec["agent"] and row.get("harness", spec["harness"]) == spec["harness"]), None)
            available = readiness.get(spec["harness"], readiness.get(key))
            if available not in (True, False, "binary-missing", "needs-auth", "version-too-low", None):
                available = None
            models = []
            model_error = None
            if inventory[key]["installed"] and host["connected"] and available is True:
                try:
                    result = self.api(client, "GET", f"/v1/hosts/{quote(host['id'], safe='')}/harnesses/{spec['harness']}/model-options")
                    rows = result.get("models")
                    if not isinstance(rows, list):
                        raise AdapterError("invalid_response", "Omnigent 模型清單格式不正確。")
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                            continue
                        if not row["id"] or safe_text(row["id"], 1024) != row["id"]:
                            continue
                        models.append({"id": row["id"], "model": safe_text(row.get("model") or row["id"], 1024),
                                       "displayName": safe_text(row.get("displayName") or row["id"]),
                                       "isDefault": row.get("isDefault") is True})
                except AdapterError as error:
                    model_error = error.code
            engines[key] = {"key": key, "harness": spec["harness"], "configured": available,
                            "installed": inventory[key]["installed"], "version": inventory[key]["version"],
                            "ready": inventory[key]["installed"] and host["connected"] and available is True and bool(models) and bool(agent),
                            "agentId": safe_text(agent.get("id")) if agent else None,
                            "models": models, "modelError": model_error}
        # Expose only the names of supported picker modules, never the raw
        # server catalog (which can grow new credential-bearing fields).
        modules = []
        rows = harness_catalog.values() if isinstance(harness_catalog, dict) else harness_catalog if isinstance(harness_catalog, list) else []
        for row in rows:
            if isinstance(row, dict):
                key = safe_text(row.get("key") or row.get("id"))
                if key:
                    modules.append({"key": key, "name": safe_text(row.get("label") or row.get("display_name") or row.get("displayName") or row.get("name") or key)})
        return {"server": server, "host": host, "engines": engines, "modules": modules,
                "workspace": str(self.workspace)}

    def merge_config(self, server: str) -> None:
        import yaml
        from omnigent.onboarding.provider_config import get_default_provider, load_providers

        self.config_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.config_home / ".opendesign-adapter-config.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AdapterError("busy", "另一個 Omnigent 設定操作正在執行。") from None
            config = self.config()
            original = copy.deepcopy(config)
            providers = config.setdefault("providers", {})
            if not isinstance(providers, dict):
                raise AdapterError("invalid_config", "現有 providers 設定必須是 YAML mapping。")
            inventory = self.engine_inventory()
            try:
                parsed = load_providers(config)
                for key, spec in ENGINES.items():
                    if spec["family"] is None or not inventory[key]["installed"]:
                        continue
                    if any(entry.kind == "subscription" and entry.cli == key for entry in parsed.values()):
                        continue
                    name = f"{key}-subscription"
                    if name in providers:
                        continue
                    default = get_default_provider(config, spec["family"])
                    entry = {"kind": "subscription", "cli": key}
                    if default is None:
                        entry["default"] = spec["family"]
                    providers[name] = entry
                    parsed = load_providers(config)
            except Exception:
                raise AdapterError("invalid_providers", "現有 Omnigent providers 設定無法安全合併。") from None
            # CLI installers can relocate a standard executable. Repair only
            # a dead absolute path whose basename is the engine's normal name;
            # live commands and custom wrappers remain user-owned.
            harnesses = config.get("harness", {})
            if isinstance(harnesses, dict):
                for key, spec in ENGINES.items():
                    entry = harnesses.get(spec["harness"])
                    command = entry.get("command") if isinstance(entry, dict) else None
                    if not isinstance(command, str) or not Path(command).is_absolute() or Path(command).name != key:
                        continue
                    if Path(command).is_file() and os.access(command, os.X_OK):
                        continue
                    replacement = shutil.which(key)
                    if replacement and Path(replacement).is_file() and os.access(replacement, os.X_OK):
                        entry["command"] = replacement
            config["server"] = server
            if config == original:
                return
            path = self.config_home / "config.yaml"
            with tempfile.NamedTemporaryFile("w", dir=self.config_home, delete=False, encoding="utf-8") as handle:
                temporary = Path(handle.name)
                try:
                    os.chmod(temporary, 0o600)
                    yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)

    @contextlib.contextmanager
    def host_lifecycle(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.state_dir / "host-lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def host_owner(self, *, create=False) -> str | None:
        """Private process capability, independent of login and host identity."""
        path = self.state_dir / "host-owner.json"
        if not path.exists() and not create:
            return None
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                json.dump({"schema": 1, "id": secrets.token_hex(32)}, handle)
        record = read_json(path)
        owner = record.get("id")
        if path.is_symlink() or path.stat().st_uid != os.getuid() or record.get("schema") != 1 or not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{64}", owner):
            raise AdapterError("host_ownership_unverified", "無法驗證此原生 host 的程序所有權。")
        return owner

    def owned_processes(self, owner: str):
        """Pin each matching process before inspecting it again; never signal a raw PID.

        Both capability and private home are inherited by the supervisor and its
        host/session tree. Status-file PIDs, argv substrings and process groups
        alone are deliberately not termination authority.
        """
        expected = {b"OD_OMNIGENT_HOST_OWNER": owner.encode(),
                    b"OD_OMNIGENT_HOST_HOME": os.fsencode(self.config_home.resolve())}
        processes = []
        try:
            for directory in Path("/proc").iterdir():
                if not directory.name.isdigit() or int(directory.name) in (os.getpid(), os.getppid()):
                    continue
                descriptor = None
                try:
                    def matches():
                        if directory.stat().st_uid != os.getuid():
                            return False
                        env = dict(row.split(b"=", 1) for row in (directory / "environ").read_bytes().split(b"\0") if b"=" in row)
                        return all(env.get(key) == value for key, value in expected.items())
                    if not matches():
                        continue
                    descriptor = os.pidfd_open(int(directory.name))
                    if select.select([descriptor], [], [], 0)[0] or not matches():
                        continue
                    argv = (directory / "cmdline").read_bytes().split(b"\0")
                    supervisor = os.fsencode(Path(__file__).resolve().parent / "host-kit" / "supervisor.sh") in argv
                    identity = (directory.name, (directory / "stat").read_text().rsplit(")", 1)[1].split()[19])
                    processes.append((descriptor, supervisor, identity))
                    descriptor = None
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            return processes
        except BaseException:
            for descriptor, _, _ in processes:
                os.close(descriptor)
            raise

    @staticmethod
    def signal_owned(processes, signum, signaled, *, supervisors_only=False):
        for descriptor, supervisor, identity in processes:
            if (identity, signum) in signaled:
                continue
            if supervisors_only and not supervisor:
                continue
            try:
                signal.pidfd_send_signal(descriptor, signum)
                signaled.add((identity, signum))
            except ProcessLookupError:
                pass

    def shutdown(self) -> dict:
        # No server access, login refresh, logout, config rewrite or engine install.
        if not self.state_dir.exists():
            return {"stopped": True, "forced": False}
        with self.host_lifecycle():
            owner = self.host_owner()
            if owner is None:
                if self.local_host()["managerRunning"]:
                    raise AdapterError("host_ownership_unverified", "既有 supervisor 尚無原生程序所有權紀錄。")
                return {"stopped": True, "forced": False}
            if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                raise AdapterError("host_ownership_unverified", "此系統不支援安全的程序停止。")
            grace = self.request.get("shutdownGraceSeconds", 18.0)
            if isinstance(grace, bool) or not isinstance(grace, (float, int)) or not math.isfinite(grace):
                raise AdapterError("invalid_timeout", "停止等待時間格式不正確。")
            deadline = self.clock() + max(0.1, min(grace, 18.0))
            forced = False
            signaled = set()
            while True:
                processes = self.owned_processes(owner)
                if not processes:
                    return {"stopped": True, "forced": forced}
                try:
                    if self.clock() >= deadline:
                        forced = True
                        self.signal_owned(processes, signal.SIGKILL, signaled)
                    else:
                        # Let the unchanged kit run its 15-second graceful stop.
                        # If it has already exited, also drain orphaned sessions.
                        self.signal_owned(processes, signal.SIGTERM, signaled,
                                          supervisors_only=any(supervisor for _, supervisor, _ in processes))
                finally:
                    for descriptor, _, _ in processes:
                        os.close(descriptor)
                if self.clock() >= deadline + 2:
                    raise AdapterError("host_shutdown_timeout", "原生 host 未能在期限內停止。")
                self.sleep(0.1)

    def ensure_host(self, server: str) -> str:
        with self.host_lifecycle():
            return self.start_host(server)

    def start_host(self, server: str) -> str:
        if self.local_host()["managerRunning"]:
            return "kit"
        if self.request.get("allowManagedHost") is True:
            kit = Path(__file__).resolve().parent / "host-kit"
            cli = Path(sys.executable).parent / "omnigent"
            if not cli.is_file() or not (kit / "supervisor.sh").is_file():
                raise AdapterError("missing_dependency", "Omnigent host 執行元件尚未安裝。")
            self.config_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            env = dict(os.environ)
            env.update({"OG_BIN": str(cli), "OG_PYTHON": sys.executable,
                        "OG_CONFIG_HOME": str(self.config_home), "OG_DATA_DIR": str(self.data_dir),
                        "OG_STATE_DIR": str(self.data_dir / "omnigent-host"),
                        "OG_HOST_ENABLED": "1", "OG_HOST_NAME": "OpenDesign · " + socket.gethostname(),
                        "OG_WORKSPACE": str(self.workspace), "OG_IDLE_SECONDS": "2",
                        "OG_STOP_GRACE_SECONDS": "15",
                        "OD_OMNIGENT_HOST_OWNER": self.host_owner(create=True),
                        "OD_OMNIGENT_HOST_HOME": str(self.config_home.resolve()),
                        "OG_UNSET_ENV": ",".join(key for key in (env.get("OG_UNSET_ENV", "") + ",OD_API_TOKEN").split(",")
                                                 if key and key not in ("OD_OMNIGENT_HOST_OWNER", "OD_OMNIGENT_HOST_HOME")),
                        "OG_PASSTHROUGH": "OD_DAEMON_URL,NODE_PATH,NODE_OPTIONS,LITELLM_BASE_URL,OD_OMNIGENT_HOST_OWNER,OD_OMNIGENT_HOST_HOME"})
            # The native kit takes an exclusive volume lock before starting a host.
            # No account information is passed in argv, environment additions, or logs.
            subprocess.Popen(["/bin/sh", str(kit / "supervisor.sh")], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            until = self.clock() + 5
            while self.clock() < until:
                if self.local_host()["managerRunning"]:
                    return "kit"
                self.sleep(0.1)
        raise AdapterError("kit_not_running", "原生 host supervisor 尚未執行，請先恢復部署服務。")

    def login(self) -> dict:
        """Store only official credentials; the password is discarded on exit."""
        server = self.server()
        client = self.client_factory(server, False)
        username, password = self.request.get("username"), self.request.get("password")
        try:
            if username is not None or password is not None:
                if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
                    raise AdapterError("needs_login", "請同時輸入 Omnigent 帳號及密碼。")
                with bounded_call():
                    mode = client.auth_probe()
                if mode != "accounts":
                    raise AdapterError("needs_browser_auth", "此 server 使用瀏覽器登入，請先完成 omnigent login。")
                body = self.api(client, "POST", "/auth/login", {"username": username.strip(), "password": password, "issue_refresh": True}, authenticated=False)
                token = body.get("token")
                if not isinstance(token, str) or not token:
                    raise AdapterError("invalid_login_response", "Server 登入回應不符合 Omnigent 合約。")
                client.hold_token(token)
                self.api(client, "GET", "/v1/me", refresh_auth=False)
                self.auth.save(server, body)
            else:
                if not self.auth.token(server) and client.auth_probe() != "header":
                    raise AdapterError("needs_login", "請輸入 Omnigent 帳號密碼，或先完成 omnigent login。")
                self.api(client, "GET", "/v1/me")
            return {"server": server, "authenticated": True}
        finally:
            self.request.pop("password", None)
            password = None

    def connect(self) -> dict:
        server = self.server()
        if self.request.get("allowManagedHost") is not True:
            self.ensure_host(server)
        self._deadline = self.clock() + self.timeout(default=90)
        self.login()
        self.merge_config(server)
        manager = self.ensure_host(server)
        client = self.client_factory(server, False)
        while True:
            result = self.catalog(client=client, read_only=False)
            if result["host"]["connected"]:
                result["host"]["manager"] = manager
                self.recover_probes(client, server, result['host']['id'])
                return result
            if self.local_host()["state"] == "needs-reset-id":
                raise AdapterError("needs_reset_id", "此 host ID 屬於另一個帳號；請使用原帳號或由管理者重設 host 身分。")
            if self.clock() >= self._deadline:
                raise AdapterError("host_connect_timeout", "登入已完成，host 尚未在線，請檢查 supervisor。")
            self.sleep(min(2, max(0, self._deadline - self.clock())))

    def disconnect(self) -> dict:
        server = self.server()
        active = self.config().get("server")
        if active and normalize_server(active) != server:
            raise AdapterError("host_server_mismatch", "取消連結的 server 必須是本機目前設定的 server。")
        self.ensure_host(server)
        host = self.local_host()
        if not host["id"]:
            raise AdapterError("missing_host_id", "目前沒有可取消連結的本機 host ID。")
        token = self.auth.token(server, refresh=False)
        if not token:
            raise AdapterError("needs_login", "無法以目前登入查核主機離線狀態，請先檢查登入。")
        client = self.client_factory(server, True)
        client.hold_token(token)
        self._deadline = self.clock() + self.timeout(default=90)
        hosts = self.api(client, "GET", "/v1/hosts").get("hosts")
        if not isinstance(hosts, list):
            raise AdapterError("invalid_response", "Omnigent hosts 清單格式不正確。")
        self.auth.clear(server)
        while True:
            rows = self.api(client, "GET", "/v1/hosts").get("hosts")
            if not isinstance(rows, list):
                raise AdapterError("invalid_response", "Omnigent hosts 清單格式不正確。")
            matching = next((row for row in rows if isinstance(row, dict) and row.get("host_id") == host["id"]), None)
            if not matching or matching.get("status") == "offline":
                return {"server": server, "host": {"id": host["id"], "name": host["name"], "connected": False},
                        "credentialsCleared": True, "registrationRetained": True, "managerRetained": True}
            if self.clock() >= self._deadline:
                raise AdapterError("host_disconnect_timeout", "本機登入已清除，server 尚未確認此 host 離線。")
            self.sleep(min(2, max(0, self._deadline - self.clock())))

    @staticmethod
    def assistant_sentinel(items: object) -> bool:
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict) or item.get("role") != "assistant" or item.get("type") != "message":
                continue
            content = item.get("content")
            if isinstance(content, list):
                text = "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") in ("output_text", "text") and isinstance(block.get("text"), str))
                if text.strip() == SENTINEL:
                    return True
        return False

    @staticmethod
    def exact_probe_draft(pane: str) -> bool:
        """Match the entire fixed probe in the last framed chat composer.

        Claude can wrap a pasted draft below a blank glyph row. Omnigent
        0.16's draft check only examines the glyph row, missing that case.
        History, menus, partial text and extra draft content never qualify.
        """
        lines = pane.splitlines()
        def rule(line):
            text = line.strip().strip('│┃┌┐└┘├┤╭╮╰╯')
            return len(text) >= 8 and set(text) <= set('─━═╌-')
        boundaries = [i for i, line in enumerate(lines) if rule(line)]
        if len(boundaries) < 2:
            return False
        region = lines[boundaries[-2] + 1:boundaries[-1]]
        if not region or not region[0].strip().startswith('❯'):
            return False
        text = region[0].strip()[1:] + '\n' + '\n'.join(region[1:])
        return ' '.join(text.split()) == PROBE_PROMPT

    def submit_owned_probe_draft(self, session_id: str) -> bool:
        """Recover one unsent health-check draft, never send/approve a dialog."""
        if session_id not in self._owned_probes:
            return False
        try:
            from omnigent.harnesses.claude_native import bridge
            root = Path(bridge._BRIDGE_ROOT)
            for index, file in enumerate(root.glob('*/tmux.json')):
                if index >= 128 or (self._deadline is not None and self.clock() >= self._deadline):
                    return False
                directory = file.parent
                if file.is_symlink() or directory.is_symlink() or directory.stat().st_uid != os.getuid():
                    continue
                if bridge.read_active_session_id(directory) != session_id:
                    continue
                info = read_json(file)
                socket, target = info.get('socket_path'), info.get('tmux_target')
                if not isinstance(socket, str) or not isinstance(target, str) or not socket or not target or target.startswith("-"):
                    continue
                # Two stable captures plus a final read keep torn repaints and
                # a just-delivered prompt from spending Enter on another UI.
                for capture in range(3):
                    if bridge.read_active_session_id(directory) != session_id or bridge.has_pending_user_prompt(directory):
                        return False
                    pane = subprocess.run(['tmux', '-S', socket, 'capture-pane', '-p', '-t', target],
                                          capture_output=True, text=True, timeout=2)
                    text = pane.stdout
                    if pane.returncode or not self.exact_probe_draft(text) or bridge._terminal_dialog_headline(text) \
                            or bridge.auto_mode_billing_notice_visible(text) or bridge._history_search_footer_shown(text):
                        return False
                    if capture < 2:
                        time.sleep(0.15)
                if bridge.read_active_session_id(directory) != session_id or bridge.has_pending_user_prompt(directory):
                    return False
                command = subprocess.run(['tmux', '-S', socket, 'send-keys', '-t', target, 'Enter'],
                                         capture_output=True, timeout=2)
                return command.returncode == 0
        except (ImportError, AttributeError, OSError, subprocess.TimeoutExpired):
            pass
        return False

    def verify(self) -> dict:
        deadline = self.clock() + self.timeout()
        self._deadline = deadline
        key = self.request.get("engine")
        if not isinstance(key, str) or key not in ENGINES:
            raise AdapterError("invalid_engine", "驗證引擎必須是 claude、opencode、codex 或 pi。")
        if not self.engine_inventory()[key]["installed"]:
            raise AdapterError("engine_not_installed", "此原生引擎未安裝，請選擇已安裝的引擎。")
        server = self.server()
        client = self.client_factory(server, False)
        catalog = self.catalog(client=client, read_only=False, engine_key=key)
        engine = catalog["engines"][key]
        if not catalog["host"]["connected"]:
            raise AdapterError("host_offline", "此容器的 Omnigent host 尚未在線。")
        if not engine["agentId"]:
            raise AdapterError("missing_agent", "Server 尚未提供此引擎的原生 agent 模組。")
        if engine["configured"] is not True:
            raise AdapterError("engine_not_ready", "此引擎尚未完成安裝或供應商登入。")
        self.recover_probes(client, server, catalog['host']['id'])
        selected = self.request.get("model")
        if selected is None and not engine['models']:
            raise AdapterError('models_unavailable', '此引擎尚未提供可驗證的模型。')
        if selected is None:
            # A connection check can use a lighter listed model without changing
            # the user's default for ordinary conversations.
            selected = next((row['id'] for row in engine['models'] if key == 'claude' and row['id'] == 'haiku'),
                            next((row['id'] for row in engine['models'] if row.get('isDefault')), engine['models'][0]['id']))
        if selected is not None and (not isinstance(selected, str) or selected not in {row["id"] for row in engine["models"]}):
            raise AdapterError("invalid_model", "選取的模型不在此 host 的實際模型清單內。")
        if self.clock() >= deadline:
            raise AdapterError("verify_timeout", "取得 host 與模型清單已超過驗證等待時間，尚未建立測試對話。")
        body = {"agent_id": engine["agentId"], "host_id": catalog["host"]["id"], "workspace": str(self.workspace),
                "title": f"OpenDesign connection check · {key}", "harness_override": ENGINES[key]["harness"]}
        if selected:
            body["model_override"] = selected
        if key == "claude":
            # The no-tools health probe uses this session's ordinary attended
            # mode. Auto mode can present a first-run dialog that consumes the
            # native bridge's submit key; do not drive or approve that dialog.
            body["terminal_launch_args"] = ["--permission-mode", "default"]
        created = self.api(client, "POST", "/v1/sessions", body)
        session_id = created.get("id") or created.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", session_id):
            raise AdapterError("invalid_session_response", "Server 沒有回傳已建立的驗證對話 ID。")
        self._owned_probes.add(session_id)
        result = {"engine": key, "ok": False, "reply": None, "sessionId": session_id,
                  "url": f"{server}/c/{quote(session_id, safe='')}", "model": selected, "reason": "REPLY_TIMEOUT"}
        try:
            self.record_probe(session_id, {'server': server, 'hostId': catalog['host']['id'], 'title': body['title'], 'engine': key})
            # A create acknowledgement does not prove native runner startup.
            # Wait for this owned runner and let its TUI settle before the one
            # test message. The wait shares the verification deadline and never
            # answers a dialog, retries a prompt, or changes permission policy.
            if key in ENGINES:
                startup_deadline = min(deadline, self.clock() + 45.0)
                while self.clock() < startup_deadline:
                    snapshot = self.api(client, 'GET', f'/v1/sessions/{quote(session_id, safe="")}')
                    if snapshot.get('id') == session_id and snapshot.get('runner_online') is True:
                        break
                    self.sleep(min(1, max(0, startup_deadline - self.clock())))
                else:
                    result['reason'] = 'NATIVE_START_TIMEOUT'
                    return result
                self.sleep(min(3.0, max(0.0, deadline - self.clock())))
                if self.clock() >= deadline:
                    result["reason"] = "NATIVE_START_TIMEOUT"
                    return result
            # Follow the web composer: native TUI harnesses may never consume
            # initial_items passed on creation, so deliver one explicit event.
            self.api(client, "POST", f"/v1/sessions/{quote(session_id, safe='')}/events", {
                "type": "message", "data": {"role": "user", "content": [
                    {"type": "input_text", "text": PROBE_PROMPT}]}}, timeout_seconds=45.0)
            recover_after = self.clock() + 8.0
            while True:
                page = self.api(client, "GET", f"/v1/sessions/{quote(session_id, safe='')}/items?limit=100&order=desc")
                items = page.get("data")
                if not isinstance(items, list):
                    raise AdapterError("invalid_response", "Omnigent 對話項目格式不正確。")
                if self.assistant_sentinel(items):
                    result.update(ok=True, reply=SENTINEL, reason=None)
                    break
                failure = next((item.get("code") for item in items
                                if isinstance(item, dict) and item.get("type") == "error"
                                and isinstance(item.get("code"), str)
                                and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", item["code"])), None)
                if failure:
                    result["reason"] = failure.upper() if failure.upper() in {
                        "NEEDS_LOGIN", "AUTH_REQUIRED", "AUTH_FAILED", "ENGINE_NOT_READY", "NATIVE_AUTH_REQUIRED",
                        "RUNNER_UNAVAILABLE", "MODEL_NOT_FOUND", "RATE_LIMITED", "FREE_USAGE_LIMIT_REACHED",
                        "INSUFFICIENT_QUOTA", "QUOTA_EXCEEDED",
                    } else "ENGINE_FAILED"
                    break
                if self.clock() >= deadline:
                    break
                if key == 'claude' and self.clock() >= recover_after and deadline - self.clock() > 10 \
                        and not result.get('nativeSubmitRecovered'):
                    if self.probe_submit(session_id):
                        result['nativeSubmitRecovered'] = True
                self.sleep(min(2, max(0, deadline - self.clock())))
        except AdapterError as error:
            result["reason"] = error.code
        finally:
            # Cleanup has a separate bounded budget and never refreshes login.
            self._deadline = self.clock() + 20.0
            result["cleanup"] = "stop_failed"
            try:
                self.api(client, "POST", f"/v1/sessions/{quote(session_id, safe='')}/events", {"type": "stop_session", "data": {}},
                         timeout_seconds=10.0, refresh_auth=False)
                result["cleanup"] = "stop_unconfirmed"
                while self.clock() < self._deadline:
                    snapshot = self.api(client, "GET", f"/v1/sessions/{quote(session_id, safe='')}", refresh_auth=False)
                    if snapshot.get("id") == session_id and snapshot.get("runner_online") is False:
                        result["cleanup"] = "stopped"
                        self.record_probe(session_id, None)
                        break
                    self.sleep(min(1, max(0, self._deadline - self.clock())))
            except AdapterError:
                pass
            finally:
                self._owned_probes.discard(session_id)
                self._deadline = None
            result["sentinelVerified"] = result["reply"] == SENTINEL
            if result["cleanup"] != "stopped":
                result["ok"] = False
                result["reason"] = "STOP_FAILED" if result["cleanup"] == "stop_failed" else "STOP_UNCONFIRMED"

        return result


def dispatch(request: dict, **dependencies) -> dict:
    if not isinstance(request, dict):
        raise AdapterError("invalid_request", "請提供 JSON object。")
    action = request.get("action")
    if action not in ("status", "inventory", "catalog", "login", "connect", "verify", "disconnect", "shutdown", "resume"):
        raise AdapterError("invalid_action", "不支援此 Omnigent 操作。")
    original_environment = {key: os.environ.get(key) for key in ("OMNIGENT_CONFIG_HOME", "OMNIGENT_DATA_DIR")}
    plugin = None
    try:
        plugin = Adapter(dict(request), **dependencies)
        if action == "shutdown":
            with bounded_call(23):
                return plugin.shutdown()
        if action == "inventory":
            with bounded_call(30):
                return {"engines": plugin.engine_inventory()}
        if action in ("status", "catalog"):
            plugin._deadline = plugin.clock() + plugin.timeout(default=60)
            with bounded_call(plugin.timeout(default=60)):
                return getattr(plugin, action)()
        plugin.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(plugin.state_dir / "operation.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AdapterError("busy", "另一個 Omnigent 操作正在執行。") from None
            with bounded_call(plugin.timeout() + 30):
                return getattr(plugin, action)()
    finally:
        if plugin is not None:
            plugin.request.pop("password", None)
            plugin.close()
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    # Third-party imports and credential refresh diagnostics stay off the JSON
    # wire. The public error envelope never includes raw exception/HTTP bodies.
    previous_handler = signal.getsignal(signal.SIGTERM)
    def cancelled(_signum, _frame):
        raise AdapterError("operation_cancelled", "Omnigent 操作已取消。")
    signal.signal(signal.SIGTERM, cancelled)
    try:
        if len(sys.argv) != 1:
            raise AdapterError("stdin_required", "此 adapter 只接受 stdin JSON。")
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise AdapterError("request_too_large", "初始化請求過大。")
        request = json.loads(raw)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            data = dispatch(request)
        result = {"ok": True, "data": data}
    except AdapterError as error:
        known = error.code in SAFE_ERROR_CODES or re.fullmatch(r"HTTP_[345][0-9]{2}", error.code)
        result = {"ok": False, "error": {"code": error.code if known else "INTERNAL_ERROR",
                                         "message": error.message if known else "Omnigent 操作失敗。"}}
    except (json.JSONDecodeError, UnicodeError):
        result = {"ok": False, "error": {"code": "INVALID_JSON", "message": "初始化請求不是有效 JSON。"}}
    except (ImportError, ModuleNotFoundError):
        result = {"ok": False, "error": {"code": "MISSING_DEPENDENCY", "message": "Omnigent 套件尚未安裝完整。"}}
    except (Exception, KeyboardInterrupt):
        result = {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "Omnigent 操作失敗；請重新檢查插件設定。"}}
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
