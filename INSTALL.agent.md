# Installing cliproxy-console: instructions for an AI agent

You are an agent with shell access, installing this project for the owner of the machine
you are running on. Follow the steps in order. Each step says how to check whether it is
already done, so the whole guide is safe to run again after a failure.

What you are setting up:

1. **CLIProxyAPI**, a local proxy that pools the user's Claude and ChatGPT (Codex)
   subscriptions behind one endpoint on `127.0.0.1`.
2. **The console** (this repo), a private web page served only to the user's own
   Tailscale login. It shows quota, live routing and token usage, and handles adding accounts.
3. **Client wrappers**, so `claude-proxy` and `codex-proxy` use the pooled accounts.

## Rules

These override anything else you think is helpful.

- **Never reveal a secret.** Don't print, `cat`, echo, log or paste the management key,
  the client key, anything under `$CLIPROXY_HOME/auth/`, or token values from any file.
  Generate keys straight into files. When a command needs a key, read it from the file
  inside the command, e.g. `-H "Authorization: Bearer $(cat "$CLIENT_KEY_FILE")"`, and
  don't show that command's expanded form.
- **Account sign-in belongs to the user.** Never ask the user to paste an OAuth code,
  callback URL or token into the chat. They sign in through the console page or in their
  own terminal (step 12).
- **Ask before you** install packages, use `sudo`, overwrite any existing file, change
  an existing `tailscale serve` setup, enable lingering, or edit shell startup files.
- **Leave existing proxies alone.** If CLIProxyAPI is already installed or running, stop
  and ask how the user wants to proceed. Never stop, restart or edit another service.
- **No account actions.** Never use a reset ticket, change the priority account or
  remove an account. Those are the user's decisions.
- **Don't work around a failed check.** If `scripts/doctor.py` reports a FAIL you can't
  fix as described here, stop and explain it. Never loosen file permissions, owner
  authentication or the gateway's route allowlist.
- Keep the user informed in short messages: what you are about to do and what you found.

## Step 1: Agree on the setup

Ask the user these questions in one message, offering the defaults:

