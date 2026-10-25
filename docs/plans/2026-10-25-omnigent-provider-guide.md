# Omnigent provider setup and quota errors

## Scope

Extend the existing native Omnigent settings section. Reuse Omnigent's deployed
new-chat setup dialog and the installed OD CLI Terminal launcher. Only present
commands for installed engines. Preserve readiness and real-reply acceptance.

## Implementation

1. Show one collapsible provider guide only after confirmed login, online host,
   host name and installed engines are available. Display the target host name.
2. Open the configured server's existing home page. Instruct users to select
   this host, then use Set up / Set up auth when offered. The reference source's
   `/settings/harnesses` route rendered General on the actual deployed frontend;
   do not generate a link to that unverified route or guess host query parameters.
3. Resolve the native terminal from the current workspace plugin catalog and
   reuse `builtInLaunchPath`. Do not guess a terminal path. Ignore stale account
   results and user-supplied launcher manifests. Copy known CLI sign-in commands;
   never execute them or collect provider secrets in the plugin guide.
4. Preserve structured FREE_USAGE_LIMIT_REACHED, INSUFFICIENT_QUOTA,
   QUOTA_EXCEEDED and RATE_LIMITED errors. Show distinct translated guidance,
   stop only owned verification sessions and retain unknown-error redaction.
   Do not infer quota from timeout or mark unauthenticated engines ready.

## Evidence and checks

- Real free OpenCode CLI request: its own log reported FREE_USAGE_LIMIT_REACHED.
  Endpoint DNS/TLS checks passed. No further inference request was sent after
  finding this limit. This does not establish that Omni emitted that error event.
- Real Omnigent homepage: target host selection and Claude Set up auth expose
  blank password-type API key and gateway fields; empty Save is disabled.
  No provider credential was submitted. Codex/Pi form completion is unverified.
- Python adapter loop regression covers immediate failure, one prompt, one
  owned stop, no raw message leakage and unknown-error fallback. Service tests
  cover receipt persistence and restart. Frontend tests cover quota copy,
  launcher trust, lazy load, account changes, installed engines and Traditional
  Chinese. Integration tests cover absent/disabled/offline connection states.
- Run package-scoped tests/typechecks, guard, production builds, then deploy
  only the authorized service. Verify card → settings → guide → real terminal,
  deep-link reload, mobile layout and reconnect after deployment.

CLI guidance was checked against installed help. Codex device sign-in is also
documented in [official authentication guidance](https://learn.chatgpt.com/docs/auth).
A real assistant reply still requires authorized provider access and available
quota; host connectivity and model listings alone do not complete acceptance.
