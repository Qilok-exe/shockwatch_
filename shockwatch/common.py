"""Shared serialization, time and configuration conventions."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re
import numpy as np
import pandas as pd

DEFAULTS = {
    "schema_version": "1", "feature_version": "0.1.0",
    "decision_delay_minutes": 10, "max_quote_age_seconds": 900,
    "max_relative_spread": .50, "min_quote_coverage": .60,
    "min_training_rows": 120, "ridge_penalty": 10., "ewma_daily_lambda": .94,
    "policy": {"extreme_flow": 5., "extreme_price": 5., "moderate_flow": 2.,
               "price_support": 2., "cusum_drift": .5, "cusum_trigger": 8.,
               "oi_support": 2.5, "clear_score": 2., "escalation_multiplier": 2.,
               "clear_trading_minutes": 390, "target_episodes_per_week": 1.},
    "notifications": {"enabled": False, "telegram": False, "email_fallback": True, "daily_email": True},
    "max_live_alert_age_minutes": 90, "daily_digest_utc_hour": 23, "synthetic": False
}

def merge(a, b):
    result = copy.deepcopy(a)
    for k, v in b.items():
        result[k] = merge(result[k], v) if isinstance(v, dict) and isinstance(result.get(k), dict) else copy.deepcopy(v)
    return result

def config(path=None):
    return merge(DEFAULTS, json.loads(Path(path).read_text())) if path else merge({}, DEFAULTS)

def utc(value):
    if isinstance(value, str) and not re.search(r"(?:Z|[+-]\d\d:\d\d)$", value):
        raise ValueError(f"Timestamp requires explicit timezone: {value!r}")
    t = pd.Timestamp(value)
    if t.tzinfo is None or pd.isna(t):
        raise ValueError("Timestamp must be non-null and timezone aware")
    return t.tz_convert("UTC")

def iso(value):
    return utc(value).isoformat()

def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return iso(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value

def dumps(value):
    return json.dumps(clean(value), sort_keys=True, separators=(",", ":"), allow_nan=False)

def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()

def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean(value), indent=2, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)

def number(row, key, default=float("nan")):
    v = row.get(key)
    return default if v is None else float(v)

def finite(v):
    return v is not None and bool(np.isfinite(v))
