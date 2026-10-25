# Installed SDK OpenCode regression tests

These are the complete upstream forwarder and reconnect test files (63 tests),
with four quota retry regression cases added to the forwarder file. They use
recording HTTP/OpenCode stubs and exercise the installed forwarder lifecycle;
they do not make model requests. The test files do not replace SDK modules.

The public base is `omnigent-ai/omnigent` commit
`12b5f67d2529b43eab7366a7288fb89f7587e48b` (project version 0.17.0). Both original
test files are byte-identical to the `eee5828` source base. See
[PROVENANCE.json](PROVENANCE.json) for hashes, modifications and validation limits.
The vendored sources are Apache-2.0; upstream [LICENSE](LICENSE) and
[NOTICE](NOTICE) are retained. New quota test additions use the same license.
This Python test bundle is intentionally vendored beside the Python SDK patches.

The acceptance target is the **entire installed Omnigent 0.16.0 SDK** in the
candidate container, with the quota patch applied. A run with one 0.16 module
injected into a 0.17 checkout does not satisfy that acceptance.

## Offline test dependencies

Minimal direct additions are `pytest==9.0.3` and `pytest-asyncio==1.4.0` in
[requirements-test.txt](requirements-test.txt). These are the versions used for
prior regression runs, not claims about the oldest compatible releases.

For Linux / Python 3.12, the full test-tool wheel set is pinned in
[requirements-offline-linux-py312.txt](requirements-offline-linux-py312.txt):
`iniconfig==2.3.0`, `packaging==25.0`, `pluggy==1.6.0`, `Pygments==2.20.0` and
`typing_extensions==4.15.0`, plus the two direct packages. No coverage, xdist,
Playwright, pytest-env or pytest-mock plugin is needed. `httpx` and all Omnigent
runtime dependencies must come from the candidate SDK installation.

The main delivery pipeline can prepare those seven wheels separately. In the
candidate container, after setting `QUOTA_NATIVE_DIR` to this bundle's absolute
path and `QUOTA_WHEELHOUSE` to its prepared wheel directory:

```bash
/opt/omnigent/venv/bin/python -m pip install --no-index \
  --find-links "$QUOTA_WHEELHOUSE" -r "$QUOTA_NATIVE_DIR/requirements-test.txt"
```

This resolves the minimal direct requirements using already installed compatible
SDK dependencies. The full wheel list is for offline availability; it need not
force replacement of compatible shared SDK packages. Do not install the
upstream repository or its development extras.

## Verify the installed target, then run

Run this first in the candidate; stop if an assertion fails. It verifies the
runtime version and rejects module imports outside its installed package.

```bash
/opt/omnigent/venv/bin/python -I -B - <<'PY'
import hashlib
import importlib.metadata
from pathlib import Path
import omnigent.harnesses.opencode_native.forwarder as forwarder
import omnigent.harnesses.opencode_native.client as client
import omnigent.harnesses.opencode_native.bridge as bridge
sdk = importlib.metadata.distribution("omnigent")
assert sdk.version == "0.16.0", "Unexpected SDK version"
root = Path(sdk.locate_file("omnigent")).resolve()
assert "site-packages" in root.parts, "Reject source/editable SDK"
for module in (forwarder, client, bridge):
    source = Path(module.__file__).resolve()
    assert source.is_relative_to(root), "Reject injected source module"
    print(module.__name__, source, hashlib.sha256(source.read_bytes()).hexdigest())
print("installed SDK", sdk.version)
PY
```

Then run both complete files using that same interpreter:

```bash
env -u PYTHONPATH -u PYTEST_ADDOPTS -u PYTEST_PLUGINS \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OMNIGENT_DISABLE_CATALOG_LOOKUP=1 \
  /opt/omnigent/venv/bin/python -I -B -m pytest \
  -p pytest_asyncio.plugin -c "$QUOTA_NATIVE_DIR/pytest.ini" \
  "$QUOTA_NATIVE_DIR/test_opencode_native_forwarder.py" \
  "$QUOTA_NATIVE_DIR/test_opencode_forwarder_reconnect.py" -q
```

`-I` excludes checkout/PYTHONPATH injection. The standalone config uses
`asyncio_mode=auto`, `--noconftest`, and `--import-mode=importlib`; the command
loads only the required asyncio plugin. No upstream conftest or repository
fixtures are loaded, and pytest cache/bytecode writes are disabled.

Expected patched result: **63 passed**, with no skipped or deselected cases.
For a red control, the unpatched 0.16 SDK should fail the three quota-notice
cases; the fourth new case checks that ordinary rate limits can recover.
Do not change a running production SDK just to obtain that red control.

The isolated suite checks event forwarding and lifecycle contracts. A live GUI
quota run is still required to confirm the real `status.message` carries a
recognized structured quota code and that its notice is visible in the UI.
