---
name: connect-outlook
description: |
  One-time setup that connects Outlook to the secretary: installs
  outlook-local-mcp (github.com/desek/outlook-local-mcp, MIT) directly from
  upstream, confirms it's registered as this plugin's MCP server, and walks
  the user through the one-time Microsoft device-code sign-in. After this
  completes, the tasks skill automatically checks Outlook alongside Slack
  whenever it syncs -- no further setup, no separate "check Outlook"
  command.

  Use this skill whenever the user wants to connect Outlook, set up email
  sync, sign in to their Microsoft account for the secretary, or asks why
  Outlook isn't showing up in their synced tasks. Trigger on phrases like:
  "connect Outlook", "set up Outlook sync", "sign in to Outlook", "why
  isn't Outlook working", "add my email to the secretary".
---

# Connect Outlook

Installs and authenticates `outlook-local-mcp` so the `tasks` skill's
auto-sync can read Outlook mail/calendar alongside Slack. This is a
one-time, mostly-interactive setup — not something that runs automatically,
unlike the recurring Slack/Outlook *check* the `tasks` skill already does
on its own.

## Before starting: say this plainly

Real Outlook access is granted to a **small, unaffiliated, third-party
open-source binary** (`desek/outlook-local-mcp`, MIT licensed) running as a
local subprocess on this machine — not a Microsoft- or Anthropic-run
service. Tell the user this directly before doing anything:
- It requests **read-only** mail/calendar access (this plugin hardcodes
  `OUTLOOK_MCP_READ_ONLY=true`, mail management disabled) — it cannot send,
  delete, or modify anything, by design.
- The signed-in session token is cached locally at
  `~/.secretary/outlook/accounts.json` — a plaintext file whose containing
  directory is restricted to the user, but whose own permissions are the
  third-party binary's responsibility, not this plugin's.
- Installing it means running `go install` against a small open-source
  project's source — a normal thing to do, but worth being aware of before
  granting it access to a real inbox.

Ask if they want to proceed before continuing.

## Phase 1 — Check current state

```bash
python3 "${SKILL_DIR}/scripts/outlook_setup_cli.py" status
```

Reports `binary_found`/`binary_path`, `go_on_path`, `accounts_present`,
`marker_present`, `setup_complete`. If `setup_complete` is already `true`,
tell the user Outlook is already connected and ask if they want to
re-verify (Phase 3) rather than redoing everything.

## Phase 2 — Install the binary

If `binary_found` is `false`:
- If `go_on_path` is `false`: point the user at https://go.dev/dl/ or
  `brew install go`, then stop and wait — don't attempt the install without
  a working `go`.
- Otherwise, run:
  ```bash
  python3 "${SKILL_DIR}/scripts/outlook_setup_cli.py" install-go
  ```
  This runs `go install github.com/desek/outlook-local-mcp/cmd/outlook-local-mcp@latest`
  and reports `installed`/`binary_path` or a clear `error`. The binary
  lands at `~/go/bin/outlook-local-mcp` — no PATH changes needed; this
  plugin's own `.mcp.json` wrapper (`scripts/outlook_mcp_server.py`) finds
  it there directly.
- **Alternative (documented, not automated here)**: Docker —
  `docker run -i --rm -e OUTLOOK_MCP_TENANT_ID=... ghcr.io/desek/outlook-local-mcp`
  — mention this only if the user specifically doesn't want to install Go;
  don't run it yourself.

## Phase 3 — Confirm the MCP server is connected

```bash
claude mcp get "plugin:secretary:outlook"
```

Look for `Connected`. If it's missing or not connected: this plugin's
`.mcp.json` registers automatically once the plugin is enabled, but a
newly-installed binary or a freshly-enabled plugin may need the session to
notice it — tell the user to try reconnecting (an `/mcp` command, if their
Claude Code version has one) or, failing that, restart their session and
re-check. Don't assert confidently which one is needed; just suggest both
in that order.

## Phase 4 — Sign in (conversational — this cannot be scripted)

This is the one part of setup no script can do: only the agent, live in
chat, can call an MCP tool and relay its result to the user.

1. Call the `account` tool with `{"operation": "help"}` first — **don't
   guess the exact sign-in verb name**, read back what it actually returns.
2. Call whatever the real sign-in/add-account verb is.
3. The tool's result will contain a device code and a verification URL
   (e.g. "go to https://microsoft.com/devicelogin and enter code XXXX-XXXX").
   **Relay this verbatim** to the user — don't paraphrase or summarize away
   the code.
4. Tell the user you'll wait for them to complete it in their browser, then
   ask them to confirm when done.
5. Once they confirm, call the `account` tool again (status/list operation)
   to verify a signed-in account is actually present — don't just take
   their word for it.

## Phase 5 — Record completion

Once Phase 4's verification confirms a real signed-in account:
```bash
python3 "${SKILL_DIR}/scripts/outlook_setup_cli.py" mark-complete --verified-via "account status check"
```
Then run `status` once more and confirm `setup_complete` is `true` (this
requires BOTH the marker just written AND a non-trivial
`~/.secretary/outlook/accounts.json` — if it's still `false`, something in
Phase 4 didn't actually complete; don't report success prematurely).

Tell the user plainly: "Outlook is connected — I'll check it automatically
alongside Slack from now on, no need to ask."

## Edge cases

- **Already connected, user runs this again**: Phase 1 catches this —
  offer to just re-verify (Phase 3) rather than reinstalling/re-signing-in.
- **`go install` fails** (network, proxy, permissions): report the CLI's
  `error` field verbatim; point at the Docker alternative if it looks like
  a persistent network issue.
- **User signs into the wrong Microsoft account**: `outlook_setup_cli.py
  clear` removes the completion marker (not the token cache itself,
  which `outlook-local-mcp` owns) so setup can be re-run cleanly; mention
  they may also want to remove `~/.secretary/outlook/accounts.json`
  directly if they want a fully clean re-sign-in.
- **Device code expires before the user finishes**: just re-run Phase 4's
  sign-in call — it's fine to retry the interactive step itself; the
  guidance against retry-looping is specifically about the automatic
  per-sync path in `tasks`, not this one-time interactive setup.
- **`claude mcp get` never shows Connected even after reconnect/restart**:
  tell the user plainly rather than guessing further — this may need
  checking the Claude Code version's MCP support directly.
