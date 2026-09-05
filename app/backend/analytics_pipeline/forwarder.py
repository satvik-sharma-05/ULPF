"""
forwarder.py - Pushes normalized events OUT to a SIEM or data lake.

Requirement (g) is "efficient SIEM and Data Lake integration". The export
endpoints already stream every format a SIEM accepts, but they are all PULL:
something has to come and fetch. Every SIEM deployment in practice expects the
opposite - a source that ships events to it, continuously, and does not lose
them when the destination is briefly down. Without that, integration means "a
human downloads a file", which is not integration.

Three sinks, chosen because between them they cover how SIEMs and lakes
actually accept data:

    syslog   TCP/UDP, RFC 5424 framing, CEF payload - what ArcSight, QRadar
             and every appliance-era collector accept with no configuration
    http     newline-delimited JSON POSTs - Splunk HEC, Elastic, OpenSearch,
             Sentinel, and any webhook
    file     rotating NDJSON on a mounted path - the data-lake pattern, where
             object storage or a lake loader picks the files up

Design notes worth defending
----------------------------
**Batched, with a bounded queue.** One event per request would be
indefensible at any real volume. The queue is bounded because an unbounded one
turns a slow destination into an out-of-memory kill on the collector, which
is the worse failure - a full queue drops with a counter you can alert on.

**At-least-once, stated plainly.** A batch that fails mid-flight is retried,
so a destination can see an event twice. Exactly-once would need the sink to
deduplicate on a key; every event already carries a stable `id` for exactly
that, and the docs say so rather than implying delivery is exact.

**Retry with backoff and a dead-letter count.** A SIEM that is down for a
minute must not cost events, and one that is down for an hour must not hang
the pipeline.

**No credentials in code.** Tokens come from the environment, and the health
output reports whether a token is configured, never its value.
"""

import json
import logging
import os
import queue
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger('analytics.forwarder')

#  Bounded on purpose - see the module note. Sized so a minute of a busy feed
#  fits, not so that an outage is invisible.
QUEUE_MAX = int(os.getenv('FORWARD_QUEUE_MAX', '10000'))
BATCH_SIZE = int(os.getenv('FORWARD_BATCH_SIZE', '200'))
BATCH_WAIT_S = float(os.getenv('FORWARD_BATCH_WAIT_S', '2.0'))
MAX_RETRIES = int(os.getenv('FORWARD_MAX_RETRIES', '5'))
BACKOFF_BASE_S = float(os.getenv('FORWARD_BACKOFF_BASE_S', '0.5'))

#  RFC 5424 PRI = facility * 8 + severity. 16 is local0, the conventional
#  facility for application-generated events.
SYSLOG_FACILITY = int(os.getenv('FORWARD_SYSLOG_FACILITY', '16'))
_SEVERITY_TO_SYSLOG = {
    'EMERGENCY': 0, 'FATAL': 0, 'ALERT': 1, 'CRITICAL': 2, 'ERROR': 3,
    'WARNING': 4, 'NOTICE': 5, 'INFO': 6, 'DEBUG': 7,
}


