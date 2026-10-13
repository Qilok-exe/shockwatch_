"""Future-only immutable forecasts and independently matured outcome revisions.

The legacy engine's reconstructed current-interval and close-to-close quantities
remain diagnostics. All metrics in this module refer to stored target identities.
"""
from __future__ import annotations
import json
import math
import pandas as pd
from .common import clean, digest, finite, iso, utc
from .features import session_intervals
from .models import har_forecast

INTERVAL_TARGET = "next_complete_interval_v1"
INTRADAY_TARGET = "next_session_intraday_v1"
FORECAST_REQUIRED = {"forecast_id", "dataset_id", "mode", "run_id", "instrument_id", "model_id", "policy_id",
    "feature_version", "target_definition_version", "asof_at", "input_watermark", "issued_at", "recorded_at",
    "target_start", "target_end", "horizon_id", "component", "predicted_variance", "forecast_status",
    "quality_reasons", "training_cutoff", "source_version", "target_id"}

def validate_forecast(record):
    record = clean(dict(record))
    if not FORECAST_REQUIRED.issubset(record):
        raise ValueError(f"Forecast missing fields: {sorted(FORECAST_REQUIRED-set(record))}")
    if record["mode"] not in {"synthetic", "historical", "prospective"}:
        raise ValueError("Unknown forecast mode")
    for key in ("asof_at", "input_watermark", "issued_at", "recorded_at", "target_start", "target_end", "training_cutoff"):
        record[key] = iso(record[key])
    if not utc(record["input_watermark"]) <= utc(record["asof_at"]) <= utc(record["issued_at"]) <= utc(record["target_start"]) < utc(record["target_end"]):
        raise ValueError("Fully future forecast violates availability/issuance/target ordering")
    if utc(record["training_cutoff"]) > utc(record["asof_at"]):
        raise ValueError("Training cutoff is after forecast information cutoff")
    if record["forecast_status"] not in {"issued", "unavailable"}:
        raise ValueError("Invalid forecast status")
    value = record["predicted_variance"]
    if record["forecast_status"] == "issued" and (not finite(value) or value <= 0):
        raise ValueError("Issued variance must be finite and positive")
    if record["forecast_status"] == "unavailable" and value is not None:
        raise ValueError("Unavailable forecast must not contain a prediction")
    if record["mode"] == "prospective" and utc(record["recorded_at"]) < utc(record["issued_at"]):
        raise ValueError("Live forecast cannot be recorded before actual issuance")
    if record["mode"] != "prospective" and record.get("simulated_issued_at") != record["issued_at"]:
        raise ValueError("Replay/synthetic forecast requires explicit simulated issuance")
    definitions = {"next_complete_interval": INTERVAL_TARGET, "next_session_intraday": INTRADAY_TARGET}
    if definitions.get(record["horizon_id"]) != record["target_definition_version"]:
        raise ValueError("Unsupported or inconsistent forecast target definition")
    if record["component"] != "intraday":
        raise ValueError("Initial fully future forecasts support intraday variance only")
    return record

def first_complete_interval(calendar, issued_at):
    issued_at = utc(issued_at)
    return next((x for x in session_intervals(calendar) if x["interval_start"] >= issued_at), None)

def _forecast(row, model, policy, target, value, method, horizon, version, issued_at, recorded_at, run_id, dataset_id, mode, reasons=()):
    start, end = iso(target["interval_start"]), iso(target["interval_end"])
    target_id = digest([dataset_id, row["instrument_id"], start, end, "intraday", version])[:32]
    forecast_id = digest([dataset_id, model["model_id"], digest(policy)[:16], row["instrument_id"],
                          iso(row["decision_at"]), iso(issued_at), target_id, method])[:32]
    receipt = utc(row.get("source_ready_at") or row["decision_at"])
    asof = max(utc(row["decision_at"]), receipt)
    watermark = max(utc(row.get("input_watermark") or row.get("available_at") or row["decision_at"]), receipt)
    input_reasons = row.get("quality_reasons") or ""
    input_reasons = input_reasons.split(";") if isinstance(input_reasons, str) else list(input_reasons)
    record = {"forecast_id": forecast_id, "dataset_id": dataset_id, "mode": mode, "run_id": run_id,
        "instrument_id": row["instrument_id"], "model_id": model["model_id"], "policy_id": digest(policy)[:16],
        "feature_version": model["feature_version"], "target_definition_version": version,
        "asof_at": iso(asof), "origin_decision_at": iso(row["decision_at"]),
        "origin_interval_end": iso(row["interval_end"]) if row.get("interval_end") else None, "input_row_hash": digest(row),
        "input_watermark": iso(watermark), "source_receipt_at": iso(receipt),
        "issued_at": iso(issued_at), "recorded_at": iso(recorded_at), "target_start": start, "target_end": end,
        "horizon_id": horizon, "component": "intraday", "method": method,
        "predicted_variance": value if finite(value) and value > 0 else None,
        "forecast_status": "issued" if finite(value) and value > 0 else "unavailable",
        "quality_reasons": [x for x in input_reasons+list(reasons) if x], "training_cutoff": model["training_cutoff"],
        "source_version": model.get("source_version", "legacy-panel-v1"), "target_id": target_id,
        "waiting_gap_minutes": (utc(start)-utc(issued_at)).total_seconds()/60,
        "timing_assumption": "actual_processing_time" if mode == "prospective" else "declared_virtual_processing_clock",
        "coverage_definition": "all_configured_intervals_with_complete_five_minute_underlying_returns"}
    if mode != "prospective":
        record["simulated_issued_at"] = record["issued_at"]
    return validate_forecast(record)

