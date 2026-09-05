"""Format-detector package - see registry.py for the priority-ordered list."""

from .entities import extract_entities
from .generic_fallback import detect_generic_fallback
from .registry import DETECTORS, JSON_DETECTORS

__all__ = ['DETECTORS',
    'JSON_DETECTORS', 'detect_generic_fallback', 'extract_entities']
