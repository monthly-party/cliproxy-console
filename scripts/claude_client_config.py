"""Keep T3 requests native so the proxy uses the installed Claude client's version.

Only the public Claude header-defaults configuration is read or patched. The
management API saves and reloads it; neither service nor account files are touched.
"""
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
import json
import shutil

CONFIG_PATH = '/v8/management/config/upstream/claude/header-defaults'
MAX_BYTES = 1024 * 1024


class ClaudeConfigurationError(Exception):
    pass


def uses_t3_proxy(settings_path, wrapper_path):
    try:
        settings = json.loads(settings_path.read_text())
    except FileNotFoundError:
        # A console installation without T3 needs no T3-specific configuration.
        return False
    except (OSError, ValueError):
        raise ClaudeConfigurationError('Cannot read the T3 provider settings.') from None
    if not isinstance(settings, dict):
        raise ClaudeConfigurationError('Unexpected T3 provider settings.')
    providers = settings.get('providerInstances', {})
    if not isinstance(providers, dict):
        raise ClaudeConfigurationError('Unexpected T3 provider instances.')
    for provider in providers.values():
        if not isinstance(provider, dict) or provider.get('driver') != 'claudeAgent':
            continue
        if provider.get('enabled', True) is not True:
            continue
        config = provider.get('config', {})
        if not isinstance(config, dict):
            raise ClaudeConfigurationError('Unexpected T3 Claude provider configuration.')
        binary = config.get('binaryPath')
        if not isinstance(binary, str) or not binary.strip():
            continue
        candidate = Path(binary).expanduser()
        if not candidate.is_absolute():
            found = shutil.which(binary)
            if found is None:
                continue
            candidate = Path(found)
        if candidate.resolve() == wrapper_path.resolve():
            # Match the selected executable, including symlinks, rather than a provider's label.
            return True
    return False


def read_claude_defaults(base_url, key):
    request = Request(base_url + CONFIG_PATH, headers={'Authorization': 'Bearer ' + key})
    try:
        with urlopen(request, timeout=10) as response:
            content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ClaudeConfigurationError('Claude compatibility configuration is too large.')
        defaults = json.loads(content)
    except HTTPError as error:
        error.close()
        if error.code == 404:
            # Omitted optional settings are returned as 404 by the proxy.
            return {}
        raise ClaudeConfigurationError('Cannot read Claude compatibility settings.') from None
    except (OSError, URLError, ValueError):
        raise ClaudeConfigurationError('Cannot read Claude compatibility settings.') from None
    if not isinstance(defaults, dict):
        raise ClaudeConfigurationError('Unexpected Claude compatibility configuration.')
    return defaults


def native_entrypoints(defaults):
    entrypoints = defaults.get('native-entrypoints', [])
    if not isinstance(entrypoints, list):
        raise ClaudeConfigurationError('Unexpected Claude native entrypoints configuration.')
    for entrypoint in entrypoints:
        if not isinstance(entrypoint, str):
            raise ClaudeConfigurationError('Unexpected Claude native entrypoints configuration.')
    stabilization = defaults.get('stabilize-device-profile')
    if stabilization is not None and type(stabilization) is not bool:
        raise ClaudeConfigurationError('Unexpected Claude device profile configuration.')
    return entrypoints


def has_t3_entrypoint(entrypoints):
    for entrypoint in entrypoints:
        if entrypoint.strip().lower() == 't3-code':
            # The proxy compares client identifiers without case or outer whitespace.
            return True
    return False


def configure_t3_claude(base_url, key):
    defaults = read_claude_defaults(base_url, key)
    entrypoints = native_entrypoints(defaults)
    recognized = has_t3_entrypoint(entrypoints)
    if recognized and defaults.get('stabilize-device-profile') is not True:
        # Once recognized, future Claude patch versions pass through without config writes.
        return
    updated_entrypoints = list(entrypoints)
    if not recognized:
        updated_entrypoints.append('t3-code')
    payload = {'native-entrypoints': updated_entrypoints, 'stabilize-device-profile': False}
    request = Request(base_url + CONFIG_PATH, data=json.dumps(payload).encode(), method='PATCH', headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise ClaudeConfigurationError('Claude compatibility settings were not saved.')
    except HTTPError as error:
        error.close()
        raise ClaudeConfigurationError(
            'Cannot save T3 compatibility settings. Use CLIProxyAPI 8.0.62 or newer.') from None
    except (OSError, URLError):
        raise ClaudeConfigurationError(
            'Cannot save T3 compatibility settings. Use CLIProxyAPI 8.0.62 or newer.') from None
    saved = read_claude_defaults(base_url, key)
    if not has_t3_entrypoint(native_entrypoints(saved)) or saved.get('stabilize-device-profile') is True:
        raise ClaudeConfigurationError('The proxy did not retain the T3 compatibility settings.')
