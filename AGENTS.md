# Agent notes

- This repo is the source of truth for the console. `$CONSOLE_DIR` (from
  `~/.config/cliproxy-console/config.env`) is only a deploy target: don't edit files there, and
  never read or copy the proxy's `private/`, `auth/` or `home/` directories.
- Test with `make test`. Deploy by committing, then `make deploy`. Deploy restarts only the
  console and rolls back if a protected unit's PID changes.
- Never restart the proxy unit as part of console work; clients depend on it.
- Using reset tickets and changing the priority account are real account actions. Don't
  trigger them while testing.
- Machine-specific values (paths, ports, hostnames, logins, unit names) belong in config.env,
  never in tracked files.
- New UI that shows an account email must use `emailHTML(index, fallback, spot)` or `aliasOf`.
