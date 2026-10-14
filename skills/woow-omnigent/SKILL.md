---
name: woow-omnigent
description: Use when a user requests Omnigent setup, connection checks, or reconnection in OpenDesign.
---

# Omnigent connection

Use OpenDesign's native Omnigent connection dialog in Plugins or Settings → Integrations.
Installing this plugin creates a setup project and conversation. The dialog requests a fresh server URL, account and masked password. Ask the user to enter credentials in that dialog; credentials belong exclusively in the private form.

The integration discovers engines already installed in this container. Setup connects the existing resident Omnigent host and verifies a fixed assistant reply and stopped test runner for each supported installed engine. A successful login alone is insufficient evidence that an engine can chat.

Once the dialog reports Ready, use Open Omnigent to choose this host, an installed engine module and its model, then start a conversation. Engine provider sign-in remains owned by the installed engine. When an engine needs sign-in, explain the reported status and use its native sign-in interface.

Re-enabling or reinstalling requires fresh connection input. Repair rechecks the current connection; Update uses the installed plugin's native upgrade action. Disconnect removes only this selected server's local login entry and confirms this host offline. Host registration and other servers remain owned by Omnigent.

If this OpenDesign build does not contain the native Omnigent integration, report that it requires the WoowTech OpenDesign Omnigent distribution. Use the documented compatible build before collecting any connection information.
