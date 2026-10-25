# Omnigent for OpenDesign

## Open the connection settings

In the compatible WoowTech OpenDesign build, go to **Plugins → Omnigent → Open**.
The built-in card appears alongside **MCP Server** and **CLI Terminal**, on both
the default and Installed tabs. It opens `/settings/omnigent` in a new tab;
the URL also supports bookmarks and reloads.

Choose **Enable and set up** (or **Configure**) to create the native setup
conversation and open the private connection form. Enter the server URL,
account, and password afresh. Merely browsing the built-in plugins does not
start a connection or reuse an existing login.

## Install into a compatible build without the built-in card

Use OpenDesign's existing Plugins → Install from source dialog:

```
github:WOOWTECH/opendesign-omnigent@v0.1.4/plugin
```

Requires the WoowTech OpenDesign Omnigent build. A stock OpenDesign plugin cannot add privileged daemon APIs or new React settings components through its manifest; the compatible build provides the native integration, and this imported package enables its lifecycle. Native launcher cards are bundled by the host application; importing a skill package does not install application code.

For an imported package, the setup conversation and private connection dialog open after installation. The integration downloads its private Omnigent dependencies when needed. Only agent engines already installed in the container are connected. The settings page shows model catalogs and verifies real assistant replies before reporting Ready. Each engine needs its own valid provider authentication; an Omnigent login does not supply it. Open Omnigent to start a conversation with the selected host, module and model.

Passwords are transient. Official access and refresh tokens are stored by Omnigent in its own private configuration home. Setup credentials do not enter OpenDesign messages, GenUI answers or settings autosave.

Use Disable to stop this integration's checks, Disconnect to remove its selected server login, and Enable to begin a fresh setup. Update refreshes the private integration runtime; replacing a built-in launcher requires an application update.
