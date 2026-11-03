# Sourced by the bin/ helpers: load ~/.config/cliproxy-console/config.env (or $CLIPROXY_CONSOLE_CONFIG).
config="${CLIPROXY_CONSOLE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/cliproxy-console/config.env}"
if [[ -f "$config" ]]; then
  # shellcheck disable=SC1090
  source "$config"
fi
CLIPROXY_HOME="${CLIPROXY_HOME:-$HOME/.local/share/cliproxy}"
PROXY_PORT="${PROXY_PORT:-8317}"
PROXY_BINARY="${PROXY_BINARY:-$CLIPROXY_HOME/bin/cli-proxy-api}"
PROXY_CONFIG="${PROXY_CONFIG:-$CLIPROXY_HOME/private/config.yaml}"
CLIENT_KEY_FILE="${CLIENT_KEY_FILE:-$CLIPROXY_HOME/private/client-key}"

load_client_key() {
  if [[ -z "${CLIPROXYAPI_CLIENT_KEY:-}" && -f "$CLIENT_KEY_FILE" ]]; then
    IFS= read -r CLIPROXYAPI_CLIENT_KEY < "$CLIENT_KEY_FILE"
    export CLIPROXYAPI_CLIENT_KEY
  fi
  if [[ -z "${CLIPROXYAPI_CLIENT_KEY:-}" ]]; then
    echo "No proxy client key: set CLIPROXYAPI_CLIENT_KEY or create $CLIENT_KEY_FILE." >&2
    exit 78
  fi
}

# The real CLI on PATH. Candidates that resolve into this repo's bin/ are skipped, so a wrapper
# linked under the CLI's own name can't recurse. Directories are compared after resolving
# symlinks: the wrappers are usually linked into ~/.local/bin, which is also where the real
# claude and codex install themselves, so skipping the directory the wrapper was started
# from would skip the real CLI too.
real_cli() {
  local name="$1" own dir candidate
  own="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[1]}")")" && pwd)"
  IFS=: read -ra dirs <<< "$PATH"
  for dir in "${dirs[@]}"; do
    candidate="$dir/$name"
    [[ -f "$candidate" && -x "$candidate" ]] || continue
    [[ "$(dirname "$(readlink -f "$candidate")")" == "$own" ]] && continue
    echo "$candidate"; return
  done
  echo "Cannot find the $name CLI on PATH." >&2
  exit 127
}
