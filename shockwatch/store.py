"""SQLite first-release ledger, migrations, immutable forecasts and atomic decisions."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
import pandas as pd
from .common import clean, digest, dumps, iso, utc
from .features import validate_panel

SCHEMA_VERSION = 2
MODES = {"synthetic", "historical", "prospective"}

class Store:
    def __init__(self, path, dataset=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        if dataset is not None:
            try:
                self.bind_dataset(dataset)
            except BaseException:
                self.db.close()
                raise

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("Database schema is newer than this application")
        # Version zero is the original starter. Additions preserve all its rows.
        self.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (hash TEXT PRIMARY KEY, name TEXT NOT NULL, imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS panel (instrument TEXT NOT NULL, interval_end TEXT NOT NULL,
            decision_at TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(instrument, interval_end));
        CREATE INDEX IF NOT EXISTS panel_time ON panel(decision_at);
        CREATE TABLE IF NOT EXISTS states (instrument TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions (instrument TEXT NOT NULL, interval_end TEXT NOT NULL,
            decision_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(instrument, interval_end));
        CREATE TABLE IF NOT EXISTS events (event_key TEXT PRIMARY KEY, decision_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, channel TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt TEXT NOT NULL, last_error TEXT);
        CREATE TABLE IF NOT EXISTS runs (started_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, expires TEXT);
        CREATE TABLE IF NOT EXISTS datasets (dataset_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('synthetic','historical','prospective')), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_registry (model_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS policy_registry (policy_id TEXT PRIMARY KEY, model_id TEXT NOT NULL REFERENCES model_registry(model_id), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS forecasts (
            forecast_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
            mode TEXT NOT NULL, run_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
            model_id TEXT NOT NULL REFERENCES model_registry(model_id), policy_id TEXT NOT NULL REFERENCES policy_registry(policy_id),
            issued_at TEXT NOT NULL, target_start TEXT NOT NULL, target_end TEXT NOT NULL,
            horizon_id TEXT NOT NULL, component TEXT NOT NULL, forecast_status TEXT NOT NULL,
            predicted_variance REAL, payload TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS forecasts_target ON forecasts(target_end,instrument_id);
        CREATE TABLE IF NOT EXISTS forecast_outcomes (
            outcome_id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES forecasts(forecast_id),
            revision INTEGER NOT NULL, status TEXT NOT NULL, evaluated_at TEXT NOT NULL,
            outcome_available_at TEXT, payload TEXT NOT NULL, hash TEXT NOT NULL,
            UNIQUE(forecast_id,revision));
        CREATE TABLE IF NOT EXISTS episode_windows (
            window_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
            instrument_id TEXT NOT NULL, target_start TEXT NOT NULL, target_end TEXT NOT NULL,
            horizon_id TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS episode_outcomes (
            outcome_id TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES episode_windows(window_id),
            revision INTEGER NOT NULL, status TEXT NOT NULL, evaluated_at TEXT NOT NULL,
            payload TEXT NOT NULL, hash TEXT NOT NULL, UNIQUE(window_id,revision));
        CREATE TABLE IF NOT EXISTS contexts (
            context_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
            available_at TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS contexts_available ON contexts(available_at);
        CREATE TABLE IF NOT EXISTS source_revisions (
            revision_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
            available_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS data_quality_incidents (
            incident_id TEXT PRIMARY KEY, dataset_id TEXT REFERENCES datasets(dataset_id),
            first_observed_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE VIEW IF NOT EXISTS latest_forecast_outcomes AS
            SELECT o.* FROM forecast_outcomes o WHERE o.revision=(SELECT max(x.revision) FROM forecast_outcomes x WHERE x.forecast_id=o.forecast_id);
        CREATE VIEW IF NOT EXISTS latest_episode_outcomes AS
            SELECT o.* FROM episode_outcomes o WHERE o.revision=(SELECT max(x.revision) FROM episode_outcomes x WHERE x.window_id=o.window_id);
        CREATE TRIGGER IF NOT EXISTS forecasts_immutable_update BEFORE UPDATE ON forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecasts_immutable_delete BEFORE DELETE ON forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_outcomes_immutable_update BEFORE UPDATE ON forecast_outcomes BEGIN SELECT RAISE(ABORT,'Outcome revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_outcomes_immutable_delete BEFORE DELETE ON forecast_outcomes BEGIN SELECT RAISE(ABORT,'Outcome revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS episode_windows_immutable_update BEFORE UPDATE ON episode_windows BEGIN SELECT RAISE(ABORT,'Episode windows are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS episode_windows_immutable_delete BEFORE DELETE ON episode_windows BEGIN SELECT RAISE(ABORT,'Episode windows are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS episode_outcomes_immutable_update BEFORE UPDATE ON episode_outcomes BEGIN SELECT RAISE(ABORT,'Outcome revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS episode_outcomes_immutable_delete BEFORE DELETE ON episode_outcomes BEGIN SELECT RAISE(ABORT,'Outcome revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS datasets_immutable_update BEFORE UPDATE ON datasets BEGIN SELECT RAISE(ABORT,'Dataset manifests are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS datasets_immutable_delete BEFORE DELETE ON datasets BEGIN SELECT RAISE(ABORT,'Dataset manifests are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS models_immutable_update BEFORE UPDATE ON model_registry BEGIN SELECT RAISE(ABORT,'Registered models are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS models_immutable_delete BEFORE DELETE ON model_registry BEGIN SELECT RAISE(ABORT,'Registered models are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS policies_immutable_update BEFORE UPDATE ON policy_registry BEGIN SELECT RAISE(ABORT,'Registered policies are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS policies_immutable_delete BEFORE DELETE ON policy_registry BEGIN SELECT RAISE(ABORT,'Registered policies are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS source_revisions_immutable_update BEFORE UPDATE ON source_revisions BEGIN SELECT RAISE(ABORT,'Source revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS source_revisions_immutable_delete BEFORE DELETE ON source_revisions BEGIN SELECT RAISE(ABORT,'Source revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS contexts_immutable_update BEFORE UPDATE ON contexts BEGIN SELECT RAISE(ABORT,'Timestamped public context is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS contexts_immutable_delete BEFORE DELETE ON contexts BEGIN SELECT RAISE(ABORT,'Timestamped public context is immutable'); END;
        PRAGMA user_version=2;
        COMMIT;
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @contextmanager
    def lease(self, now):
        owner = uuid.uuid4().hex
        with self.transaction():
            old = self.db.execute("SELECT * FROM lease WHERE id=1").fetchone()
            if old and utc(old["expires"]) > utc(now):
                raise RuntimeError("Another worker owns the lease; inspect the prior run before retrying")
            self.db.execute("INSERT OR REPLACE INTO lease VALUES(1,?,?)", (owner, iso(utc(now)+pd.Timedelta(minutes=30))))
        try:
            yield
        finally:
            self.db.execute("DELETE FROM lease WHERE id=1 AND owner=?", (owner,))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(value)))

    def bind_dataset(self, dataset):
        dataset = clean(dict(dataset))
        if not isinstance(dataset.get("dataset_id"), str) or not dataset["dataset_id"].strip() or dataset.get("mode") not in MODES:
            raise ValueError("Dataset requires a nonempty dataset_id and explicit mode")
        identity = {k: dataset[k] for k in ("dataset_id", "mode")}
        old = self.get("dataset_identity")
        if old and old != identity:
            raise ValueError("Dataset/mode mismatch: use a separate operational database")
        if not old and self.db.execute("SELECT count(*) FROM panel").fetchone()[0]:
            # Original unlabelled stores may be inspected, but cannot acquire a new identity.
            if not dataset["dataset_id"].startswith("legacy-"):
                raise ValueError("Unlabelled legacy ledger requires a legacy identity or a new database")
        registered = self.db.execute("SELECT payload FROM datasets WHERE dataset_id=?", (dataset["dataset_id"],)).fetchone()
        if registered and json.loads(registered[0]) != dataset:
            raise ValueError("Dataset manifest changed; create a versioned dataset")
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?)", (dataset["dataset_id"], dataset["mode"], dumps(dataset)))
            self.set("dataset_identity", identity)
        return identity

    def identity(self):
        return self.get("dataset_identity")

    def check_identity(self, record, allow_legacy=False):
        identity = self.identity()
        if identity is None:
            return
        for key, expected in identity.items():
            actual = record.get(key)
            if actual is None and allow_legacy and identity["dataset_id"].startswith("legacy-"):
                continue
            if actual != expected:
                raise ValueError(f"{key} mismatch or missing: source/model belongs to a different dataset")

    def register_model_policy(self, model, policy_artifact):
        self.check_identity(model, allow_legacy=True)
        dataset_id = self.identity()["dataset_id"]
        policy_id = digest(policy_artifact["policy"])[:16]
        for table, key, parent, payload in (
            ("model_registry", model["model_id"], dataset_id, model),
            ("policy_registry", policy_id, model["model_id"], policy_artifact)):
            old = self.db.execute(f"SELECT payload FROM {table} WHERE {table.split('_')[0]}_id=?", (key,)).fetchone()
            if old and json.loads(old[0]) != clean(payload):
                raise ValueError("Registered model/policy identity was modified")
            self.db.execute(f"INSERT OR IGNORE INTO {table} VALUES(?,?,?)", (key, parent, dumps(payload)))
        return policy_id

    def ingest(self, path, now):
        raw = Path(path).read_bytes()
        file_hash = hashlib.sha256(raw).hexdigest()
        if self.db.execute("SELECT 1 FROM files WHERE hash=?", (file_hash,)).fetchone():
            return 0
        frame = validate_panel(pd.read_csv(path, float_precision="round_trip"))
        count = 0
        with self.transaction():
            for row in frame.to_dict("records"):
                row = clean(row)
                self.check_identity(row, allow_legacy=True)
                for col in ("available_at", "input_watermark"):
                    if row.get(col) is not None:
                        if utc(row[col]) > utc(row["decision_at"]):
                            raise ValueError("Panel includes inputs unavailable at its as-of decision")
                value_hash = digest(row)
                old = self.db.execute("SELECT hash FROM panel WHERE instrument=? AND interval_end=?", (row["instrument_id"], row["interval_end"])).fetchone()
                if old and old[0] != value_hash:
                    raise ValueError("Revised first-release row: preserve the source and replay in a separate research database")
                if old:
                    continue
                self.db.execute("INSERT INTO panel VALUES(?,?,?,?,?)", (row["instrument_id"], row["interval_end"], row["decision_at"], dumps(row), value_hash))
                count += 1
            self.db.execute("INSERT INTO files VALUES(?,?,?)", (file_hash, Path(path).name, iso(now)))
        return count

    def pending(self, after, now, context):
        rows = self.db.execute("SELECT payload FROM panel WHERE decision_at>? AND decision_at<=? ORDER BY decision_at,instrument", (iso(after), iso(now))).fetchall()
        result = [json.loads(r[0]) for r in rows]
        result = [r for r in result if utc(r.get("source_ready_at") or r["decision_at"]) <= utc(now)]
        for symbol in {r["instrument_id"] for r in context}:
            old = self.db.execute("SELECT payload FROM panel WHERE instrument=? AND decision_at<=? ORDER BY decision_at DESC LIMIT 1", (symbol, iso(after))).fetchone()
            result.append(json.loads(old[0]) if old else next(r for r in context if r["instrument_id"] == symbol))
        return pd.DataFrame(result)

    def state(self, symbol, default):
        row = self.db.execute("SELECT payload FROM states WHERE instrument=?", (symbol,)).fetchone()
        return json.loads(row[0]) if row else default

    def commit_decision(self, state, result, events, now, live_age):
        self.db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (result["instrument_id"], result["interval_end"], result["decision_at"], dumps(result)))
        self.db.execute("INSERT OR REPLACE INTO states VALUES(?,?)", (result["instrument_id"], dumps(state)))
        if result.get("quality_reasons"):
            self.record_quality_incident({"instrument_id": result["instrument_id"], "interval_end": result["interval_end"],
                "decision_at": result["decision_at"], "severity": "degraded", "quality_reasons": result["quality_reasons"],
                "source_reference": {"instrument_id": result["instrument_id"], "interval_end": result["interval_end"]}}, now)
        for event in events:
            key = digest([event["episode_id"], event["type"], event["decision_at"]])[:24]
            self.db.execute("INSERT INTO events VALUES(?,?,?)", (key, event["decision_at"], dumps(event)))
            if event["type"] in ("opened", "updated"):
                old = (utc(now)-utc(event["decision_at"])).total_seconds() > 60*live_age
                message = (f"SHOCKWATCH {event['type']}: {event['instrument_id']}\nEpisode {event['episode_id']}\n"
                           f"At {event['decision_at']}\nReasons: {', '.join(event['reasons'])}\n"
                           "Unusual market activity; explanation requires review.")
                mode = (self.identity() or {}).get("mode")
                status = "blocked_synthetic" if mode == "synthetic" else "historical_only" if old or mode == "historical" else "pending"
                self.enqueue(key, "urgent", message, now, status)

    def insert_forecast(self, record):
        from .outcomes import validate_forecast
        record = validate_forecast(record)
        self.check_identity(record)
        key, value_hash = record["forecast_id"], digest(record)
        old = self.db.execute("SELECT hash FROM forecasts WHERE forecast_id=?", (key,)).fetchone()
        if old:
            if old[0] != value_hash:
                raise ValueError("Immutable forecast identity already contains different data")
            return False
        self.db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            key, record["dataset_id"], record["mode"], record["run_id"], record["instrument_id"],
            record["model_id"], record["policy_id"], record["issued_at"], record["target_start"], record["target_end"],
            record["horizon_id"], record["component"], record["forecast_status"], record["predicted_variance"], dumps(record), value_hash))
        return True

    def insert_episode_window(self, record):
        self.check_identity(record)
        key, value_hash = record["window_id"], digest(record)
        old = self.db.execute("SELECT hash FROM episode_windows WHERE window_id=?", (key,)).fetchone()
        if old:
            if old[0] != value_hash:
                raise ValueError("Episode follow-up window changed")
            return False
        self.db.execute("INSERT INTO episode_windows VALUES(?,?,?,?,?,?,?,?,?)", (
            key, record["episode_id"], record["dataset_id"], record["instrument_id"], record["target_start"],
            record["target_end"], record["horizon_id"], dumps(record), value_hash))
        return True

    def append_outcome(self, kind, reference_id, evaluation):
        if kind not in ("forecast", "episode"):
            raise ValueError("Unknown outcome kind")
        table, ref = ("forecast_outcomes", "forecast_id") if kind == "forecast" else ("episode_outcomes", "window_id")
        evaluation = clean(dict(evaluation))
        self.check_identity(evaluation)
        parent_table = "forecasts" if kind == "forecast" else "episode_windows"
        parent_row = self.db.execute(f"SELECT payload FROM {parent_table} WHERE {ref}=?", (reference_id,)).fetchone()
        if not parent_row:
            raise ValueError("Outcome reference does not identify a stored immutable target")
        parent = json.loads(parent_row["payload"])
        contract_keys = {"dataset_id", "mode", "instrument_id", "horizon_id", "target_start", "target_end"}
        if kind == "forecast":
            contract_keys |= {"target_id", "target_definition_version", "component"}
        else:
            contract_keys |= {"episode_id", "component_definition"}
        if evaluation.get(ref, reference_id) != reference_id or any(evaluation.get(k) != parent.get(k) for k in contract_keys):
            raise ValueError("Outcome provenance/target contract differs from its immutable reference")
        if evaluation.get("status") in {"complete", "revised"}:
            variance = evaluation.get("observed_variance" if kind == "forecast" else "realized_variance")
            if variance is None or not isinstance(variance, (int, float)) or not pd.notna(variance) or variance < 0 or evaluation.get("target_coverage") != 1:
                raise ValueError("Complete outcome requires nonnegative variance and full target coverage")
            if utc(evaluation["evaluated_at"]) < utc(parent["target_end"]):
                raise ValueError("Outcome cannot mature before its target ends")
        source_revision = evaluation.get("source_revision", "first_release")
        if source_revision != "first_release":
            source = self.db.execute("SELECT available_at FROM source_revisions WHERE revision_id=?", (source_revision,)).fetchone()
            if not source or utc(source["available_at"]) > utc(evaluation["evaluated_at"]):
                raise ValueError("Revised outcome requires available registered source evidence")
            if evaluation.get("outcome_available_at"):
                evaluation["outcome_available_at"] = iso(max(utc(evaluation["outcome_available_at"]), utc(source["available_at"])))
        if evaluation.get("status") not in {"pending", "complete", "incomplete_data", "invalid_target", "revised"}:
            raise ValueError("Invalid outcome status")
        # Re-evaluation at another clock is idempotent if evidence and result agree.
        evidence = {k: v for k, v in evaluation.items() if k not in ("evaluated_at", "revision", "outcome_id")}
        value_hash = digest(evidence)
        old = self.db.execute(f"SELECT revision,hash,payload FROM {table} WHERE {ref}=? ORDER BY revision DESC LIMIT 1", (reference_id,)).fetchone()
        if old:
            previous = json.loads(old["payload"])
            if previous.get("status") in {"complete", "revised"} and evaluation.get("status") == "complete" and (previous.get("source_revision") != evaluation.get("source_revision") or previous.get("status") == "revised" or (kind == "episode" and previous.get("public_events") != evaluation.get("public_events"))):
                evaluation["status"] = "revised"
                evaluation["evaluation_status"] = "complete"
                evidence = {k: v for k, v in evaluation.items() if k not in ("evaluated_at", "revision", "outcome_id")}
                value_hash = digest(evidence)
        if old and old["hash"] == value_hash:
            return False
        revision = old["revision"]+1 if old else 1
        outcome_id = digest([kind, reference_id, revision, value_hash])[:32]
        evaluation.update({ref: reference_id, "revision": revision, "outcome_id": outcome_id})
        if kind == "forecast":
            self.db.execute("INSERT INTO forecast_outcomes VALUES(?,?,?,?,?,?,?,?)", (
                outcome_id, reference_id, revision, evaluation["status"], iso(evaluation["evaluated_at"]),
                evaluation.get("outcome_available_at"), dumps(evaluation), value_hash))
        else:
            self.db.execute("INSERT INTO episode_outcomes VALUES(?,?,?,?,?,?,?)", (
                outcome_id, reference_id, revision, evaluation["status"], iso(evaluation["evaluated_at"]), dumps(evaluation), value_hash))
        return True

    def insert_context(self, record):
        record = clean(dict(record))
        self.check_identity(record)
        required = {"context_id", "dataset_id", "mode", "instrument_id", "public_at", "available_at", "source_reference"}
        if not required.issubset(record):
            raise ValueError("Public context requires an identity, instrument, timestamps and source reference")
        for key in ("public_at", "available_at"):
            record[key] = iso(record[key])
        value_hash = digest(record)
        old = self.db.execute("SELECT hash FROM contexts WHERE context_id=?", (record["context_id"],)).fetchone()
        if old:
            if old["hash"] != value_hash:
                raise ValueError("Context identifier contains different timestamped evidence")
            return False
        self.db.execute("INSERT INTO contexts VALUES(?,?,?,?,?)", (record["context_id"], record["dataset_id"], record["available_at"], dumps(record), value_hash))
        return True

    def record_quality_incident(self, record, now):
        if self.identity() is None:
            return False
        record = {**self.identity(), **clean(dict(record))}
        self.check_identity(record)
        key = record.get("incident_id") or digest([record["dataset_id"], record.get("instrument_id"),
            record.get("interval_end"), record.get("status"), record.get("quality_reasons")])[:32]
        record.update(incident_id=key, first_observed_at=iso(now))
        cursor = self.db.execute("INSERT OR IGNORE INTO data_quality_incidents VALUES(?,?,?,?)", (key, record["dataset_id"], iso(now), dumps(record)))
        return cursor.rowcount > 0

    def record_source_revision(self, record):
        record = clean(dict(record))
        self.check_identity(record)
        required = {"revision_id", "dataset_id", "mode", "available_at", "original_reference", "amended_reference", "source_version"}
        if not required.issubset(record):
            raise ValueError("Source revision requires original/amended evidence and availability")
        record["available_at"] = iso(record["available_at"])
        old = self.db.execute("SELECT payload FROM source_revisions WHERE revision_id=?", (record["revision_id"],)).fetchone()
        if old:
            if json.loads(old[0]) != record:
                raise ValueError("Source revision identifier already has different evidence")
            return False
        self.db.execute("INSERT INTO source_revisions VALUES(?,?,?,?)", (record["revision_id"], record["dataset_id"], record["available_at"], dumps(record)))
        return True

    def enqueue(self, key, channel, body, now, status="pending"):
        self.db.execute("INSERT OR IGNORE INTO outbox(id,channel,body,status,next_attempt) VALUES(?,?,?,?,?)", (key, channel, body, status, iso(now)))

    def backup(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
        finally:
            target.close()

    def close(self):
        self.db.close()
