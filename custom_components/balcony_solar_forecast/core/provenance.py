"""Compact calculation identity; never stores coordinates or learner samples."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime

MODEL_CONTRACT = 'postclip-dc-ac-v2-positive-sky'
_HASH_KEYS = {'config_sha256', 'learner_sha256', 'weather_sha256'}
_TEXT_KEYS = {'model_contract', 'integration_version', 'basis'}


def digest(value: object) -> str:
    def encode(item: object) -> object:
        if isinstance(item, datetime):
            return item.isoformat()
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        raise TypeError('Unsupported provenance value')
    raw = json.dumps(value, default=encode, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def bounded_provenance(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    out = {}
    for key, item in value.items():
        if not isinstance(item, str):
            continue
        if key in _HASH_KEYS and re.fullmatch(r'[a-f0-9]{64}', item) or key in _TEXT_KEYS and re.fullmatch(r'[A-Za-z0-9._-]{1,80}', item):
            out[key] = item
        elif key == 'weather_fetched_at':
            try:
                stamp = datetime.fromisoformat(item)
            except ValueError:
                continue
            if stamp.tzinfo is not None:
                out[key] = stamp.isoformat()
    return out or None