def build_forecasts(row, state, model, policy, calendar, *, dataset_id, mode, issued_at, recorded_at, run_id):
    """Use post-observation frozen EWMA state; never select an already-started target."""
    row = clean(dict(row))
    issued_at = utc(issued_at)
    m = model["symbols"][row["instrument_id"]]
    records = []
    target = first_complete_interval(calendar, issued_at)
    if target:
        seasonal = m["seasonality"].get(str(target["slot"]))
        value = state["ewma_level"]*seasonal*target["minutes"] if seasonal is not None else None
        reasons = [] if seasonal is not None else ["target_slot_not_in_frozen_training_seasonality"]
        records.append(_forecast(row, model, policy, target, value, "seasonal_ewma", "next_complete_interval",
                                 INTERVAL_TARGET, issued_at, recorded_at, run_id, dataset_id, mode, reasons))
    if row.get("is_session_end") and model.get("intraday_target_definition_version") == INTRADAY_TARGET:
        sessions = calendar.sort_values("open_utc").to_dict("records")
        next_day = next((x for x in sessions if str(x["session"]) > str(row["session"])), None)
        if next_day and utc(next_day["open_utc"]) < issued_at:
            next_day = None
        if next_day:
            target = {"interval_start": utc(next_day["open_utc"]), "interval_end": utc(next_day["close_utc"])}
            history = [float("nan") if x is None else x for x in state.get("intraday_history", [])]
            har = har_forecast({"har": m.get("intraday_har")}, history)
            slots = [x for x in session_intervals(calendar) if x["session"] == str(next_day["session"])]
            seasonal_complete = all(str(x["slot"]) in m["seasonality"] for x in slots)
            seasonal_daily = state["ewma_level"]*sum(m["seasonality"][str(x["slot"])]*x["minutes"] for x in slots) if seasonal_complete else None
            for method, value in (("seasonal_intraday_ewma", seasonal_daily), ("intraday_har", har)):
                reasons = [] if finite(value) and value > 0 else ["insufficient_complete_intraday_training_or_history"]
                records.append(_forecast(row, model, policy, target, value, method, "next_session_intraday",
                                         INTRADAY_TARGET, issued_at, recorded_at, run_id, dataset_id, mode, reasons))
    return records

def issue_forecasts(store, row, state, model, policy, calendar, issued_at, recorded_at, run_id):
    identity = store.identity()
    records = build_forecasts(row, state, model, policy, calendar, **identity,
                              issued_at=issued_at, recorded_at=recorded_at, run_id=run_id)
    return sum(store.insert_forecast(record) for record in records)

def register_episode_windows(store, events, calendar, issued_at, recorded_at):
    identity = store.identity()
    sessions = [x for x in calendar.sort_values("open_utc").to_dict("records") if utc(x["open_utc"]) >= utc(issued_at)]
    count = 0
    for event in events:
        if event["type"] != "opened":
            continue
        for length, horizon in ((1, "next_session"), (5, "five_sessions")):
            if len(sessions) < length:
                continue
            selected = sessions[:length]
            window = {**identity, "window_id": digest([identity["dataset_id"], event["episode_id"], horizon])[:32],
                "episode_id": event["episode_id"], "instrument_id": event["instrument_id"],
                "horizon_id": horizon, "issued_at": iso(issued_at), "recorded_at": iso(recorded_at),
                "target_start": iso(selected[0]["open_utc"]), "target_end": iso(selected[-1]["close_utc"]),
                "sessions": [str(x["session"]) for x in selected],
                "component_definition": "first_open_to_last_close_intraday_plus_intervening_overnight",
                "target_definition_version": "episode_open_to_close_windows_v1",
                "context_window_start": iso(issued_at), "public_event_window_definition": "issuance_through_target_end",
                "interpretation": "descriptive_forward_activity_no_directional_trade_or_win_label"}
            count += store.insert_episode_window(window)
    return count