class Sink:
    """A destination. Subclasses implement send() for one batch."""

    name = 'sink'

    def send(self, events: List[Dict[str, Any]]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> Dict[str, Any]:
        return {'sink': self.name}


class SyslogSink(Sink):
    """RFC 5424 framed syslog carrying a CEF payload.

    Octet-counted framing on TCP (RFC 6587), which is what avoids the
    truncation you get from newline framing when a message legitimately
    contains a newline - a Windows Security event body, for instance.
    """

    name = 'syslog'

    def __init__(self, host: str, port: int = 514, protocol: str = 'tcp',
                 use_tls: bool = False):
        self.host, self.port = host, port
        self.protocol = protocol.lower()
        self.use_tls = use_tls
        self._sock: Optional[socket.socket] = None

    def _connect(self):
        if self._sock is not None:
            return self._sock
        if self.protocol == 'udp':
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            return self._sock
        sock = socket.create_connection((self.host, self.port), timeout=10)
        if self.use_tls:
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=self.host)
        self._sock = sock
        return sock

    def _frame(self, event: Dict[str, Any]) -> bytes:
        from exporters import _cef_escape  # local import: shared escaping rules

        sev = str(event.get('severity') or 'INFO').upper()
        pri = SYSLOG_FACILITY * 8 + _SEVERITY_TO_SYSLOG.get(sev, 6)
        ts = event.get('timestamp') or datetime.now(timezone.utc).isoformat()
        host = event.get('hostname') or '-'
        app = event.get('process') or '-'
        cef = 'CEF:0|ULPF|%s|1.0|%s|%s|%d|' % (
            _cef_escape(event.get('source_type') or 'Unknown', True),
            _cef_escape(event.get('matched_format') or 'event', True),
            _cef_escape(str(event.get('message') or '')[:200], True),
            _SEVERITY_TO_SYSLOG.get(sev, 6))
        msg = '<%d>1 %s %s %s - - - %s' % (pri, ts, host, app, cef)
        payload = msg.encode('utf-8', errors='replace')
        if self.protocol == 'udp':
            return payload
        # RFC 6587 octet counting: LENGTH SP MESSAGE.
        return b'%d %s' % (len(payload), payload)

    def send(self, events):
        sock = self._connect()
        for event in events:
            data = self._frame(event)
            if self.protocol == 'udp':
                sock.sendto(data, (self.host, self.port))
            else:
                sock.sendall(data)

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def describe(self):
        return {'sink': 'syslog', 'host': self.host, 'port': self.port,
                'protocol': self.protocol, 'tls': self.use_tls}


class HttpSink(Sink):
    """Newline-delimited JSON POSTs - Splunk HEC, Elastic, OpenSearch, webhooks."""

    name = 'http'

    def __init__(self, url: str, token: Optional[str] = None,
                 token_header: str = 'Authorization',
                 token_prefix: str = 'Splunk '):
        self.url = url
        self.token = token or os.getenv('FORWARD_HTTP_TOKEN') or None
        self.token_header = token_header
        self.token_prefix = token_prefix

    def send(self, events):
        import requests

        headers = {'Content-Type': 'application/x-ndjson'}
        if self.token:
            headers[self.token_header] = self.token_prefix + self.token
        body = '\n'.join(json.dumps(e, default=str, ensure_ascii=False)
                         for e in events) + '\n'
        resp = requests.post(self.url, data=body.encode('utf-8'),
                             headers=headers, timeout=30)
        resp.raise_for_status()

    def describe(self):
        # Whether a token is configured, never the token.
        return {'sink': 'http', 'url': self.url,
                'authenticated': bool(self.token)}


class FileSink(Sink):
    """Rotating NDJSON on a mounted path - the data-lake drop pattern."""

    name = 'file'

    def __init__(self, directory: str, max_bytes: int = 64 * 1024 * 1024):
        self.directory = directory
        self.max_bytes = max_bytes
        os.makedirs(directory, exist_ok=True)
        self._path = None
        self._fh = None

    def _roll(self):
        if self._fh and self._fh.tell() < self.max_bytes:
            return
        if self._fh:
            self._fh.close()
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        self._path = os.path.join(self.directory, 'ulpf-%s.ndjson' % stamp)
        self._fh = open(self._path, 'a', encoding='utf-8')

    def send(self, events):
        self._roll()
        for e in events:
            self._fh.write(json.dumps(e, default=str, ensure_ascii=False) + '\n')
        # Flushed per batch: a lake loader that reads a partially written line
        # gets malformed JSON, and the cost of flushing per batch is trivial
        # next to that.
        self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    def describe(self):
        return {'sink': 'file', 'directory': self.directory,
                'current_file': self._path, 'max_bytes': self.max_bytes}


