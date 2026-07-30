# Outlook connection troubleshooting

Read this when `/connect-outlook` hits a snag, or when the user asks why
Outlook isn't showing up in synced tasks.

## `go` is not installed

Install it, then re-run `outlook_setup_cli.py install-go`:

- macOS: `brew install go`
- Direct download: https://go.dev/dl/

Verify with `go version` before retrying.

## `go install` fails with a network error

Options in order of preference:

**Corporate Go module proxy:**

```bash
export GOPROXY=https://your-mirror.example.com,direct
export GONOSUMCHECK=1   # only if your mirror doesn't serve checksums
```

**TLS-intercepting proxy:**

```bash
export GOFLAGS="-insecure"   # last resort only -- prefer the CA cert below
export SSL_CERT_FILE=/path/to/corp-ca.pem
```

**Air-gapped machine**: `go install` needs network access to fetch the
module the first time. Build on a connected machine instead
(`GOBIN=/tmp/out go install github.com/desek/outlook-local-mcp/cmd/outlook-local-mcp@latest`),
then copy the resulting binary to `~/go/bin/outlook-local-mcp` on the
target machine, or point `OUTLOOK_MCP_BIN` at wherever you copied it.

## Docker fallback (no Go toolchain at all)

Documented here as a manual alternative — `connect-outlook` does not
automate this path. If you'd rather not install Go:

```bash
docker run -i --rm \
  -e OUTLOOK_MCP_AUTH_METHOD=device_code \
  -e OUTLOOK_MCP_READ_ONLY=true \
  -e OUTLOOK_MCP_MAIL_MANAGE_ENABLED=false \
  -v ~/.secretary/outlook:/data/auth \
  -e OUTLOOK_MCP_ACCOUNTS_PATH=/data/auth/accounts.json \
  ghcr.io/desek/outlook-local-mcp
```

You'd then need to point this plugin's `.mcp.json` at a wrapper that
invokes `docker run` instead of the native binary — not built here; treat
this as a starting point if you want to adapt it yourself.

## Device code expired before finishing sign-in

Just retry — ask the agent to call the `account` tool's sign-in operation
again in chat; a fresh code will be issued. Device codes are normally only
valid for a short window (typically well under 30 minutes).

## Signed into the wrong Microsoft account

```bash
python3 "${SKILL_DIR}/scripts/outlook_setup_cli.py" clear
rm ~/.secretary/outlook/accounts.json
```

Then re-run `/connect-outlook` for a fully clean sign-in. `clear` alone only
removes the completion marker (`setup.json`); the token cache itself
(`accounts.json`) is owned by `outlook-local-mcp`, so it's removed
separately and explicitly here rather than by any script in this plugin.

## `claude mcp get "plugin:secretary:outlook"` doesn't show Connected

- Confirm the `secretary` plugin itself is enabled: `claude plugin list`.
- Try an in-session MCP reconnect if your Claude Code version has one
  (e.g. an `/mcp` command), otherwise restart the session.
- Confirm the binary actually resolves:
  `python3 plugins/secretary/scripts/outlook_mcp_server.py --check`.
- If it still won't connect, this may be a Claude Code version limitation
  rather than something in this plugin — check your version with
  `claude --version` before assuming the setup itself is broken.

## Outlook MCP tools report "not connected"/"no account" despite `delegate` status

The connector's local check (`~/.secretary/outlook/accounts.json` +
completion marker) confirmed setup once, but the live MCP session
disagrees — most likely the token was revoked at Microsoft's end, or the
cache file was moved/corrupted since. Re-run `/connect-outlook` to sign in
again; there's no automatic recovery from this, by design (see the
plugin's `outlook.py` connector docstring).