def _target_rows(record, panel, calendar, now):
    start, end, now = utc(record["target_start"]), utc(record["target_end"]), utc(now)
    intervals = calendar if isinstance(calendar, list) else session_intervals(calendar)
    expected = [x for x in intervals if start <= x["interval_start"] and x["interval_end"] <= end]
    whole = bool(expected) and expected[0]["interval_start"] == start and expected[-1]["interval_end"] == end
    if record.get("horizon_id") == "next_complete_interval":
        whole = whole and len(expected) == 1
    elif record.get("horizon_id") == "next_session_intraday":
        whole = whole and len({x["session"] for x in expected}) == 1 and expected[0]["slot"] == 0 and expected[-1]["is_session_end"]
    if isinstance(panel, dict):
        candidates = panel.get(record["instrument_id"], {})
        candidate_rows = [candidates[iso(x["interval_end"])] for x in expected if iso(x["interval_end"]) in candidates]
    else:
        candidate_rows = panel
    rows = {}
    for row in candidate_rows:
        row = clean(dict(row))
        if row["instrument_id"] != record["instrument_id"]:
            continue
        if not (start <= utc(row["interval_start"]) and utc(row["interval_end"]) <= end):
            continue
        available = max(utc(row["decision_at"]), utc(row.get("available_at") or row["decision_at"]), utc(row.get("input_watermark") or row["decision_at"]), utc(row.get("source_ready_at") or row["decision_at"]))
        if available <= now:
            key = iso(row["interval_end"])
            if key in rows:
                raise ValueError("Duplicate target observation requires an explicit source revision")
            rows[key] = row
    good, reasons = [], []
    for interval in expected:
        row = rows.get(iso(interval["interval_end"]))
        if row is None:
            reasons.append("missing_or_not_yet_available_interval:"+iso(interval["interval_end"]))
        elif not finite(row.get("rv")) or row["rv"] < 0 or str(row.get("underlying_complete", True)).lower() not in {"true", "1"}:
            reasons.append("incomplete_underlying_interval:"+iso(interval["interval_end"]))
        elif utc(row["interval_start"]) != interval["interval_start"] or float(row["minutes"]) != interval["minutes"]:
            reasons.append("interval_definition_mismatch:"+iso(interval["interval_end"]))
        else:
            good.append(row)
    status = "invalid_target" if not whole else "pending" if now < end else "complete" if len(good) == len(expected) else "incomplete_data"
    return status, expected, good, reasons

def calculate_forecast_outcome(record, panel, calendar, now, source_revision="first_release"):
    """Pure evaluator shared by persistent workers and frozen experiment packets."""
    status, expected, good, reasons = _target_rows(record, panel, calendar, now)
    complete = status == "complete"
    variance = sum(float(x["rv"]) for x in good) if complete else None
    available = max([max(utc(x["decision_at"]), utc(x.get("available_at") or x["decision_at"]), utc(x.get("input_watermark") or x["decision_at"]), utc(x.get("source_ready_at") or x["decision_at"])) for x in good], default=None) if complete else None
    prediction = record.get("predicted_variance")
    valid_loss = complete and record.get("forecast_status") == "issued" and finite(prediction) and prediction > 0
    loss = {"qlike": float(math.log(prediction)+variance/prediction), "squared_error": float((variance-prediction)**2),
            "absolute_error": float(abs(variance-prediction))} if valid_loss else None
    return {"forecast_id": record["forecast_id"], "dataset_id": record["dataset_id"], "mode": record["mode"],
        "instrument_id": record["instrument_id"], "horizon_id": record["horizon_id"],
        "target_start": record["target_start"], "target_end": record["target_end"],
        "status": status, "observed_variance": variance, "target_coverage": len(good)/len(expected) if expected else 0.,
        "required_intervals": len(expected), "complete_intervals": len(good), "component": record["component"],
        "target_definition_version": record["target_definition_version"], "target_id": record["target_id"],
        "outcome_available_at": iso(available) if available else None, "evaluated_at": iso(now),
        "source_revision": source_revision, "source_evidence_hash": digest(good), "quality_reasons": reasons,
        "loss": loss, "loss_status": "evaluated" if valid_loss else "forecast_unavailable" if complete else "target_not_complete"}

