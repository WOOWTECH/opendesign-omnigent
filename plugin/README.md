# Omnigent for OpenDesign

Install through OpenDesign's existing Plugins → Install from source dialog:

```
github:WOOWTECH/opendesign-omnigent@v0.1.3/plugin
```

Requires the WoowTech OpenDesign Omnigent build. A stock OpenDesign plugin cannot add privileged daemon APIs or new React settings components through its manifest; the compatible build provides the fixed native integration, and this package enables its lifecycle.

The setup project and private connection dialog open after installation. Enter your server, account and password afresh. Only agent engines already installed in the container are connected. The dialog shows actual model catalogs and verifies real assistant replies before reporting Ready. Open Omnigent to start your conversation with the selected host, module and model.

Passwords are transient. Official access and refresh tokens are stored by Omnigent in its own private configuration home. Setup credentials do not enter OpenDesign messages, GenUI answers or settings autosave.

Use Disable to stop this integration's checks, Disconnect to remove its selected server login, and Enable to begin a fresh setup. The resident host continues to be supervised by the container's original host-kit.
