# OpenCode pre-launch discovery: proposed OpenDesign-only SDK patch

Confirmed defect: installed Omnigent 0.16.0 `HostProcess._handle_model_options`
has no `opencode-native` branch. The canonical native OpenCode harness reaches
the unsupported-harness result before any CLI discovery. The SDK already
contains the official OpenCode CLI catalog parser and native agent/harness.
This patch wires that existing parser into the host RPC dispatcher.

The original dispatcher fails the offline catalog regression. The proposed
replacement passes all five focused tests. It has **not been applied** to the
target, main checkout, or local SDK. Main owns the derivative image and rollout.

## Runtime evidence, 2026-10-12 UTC

All target commands used read-only `kubectl exec` into namespace
`paas-ws-7948b087`, pod `svc-61d5c868-open-design-0`, container `open-design`.
Python ran with `-B`; no chats, configuration edits, engine installations,
restarts, or deployment actions were performed.

| Probe | Result |
| --- | --- |
| `opencode --version` | 1.18.33, exit 0, 0.975 s |
| `opencode models --help` | exit 0, 1.340 s; `--refresh` supported |
| `opencode models` | exit 0, 2.824 s; 390 output lines, no stderr |
| Official `list_opencode_cli_model_options(refresh=False, timeout=10)` | 390 model rows, 2.775–2.987 s |
| `GET https://omnigent.woowtech.io/health` | HTTP 200 JSON, 0.586 s |
| Read-only owned-host lookup | HTTP 200 JSON, online, 0.344 s |
| Owned-host OpenCode model-options GET | HTTP 502, zero models, 0.424–0.605 s |

The only host queried was `8da85eaaeb104d50a9f784e646e86265`, obtained from the
`host.host_id` config metadata. Requests used official
`omnigent.cli_auth.load_token(server)` in memory following Main's fresh GUI
login. No token refresh/write, credential-file inspection, token output, raw
HTTP errors, or environment values were used. Only fixed categories, timings,
counts, and sanitized model/provider identifiers were reported.

The model endpoint produced both HTML and generic JSON 502 responses. Neither
contained an identifiable SDK error in the sanitized classification. Changing
`httpx` proxy inheritance (`trust_env=True/False`) did not resolve it. The
installed dispatch defect is independently proven; the transport responses do
not establish that it is the only live defect. No proxy configuration change
is proposed.

The installed version bounds are `>=1.17.7,<1.19.0`, and its version checker
accepts 1.18.33. The dispatcher failure occurs before invoking OpenCode, and
the successful CLI measurements fit comfortably below the RPC deadline.
Neither a CLI 1.18 version mismatch nor a cold CLI discovery timeout explains
the missing pre-launch branch.

All 390 rows have printable, whitespace-free identifiers. Providers are
`opencode` and `openrouter`. Eighteen OpenRouter IDs contain `~`; these are
legitimate model names, not parser noise. Examples observed:

- `opencode/big-pickle`
- `opencode/exo-free`
- `openrouter/~anthropic/claude-sonnet-latest`
- `openrouter/aion-labs/aion-2.0`

## Exact contract and change

Installed host RPC request: `HostModelOptionsFrame(request_id, harness)`.
Response: `HostModelOptionsResultFrame(request_id, status, models, error,
routable_models)`. Canonicalization occurs before dispatch. Installed server
`server/routes/hosts.py` gives this RPC 15 seconds and converts a host response
with `status != "ok"` into HTTP 502; timeout becomes HTTP 504.

The patch adds one branch inside `_handle_model_options`, immediately before
the existing Pi branch. It uses official `config.load_effective_config()` and
`harness_startup_config.resolve_harness_command("opencode-native", ...)` so
configured executable precedence remains SDK-owned. Discovery uses the
unchanged official `list_opencode_cli_model_options` parser: full
`provider/model` IDs, `providerID`, `model`, `displayName`, `name`, `isDefault`.
Existing `_with_model_configuration_source` supplies source metadata.

The exact CLI invocation is `[resolved_executable, "models"]`. It does not
force `--refresh`, start `opencode serve`, create a session, or install a CLI.
The SDK's `subprocess.run(timeout=10)` supplies subprocess timeout/kill/wait
cleanup; the host awaits its worker with a 12-second deadline, leaving three
seconds before the server RPC limit. Async cancellation propagates. Cancelling
`asyncio.to_thread` does not terminate the thread; its CLI remains bounded by
the independent subprocess timeout. All discovery exceptions become a fixed
failure string without logging CLI stderr or exception detail. Empty catalogs
also fail honestly.

## Artifacts and hash pin

- `baseline/host/connect.py`: verified static original, unchanged.
- `baseline/harnesses/opencode_native/app_server.py`: verified unchanged
  official parser used by offline tests.
- `replacement/host/connect.py`: complete proposed SDK replacement.
- `opencode-host-model-options.patch`: one-branch unified diff.
- `manifest.json`: exact target path, baseline/replacement SHA-256 and budgets.
- `test_opencode_discovery.py`: five offline tests using extracted production
  dispatcher/parser ASTs and mocked external seams.
- `DIAGNOSIS.md`: this report.

Target file:
`/opt/omnigent/venv/lib/python3.12/site-packages/omnigent/host/connect.py`

Baseline SHA-256:
`1093a7c89de966c39e598f683b2651073abf613152e4e875676135bd28138ad1`

Replacement SHA-256:
`2de8af76672e8008731c7fe4500eec059cd0d54b68091a8b364f631af911d75e`

Main should reject a differing baseline rather than overwrite an unknown SDK.
The runtime baseline was rechecked after preparing the replacement and still
matched. Only the designated OpenDesign derivative needs the replacement.

## Verification commands/results

From this artifact directory:

```sh
OD_OPENCODE_SOURCE=baseline python3 -B -m unittest -v test_opencode_discovery.DiscoveryTests.test_supported_opencode_catalog
# exit 1: expected regression, no parser/resolver call for native OpenCode
python3 -B -m unittest -v test_opencode_discovery
# exit 0: 5 tests passed in 0.324 seconds
```

The tests cover full IDs including `~` and nested slashes, canonical/alias
dispatch, configured executable forwarding, refresh disabled, safe CLI
failures/empty results, both timeout budgets, cancellation propagation, and
unchanged Claude/Codex dispatch. The scope test verifies that removing the new
branch produces the exact original module AST. Both replacement and test file
also compiled successfully in memory; manifest hashes matched.

## Remaining validation and unsupported guarantees

The existing RPC is explicitly an ambient pre-launch preview. A session that
pins a provider resolves its own catalog at launch. This patch does not invent
provider gateway bindings, modify per-session model allowlists, or alter native
OpenCode transport/agent contracts. Enumerated CLI catalog IDs alone do not
prove provider authentication, billable routing, or an assistant sentinel.

After Main builds/applies the replacement, repeat the owned-host model-options
GET and require HTTP 200 with nonempty actual model IDs. The live 502 may need
separate investigation if it persists. Actual OpenCode sentinel execution and
stop cleanup remain Main's separately authorized verification; this task
created no session. No live success claim is made here.