def calculate_episode_outcome(record, panel, calendar, now, source_revision="first_release", contexts=()):
    status, expected, good, reasons = _target_rows(record, panel, calendar, now)
    variance, signed = None, None
    if status == "complete":
        by_session = {}
        for row in good:
            by_session.setdefault(row["session"], []).append(row)
        overnight = [by_session[session][0].get("overnight_rv") for session in record["sessions"][1:]]
        if any(not finite(x) for x in overnight):
            status = "incomplete_data"
            reasons.append("missing_intervening_overnight_returns")
        else:
            variance = sum(float(x["rv"]) for x in good)+sum(float(x) for x in overnight)
            first, last = good[0].get("underlying_start_price"), good[-1].get("underlying_end_price")
            if finite(first) and finite(last) and first > 0 and last > 0:
                signed = last/first-1
            else:
                reasons.append("signed_return_unavailable_missing_adjusted_price_evidence")
    context_start = utc(record.get("context_window_start") or record.get("issued_at") or record["target_start"])
    public_events = []
    for event in contexts:
        if event.get("dataset_id") != record["dataset_id"] or event.get("mode") != record["mode"]:
            raise ValueError("Public context belongs to a different dataset/mode")
        if event["instrument_id"] not in {record["instrument_id"], "*"}:
            continue
        if utc(event["available_at"]) <= utc(now) and context_start <= utc(event["public_at"]) <= utc(record["target_end"]):
            public_events.append({**event, "occurrence_status": "scheduled_future" if utc(event["public_at"]) > utc(now) else "at_or_before_evaluation"})
    public_events.sort(key=lambda x: (x["public_at"], x["context_id"]))
    available = max([max(utc(x["decision_at"]), utc(x.get("available_at") or x["decision_at"]),
        utc(x.get("source_ready_at") or x["decision_at"]), utc(x.get("input_watermark") or x["decision_at"])) for x in good]
        +[utc(x["available_at"]) for x in public_events]) if status == "complete" else None
    return {"window_id": record["window_id"], "episode_id": record["episode_id"], "dataset_id": record["dataset_id"],
        "mode": record["mode"], "instrument_id": record["instrument_id"], "horizon_id": record["horizon_id"],
        "target_start": record["target_start"], "target_end": record["target_end"],
        "status": status, "signed_underlying_return": signed, "absolute_underlying_return": abs(signed) if signed is not None else None,
        "realized_variance": variance, "target_coverage": len(good)/len(expected) if expected else 0.,
        "required_intervals": len(expected), "complete_intervals": len(good), "evaluated_at": iso(now),
        "outcome_available_at": iso(available) if available else None,
        "source_revision": source_revision, "source_evidence_hash": digest(good), "quality_reasons": reasons,
        "component_definition": record["component_definition"], "public_events": public_events,
        "context_evidence_hash": digest(public_events), "public_event_window_definition": "issuance_through_target_end",
        "interpretation": record["interpretation"]}

def evaluate_outcomes(store, calendar, now):
    """Append changed readiness/evidence only; original forecasts never change."""
    panel = {}
    for x in store.db.execute("SELECT payload FROM panel ORDER BY interval_end,instrument"):
        row = json.loads(x[0])
        panel.setdefault(row["instrument_id"], {})[iso(row["interval_end"])] = row
    intervals = session_intervals(calendar)
    contexts = [json.loads(x[0]) for x in store.db.execute("SELECT payload FROM contexts WHERE available_at<=? ORDER BY available_at", (iso(now),))]
    report = {"forecast_evaluations": 0, "episode_evaluations": 0}
    with store.transaction():
        for row in store.db.execute("SELECT f.forecast_id,f.payload FROM forecasts f LEFT JOIN latest_forecast_outcomes o ON o.forecast_id=f.forecast_id WHERE o.status IS NULL OR o.status NOT IN ('complete','revised') ORDER BY f.target_end").fetchall():
            record = json.loads(row["payload"])
            evaluation = calculate_forecast_outcome(record, panel, intervals, now)
            report["forecast_evaluations"] += store.append_outcome("forecast", row["forecast_id"], evaluation)
        for row in store.db.execute("SELECT w.window_id,w.payload,o.payload AS latest_payload FROM episode_windows w LEFT JOIN latest_episode_outcomes o ON o.window_id=w.window_id ORDER BY w.target_end").fetchall():
            record = json.loads(row["payload"])
            evaluation = calculate_episode_outcome(record, panel, intervals, now, contexts=contexts)
            latest = json.loads(row["latest_payload"]) if row["latest_payload"] else None
            if latest and latest.get("source_revision", "first_release") != "first_release":
                # Immutable first-release panel cannot reconstruct corrected observations.
                # Retain the latest registered correction while refreshing public context.
                refreshed = {k: v for k, v in latest.items() if k not in {"revision", "outcome_id"}}
                refreshed.update(evaluated_at=iso(now), public_events=evaluation["public_events"],
                    context_evidence_hash=evaluation["context_evidence_hash"])
                if refreshed.get("outcome_available_at") and evaluation.get("public_events"):
                    refreshed["outcome_available_at"] = iso(max([utc(refreshed["outcome_available_at"])]+[utc(x["available_at"]) for x in evaluation["public_events"]]))
                evaluation = refreshed
            report["episode_evaluations"] += store.append_outcome("episode", row["window_id"], evaluation)
    return report
