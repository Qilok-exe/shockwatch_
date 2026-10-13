import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd

from shockwatch.common import config, utc
from shockwatch.features import SCHEMAS, read_tables
from shockwatch.scenarios import CATALOGUE, _calendar, _scenario_schedule, evaluate_scenarios, generate_scenarios
from shockwatch.sources import CumulativeVolume, SyntheticSource, file_hash


class ScenarioScheduleTests(unittest.TestCase):
    def test_complete_schedule_preserves_independent_partial_release(self):
        for sessions in (140, 160, 180):
            with self.subTest(sessions=sessions):
                calendar = _calendar(sessions)
                schedule = _scenario_schedule(calendar)
                omitted = schedule["omit_day"]
                self.assertGreaterEqual(omitted, 100)
                self.assertNotIn(omitted, schedule["reserved_days"])
                self.assertNotEqual(omitted, schedule["scenario_day"]["partial_release"])
                self.assertEqual(schedule, _scenario_schedule(calendar))
                for dates in schedule["instances"].values():
                    self.assertNotIn(omitted, dates)
                for first in schedule["instances"]["two_events_week"]:
                    a = pd.Timestamp(calendar.iloc[first].session).isocalendar()[:2]
                    b = pd.Timestamp(calendar.iloc[first + 2].session).isocalendar()[:2]
                    self.assertEqual(a, b)

    def test_default_omission_retains_a_distinct_raw_calendar_fault(self):
        calendar = _calendar(160)
        schedule = _scenario_schedule(calendar)
        self.assertEqual(schedule["omit_day"], 104)
        self.assertEqual(schedule["scenario_day"]["partial_release"], 119)
        self.assertNotEqual(calendar.iloc[schedule["omit_day"]].session, calendar.iloc[schedule["scenario_day"]["partial_release"]].session)
        self.assertEqual(set(schedule["instances"]["persistent_oi"]), {112, 149})


class CumulativeVolumeTests(unittest.TestCase):
    def test_first_observation_reset_correction_and_restart(self):
        converter = CumulativeVolume()
        args = ("contract", "2024-01-02")
        first = converter.observe(*args, "2024-01-02T15:30:00Z", 25)
        self.assertIsNone(first["interval_volume"])
        self.assertEqual(converter.observe(*args, "2024-01-02T16:30:00Z", 32)["interval_volume"], 7)
        self.assertEqual(converter.observe(*args, "2024-01-02T17:30:00Z", 3, reset=True)["reason"], "declared_cumulative_reset")
        state = json.loads(json.dumps(converter.state))
        restored = CumulativeVolume(state)
        self.assertEqual(restored.observe(*args, "2024-01-02T18:30:00Z", 1)["reason"], "negative_cumulative_correction")
        self.assertEqual(restored.observe(*args, "2024-01-02T19:30:00Z", 9)["interval_volume"], 8)
        # A new session cannot inherit yesterday's cumulative anchor.
        self.assertIsNone(restored.observe("contract", "2024-01-03", "2024-01-03T15:30:00Z", 100)["interval_volume"])
        self.assertEqual(restored.observe("new", "2024-01-03", "2024-01-03T15:30:00Z", 15, baseline=4)["interval_volume"], 11)

    def test_missing_counter_does_not_assign_two_intervals_to_one(self):
        converter = CumulativeVolume()
        args = ("c", "2024-01-02")
        self.assertEqual(converter.observe(*args, "2024-01-02T15:30:00Z", 10, baseline=0)["interval_volume"], 10)
        self.assertEqual(converter.observe(*args, "2024-01-02T16:30:00Z", None)["reason"], "missing_cumulative_observation")
        self.assertEqual(converter.observe(*args, "2024-01-02T17:30:00Z", 30)["reason"], "missing_preceding_counter")
        self.assertEqual(converter.observe(*args, "2024-01-02T18:30:00Z", 34)["interval_volume"], 4)

    def test_duplicate_and_changed_first_release(self):
        converter = CumulativeVolume()
        args = ("c", "2024-01-02", "2024-01-02T15:30:00Z")
        expected = converter.observe(*args, 15, baseline=0)
        self.assertEqual(converter.observe(*args, 15, baseline=0), expected)
        with self.assertRaisesRegex(ValueError, "Changed first-release"):
            converter.observe(*args, 16, baseline=0)
        with self.assertRaisesRegex(ValueError, "Out-of-order"):
            converter.observe("c", "2024-01-02", "2024-01-02T14:30:00Z", 10)
        for value in (-1, 1.5, float("nan")):
            with self.assertRaises(ValueError):
                converter.observe("bad", "2024-01-02", "2024-01-02T15:30:00Z", value)


class SyntheticSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name)
        cls.bundle = cls.path / "bundle"
        cls.cfg = config()
        cls.manifest = generate_scenarios(cls.bundle, sessions=8, seed=931, faults=False, cfg=cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def source(self, name):
        return SyntheticSource(self.bundle, self.path / name, self.cfg)

    def test_deterministic_raw_inputs_and_truth_separation(self):
        second = self.path / "same_seed"
        other = generate_scenarios(second, sessions=8, seed=931, faults=False, cfg=self.cfg)
        self.assertEqual(self.manifest["manifest_id"], other["manifest_id"])
        for relative, expected in self.manifest["identity"]["content_hashes"].items():
            self.assertEqual(file_hash(second / relative), expected)
        tables = read_tables(self.bundle / "raw")
        for name, columns in SCHEMAS.items():
            self.assertEqual(list(tables[name].columns), columns)
        panel = pd.read_csv(self.bundle / "panel.csv")
        for forbidden in ("scenario_id", "case_id", "event_group", "public_at", "future_price", "injection_flag"):
            self.assertNotIn(forbidden, panel.columns)
        self.assertTrue(panel.dataset_id.eq(self.manifest["dataset_id"]).all())
        self.assertTrue(panel["mode"].eq("synthetic").all())
        self.assertTrue((pd.to_datetime(panel.input_watermark, utc=True) <= pd.to_datetime(panel.decision_at, utc=True)).all())
        self.assertEqual(len(pd.read_csv(self.bundle / "truth/scenarios.csv")), len(CATALOGUE))

    def test_atomic_publish_restart_and_no_duplicate(self):
        source = self.source("restart")
        first_at = source.next_available_at()
        first = source.deliver(first_at, max_cohorts=1)
        self.assertEqual(first["emitted_rows"], 3)
        filename = source.inbox / first["files"][0]
        self.assertEqual(len(pd.read_csv(filename)), 3)
        self.assertFalse(list(source.inbox.glob("*.tmp")))
        restarted = self.source("restart")
        repeated = restarted.deliver(first_at)
        self.assertEqual(repeated["emitted_rows"], 0)
        self.assertEqual(repeated["delivered_total"], 1)
        with self.assertRaisesRegex(ValueError, "backwards"):
            restarted.deliver(utc(first_at) - pd.Timedelta(seconds=1))

    def test_crash_after_publish_before_checkpoint_recovers(self):
        source = self.source("crash")
        at = source.next_available_at()
        with patch("shockwatch.sources.atomic_json", side_effect=OSError("simulated checkpoint failure")):
            with self.assertRaises(OSError):
                source.deliver(at, max_cohorts=1)
        self.assertEqual(len(list(source.inbox.glob("*.csv"))), 1)
        self.assertFalse(source.checkpoint_path.exists())
        restored = self.source("crash")
        report = restored.deliver(at, max_cohorts=1)
        self.assertEqual(report["delivered_total"], 1)
        self.assertEqual(len(list(source.inbox.glob("*.csv"))), 1)
        self.assertEqual(len(pd.read_csv(next(source.inbox.glob("*.csv")))), 3)

    def test_late_cohort_waits_and_cannot_be_overtaken(self):
        source = self.source("delay")
        at, group = source._cohorts[0]
        group = group.copy()
        group.loc[group.instrument_id.eq("LMT"), "source_ready_at"] = str(utc(at) + pd.Timedelta(hours=2))
        source._cohorts[0] = (at, group)
        before = source.deliver(utc(at) + pd.Timedelta(hours=1))
        self.assertEqual(before["emitted_cohorts"], 0)
        self.assertFalse(list(source.inbox.glob("*.csv")))
        after = source.deliver(utc(at) + pd.Timedelta(hours=2), max_cohorts=1)
        self.assertEqual(after["emitted_cohorts"], 1)
        published = pd.read_csv(source.inbox / after["files"][0])
        self.assertTrue((pd.to_datetime(published.input_watermark, utc=True) <= pd.to_datetime(published.decision_at, utc=True)).all())

    def test_manifest_tamper_and_config_change_rejected(self):
        changed_cfg = {**self.cfg, "decision_delay_minutes": 20}
        with self.assertRaisesRegex(ValueError, "configuration differs"):
            SyntheticSource(self.bundle, self.path / "bad_config", changed_cfg)
        with self.assertRaisesRegex(ValueError, "different immutable generation"):
            generate_scenarios(self.bundle, sessions=8, seed=932, faults=False, cfg=self.cfg)
        modified = self.path / "tampered"
        import shutil
        shutil.copytree(self.bundle, modified)
        bars = modified / "raw/bars.csv"
        bars.write_text(bars.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "Changed first-release"):
            SyntheticSource(modified, self.path / "bad_raw", self.cfg)

    def test_public_context_obeys_availability_and_truth_cannot_change_source(self):
        folder = self.path / "context_fixture"
        manifest = generate_scenarios(folder, sessions=8, seed=949, faults=True, cfg=self.cfg)
        source = SyntheticSource(folder, self.path / "context_runtime", self.cfg)
        events = pd.read_csv(folder / "source_archive/public_context.csv")
        first = events.iloc[0]
        self.assertEqual(source.available_context(utc(first.available_at) - pd.Timedelta(seconds=1)), [])
        available = source.available_context(first.available_at)
        self.assertEqual(len(available), 1)
        self.assertEqual(available[0]["context_id"], first.context_id)
        self.assertEqual(available[0]["dataset_id"], manifest["dataset_id"])
        self.assertGreater(utc(available[0]["public_at"]), utc(available[0]["available_at"]))
        # Evaluation answers cannot affect a subsequent source construction.
        truth_file = folder / "truth/scenarios.csv"
        truth_file.write_text("scenario_id,future_price\nchanged,999\n")
        reconstructed = SyntheticSource(folder, self.path / "context_runtime_again", self.cfg)
        self.assertEqual(reconstructed.available_context(first.available_at), available)

    def test_fault_report_is_measured_without_database(self):
        folder = self.path / "measured_faults"
        generate_scenarios(folder, sessions=8, seed=947, faults=True, cfg=self.cfg)
        report = evaluate_scenarios(folder)
        self.assertEqual(report["mode"], "synthetic")
        self.assertFalse(report["database_supplied"])
        self.assertTrue(report["all_measured_checks_passed"], report["checks"])
        self.assertFalse(report["scenario_response_passed"], "Source integrity without a detector database is not a passed detector response")
        self.assertEqual(report["forecast_rows"], 0)
        self.assertTrue(all(row["stored_decisions"] == 0 for row in report["responses"]))

    def test_faults_keep_missing_and_split_meaning(self):
        folder = self.path / "faults"
        manifest = generate_scenarios(folder, sessions=8, seed=946, faults=True, cfg=self.cfg)
        tables = read_tables(folder / "raw")
        self.assertTrue(tables["options"].interval_volume.isna().any())
        self.assertTrue(tables["bars"].previous_close_factor.eq(.5).any())
        self.assertTrue(tables["options"].standard_deliverable.eq(False).any())
        cumulative = pd.read_csv(folder / "source_archive/cumulative_volume.csv")
        self.assertIn("negative_cumulative_correction", cumulative.reason.tolist())
        self.assertIn("declared_cumulative_reset", cumulative.reason.tolist())
        panel = pd.read_csv(folder / "panel.csv")
        split = tables["bars"][tables["bars"].previous_close_factor.eq(.5)].iloc[0]
        known = panel[panel.instrument_id.eq("SPY") & (pd.to_datetime(panel.interval_start, utc=True) == split.bar_start)]
        self.assertEqual(len(known), 1)
        self.assertLess(float(known.rv.iloc[0]), .05)
        self.assertAlmostEqual(float(known.underlying_start_price.iloc[0]), split.open / .5)
        report = json.loads((folder / "truth/fault_report.json").read_text())
        omitted = report["omitted_raw_session"]
        self.assertNotIn(omitted, pd.to_datetime(tables["bars"].bar_end, utc=True).dt.strftime("%Y-%m-%d").tolist())
        self.assertIn(omitted, tables["sessions"].session.tolist())


if __name__ == "__main__":
    unittest.main()
