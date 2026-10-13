"""Read-only deployment checks and logical backup comparison."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from .common import digest, utc
from .features import session_intervals


def doctor(runtime):
    """Diagnose configuration and immutable source integrity without mutations."""
    root = Path(runtime)
    report = {"runtime": str(root.resolve()), "python": sys.version, "checks": [], "status": "ok"}

    def check(name, ok, detail):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            report["status"] = "failed"

    def object_file(path, label):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                check(label, False, "Expected a JSON object")
                return None
            check(label, True, "JSON object readable")
            return value
        except (OSError, ValueError):
            check(label, False, "Missing or invalid JSON")
            return None

    loaded = {name: object_file(root / name, name) for name in ("model.json", "policy.json", "config.json", "dataset.json")}
    if any(value is None for value in loaded.values()):
        return report
    model, policy, cfg, dataset = [loaded[n] for n in ("model.json", "policy.json", "config.json", "dataset.json")]
    identity = {"dataset_id": dataset.get("dataset_id"), "mode": dataset.get("mode")}
    check("identity", isinstance(identity["dataset_id"], str) and bool(identity["dataset_id"]) and identity["mode"] in ("synthetic", "historical", "prospective"), identity)
    for label, obj in (("model", model), ("policy", policy), ("config", cfg)):
        check(label + " identity", all(obj.get(k) == v for k, v in identity.items()), "Must match runtime dataset")
    check("policy model", policy.get("model_id") == model.get("model_id"), "Frozen policy belongs to model")
    check("policy review", policy.get("reviewed", False), "Required before worker decisions")
    notifications = cfg.get("notifications", {})
    check("notification configuration", isinstance(notifications, dict), "Expected an object; credentials are not inspected or exported")
    check("notifications", isinstance(notifications, dict) and not (identity["mode"] == "synthetic" and notifications.get("enabled", False)), "Synthetic outbound guard")
    check("target refit", model.get("intraday_target_definition_version") == "next_session_intraday_v1", "Exact intraday target refit marker")
    try:
        import pandas as pd
        intervals = session_intervals(pd.read_csv(root / "sessions.csv"))
        check("calendar", bool(intervals), {"intervals": len(intervals)})
    except (OSError, ValueError, KeyError, TypeError):
        check("calendar", False, "Missing or invalid timezone-aware calendar")

    accepted_files = {}
    db = root / "monitor.db"
    if db.exists():
        con = None
        try:
            con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            row = con.execute("SELECT value FROM meta WHERE key='dataset_identity'").fetchone()
            check("database identity", bool(row) and json.loads(row[0]) == identity, "Separate database per dataset/mode")
            check("database integrity", con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity_check")
            accepted_files = {row[1]: row[0] for row in con.execute("SELECT hash,name FROM files")}
        except (sqlite3.DatabaseError, OSError, ValueError):
            check("database readable", False, "Unreadable ledger or invalid identity")
        finally:
            if con is not None:
                con.close()
    else:
        check("database", True, "Not created yet; first worker binds identity")

    if identity["mode"] == "synthetic":
        deployment = object_file(root / "deployment.json", "deployment.json")
        if deployment is None:
            return report
        check("deployment identity", all(deployment.get(k) == v for k, v in identity.items()), "Must match runtime dataset")
        bundle_value = deployment.get("bundle")
        if not isinstance(bundle_value, str) or not bundle_value:
            check("source bundle", False, "Missing source bundle path")
            return report
        bundle = Path(bundle_value).resolve()
        check("source bundle", bundle.is_dir(), "Configured immutable source bundle exists")
        manifest = object_file(bundle / "manifest.json", "source manifest")
        if manifest is None:
            return report
        source_identity = manifest.get("identity")
        if not isinstance(source_identity, dict):
            check("source manifest identity", False, "Expected an immutable source identity object")
            return report
        check("source manifest hash", manifest.get("manifest_id") == digest(source_identity), "Manifest content identity SHA-256")
        check("source dataset identity", all(manifest.get(k) == v and source_identity.get(k) == v for k, v in identity.items()), "Source manifest, panel identity and runtime agree")
        builder = source_identity.get("builder_config")
        required_builder = {"schema_version", "feature_version", "decision_delay_minutes", "max_quote_age_seconds", "max_relative_spread", "min_quote_coverage"}
        check("source builder configuration", isinstance(builder, dict) and required_builder.issubset(builder) and builder == {k: cfg.get(k) for k in builder}, "Frozen input cutoffs and feature settings match runtime")
        hashes = source_identity.get("content_hashes")
        if not isinstance(hashes, dict) or not hashes:
            check("source content hashes", False, "No immutable source hashes declared")
            return report
        required = {"raw/sessions.csv", "raw/batches.csv", "raw/bars.csv", "raw/options.csv", "raw/oi.csv", "panel.csv", "source_archive/public_context.csv"}
        check("source canonical coverage", required.issubset(hashes), "Required canonical raw tables, feature cache and public-context archive are hashed")
        from .sources import file_hash
        intact = 0
        for relative, expected in hashes.items():
            try:
                if not isinstance(relative, str) or not isinstance(expected, str):
                    raise ValueError("Invalid source hash entry")
                candidate = (bundle / relative).resolve()
                if bundle not in candidate.parents:
                    raise ValueError("Source path escapes bundle")
                if file_hash(candidate) != expected:
                    raise ValueError("Changed first-release source")
                intact += 1
            except (OSError, ValueError):
                check("source content " + str(relative), False, "Missing, escaped or changed first-release content")
        check("source content hashes", intact == len(hashes), {"verified_files": intact, "declared_files": len(hashes)})
        try:
            from .features import SCHEMAS
            import csv
            headers_ok = True
            for name, expected_columns in SCHEMAS.items():
                with (bundle / "raw" / (name + ".csv")).open("r", encoding="utf-8", newline="") as stream:
                    headers_ok &= next(csv.reader(stream), []) == expected_columns
            check("source canonical schemas", headers_ok, "Exact schema-1 raw headers")
            cached_identity = pd.read_csv(bundle / "panel.csv", usecols=["dataset_id", "mode"])
            check("source panel identity", len(cached_identity) > 0 and all(cached_identity[key].eq(value).all() for key, value in identity.items()), "Every cached feature row carries this dataset/mode")
        except (OSError, ValueError, KeyError):
            check("source canonical schemas", False, "Canonical headers or cached feature identity are unreadable")

        cursor_path = root / "source_checkpoint.json"
        if cursor_path.exists():
            cursor = object_file(cursor_path, "source checkpoint")
            if cursor is None:
                return report
            check("source checkpoint identity", cursor.get("manifest_id") == manifest.get("manifest_id") and all(cursor.get(k) == v for k, v in identity.items()), "Persisted cursor belongs to this immutable source")
            emitted = cursor.get("emitted")
            if not isinstance(emitted, dict):
                check("source checkpoint deliveries", False, "Expected emitted cohort hash mapping")
                return report
            try:
                batches = pd.read_csv(bundle / "raw/batches.csv")
                allowed_ends = {utc(value).isoformat() for value in batches.interval_end}
            except (OSError, ValueError, AttributeError):
                allowed_ends = set()
                check("source checkpoint population", False, "Canonical source batch population is unreadable")
            verified = 0
            for at, expected in emitted.items():
                try:
                    normalized = utc(at).isoformat()
                    if normalized not in allowed_ends or not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                        raise ValueError("Invalid emitted identity")
                    filename = "cohort_" + utc(at).strftime("%Y%m%dT%H%M%SZ") + ".csv"
                    current = root / "inbox" / filename
                    if current.is_file():
                        known = file_hash(current)
                    else:
                        known = accepted_files.get(filename)
                    if known != expected:
                        raise ValueError("Emitted content does not match first-release ledger or inbox")
                    verified += 1
                except (OSError, ValueError, TypeError):
                    pass
            check("source checkpoint deliveries", verified == len(emitted), {"verified_cohorts": verified, "emitted_cohorts": len(emitted)})
            try:
                if cursor.get("virtual_asof") is not None:
                    virtual = utc(cursor["virtual_asof"])
                    if emitted and virtual < max(utc(value) for value in emitted):
                        raise ValueError("Source clock precedes published cohorts")
                elif emitted:
                    raise ValueError("Published cohorts require a source clock")
                if cursor.get("wall_processed_at") is not None:
                    utc(cursor["wall_processed_at"])
                check("source checkpoint clock", True, "Explicit UTC virtual and wall processing times")
            except (ValueError, TypeError):
                check("source checkpoint clock", False, "Invalid source clock timestamps")
        else:
            check("source checkpoint", True, "Not created yet; no source publication has run")
    return report


def logical_digest(db_path):
    """Exclude volatile lease/run/backup timestamps; compare durable numerical state."""
    con = sqlite3.connect(Path(db_path).resolve(strict=True).as_uri()+"?mode=ro",uri=True)
    try:
        present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        evidence = {}
        for table in ("datasets","model_registry","policy_registry","panel","states","decisions","events","outbox","forecasts","forecast_outcomes","episode_windows","episode_outcomes","contexts","source_revisions","data_quality_incidents","experiments","experiment_metrics","files"):
            if table in present:
                columns = [r[1] for r in con.execute("PRAGMA table_info("+table+")")]
                rows = [dict(zip(columns,r)) for r in con.execute("SELECT * FROM "+table)]
                evidence[table] = sorted(rows,key=lambda x:digest(x))
        if "meta" in present:
            meta={r[0]:json.loads(r[1]) for r in con.execute("SELECT key,value FROM meta WHERE key IN ('dataset_identity','deployment_id','watermark','source_state')")}
            evidence["stable_meta"]=[meta]
        return {"sha256":digest(evidence),"counts":{k:len(v) for k,v in evidence.items()}}
    finally:
        con.close()


def numerical_digest(db_path):
    """Compare replayable values while retaining separate execution audit times."""
    con=sqlite3.connect(Path(db_path).resolve(strict=True).as_uri()+"?mode=ro",uri=True)
    try:
        evidence={}
        present={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("states","decisions","events","forecasts","forecast_outcomes","episode_windows","episode_outcomes","contexts","data_quality_incidents"):
            if table not in present:
                continue
            values=[]
            for row in con.execute("SELECT payload FROM "+table):
                value=json.loads(row[0])
                value={k:v for k,v in value.items() if k not in ("run_id","recorded_at")}
                values.append(value)
            evidence[table]=sorted(values,key=digest)
        if "outbox" in present:
            columns=[r[1] for r in con.execute("PRAGMA table_info(outbox)")]
            evidence["outbox"]=sorted([dict(zip(columns,row)) for row in con.execute("SELECT * FROM outbox")],key=digest)
        watermark=con.execute("SELECT value FROM meta WHERE key='watermark'").fetchone()
        evidence["watermark"]=[json.loads(watermark[0]) if watermark else None]
        return {"sha256":digest(evidence),"counts":{k:len(v) for k,v in evidence.items()}}
    finally:
        con.close()
