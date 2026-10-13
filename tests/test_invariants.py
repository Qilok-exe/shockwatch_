import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import pandas as pd
from shockwatch.common import config, iso, utc
from shockwatch.audit import coverage
from shockwatch.detector import initial_state, transition
from shockwatch.engine import case_metrics, replay
from shockwatch.features import _oi_evidence, build_panel, session_intervals
from shockwatch.models import fit
from shockwatch.notifications import dispatch
from shockwatch.store import Store
from shockwatch.synthetic import generate, raw_fixture
from shockwatch.worker import run_once

class TimingAndDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = raw_fixture(self.tmp.name)
        self.cfg = config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_naive_timestamps_rejected(self):
        with self.assertRaises(ValueError):
            utc("2024-03-11 10:30")
        self.assertEqual(iso("2024-03-11T10:30:00-04:00"), "2024-03-11T14:30:00+00:00")

    def test_calendar_dst_and_short_session(self):
        f = pd.DataFrame([{"session": "2024-03-08", "open_utc": utc("2024-03-08T14:30:00Z"), "close_utc": utc("2024-03-08T21:00:00Z")},
                          {"session": "2024-03-11", "open_utc": utc("2024-03-11T13:30:00Z"), "close_utc": utc("2024-03-11T17:00:00Z")}])
        self.assertEqual([x["minutes"] for x in session_intervals(f)], [60.]*6+[30.]+[60.]*3+[30.])

    def test_stale_quotes_do_not_erase_observed_flow(self):
        options = pd.read_csv(self.path/"options.csv")
        options["quote_time"] = "2024-03-11T13:00:00Z"
        options.to_csv(self.path/"options.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertTrue(p.flow_eligible.all())
        self.assertFalse(p.price_eligible.any())
        self.assertTrue(p.iv30.isna().all())

    def test_missing_quote_time_disables_price_but_preserves_flow(self):
        options = pd.read_csv(self.path/"options.csv")
        options["quote_time"] = None
        options.to_csv(self.path/"options.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertTrue(p.flow_eligible.all())
        self.assertFalse(p.price_eligible.any())

    def test_future_quote_time_cannot_enter_delta_mix(self):
        options = pd.read_csv(self.path/"options.csv")
        options["quote_time"] = "2024-03-12T13:30:00Z"
        options.to_csv(self.path/"options.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertTrue(p.flow_eligible.all())
        self.assertFalse(p.price_eligible.any())
        self.assertTrue(p.otm_contract_fraction_call_7_30.isna().all())

    def test_missing_volume_is_not_zero(self):
        options = pd.read_csv(self.path/"options.csv")
        options.loc[0, "interval_volume"] = np.nan
        options.to_csv(self.path/"options.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertFalse(p.flow_eligible.iloc[0])
        self.assertTrue(pd.isna(p.volume_call_7_30.iloc[0]))
        options["interval_volume"] = 0.
        options.to_csv(self.path/"options.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertTrue(p.flow_eligible.all())
        self.assertEqual(p.volume_call_7_30.iloc[0], 0.)

    def test_future_arrival_disables_incomplete_batch(self):
        options = pd.read_csv(self.path/"options.csv")
        options.loc[0, "available_at"] = "2024-03-12T14:00:00Z"
        options.to_csv(self.path/"options.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertFalse(p.flow_eligible.iloc[0])
        self.assertIn("late_chain", p.quality_reasons.iloc[0])

    def test_missing_five_minute_bar_blocks_alert_inputs(self):
        bars = pd.read_csv(self.path/"bars.csv")
        bars.iloc[1:].to_csv(self.path/"bars.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertFalse(p.flow_eligible.iloc[0])
        self.assertTrue(pd.isna(p.rv.iloc[0]))

    def test_surface_does_not_extrapolate(self):
        options = pd.read_csv(self.path/"options.csv")
        options["expiry_utc"] = "2024-05-30T20:00:00Z"
        options.to_csv(self.path/"options.csv", index=False)
        self.assertTrue(build_panel(self.path, self.cfg).iv30.isna().all())

    def test_later_oi_release_cannot_confirm_earlier_decision(self):
        chain = pd.DataFrame({"contract_id": ["c"], "put_call": ["C"]})
        positions = pd.DataFrame({"contract_id": ["c", "c"], "position_date": ["2024-03-07", "2024-03-08"], "open_interest": [100, 200],
                                  "published_at": [utc("2024-03-08T12:00:00Z"), utc("2024-03-11T15:00:00Z")],
                                  "available_at": [utc("2024-03-08T12:01:00Z"), utc("2024-03-11T15:01:00Z")]})
        dates = ["2024-03-07", "2024-03-08", "2024-03-11"]
        before = _oi_evidence(chain, positions, utc("2024-03-11T14:40:00Z"), dates)
        after = _oi_evidence(chain, positions, utc("2024-03-11T15:40:00Z"), dates)
        self.assertIsNone(before["oi_token"])
        self.assertEqual(after["oi_call"], 1.)

    def test_split_does_not_create_fake_overnight_move(self):
        cal = pd.read_csv(self.path/"sessions.csv")
        prior = pd.DataFrame([{"session": "2024-03-08", "open_utc": "2024-03-08T14:30:00Z", "close_utc": "2024-03-08T21:00:00Z"}])
        pd.concat([prior, cal]).to_csv(self.path/"sessions.csv", index=False)
        bars = pd.read_csv(self.path/"bars.csv")
        bars.loc[0, "previous_close_factor"] = .5
        priorbar = {"instrument_id": "SPY", "bar_start": "2024-03-08T20:55:00Z", "bar_end": "2024-03-08T21:00:00Z",
                    "open": 200., "high": 200., "low": 200., "close": 200., "volume": 1, "previous_close_factor": 1., "available_at": "2024-03-08T21:00:20Z"}
        pd.concat([pd.DataFrame([priorbar]), bars]).to_csv(self.path/"bars.csv", index=False)
        p = build_panel(self.path, self.cfg)
        self.assertEqual(p.overnight_rv.iloc[0], 0.)
        self.assertLess(p.rv.iloc[0], .001)

class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.policy = config()["policy"]
        self.row = {"instrument_id": "X", "decision_at": "2024-01-02T15:40:00+00:00", "minutes": 60, "oi_token": None}
        self.scores = {"flow_call": 0., "flow_put": 0., "price": 0., "oi_call": 0., "oi_put": 0.}

    def test_exceptional_single_channel_can_trigger(self):
        for feature in ("flow_call", "price"):
            state, events = transition(initial_state(), self.row, {**self.scores, feature: 7.}, self.policy, "model")
            self.assertEqual(events[0]["type"], "opened")
            self.assertIsNotNone(state["active"])

    def test_repeated_daily_oi_is_not_new_support(self):
        state = initial_state()
        state.update(oi_token="2024-01-01", cusum_call=9)
        row = {**self.row, "oi_token": "2024-01-01"}
        scores = {**self.scores, "flow_call": 3., "oi_call": 4.}
        state, events = transition(state, row, scores, self.policy, "model")
        self.assertEqual(events, [])
        row["oi_token"] = "2024-01-02"
        _, events = transition(state, row, scores, self.policy, "model")
        self.assertEqual(events[0]["type"], "opened")

    def test_missing_data_cannot_clear_episode(self):
        state, _ = transition(initial_state(), self.row, {**self.scores, "price": 8.}, self.policy, "model")
        for _ in range(30):
            state, _ = transition(state, self.row, {k: None for k in self.scores}, self.policy, "model")
        self.assertIsNotNone(state["active"])

    def test_quiet_closes_and_later_event_reopens(self):
        state, opened = transition(initial_state(), self.row, {**self.scores, "price": 8.}, self.policy, "model")
        events = []
        for i in range(7):
            row = {**self.row, "decision_at": iso(utc(self.row["decision_at"])+pd.Timedelta(hours=i+1))}
            state, new = transition(state, row, self.scores, self.policy, "model")
            events.extend(new)
        self.assertEqual(events[-1]["type"], "closed")
        row["decision_at"] = "2024-01-05T15:40:00+00:00"
        _, second = transition(state, row, {**self.scores, "price": 8.}, self.policy, "model")
        self.assertNotEqual(opened[0]["episode_id"], second[0]["episode_id"])

    def test_high_historical_cusum_does_not_prevent_quiet_closure(self):
        policy = {**self.policy, "clear_trading_minutes": 195}
        state, opened = transition(initial_state(), self.row, {**self.scores, "flow_call": 35.}, policy, "model")
        self.assertGreater(state["cusum_call"], policy["cusum_trigger"])
        events = []
        for i in range(3):
            row = {**self.row, "decision_at": iso(utc(self.row["decision_at"])+pd.Timedelta(hours=i+1))}
            state, new = transition(state, row, self.scores, policy, "model")
            events.extend(new)
        self.assertEqual(events, [])
        self.assertEqual(state["quiet_minutes"], 180)
        missing = {**self.scores, "price": None}
        state, new = transition(state, {**row, "decision_at": "2024-01-03T18:40:00+00:00"}, missing, policy, "model")
        self.assertEqual(new, [])
        self.assertEqual(state["quiet_minutes"], 180)
        state, events = transition(state, {**row, "decision_at": "2024-01-03T19:40:00+00:00", "minutes": 15}, self.scores, policy, "model")
        self.assertEqual([e["type"] for e in events], ["closed"])
        self.assertIsNone(state["active"])
        self.assertGreater(state["cusum_call"], policy["cusum_trigger"])
        later = {**row, "decision_at": "2024-01-08T15:40:00+00:00"}
        state, events = transition(state, later, {**self.scores, "flow_put": 35.}, policy, "model")
        self.assertEqual([e["type"] for e in events], ["opened"])
        self.assertNotEqual(events[0]["episode_id"], opened[0]["episode_id"])
        self.assertEqual(state["current_decision_evidence"], ["A_exceptional_flow"])

    def test_current_evidence_update_and_severity_escalation_are_distinct(self):
        state, opened = transition(initial_state(), self.row, {**self.scores,"flow_call":8.}, self.policy, "model")
        self.assertEqual(state["current_decision_evidence"],["A_exceptional_flow"])
        opening_reasons=opened[0]["reasons"][:]
        later={**self.row,"decision_at":"2024-01-02T16:40:00+00:00"}
        state, update = transition(state, later, {**self.scores,"price":8.}, self.policy, "model")
        self.assertEqual([e["type"] for e in update],["updated"])
        self.assertEqual(update[0]["new_reasons"],["A_exceptional_repricing"])
        self.assertEqual(state["current_decision_evidence"],["A_exceptional_repricing"])
        self.assertEqual(opening_reasons,["A_exceptional_flow"])
        later["decision_at"]="2024-01-02T17:40:00+00:00"
        state, update = transition(state, later, {**self.scores,"price":20.}, self.policy, "model")
        self.assertEqual([e["type"] for e in update],["updated"])
        self.assertTrue(update[0]["severity_escalated"])

    def test_portfolio_has_no_weekly_cap(self):
        events = []
        for symbol in ("A", "B", "C", "D"):
            _, e = transition(initial_state(), {**self.row, "instrument_id": symbol}, {**self.scores, "price": 8.}, self.policy, "model")
            events.extend(e)
        self.assertEqual(len(events), 4)

class PersistenceAndModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name)
        generate(cls.path/"data", sessions=75)
        cls.panel = pd.read_csv(cls.path/"data/panel.csv", float_precision="round_trip")
        cls.cfg = config()
        cls.cfg["synthetic"] = True
        cls.cutoff = sorted(cls.panel.session.unique())[59]+"T23:59:00Z"
        cls.model = fit(cls.panel, cls.cutoff, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_future_data_cannot_change_fitted_model(self):
        changed = self.panel.copy()
        future = pd.to_datetime(changed.decision_at, utc=True) > utc(self.cutoff)
        changed.loc[future, ["rv", "iv30", "stock_volume", "volume_call_7_30"]] *= 1000
        self.assertEqual(self.model, fit(changed, self.cutoff, self.cfg))

    def test_transaction_rollback_and_backup(self):
        with tempfile.TemporaryDirectory() as t:
            store = Store(Path(t)/"monitor.db")
            with self.assertRaises(RuntimeError):
                with store.transaction():
                    store.set("must_not_commit", 1)
                    raise RuntimeError("simulated crash before commit")
            self.assertIsNone(store.get("must_not_commit"))
            store.set("committed", 2)
            store.backup(Path(t)/"backup.db")
            store.close()
            restored = Store(Path(t)/"backup.db")
            self.assertEqual(restored.get("committed"), 2)
            restored.close()

    def test_conflicting_revision_cannot_replace_original(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            panel = self.panel.iloc[:3].copy()
            panel.to_csv(path/"a.csv", index=False)
            store = Store(path/"monitor.db")
            self.assertEqual(store.ingest(path/"a.csv", self.cutoff), 3)
            panel.loc[0, "rv"] *= 3
            panel.to_csv(path/"b.csv", index=False)
            with self.assertRaises(ValueError):
                store.ingest(path/"b.csv", self.cutoff)
            self.assertEqual(store.db.execute("SELECT count(*) FROM panel").fetchone()[0], 3)
            store.close()

    def test_worker_split_restart_matches_replay(self):
        with tempfile.TemporaryDirectory() as t:
            p, selection = Path(t), {"model_id": self.model["model_id"], "reviewed": True, "policy": self.cfg["policy"]}
            inbox = p/"inbox"
            inbox.mkdir()
            self.panel.to_csv(inbox/"panel.csv", index=False)
            panel = pd.read_csv(inbox/"panel.csv", float_precision="round_trip")
            dates = sorted(panel.session.unique())
            first = run_once(inbox, p/"monitor.db", self.model, selection, self.path/"data/sessions.csv", self.cfg, dates[67]+"T23:59:00Z")
            second = run_once(inbox, p/"monitor.db", self.model, selection, self.path/"data/sessions.csv", self.cfg, dates[-1]+"T23:59:00Z")
            third = run_once(inbox, p/"monitor.db", self.model, selection, self.path/"data/sessions.csv", self.cfg, dates[-1]+"T23:59:00Z")
            expected, _ = replay(panel, self.model)
            store = Store(p/"monitor.db")
            actual = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM decisions ORDER BY decision_at,instrument")]
            store.close()
            self.assertGreater(first["decisions"], 0)
            self.assertGreater(second["decisions"], 0)
            self.assertEqual(third["decisions"], 0)
            self.assertEqual(actual, expected)

    def test_telegram_failure_uses_email_and_never_logs_secret(self):
        with tempfile.TemporaryDirectory() as t:
            store = Store(Path(t)/"monitor.db")
            store.enqueue("test", "urgent", "test body", self.cutoff)
            def bad(_):
                raise RuntimeError("secret-token-must-not-be-logged")
            calls = []
            sent = dispatch(store, {"enabled": True, "telegram": True, "email_fallback": True}, self.cutoff,
                            {"telegram": bad, "email": lambda body: calls.append(body) or "accepted_email"})
            self.assertEqual(sent[0]["status"], "accepted_email_fallback")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("secret-token", json.dumps(sent))
            store.close()

    def test_pending_retry_survives_restart(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)/"monitor.db"
            store = Store(path)
            store.enqueue("retry", "urgent", "body", self.cutoff)
            def bad(_):
                raise TimeoutError("private endpoint")
            cfg = {"enabled": True, "telegram": False, "email_fallback": True}
            dispatch(store, cfg, self.cutoff, {"email": bad})
            store.close()
            store = Store(path)
            self.assertEqual(store.db.execute("SELECT status,attempts FROM outbox").fetchone()[:], ("pending", 1))
            sent = dispatch(store, cfg, iso(utc(self.cutoff)+pd.Timedelta(minutes=10)), {"email": lambda _: "accepted_email"})
            self.assertEqual(sent[0]["status"], "accepted_email")
            store.close()

    def test_unverified_cases_rejected(self):
        with self.assertRaises(ValueError):
            case_metrics([], pd.DataFrame([{"status": "research_lead"}]))

    def test_entire_missing_day_is_visible_to_coverage_audit(self):
        calendar = pd.read_csv(self.path/"data/sessions.csv")
        for col in ("open_utc", "close_utc"):
            calendar[col] = [utc(x) for x in calendar[col]]
        day = sorted(self.panel.session.unique())[65]
        report = coverage(self.panel[~self.panel.session.eq(day)], calendar, self.model["universe"], day+"T00:00:00Z", day+"T23:59:00Z")
        self.assertEqual(report["missing_rows"], 21)

    def test_missing_day_invalidates_har_consecutive_history(self):
        dates = sorted(self.panel.session.unique())
        decisions, _ = replay(self.panel[~self.panel.session.eq(dates[65])], self.model)
        after = [d for d in decisions if d["session"] == dates[66] and d["next_session_date"]]
        self.assertEqual(len(after), 3)
        self.assertTrue(all(d["next_session_har_total_variance"] is None for d in after))

    def test_related_case_family_cannot_straddle_folds(self):
        labels = pd.DataFrame([
            {"case_id": "a", "event_group": "same_event", "instrument_id": "SPY", "window_start": "2024-01-01T00:00:00Z", "public_at": "2024-01-10T00:00:00Z", "status": "synthetic"},
            {"case_id": "b", "event_group": "same_event", "instrument_id": "UAL", "window_start": "2024-01-09T00:00:00Z", "public_at": "2024-01-20T00:00:00Z", "status": "synthetic"}])
        self.assertEqual(case_metrics([], labels, "2024-01-01T00:00:00Z", "2024-01-15T00:00:00Z")["case_count"], 0)

    def test_existing_episode_update_is_separate_from_new_detection(self):
        labels = pd.DataFrame([{"case_id": "a", "event_group": "g", "instrument_id": "SPY", "window_start": "2024-01-03T00:00:00Z", "public_at": "2024-01-05T00:00:00Z", "status": "synthetic"}])
        events = [{"type": "opened", "instrument_id": "SPY", "decision_at": "2024-01-02T15:00:00Z"}, {"type": "updated", "instrument_id": "SPY", "decision_at": "2024-01-03T15:00:00Z"}]
        result = case_metrics(events, labels)
        self.assertEqual(result["detected_cases"], 0)
        self.assertEqual(result["cases_with_new_or_updated_alert"], 1)

if __name__ == "__main__":
    unittest.main()
