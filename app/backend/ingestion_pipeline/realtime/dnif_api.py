"""
dnif_api.py - DNIF API Receiver & Poller

Fetches raw logs from DNIF API endpoint or, when DNIF isn't configured yet,
falls back to replaying real lines from DNIF_REPLAY_PATH (the local corpus by
default) so the realtime path is exercised with the same NSX/vCenter/ESXi/Aria/
vROps/Windows shapes the parser actually has to handle, rather than invented
ones.
"""

import os
import time
import glob
import requests
import logging
import random
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from core.config import DNIF_CONFIG, PIPELINE_CONFIG
from core.multiline import reassemble

logger = logging.getLogger(__name__)

# Replay source when DNIF isn't configured. Points at the same corpus the batch
# pipeline reads, so the realtime path exercises real log shapes rather than
# invented ones - searched recursively, since the corpus nests one level deep.
_REPLAY_DIR = DNIF_CONFIG.get('replay_path', './logs')


class DNIFClient:
    def __init__(self):
        self.api_url = DNIF_CONFIG['dnif_api_url'].rstrip('/')
        self.api_key = DNIF_CONFIG['dnif_api_key']
        self.tenant = DNIF_CONFIG['dnif_tenant']
        self.poll_interval = DNIF_CONFIG['poll_interval']
        self.batch_size = DNIF_CONFIG['batch_size']
        self.timeout = DNIF_CONFIG['timeout']
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'X-Tenant-Id': self.tenant,
        }

    def fetch_logs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch raw logs from DNIF API endpoint with fallback to test data generation."""
        if PIPELINE_CONFIG.get('test_mode') or self.api_url == 'http://your-dnif-server:8080':
            return self.generate_synthetic_logs(count=limit or 5)

        endpoint = f"{self.api_url}/api/v1/logs/events"
        params = {
            'limit': limit or self.batch_size,
            'source': 'vRLI',
            'status': 'UNPARSED'
        }

        try:
            response = requests.get(
                endpoint,
                headers=self.headers,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            events = data.get('events', data.get('data', []))
            logger.info(f"Fetched {len(events)} logs from DNIF API")
            return events
        except Exception as e:
            logger.warning(f"DNIF API fetch failed ({e}). Falling back to synthetic log generator.")
            return self.generate_synthetic_logs(count=limit or 3)

    def generate_synthetic_logs(self, count: int = 5) -> List[Dict[str, Any]]:
        """DNIF isn't configured yet, so replay real sample records (NSX,
        vCenter, ESXi, Aria Automation, vROps, Windows Security auditing,
        ...) from DNIF_REPLAY_PATH instead of fabricating logs from a domain
        (hardware vendor alerts) this environment doesn't actually have.
        Falls back to a single generic line if that path is missing or empty
        (e.g. a deployment shipped without the corpus)."""
        records = self._load_fixture_records()
        if not records:
            records = ['2026-01-01T00:00:00.000Z unknown-host UNKNOWN 0 - - DNIF not configured and no sample fixtures found']

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        synthetic_batch = []
        for _ in range(count):
            synthetic_batch.append({
                'raw': random.choice(records),
                'source': 'DNIF_POLLER_SAMPLE_REPLAY',
                'received_at': timestamp,
                'tenant': self.tenant
            })
        return synthetic_batch

    # The replay source is the full 33GB corpus, so this is hard-capped: a
    # replay pool only needs enough variety to exercise every detector, not
    # every line of every export held in memory for the process's lifetime.
    _MAX_REPLAY_RECORDS = 2000

    def _load_fixture_records(self) -> List[str]:
        records: List[str] = []
        if not os.path.isdir(_REPLAY_DIR):
            return records
        # Recursive: the corpus is logs/<export>.txt.tar/export1.txt, but a flat
        # folder of .txt files works the same way.
        paths = sorted(glob.glob(os.path.join(_REPLAY_DIR, '**', '*.txt'), recursive=True))
        for path in paths:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for record in reassemble(f):
                    records.append(record)
                    if len(records) >= self._MAX_REPLAY_RECORDS:
                        return records
        return records
