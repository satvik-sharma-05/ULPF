"""
base_service.py - Shared lifecycle for all pipeline service roles.

Handles checkpointing and graceful start/stop logging so producer_service.py
and consumer_service.py don't each reimplement it.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from core.config import PIPELINE_CONFIG

logger = logging.getLogger(__name__)


class BaseService:
    service_name = "Service"

    def __init__(self):
        self.running = True
        self.processed_count = 0
        self.error_count = 0
        self.start_time = datetime.now(timezone.utc)
        self.checkpoint_file = PIPELINE_CONFIG['checkpoint_file']
        self._load_checkpoint()

    def request_stop(self):
        self.running = False

    def run(self):
        raise NotImplementedError

    def shutdown(self):
        self._save_checkpoint()
        uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        logger.info(
            f"{self.service_name} stopped. Processed: {self.processed_count}, "
            f"Errors: {self.error_count}, Uptime: {uptime:.0f}s"
        )

    def _checkpoint_extra(self) -> Dict[str, Any]:
        """Subclasses can override to persist extra state (e.g. last DNIF offset)."""
        return {}

    def _save_checkpoint(self):
        try:
            os.makedirs(os.path.dirname(self.checkpoint_file) or '.', exist_ok=True)
            data = {
                'service': self.service_name,
                'processed_count': self.processed_count,
                'error_count': self.error_count,
                'last_checkpoint': datetime.now(timezone.utc).isoformat(),
                'uptime_seconds': (datetime.now(timezone.utc) - self.start_time).total_seconds(),
            }
            data.update(self._checkpoint_extra())
            with open(self.checkpoint_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not write checkpoint file: {e}")

    def _load_checkpoint(self):
        if not os.path.exists(self.checkpoint_file):
            return
        try:
            with open(self.checkpoint_file, 'r') as f:
                data = json.load(f)
            if data.get('service') == self.service_name:
                self.processed_count = data.get('processed_count', 0)
                logger.info(f"Loaded previous checkpoint: {self.processed_count} items previously processed.")
        except Exception as e:
            logger.warning(f"Could not load checkpoint file: {e}")
