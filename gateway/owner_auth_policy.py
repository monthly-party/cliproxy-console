"""Staged owner-bound Serve authentication policy; not deployed."""
from urllib.parse import urlsplit


def single_header(headers, name):
    if hasattr(headers, 'get_all'):
        values = headers.get_all(name, [])
    else:
        value = headers.get(name, headers.get(name.lower()))
        values = [] if value is None else ([value] if isinstance(value, str) else value)
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


def authorize_owner_request(method, path, peer_ip, headers, owner_login, expected_origin):
    for name in ['Host', 'Tailscale-User-Login', 'Origin', 'Sec-Fetch-Site', 'Content-Type']:
        if hasattr(headers, 'get_all'):
            if len(headers.get_all(name, [])) > 1:
                return False
        else:
            value = headers.get(name, headers.get(name.lower()))
            if value is not None and not isinstance(value, str):
                return False
    origin = urlsplit(expected_origin)
    if origin.scheme != 'https' or not origin.netloc or origin.path or origin.query or origin.fragment:
        return False
    if peer_ip != '127.0.0.1' or not owner_login:
        return False
    if single_header(headers, 'Tailscale-User-Login') != owner_login:
        return False
    if single_header(headers, 'Host') != origin.netloc:
        return False
    supplied_origin = single_header(headers, 'Origin')
    if supplied_origin is not None and supplied_origin != expected_origin:
        return False
    # Navigating to the static page is allowed; API operations must originate
    # in the exact dashboard origin. Browser Fetch Metadata cannot be set by JS.
    if method == 'GET' and path in {'/', '/management.html'}:
        return True
    fetch_site = single_header(headers, 'Sec-Fetch-Site')
    if fetch_site is not None and fetch_site != 'same-origin':
        return False
    if supplied_origin is None and fetch_site != 'same-origin':
        return False
    if method not in {'GET', 'POST', 'DELETE'}:
        return False
    if method in {'POST', 'DELETE'}:
        content_type = single_header(headers, 'Content-Type')
        if not content_type or content_type.split(';', 1)[0].strip().lower() != 'application/json':
            return False
    return True