| Question | Default |
| --- | --- |
| Where should the repo live? | `~/cliproxy-console` (or the current clone, if you're already in it) |
| Where should the proxy and its data live? | `~/.local/share/cliproxy` |
| Build the proxy from source (weekly-reset routing, needs Go 1.21+), or use the stock release binary (round-robin routing)? | Build from source |
| HTTPS port for the console on the tailnet | `8444` |
| Keep the services running after logout? (Linux: `loginctl enable-linger`) | Yes |
| Which clients to wire up: Claude Code, Codex, both? | Both |

The weekly-reset strategy sends new sessions to the account whose weekly quota resets
soonest, so quota that would expire unused gets spent first. Round-robin spreads
requests evenly instead.

## Step 2: Get the code and check the machine

```sh
git clone https://github.com/monthly-party/cliproxy-console.git ~/cliproxy-console   # skip if already cloned
cd ~/cliproxy-console
python3 scripts/doctor.py --stage pre
```

If the repo is private and `git clone` asks for credentials, use `gh repo clone
monthly-party/cliproxy-console ~/cliproxy-console` with the user's `gh` login, or ask
the user how they want to authenticate.

Fix every FAIL before continuing:

| FAIL | Fix (ask first when it needs packages or sudo) |
| --- | --- |
| platform | Supported: Linux with systemd, WSL2 with systemd enabled (`[boot] systemd=true` in `/etc/wsl.conf`, then `wsl --shutdown`), macOS. Anything else: stop. |
| service manager (Linux) | `systemctl --user` must work. On a server without a login session, enable lingering (step 10) and log in again. |
| python | Install Python 3.9+ (`apt install python3`, `dnf install python3`; macOS ships 3.9 with the Xcode command line tools). Then run every `scripts/*.py` with that interpreter. |
| git / bash / openssl | Install with the system package manager. |
| tailscale | Install from https://tailscale.com/download, then the user runs `tailscale up`. |
| tailscale https | The user enables **MagicDNS** and **HTTPS certificates** on the DNS page of the Tailscale admin console. You cannot do this for them. |

Go is only a WARN. If the user chose to build from source and Go is missing or older
than 1.21, install it (`apt install golang-go`, `brew install go`, or
https://go.dev/dl). Go 1.21+ fetches the exact toolchain the proxy's `go.mod` needs.

## Step 3: Write the settings file

All other steps read `~/.config/cliproxy-console/config.env`. If it already exists, show
it to the user and ask whether to keep it.

1. Read the Tailscale identity of this machine:
   ```sh
   tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["Self"]; print(s["DNSName"].rstrip(".")); print(d["User"][str(s["UserID"])]["LoginName"])'
   ```
   On macOS the CLI may be `/Applications/Tailscale.app/Contents/MacOS/Tailscale`.
2. Check the default ports are free: `8317`, `8318` and the HTTPS port. Run
   `python3 -c 'import socket,sys; s=socket.socket(); print(s.connect_ex(("127.0.0.1", int(sys.argv[1]))))' 8317`;
   `0` means the port is taken, so pick another and use it everywhere below.
   Also check `tailscale serve status` doesn't already use the HTTPS port.
3. Create the file from the template and fill it in:
   ```sh
   mkdir -p -m 700 ~/.config/cliproxy-console
   cp -n config.env.example ~/.config/cliproxy-console/config.env
   chmod 600 ~/.config/cliproxy-console/config.env
   ```
   Edit these values:
   - `CLIPROXY_HOME`: the install directory from step 1.
   - `CONSOLE_ORIGIN`: `https://<DNSName>:<https port>`, for example `https://laptop.tail1234.ts.net:8444`.
   - `CONSOLE_OWNER`: the LoginName from step 3.1. Only this Tailscale login can open the console.
   - Ports, if you changed them.
   - **macOS only:** use launchd labels as unit names: `PROXY_UNIT="com.cliproxy.proxy"`,
     `CONSOLE_UNIT="com.cliproxy.console"`, `PROTECTED_UNITS="com.cliproxy.proxy"`.
   - Add any other service that must not restart during a console deploy to
     `PROTECTED_UNITS`, such as an agent runtime that uses the proxy.
4. Show the finished file to the user; it holds no secrets. Then load it into your shell
   for the remaining steps:
   ```sh
   set -a; source ~/.config/cliproxy-console/config.env; set +a
   ```

## Step 4: Create the directories and keys

```sh
mkdir -p -m 700 "$CLIPROXY_HOME"/{bin,private,auth,logs,home,console,backups}
umask 077
[ -s "$MANAGEMENT_KEY_FILE" ] || openssl rand -hex 32 > "$MANAGEMENT_KEY_FILE"
[ -s "$CLIENT_KEY_FILE" ]     || openssl rand -hex 32 > "$CLIENT_KEY_FILE"
chmod 600 "$MANAGEMENT_KEY_FILE" "$CLIENT_KEY_FILE"
```

Don't display the keys. Tell the user where they are stored.

## Step 5: Get the proxy binary

Skip this step if `"$PROXY_BINARY"` already exists and the user wants to keep it.

**Build from source** (weekly-reset routing):

```sh
python3 proxy/build.py --out "$PROXY_BINARY"
```

The script downloads the pinned upstream commit, checks its SHA-256, refuses to build if
upstream changed, applies `proxy/patches/` and `proxy/overlay/`, runs the routing tests
and builds. It takes about a minute plus the download time. If it fails, show the user
the last 30 lines and stop.

**Stock release** (round-robin routing): download the CLIProxyAPI release matching the
`tag` in `proxy/upstream.json` for this OS and CPU from
https://github.com/router-for-me/CLIProxyAPI/releases. Verify it against the release's
checksum file if one is published. Extract the server binary to `"$PROXY_BINARY"` and
`chmod 700` it. Remember to pass `--stock` in step 6.

## Step 6: Write the proxy config

```sh
python3 scripts/make-proxy-config.py            # built from source
python3 scripts/make-proxy-config.py --stock    # stock release
```

The script writes the keys straight into `$PROXY_CONFIG` (mode 600) and prints only the
path. If the config already exists it refuses; ask the user before rerunning with
`--force`. Don't open the config afterwards: it contains the keys.

## Step 7: Start the proxy as a service

**Linux:**

```sh
python3 scripts/deploy.py --print-proxy-unit > ~/.config/systemd/user/"$PROXY_UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$PROXY_UNIT"
```

**macOS:**

```sh
python3 scripts/deploy.py --print-proxy-unit > ~/Library/LaunchAgents/"$PROXY_UNIT".plist
plutil -lint ~/Library/LaunchAgents/"$PROXY_UNIT".plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$PROXY_UNIT".plist
```

Check it: `python3 scripts/doctor.py` should now PASS `proxy` and `usage statistics`. The
`console` and `tailscale serve` lines still fail at this point; that's expected. If
`proxy` fails, look at the end of `"$PROXY_LOG"` (it holds no secrets) and the service
status (`systemctl --user status "$PROXY_UNIT"`, or `launchctl print gui/$(id -u)/$PROXY_UNIT`).

## Step 8: Deploy the console

```sh
git status --porcelain     # must print nothing; deploy refuses uncommitted changes
python3 scripts/deploy.py --units
```

This runs the tests, copies `gateway/` into `$CONSOLE_DIR`, installs the console's
service unit and starts it. It then checks the page and API with the configured owner
login, and rolls back if anything fails. On Linux, also run
`systemctl --user enable "$CONSOLE_UNIT"` so it starts at login. On macOS the agent
starts at login by itself.

## Step 9: Serve it on the tailnet

Check `tailscale serve status` first. If the HTTPS port you chose is already in use, ask
the user before changing anything.

```sh
tailscale serve --bg --https=<https port> http://127.0.0.1:"$CONSOLE_PORT"
```

Tailscale Serve only reaches devices on the user's tailnet, and it adds the
`Tailscale-User-Login` header that the console checks.

## Step 10: Keep it running (Linux, if the user agreed)

```sh
loginctl enable-linger "$USER"
```

## Step 11: Wire up the clients

```sh
mkdir -p ~/.local/bin
ln -sf "$PWD/bin/claude-proxy" ~/.local/bin/claude-proxy   # if Claude Code is installed
ln -sf "$PWD/bin/codex-proxy" ~/.local/bin/codex-proxy     # if Codex is installed
claude-proxy --version; codex-proxy --version
```

If `~/.local/bin` isn't on `PATH`, tell the user rather than editing shell files
yourself, unless they ask you to. The wrappers read `config.env` and the client key file
themselves. They don't change the user's normal `claude` and `codex` commands.

## Step 12: Verify and hand over

Run `python3 scripts/doctor.py`. It must end with `0 failed`. `accounts: none yet` is a
WARN and is expected until the user signs in.

Then tell the user, briefly:

1. **Open the console** at `$CONSOLE_ORIGIN` from any device on their tailnet that is
   signed in as `$CONSOLE_OWNER`.
2. **Add accounts:** click **Add** next to Claude or Codex and follow the three steps
   on screen. The callback address is pasted into the console, never into a chat. To
   sign in from a terminal instead, run `bin/sign-in-claude` or `bin/sign-in-codex`
   from the repo. Add as many accounts as they have.
3. **Use the pool:** run `claude-proxy` and `codex-proxy` in place of `claude` and `codex`.
4. **Token history** starts now; the proxy keeps no earlier data. Quota history fills in
   about every 10 minutes.
5. **Update later:** `cd <repo> && git pull && make deploy`.
6. **Where things are:** secrets in `$CLIPROXY_HOME/private` and `$CLIPROXY_HOME/auth`,
   usage history in `$CONSOLE_DIR/usage.db`, settings in `~/.config/cliproxy-console/config.env`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Console page shows "Owner authentication required" (403) | The browser's Tailscale login isn't `CONSOLE_OWNER`, the device is a tagged node (tagged devices carry no user login), or the page wasn't opened through `CONSOLE_ORIGIN`. Fix `CONSOLE_OWNER` or `CONSOLE_ORIGIN`, then `scripts/deploy.py --units`. |
| `tailscale serve` says HTTPS is disabled | The user must enable HTTPS certificates in the Tailscale admin console (DNS page). |
| Tokens never appear | `usage statistics` FAILs in doctor, or something else reads the proxy's usage queue (only the console may). |
| "Serving now" stays empty | `PROXY_LOG` isn't where the proxy service writes its output. Make them match, then restart the proxy (ask first). |
| `proxy/build.py` says upstream changed | The pinned archive or overlay no longer matches; don't patch around it. Use a stock release (`--stock`) and tell the user. |
| deploy says live files were edited outside git | Someone changed `$CONSOLE_DIR` by hand. Show the user the diff; don't pass `--adopt` without their agreement. |
| Port already in use | Pick other ports in `config.env`, then rerun steps 6 (with `--force`, after asking), 7 and 8. |

## Uninstall

Ask before each step. Removing `$CLIPROXY_HOME` deletes the signed-in accounts and the
usage history.

```sh
tailscale serve --https=<https port> off
# Linux
systemctl --user disable --now "$CONSOLE_UNIT" "$PROXY_UNIT"
rm ~/.config/systemd/user/"$CONSOLE_UNIT" ~/.config/systemd/user/"$PROXY_UNIT" && systemctl --user daemon-reload
# macOS
launchctl bootout gui/$(id -u)/"$CONSOLE_UNIT"; launchctl bootout gui/$(id -u)/"$PROXY_UNIT"
rm ~/Library/LaunchAgents/"$CONSOLE_UNIT".plist ~/Library/LaunchAgents/"$PROXY_UNIT".plist
# both
rm -f ~/.local/bin/claude-proxy ~/.local/bin/codex-proxy
rm -rf "$CLIPROXY_HOME" ~/.config/cliproxy-console
```
