"""Scheduled CPU job with persistent state and independent health checks."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from urllib.request import urlopen
import pandas as pd
from .common import clean, digest, dumps, iso, save_json, utc
from .engine import decide, engine_state
from .features import session_intervals
from .models import enrich
from .notifications import dispatch
from .store import Store
from .outcomes import issue_forecasts, register_episode_windows, evaluate_outcomes

def external_heartbeat(status):
    name = "SHOCKWATCH_HEARTBEAT_OK_URL" if status == "ok" else "SHOCKWATCH_HEARTBEAT_FAIL_URL"
    endpoint = os.getenv(name)
    if not endpoint:
        return "not_configured"
    if not endpoint.startswith("https://"):
        return "invalid_https_required"
    try:
        with urlopen(endpoint, timeout=5) as response:
            return "accepted" if response.status < 300 else "rejected"
    except Exception as exc:
        return type(exc).__name__

def health(store, calendar, model, cfg, now):
    intervals = session_intervals(calendar)
    past = [x for x in intervals if x["interval_end"]+pd.Timedelta(minutes=cfg["decision_delay_minutes"]) <= utc(now)]
    if not past:
        return {"status": "waiting_for_first_calendar_interval", "expected": 0, "healthy": 0}
    key = iso(past[-1]["interval_end"])
    results = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM decisions WHERE interval_end=?", (key,))]
    known = {r["instrument_id"]: r for r in results}
    missing = [s for s in model["universe"] if s not in known]
    degraded = [s for s in known if not (known[s]["flow_eligible"] and known[s]["price_eligible"])]
    expired = utc(now) > utc(calendar.close_utc.max())+pd.Timedelta(days=1)
    failed = store.db.execute("SELECT count(*) FROM outbox WHERE status='failed'").fetchone()[0]
    status = "calendar_expired" if expired else "degraded" if missing or degraded or failed else "healthy"
    return {"status": status, "expected_interval_end": key, "expected": len(model["universe"]),
            "healthy": len(model["universe"])-len(missing)-len(degraded), "missing": missing, "degraded": degraded, "failed_notifications": failed}

def run_once(inbox, db_path, model, policy_artifact, calendar_path, cfg, now=None, senders=None, dataset_manifest=None):
    started_clock = time.perf_counter()
    recorded_at = utc(pd.Timestamp.now(tz="UTC"))
    now = utc(now or recorded_at)
    mode = model.get("mode", "synthetic" if model["synthetic"] else "historical")
    dataset_id = model.get("dataset_id", "legacy-"+mode)
    if mode not in {"synthetic", "historical", "prospective"} or bool(model["synthetic"]) != (mode == "synthetic"):
        raise ValueError("Model dataset mode and synthetic flag disagree")
    for key, expected in (("dataset_id", dataset_id), ("mode", mode)):
        if cfg.get(key, expected) != expected:
            raise ValueError("Worker configuration belongs to a different dataset/mode")
        if policy_artifact.get(key, expected if dataset_id.startswith("legacy-") else None) != expected:
            raise ValueError("Policy dataset identity mismatch or missing")
    issue_clock = cfg.get("forecast_issue_clock", "processing")
    if issue_clock not in {"processing", "scheduled_replay"} or (issue_clock == "scheduled_replay" and mode != "historical"):
        raise ValueError("Scheduled forecast reconstruction is only permitted in explicit historical replay")
    dataset = dataset_manifest or cfg.get("dataset_manifest") or {"dataset_id": dataset_id, "mode": mode,
        "source": "seeded_artificial" if mode == "synthetic" else "external_adapter",
        "source_version": model.get("source_version", "legacy-panel-v1"),
        "availability_assumptions": "declared_virtual_processing_clock" if mode != "prospective" else "actual_received_inputs"}
    if {key: dataset.get(key) for key in ("dataset_id", "mode")} != {"dataset_id": dataset_id, "mode": mode}:
        raise ValueError("Dataset manifest and model disagree")
    calendar = pd.read_csv(calendar_path)
    for col in ("open_utc", "close_utc"):
        calendar[col] = [utc(x) for x in calendar[col]]
    if model["synthetic"] and cfg["notifications"]["enabled"]:
        raise ValueError("Synthetic models cannot send notifications")
    if policy_artifact.get("model_id") != model["model_id"]:
        raise ValueError("Policy belongs to a different model")
    if not policy_artifact.get("reviewed", False):
        raise ValueError("Policy must be explicitly marked reviewed after inspecting validation")
    policy = policy_artifact["policy"]
    identity = digest([dataset_id, mode, model["model_id"], policy])
    store = Store(db_path, dataset)
    run_id = uuid.uuid4().hex
    report = {"run_id": run_id, "dataset_id": dataset_id, "mode": mode, "started_at": iso(now),
              "recorded_at": iso(recorded_at), "imported_rows": 0, "decisions": 0, "events": 0, "forecasts": 0, "episode_windows": 0, "superseded_forecast_decisions": 0, "errors": []}
    try:
        with store.lease(now):
            if store.get("deployment_id", identity) != identity:
                raise ValueError("Model/policy changed; use a new shadow database and review the transition")
            store.set("deployment_id", identity)
            with store.transaction():
                store.register_model_policy(model, policy_artifact)
            for path in sorted(Path(inbox).glob("*.csv")):
                try:
                    report["imported_rows"] += store.ingest(path, now)
                    archive = Path(db_path).parent/"archive"/now.strftime("%Y-%m-%d")
                    archive.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(archive/(uuid.uuid4().hex[:12]+"_"+path.name)))
                except Exception as exc:
                    error = {"file": path.name, "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
                    report["errors"].append(error)
                    incident = {"dataset_id": dataset_id, "mode": mode, "severity": "ingest_rejected",
                                "first_observed_at": iso(now), "run_id": run_id, **error}
                    key = digest([dataset_id, path.name, error["reason"]])[:32]
                    store.db.execute("INSERT OR IGNORE INTO data_quality_incidents VALUES(?,?,?,?)", (key, dataset_id, iso(now), dumps(incident)))
            after = store.get("watermark", model["training_cutoff"])
            panel = store.pending(after, now, model["context_rows"])
            if len(panel):
                enriched = enrich(panel, model["universe"])
                enriched = enriched[pd.to_datetime(enriched.decision_at, utc=True) > utc(after)]
                latest_by_symbol = enriched.groupby("instrument_id").decision_at.max().to_dict()
                for at, group in enriched.groupby("decision_at", sort=True):
                    with store.transaction():
                        for row in group.to_dict("records"):
                            row = clean(row)
                            symbol = row["instrument_id"]
                            state = store.state(symbol, engine_state(model, symbol))
                            new_state, decision, events = decide(row, model, policy, state)
                            store.commit_decision(new_state, decision, events, now, cfg["max_live_alert_age_minutes"])
                            issued_at = utc(pd.Timestamp.now(tz="UTC")) if mode == "prospective" else max(utc(at), utc(row.get("source_ready_at") or at)) if issue_clock == "scheduled_replay" else now
                            forecast_recorded_at = utc(pd.Timestamp.now(tz="UTC"))
                            if issue_clock == "scheduled_replay" or utc(row["decision_at"]) == utc(latest_by_symbol[symbol]):
                                report["forecasts"] += issue_forecasts(store, row, new_state, model, policy, calendar, issued_at, forecast_recorded_at, run_id)
                            else:
                                report["superseded_forecast_decisions"] += 1
                            report["episode_windows"] += register_episode_windows(store, events, calendar, issued_at, forecast_recorded_at)
                            report["decisions"] += 1
                            report["events"] += len(events)
                        store.set("watermark", at)
            report["outcomes"] = evaluate_outcomes(store, calendar, now)
            report["data_health"] = health(store, calendar, model, cfg, now)
            if report["errors"]:
                report["data_health"]["status"] = "ingest_error"
            if report["data_health"]["status"] not in ("healthy", "waiting_for_first_calendar_interval"):
                store.record_quality_incident({"status": report["data_health"]["status"],
                    "interval_end": report["data_health"].get("expected_interval_end"), "severity": "degraded",
                    "quality_reasons": report["data_health"], "source_reference": "worker_health_snapshot"}, now)
            if report["data_health"]["status"] not in ("healthy", "waiting_for_first_calendar_interval"):
                key = digest(["health", now.strftime("%Y-%m-%dT%H"), report["data_health"]["status"]])[:24]
                store.enqueue(key, "urgent", "SHOCKWATCH data health: "+dumps(report["data_health"]), now,
                              "blocked_synthetic" if mode == "synthetic" else "historical_only" if mode == "historical" else "pending")
            if now.hour >= cfg["daily_digest_utc_hour"] and cfg["notifications"]["daily_email"]:
                day = now.strftime("%Y-%m-%d")
                counts = dict(store.db.execute("SELECT status,count(*) FROM outbox GROUP BY status").fetchall())
                events_today = store.db.execute("SELECT count(*) FROM events WHERE decision_at>=?", (day,)).fetchone()[0]
                body = f"SHOCKWATCH daily summary {day}\nHealth: {dumps(report['data_health'])}\nEpisode state changes today: {events_today}\nOutbox status totals: {dumps(counts)}"
                store.enqueue("digest:"+day, "email", body, now,
                              "blocked_synthetic" if mode == "synthetic" else "historical_only" if mode == "historical" else "pending")
            report["notifications"] = dispatch(store, cfg["notifications"], now, senders)
            report["status"] = "ok" if report["data_health"]["status"] in ("healthy", "waiting_for_first_calendar_interval") else "degraded"
            report["external_heartbeat"] = external_heartbeat(report["status"]) if mode == "prospective" else "disabled_"+mode
            report["runtime_seconds"] = time.perf_counter()-started_clock
            store.db.execute("INSERT INTO runs VALUES(?,?,?)", (iso(now), report["status"], dumps(report)))
            store.set("last_run", report)
            save_json(Path(db_path).parent/"heartbeat.json", report)
            backup_day = now.strftime("%Y-%m-%d")
            if store.get("backup_day") != backup_day:
                store.backup(Path(db_path).parent/"backups"/(backup_day+".db"))
                store.set("backup_day", backup_day)
                history = [x for x in store.get("backup_history", []) if x != backup_day]+[backup_day]
                store.set("backup_history", history[-7:])
                for old_day in history[:-7]:
                    (Path(db_path).parent/"backups"/(old_day+".db")).unlink(missing_ok=True)
    finally:
        store.close()
    return clean(report)
