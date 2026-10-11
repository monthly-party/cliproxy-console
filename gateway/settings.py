"""Where the proxy listens. The gateway unit sets PROXY_PORT from config.env."""
import os

PROXY_HOST = '127.0.0.1'
PROXY_PORT = int(os.environ.get('PROXY_PORT', '8317'))
PROXY_BASE = f'http://{PROXY_HOST}:{PROXY_PORT}'