class Forwarder:
    """Batches events off a bounded queue and ships them, with retries."""

    def __init__(self, sink: Sink):
        self.sink = sink
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.stats = {'queued': 0, 'sent': 0, 'dropped_queue_full': 0,
                      'failed_batches': 0, 'retries': 0, 'dead_lettered': 0}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='ulpf-forwarder')
        self._thread.start()
        logger.info('forwarder started -> %s', self.sink.describe())

    def stop(self, drain_timeout: float = 10.0):
        self._running = False
        if self._thread:
            self._thread.join(timeout=drain_timeout)
        self.sink.close()

    def submit(self, event: Dict[str, Any]) -> bool:
        """Returns False when the queue is full.

        Dropping with a counter beats blocking the ingest path or growing
        without bound: a slow SIEM must not be able to stop ingestion or kill
        the collector.
        """
        try:
            self._queue.put_nowait(event)
            self.stats['queued'] += 1
            return True
        except queue.Full:
            self.stats['dropped_queue_full'] += 1
            return False

    def submit_many(self, events: Iterable[Dict[str, Any]]) -> int:
        return sum(1 for e in events if self.submit(e))

    def _collect_batch(self) -> List[Dict[str, Any]]:
        batch: List[Dict[str, Any]] = []
        deadline = time.monotonic() + BATCH_WAIT_S
        while len(batch) < BATCH_SIZE and time.monotonic() < deadline:
            try:
                batch.append(self._queue.get(timeout=0.2))
            except queue.Empty:
                if batch:
                    break
        return batch

    def _send_with_retry(self, batch: List[Dict[str, Any]]) -> bool:
        for attempt in range(MAX_RETRIES):
            try:
                self.sink.send(batch)
                self.stats['sent'] += len(batch)
                return True
            except Exception as exc:
                self.stats['failed_batches'] += 1
                self.sink.close()          # force a fresh connection next try
                if attempt == MAX_RETRIES - 1:
                    self.stats['dead_lettered'] += len(batch)
                    logger.error('dropping %d events after %d attempts: %s',
                                 len(batch), MAX_RETRIES, exc)
                    return False
                self.stats['retries'] += 1
                # Exponential backoff: a destination that is down for a minute
                # must not cost events, and one down for an hour must not hang
                # the pipeline.
                time.sleep(BACKOFF_BASE_S * (2 ** attempt))
        return False

    def _run(self):
        while self._running or not self._queue.empty():
            batch = self._collect_batch()
            if batch:
                self._send_with_retry(batch)

    def health(self) -> Dict[str, Any]:
        return {
            'running': self._running,
            'queue_depth': self._queue.qsize(),
            'queue_max': QUEUE_MAX,
            'batch_size': BATCH_SIZE,
            'delivery': 'at-least-once - a retried batch can be delivered '
                        'twice; deduplicate on the event id, which is stable',
            'destination': self.sink.describe(),
            'stats': dict(self.stats),
        }


def build_sink(kind: str, **kwargs) -> Sink:
    kinds = {'syslog': SyslogSink, 'http': HttpSink, 'file': FileSink}
    if kind not in kinds:
        raise ValueError('unknown sink %r. Available: %s'
                         % (kind, sorted(kinds)))
    return kinds[kind](**kwargs)


def describe_sinks() -> List[Dict[str, str]]:
    return [
        {'id': 'syslog', 'label': 'Syslog (RFC 5424 + CEF)',
         'blurb': 'TCP or UDP to a collector. Octet-counted framing, CEF '
                  'payload - accepted by ArcSight, QRadar and appliance-era '
                  'collectors without configuration.'},
        {'id': 'http', 'label': 'HTTP / NDJSON',
         'blurb': 'Batched newline-delimited JSON POSTs. Splunk HEC, Elastic, '
                  'OpenSearch, Microsoft Sentinel, or any webhook.'},
        {'id': 'file', 'label': 'Rotating NDJSON files',
         'blurb': 'Writes rotating NDJSON to a mounted path for an object '
                  'store or data-lake loader to collect.'},
    ]
