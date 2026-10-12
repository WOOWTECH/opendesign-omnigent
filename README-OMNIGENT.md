# OpenDesign Omnigent native integration

WoowTech extension of upstream `nexu-io/open-design` at `89e64d813bb1c7a11519b3f668f011f7017637d7` (OpenDesign package version 0.23.1).

The existing plugin installer remains responsible for downloading, importing, upgrading and removing packages. The native daemon and React shell provide a fixed Omnigent integration; stock plugin manifests cannot add daemon routes or settings components.

Install [the plugin](plugin/README.md) on the compatible build. A dedicated project and conversation open alongside the private setup dialog. Every fresh installation and re-enable requests a server URL, account and masked password. The integration prepares Omnigent if absent, then connects only supported engines already installed in the container. Existing engine versions and provider credentials remain owned by those engines.

Settings → Integrations provides connection checks, actual engine model selection, Open Omnigent, Repair, Disconnect, Disable and Update. A Ready result requires an actual fixed assistant reply and confirmed stopped verification runner for every installed supported engine. Server, host, installed CLI versions and approving workspace are bound to the receipts. Interrupted probes have a private ownership journal for scoped recovery.

Passwords enter one authenticated same-origin setup request and a dedicated subprocess's stdin. They are transient. Omnigent's official library owns token persistence. Neither plugin inputs, OpenDesign settings autosave, GenUI responses nor chat messages carry credentials.

CLI parity:

```
od omnigent status
od omnigent enable
od omnigent setup
od omnigent verify --engine opencode --model provider/model
od omnigent disable
od omnigent disconnect
od omnigent repair
od omnigent update
```

`setup` accepts a private stdin object with `server`, `username` and `password`; use a hidden interactive reader or secret manager rather than writing credential files or shell history. Run `od omnigent --help` for native authentication, workspace and daemon URL options.

The production derivative recipe is [deploy/omnigent/Dockerfile](deploy/omnigent/Dockerfile). It pins the original platform image, replaces built daemon/UI outputs and reapplies its existing web patches. It preserves the resident host supervisor, agents, runtime dependencies, entrypoint and persistent storage. Deployment must retain the instance's complete current Helm values and native auth; backups and operator desired state are required for rollback.

The upstream Apache-2.0 license remains in LICENSE. This repository adds the Omnigent integration; a successful test suite or login is not a substitute for live engine acceptance on the deployed instance.
