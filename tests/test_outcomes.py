import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import pandas as pd
from shockwatch.common import digest, iso, utc
from shockwatch.features import session_intervals
from shockwatch.outcomes import (build_forecasts, calculate_forecast_outcome,
    calculate_episode_outcome, evaluate_outcomes, first_complete_interval,
    register_episode_windows, validate_forecast)
from shockwatch.store import Store
from shockwatch.common import config
from shockwatch.models import fit
from shockwatch.synthetic import generate
from shockwatch.worker import run_once

class ForecastContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.calendar = pd.DataFrame([
            {"session": "2024-03-08", "open_utc": "2024-03-08T14:30:00Z", "close_utc": "2024-03-08T21:00:00Z"},
            {"session": "2024-03-11", "open_utc": "2024-03-11T13:30:00Z", "close_utc": "2024-03-11T17:00:00Z"},
            {"session": "2024-03-12", "open_utc": "2024-03-12T13:30:00Z", "close_utc": "2024-03-12T20:00:00Z"}])
        self.dataset = {"dataset_id": "artificial-1", "mode": "synthetic", "source": "test_generator_v1"}
        self.model = {**self.dataset, "model_id": "model-1", "training_cutoff": "2024-03-07T21:10:00Z",
            "feature_version": "test-1", "source_version": "test-1", "intraday_target_definition_version": "next_session_intraday_v1",
            "symbols": {"SPY": {"seasonality": {str(x): .001 for x in range(7)}, "intraday_har": None}}}
        self.policy = {"extreme_flow": 5}
        self.state = {"ewma_level": 2., "intraday_ewma": .3, "intraday_history": [.2]*22}
        self.row = {"instrument_id": "SPY", "decision_at": "2024-03-11T14:40:00Z", "session": "2024-03-11", "is_session_end": False}

    def tearDown(self):
        self.tmp.cleanup()

    def forecasts(self, row=None, issued_at=None):
        row = row or self.row
        return build_forecasts(row, self.state, self.model, self.policy, self.calendar,
            **{k: self.dataset[k] for k in ("dataset_id", "mode")}, issued_at=issued_at or row["decision_at"],
            recorded_at="2026-10-12T15:00:00Z", run_id="run-1")

    def store(self):
        store = Store(self.path/"test.db", self.dataset)
        store.register_model_policy(self.model, {"model_id": self.model["model_id"], "policy": self.policy, "reviewed": True})
        return store

    def target_panel(self, forecast, rv=.01, late=False):
        rows = []
        for item in session_intervals(self.calendar):
            if utc(forecast["target_start"]) <= item["interval_start"] and item["interval_end"] <= utc(forecast["target_end"]):
                at = item["interval_end"]+pd.Timedelta(minutes=20 if late else 10)
                rows.append({**item, "instrument_id": "SPY", "rv": rv, "decision_at": iso(at), "available_at": iso(at),
                    "underlying_start_price": 100., "underlying_end_price": 95., "overnight_rv": .5, "data_ok": False})
        return rows

    def test_ten_minute_delay_skips_already_started_target(self):
        f = self.forecasts()[0]
        self.assertEqual(f["target_start"], "2024-03-11T15:30:00+00:00")
        self.assertEqual(f["target_end"], "2024-03-11T16:30:00+00:00")
        self.assertEqual(f["waiting_gap_minutes"], 50.)
        self.assertAlmostEqual(f["predicted_variance"], .12)
        self.assertEqual(f["issued_at"], f["simulated_issued_at"])
        self.assertNotEqual(f["recorded_at"], f["issued_at"])

    def test_dst_early_close_and_closed_days_choose_real_calendar(self):
        next_day = first_complete_interval(self.calendar, "2024-03-08T21:10:00Z")
        self.assertEqual(iso(next_day["interval_start"]), "2024-03-11T13:30:00+00:00")
        half_hour = first_complete_interval(self.calendar, "2024-03-11T16:30:00Z")
        self.assertEqual(half_hour["minutes"], 30.)
        after_close = first_complete_interval(self.calendar, "2024-03-11T17:10:00Z")
        self.assertEqual(after_close["session"], "2024-03-12")

    def test_future_inputs_and_started_targets_rejected(self):
        original = self.forecasts()[0]
        for updates in ({"input_watermark": "2024-03-11T14:45:00Z"}, {"target_start": "2024-03-11T14:30:00Z"},
                        {"predicted_variance": -1}, {"simulated_issued_at": None}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_forecast({**original, **updates})

    def test_transport_and_actual_prospective_recording_cannot_be_backdated(self):
        transported = {**self.row, "source_ready_at": "2024-03-11T15:15:00Z"}
        with self.assertRaises(ValueError):
            self.forecasts(transported, "2024-03-11T14:40:00Z")
        result = self.forecasts(transported, "2024-03-11T15:15:00Z")[0]
        self.assertEqual(result["asof_at"], "2024-03-11T15:15:00+00:00")
        self.assertEqual(result["input_watermark"], result["asof_at"])
        with self.assertRaises(ValueError):
            validate_forecast({**result, "mode": "prospective", "recorded_at": "2024-03-11T15:00:00Z"})

    def test_next_session_targets_refitted_intraday_and_skip_outage(self):
        row = {**self.row, "is_session_end": True, "decision_at": "2024-03-08T21:10:00Z", "session": "2024-03-08"}
        forecasts = self.forecasts(row)
        daily = [x for x in forecasts if x["horizon_id"] == "next_session_intraday"]
        self.assertEqual(len(daily), 2)
        self.assertEqual(len({x["target_id"] for x in daily}), 1)
        self.assertEqual(daily[0]["target_start"], "2024-03-11T13:30:00+00:00")
        self.assertEqual(daily[1]["forecast_status"], "unavailable")
        delayed = self.forecasts(row, "2024-03-11T14:40:00Z")
        self.assertFalse(any(x["horizon_id"] == "next_session_intraday" for x in delayed))
        del self.model["intraday_target_definition_version"]
        self.assertFalse(any(x["horizon_id"] == "next_session_intraday" for x in self.forecasts(row)))

    def test_pending_late_partial_and_complete_are_distinct(self):
        forecast = self.forecasts()[0]
        rows = self.target_panel(forecast, late=True)
        pending = calculate_forecast_outcome(forecast, rows, self.calendar, "2024-03-11T16:25:00Z")
        late = calculate_forecast_outcome(forecast, rows, self.calendar, "2024-03-11T16:40:00Z")
        complete = calculate_forecast_outcome(forecast, rows, self.calendar, "2024-03-11T16:50:00Z")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(late["status"], "incomplete_data")
        self.assertIsNone(late["observed_variance"])
        self.assertEqual(complete["status"], "complete")
        self.assertAlmostEqual(complete["observed_variance"], .01)
        self.assertEqual(complete["dataset_id"], self.dataset["dataset_id"])
        self.assertIsNotNone(complete["loss"])
        rows[0]["rv"] = None
        missing = calculate_forecast_outcome(forecast, rows, self.calendar, "2024-03-11T17:00:00Z")
        self.assertEqual(missing["status"], "incomplete_data")
        self.assertIsNone(missing["loss"])

    def test_intraday_outcome_excludes_overnight_and_requires_all_slots(self):
        row = {**self.row, "is_session_end": True, "decision_at": "2024-03-08T21:10:00Z", "session": "2024-03-08"}
        forecast = next(x for x in self.forecasts(row) if x["method"] == "seasonal_intraday_ewma")
        rows = self.target_panel(forecast)
        result = calculate_forecast_outcome(forecast, rows, self.calendar, "2024-03-11T17:30:00Z")
        self.assertAlmostEqual(result["observed_variance"], .04)
        self.assertEqual(result["required_intervals"], 4)
        partial = calculate_forecast_outcome(forecast, rows[:-1], self.calendar, "2024-03-11T17:30:00Z")
        self.assertIsNone(partial["observed_variance"])
        self.assertEqual(partial["target_coverage"], .75)

    def test_forecast_and_outcome_immutable_with_append_only_revision(self):
        store = self.store()
        forecast = self.forecasts()[0]
        self.assertTrue(store.insert_forecast(forecast))
        self.assertFalse(store.insert_forecast(forecast))
        with self.assertRaises(ValueError):
            store.insert_forecast({**forecast, "predicted_variance": 123.})
        with self.assertRaises(sqlite3.IntegrityError):
            store.db.execute("UPDATE forecasts SET predicted_variance=123")
        evaluation = calculate_forecast_outcome(forecast, self.target_panel(forecast), self.calendar, "2024-03-11T17:00:00Z")
        self.assertTrue(store.append_outcome("forecast", forecast["forecast_id"], evaluation))
        self.assertFalse(store.append_outcome("forecast", forecast["forecast_id"], {**evaluation, "evaluated_at": "2024-03-12T17:00:00Z"}))
        store.record_source_revision({**self.dataset, "revision_id": "correction-v2", "available_at": "2024-03-12T16:00:00Z",
            "original_reference": "first_release_hash", "amended_reference": "corrected_input_hash", "source_version": "test-2"})
        corrected = calculate_forecast_outcome(forecast, self.target_panel(forecast, rv=.02), self.calendar,
                                               "2024-03-12T17:00:00Z", source_revision="correction-v2")
        self.assertTrue(store.append_outcome("forecast", forecast["forecast_id"], corrected))
        self.assertFalse(store.append_outcome("forecast", forecast["forecast_id"], corrected))
        rows = store.db.execute("SELECT status,payload FROM forecast_outcomes ORDER BY revision").fetchall()
        self.assertEqual([r[0] for r in rows], ["complete", "revised"])
        self.assertAlmostEqual(json.loads(rows[0][1])["observed_variance"], .01)
        self.assertAlmostEqual(json.loads(rows[1][1])["observed_variance"], .02)
        self.assertEqual(json.loads(store.db.execute("SELECT payload FROM forecasts").fetchone()[0]), forecast)
        store.close()

    def test_outcome_cannot_be_attached_to_a_different_target(self):
        store = self.store()
        forecast = self.forecasts()[0]
        store.insert_forecast(forecast)
        evaluation = calculate_forecast_outcome(forecast, self.target_panel(forecast), self.calendar, "2024-03-11T17:00:00Z")
        for changed in ({"target_id": "different-target"}, {"target_end": "2024-03-12T16:30:00Z"}, {"target_coverage": .5}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                store.append_outcome("forecast", forecast["forecast_id"], {**evaluation, **changed})
        self.assertEqual(store.db.execute("SELECT count(*) FROM forecast_outcomes").fetchone()[0], 0)
        store.close()

    def test_future_correction_cannot_revise_an_earlier_outcome(self):
        store = self.store()
        forecast = self.forecasts()[0]
        store.insert_forecast(forecast)
        source = {**self.dataset, "revision_id": "future-correction", "available_at": "2024-03-12T16:00:00Z",
            "original_reference": "first_release_hash", "amended_reference": "correction_hash", "source_version": "test-2"}
        self.assertTrue(store.record_source_revision(source))
        self.assertFalse(store.record_source_revision(source))
        evaluation = calculate_forecast_outcome(forecast, self.target_panel(forecast), self.calendar,
            "2024-03-11T17:00:00Z", source_revision="future-correction")
        with self.assertRaises(ValueError):
            store.append_outcome("forecast", forecast["forecast_id"], evaluation)
        self.assertEqual(store.db.execute("SELECT count(*) FROM forecast_outcomes").fetchone()[0], 0)
        store.close()

    def test_dataset_binding_cannot_cross_modes_or_change_manifest(self):
        store = self.store()
        with self.assertRaises(ValueError):
            store.bind_dataset({**self.dataset, "mode": "prospective"})
        with self.assertRaises(ValueError):
            store.bind_dataset({**self.dataset, "source": "real_feed"})
        with self.assertRaises(ValueError):
            store.check_identity({"dataset_id": "other", "mode": "synthetic"})
        with self.assertRaises(ValueError):
            store.check_identity({})
        store.close()
        with self.assertRaises(ValueError):
            Store(self.path/"test.db", {"dataset_id": "real-data", "mode": "prospective"})

    def test_forecast_transaction_rolls_back_without_partial_state(self):
        store = self.store()
        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.insert_forecast(self.forecasts()[0])
                store.set("simulated_decision_state", {"n": 1})
                raise RuntimeError("failure after forecast insertion")
        self.assertEqual(store.db.execute("SELECT count(*) FROM forecasts").fetchone()[0], 0)
        self.assertIsNone(store.get("simulated_decision_state"))
        store.close()

    def test_episode_follow_up_preserves_negative_returns(self):
        store = self.store()
        event = {"type": "opened", "episode_id": "episode-1", "instrument_id": "SPY"}
        self.assertEqual(register_episode_windows(store, [event], self.calendar, "2024-03-08T21:10:00Z", "2026-10-12T15:00:00Z"), 1)
        window = json.loads(store.db.execute("SELECT payload FROM episode_windows").fetchone()[0])
        result = calculate_episode_outcome(window, self.target_panel(window), self.calendar, "2024-03-11T17:30:00Z")
        self.assertEqual(result["status"], "complete")
        self.assertAlmostEqual(result["signed_underlying_return"], -.05)
        self.assertAlmostEqual(result["absolute_underlying_return"], .05)
        self.assertNotIn("win", result)
        self.assertTrue(store.append_outcome("episode", window["window_id"], result))
        store.close()

    def test_public_context_matures_separately_without_future_availability(self):
        store = self.store()
        event = {"type": "opened", "episode_id": "episode-context", "instrument_id": "SPY"}
        register_episode_windows(store, [event], self.calendar, "2024-03-08T21:10:00Z", "2026-10-12T15:00:00Z")
        window = json.loads(store.db.execute("SELECT payload FROM episode_windows").fetchone()[0])
        context = {**self.dataset, "context_id": "scheduled-1", "instrument_id": "SPY", "public_at": "2024-03-11T16:00:00Z",
            "available_at": "2024-03-11T15:00:00Z", "event_type": "scheduled_public_event", "title": "Known scheduled event",
            "source_reference": "synthetic://scheduled-public-event"}
        self.assertTrue(store.insert_context(context))
        self.assertFalse(store.insert_context(context))
        with self.assertRaises(ValueError):
            store.insert_context({**context, "title": "changed evidence"})
        pending = calculate_episode_outcome(window, self.target_panel(window), self.calendar, "2024-03-11T14:50:00Z", contexts=[context])
        self.assertEqual(pending["public_events"], [])
        known_schedule = calculate_episode_outcome(window, self.target_panel(window), self.calendar, "2024-03-11T15:10:00Z", contexts=[context])
        self.assertEqual(known_schedule["status"], "pending")
        self.assertEqual(known_schedule["public_events"][0]["occurrence_status"], "scheduled_future")
        complete = calculate_episode_outcome(window, self.target_panel(window), self.calendar, "2024-03-11T17:30:00Z", contexts=[context])
        self.assertEqual(complete["public_events"][0]["occurrence_status"], "at_or_before_evaluation")
        unrelated = {**context, "dataset_id": "real-data", "mode": "prospective"}
        with self.assertRaises(ValueError):
            calculate_episode_outcome(window, self.target_panel(window), self.calendar, "2024-03-11T17:30:00Z", contexts=[unrelated])
        store.close()

    def test_quality_incident_is_idempotent_and_transactional(self):
        store = self.store()
        incident = {"instrument_id": "SPY", "interval_end": "2024-03-11T16:30:00Z",
            "quality_reasons": "missing_underlying_bar", "severity": "degraded"}
        self.assertTrue(store.record_quality_incident(incident, "2024-03-11T16:40:00Z"))
        self.assertFalse(store.record_quality_incident(incident, "2024-03-11T16:45:00Z"))
        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.record_quality_incident({**incident, "instrument_id": "UAL"}, "2024-03-11T16:40:00Z")
                raise RuntimeError("decision rollback")
        self.assertEqual(store.db.execute("SELECT count(*) FROM data_quality_incidents").fetchone()[0], 1)
        store.close()

    def test_worker_catch_up_issues_latest_state_once_at_processing_clock(self):
        panel, calendar, _ = generate(self.path/"bundle", sessions=75)
        dates = sorted(panel.session.unique())
        cfg = config()
        cfg["synthetic"] = True
        model = fit(panel, dates[59]+"T23:59:00Z", cfg)
        policy = {"model_id": model["model_id"], "reviewed": True, "policy": cfg["policy"]}
        inbox = self.path/"inbox"
        inbox.mkdir()
        panel.to_csv(inbox/"panel.csv", index=False)
        now = dates[70]+"T23:59:00Z"
        report = run_once(inbox, self.path/"catchup.db", model, policy, self.path/"bundle/sessions.csv", cfg, now=now)
        self.assertGreater(report["superseded_forecast_decisions"], 0)
        store = Store(self.path/"catchup.db")
        forecasts = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM forecasts")]
        self.assertGreater(len(forecasts), 0)
        self.assertEqual(len({(x["target_id"], x["method"]) for x in forecasts}), len(forecasts))
        self.assertTrue(all(x["issued_at"] == iso(now) for x in forecasts))
        self.assertTrue(all(utc(x["target_start"]) >= utc(now) for x in forecasts))
        origins = {s: max(panel[panel.instrument_id.eq(s)&panel.session.eq(dates[70])].decision_at) for s in model["universe"]}
        self.assertTrue(all(x["origin_decision_at"] == iso(origins[x["instrument_id"]]) for x in forecasts))
        decisions_before = store.db.execute("SELECT count(*) FROM decisions").fetchone()[0]
        store.close()
        repeated = run_once(inbox, self.path/"catchup.db", model, policy, self.path/"bundle/sessions.csv", cfg, now=now)
        self.assertEqual(repeated["decisions"], 0)
        self.assertEqual(repeated["forecasts"], 0)
        store = Store(self.path/"catchup.db")
        self.assertEqual(store.db.execute("SELECT count(*) FROM decisions").fetchone()[0], decisions_before)
        self.assertEqual(store.db.execute("SELECT count(*) FROM forecasts").fetchone()[0], len(forecasts))
        store.close()

    def test_original_schema_migration_and_backup_preserve_records(self):
        db = sqlite3.connect(self.path/"original.db")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES('existing','123')")
        db.commit()
        db.close()
        store = Store(self.path/"original.db")
        self.assertEqual(store.get("existing"), 123)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
        store.backup(self.path/"backup.db")
        store.close()
        restored = Store(self.path/"backup.db")
        self.assertEqual(restored.get("existing"), 123)
        self.assertEqual(restored.db.execute("SELECT count(*) FROM forecast_outcomes").fetchone()[0], 0)
        restored.close()

if __name__ == "__main__":
    unittest.main()
