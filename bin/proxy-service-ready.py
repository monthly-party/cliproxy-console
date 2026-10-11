#!/usr/bin/env python3
"""ExecStartPost probe: wait until the proxy answers /v1/models with the client key."""
from pathlib import Path
from urllib.request import Request, urlopen
import os
import time

port = os.environ.get('PROXY_PORT', '8317')
home = Path(os.environ.get('CLIPROXY_HOME', Path.home() / '.local/share/cliproxy'))
key_file = Path(os.environ.get('CLIENT_KEY_FILE', home / 'private/client-key'))
key = key_file.read_text().strip()
for attempt in range(40):
    try:
        with urlopen(Request(f'http://127.0.0.1:{port}/v1/models',
                             headers={'Authorization': 'Bearer ' + key}), timeout=1) as response:
            if response.status == 200:
                print('Proxy ready')
                break
    except Exception:
        time.sleep(.25)
else:
    raise SystemExit('Proxy readiness failed')
