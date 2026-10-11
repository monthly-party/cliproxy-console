"""Which account is serving right now, read from the proxy's own log.

The proxy logs one selector line when it picks an account for a request and one
gin_logger line when that request finishes, both tagged with the same request id.
A request is in flight between the two. Sticky sessions come from the same selector
lines. Session ids are hashed on read; nothing from the log is written anywhere.
"""
from datetime import datetime
from pathlib import Path
import hashlib
import re
import threading
import time

SELECT = re.compile(r'^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[(?P<rid>[0-9a-f]{8})\] \[info \] \[selector\.go:\d+\] '
                    r'session-affinity: (?P<kind>[a-z ,]+?) \| session=(?P<session>\S+) .*?auth=(?P<auth>[^\s/\\]+\.json) '
                    r'provider=\S+ model=(?P<model>\S+)')
DONE = re.compile(r'^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[(?P<rid>[0-9a-f]{8})\] \[\w+\s*\] \[gin_logger\.go:\d+\] '
                  r'(?P<status>\d{3}) \|')
STALE_SECONDS = 30 * 60        # an unmatched start older than this lost its completion line
SESSION_WINDOW = 30 * 60       # sessions seen this recently count as bound to the account
BACKFILL_BYTES = 4 * 1024 * 1024


def stamp(text):
    # The proxy writes naive local wall-clock time.
    return time.mktime(datetime.strptime(text, '%Y-%m-%d %H:%M:%S').timetuple())


class LiveActivity:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        self.offset = None
        self.inode = None
        self.anchor = b''
        self.partial = b''
        self.inflight = {}
        self.sessions = {}
        self.last_start = {}
        self.last_done = {}

    def read(self):
        """Consume new log bytes. Starts near the end so a restart sees current work."""
        try:
            info = self.path.stat()
        except OSError:
            return
        size = info.st_size
        if self.offset is not None and info.st_ino == self.inode and size >= self.offset and self.anchor:
            # Same file and not shorter: still restart if the bytes before our offset changed
            # (truncated and refilled between two reads).
            with self.path.open('rb') as source:
                source.seek(self.offset - len(self.anchor))
                if source.read(len(self.anchor)) != self.anchor:
                    self.offset = None
        if self.offset is None or info.st_ino != self.inode or size < self.offset:
            self.inode = info.st_ino
            self.offset = max(0, size - BACKFILL_BYTES)
            self.partial = b''
            self.anchor = b''
            skip_first = self.offset > 0
        else:
            skip_first = False
        if size == self.offset:
            self.expire()
            return
        with self.path.open('rb') as source:
            source.seek(self.offset)
            chunk = source.read(min(size - self.offset, 8 * 1024 * 1024))
        self.offset += len(chunk)
        self.anchor = (self.anchor + chunk)[-64:]
        lines = (self.partial + chunk).split(b'\n')
        self.partial = lines.pop()
        if skip_first and lines:
            lines.pop(0)
        with self.lock:
            for raw in lines:
                self.feed(raw.decode('utf-8', 'replace'))
        self.expire()

    def feed(self, line):
        if '[selector.go:' in line:
            match = SELECT.match(line)
            if not match:
                return
            started = stamp(match['ts'])
            session = hashlib.sha256(match['session'].encode()).hexdigest()[:12]
            # A retry re-selects under the same request id; the latest pick is the one serving.
            self.inflight[match['rid']] = {'auth': match['auth'], 'model': match['model'][:96], 'started': started,
                                           'session': session, 'binding': match['kind'].strip()}
            self.sessions[session] = (match['auth'], started)
            self.last_start[match['auth']] = started
        elif '[gin_logger.go:' in line:
            match = DONE.match(line)
            if match and match['rid'] in self.inflight:
                request = self.inflight.pop(match['rid'])
                self.last_done[request['auth']] = stamp(match['ts'])

    def expire(self):
        now = self.clock()
        with self.lock:
            for rid in [r for r, v in self.inflight.items() if now - v['started'] > STALE_SECONDS]:
                del self.inflight[rid]
            for session in [s for s, (_, seen) in self.sessions.items() if now - seen > SESSION_WINDOW]:
                del self.sessions[session]

    def snapshot(self):
        """Per auth-file view: requests in flight, bound sessions, last activity."""
        now = self.clock()
        with self.lock:
            view = {}
            for request in self.inflight.values():
                entry = view.setdefault(request['auth'], {'in_flight': 0, 'requests': [], 'sessions': 0})
                entry['in_flight'] += 1
                entry['requests'].append({'model': request['model'], 'running_seconds': max(0, round(now - request['started'])),
                                          'new_binding': 'miss' in request['binding'], 'fallback': 'fallback' in request['binding']})
            for auth, _ in self.sessions.values():
                view.setdefault(auth, {'in_flight': 0, 'requests': [], 'sessions': 0})['sessions'] += 1
            for auth in set(self.last_start) | set(self.last_done):
                entry = view.setdefault(auth, {'in_flight': 0, 'requests': [], 'sessions': 0})
                entry['last_started_at'] = self.last_start.get(auth)
                entry['last_finished_at'] = self.last_done.get(auth)
            for entry in view.values():
                entry['requests'].sort(key=lambda r: -r['running_seconds'])
                entry['requests'] = entry['requests'][:8]
            return view
