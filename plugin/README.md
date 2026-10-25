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
github:WOOWTECH/opendesign-omnigent@v0.1.6/plugin
```

Requires the WoowTech OpenDesign Omnigent build. A stock OpenDesign plugin cannot add privileged daemon APIs or new React settings components through its manifest; the compatible build provides the native integration, and this imported package enables its lifecycle. Native launcher cards are bundled by the host application; importing a skill package does not install application code.

For an imported package, the setup conversation and private connection dialog open after installation. The integration downloads its private Omnigent dependencies when needed. Only agent engines already installed in the container are connected. The settings page shows model catalogs and verifies real assistant replies before reporting Ready. Each engine needs its own valid provider authentication; an Omnigent login does not supply it. Open Omnigent to start a conversation with the selected host, module and model.

## Finish provider sign-in

The integration connects the host and detects installed engines. Provider
sign-in and a successful model reply are separate checks. Use the provider
setup guide in the Omnigent settings section, complete the vendor's native
sign-in on this same OpenDesign host, then return to Refresh and Verify. Do not
paste provider credentials into the setup conversation.

The guide uses the installed native CLI Terminal launcher when available.
Missing engines are not installed. Free models can still have usage limits;
a visible model in the catalog does not establish available inference quota.
When verification receives a structured quota or rate-limit error, it reports
that reason and stops its own test session. A timeout alone is not classified
as a quota error.

The compatible v0.1.6 native host also displays a conversation notice when
OpenCode retries an explicit provider `usage_limit_reached` or
`insufficient_quota` error. Choose **Stop**, then select an available model for
a new conversation or wait for that provider's quota to reset. Temporary rate
limits retain OpenCode's normal retry behavior. This SDK behavior requires the
matching native host build; importing the skill package alone does not replace
an existing host's Python SDK.

In local mode, an enabled connection that completed server login resumes when
the daemon restarts. Workspace-scoped connections resume after a native
workspace request re-establishes that workspace authority. New installations,
disconnected connections and disabled integrations do not auto-connect.

Passwords are transient. Official access and refresh tokens are stored by Omnigent in its own private configuration home. Setup credentials do not enter OpenDesign messages, GenUI answers or settings autosave.

Use Disable to stop this integration's checks, Disconnect to remove its selected server login, and Enable to begin a fresh setup. Update refreshes the private integration runtime; replacing a built-in launcher requires an application update.
