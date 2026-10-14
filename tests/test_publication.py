import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd

from shockwatch.publication import export_public_site, validate_public_site
from shockwatch.experiments import _family_metrics
from shockwatch.scenarios import expected_outcome
from shockwatch.web import TABLES


def snapshot(mode="synthetic"):
    tables={name:[] for name in TABLES}
    tables["decisions"]=[{"dataset_id":"fixture","mode":mode,"instrument_id":"UAL","decision_at":"2024-01-02T15:40:00+00:00",
        "scores":{"flow_call":8.,"flow_put":0.,"price":.5},"current_decision_evidence":["A_exceptional_flow"],
        "active_episode":{"episode_id":"episode-1","reasons":["A_exceptional_flow"]},"quality_reasons":[],
        "private_detail":"DO_NOT_EXPORT_SECRET"}]
    tables["events"]=[{"episode_id":"episode-1","instrument_id":"UAL","decision_at":"2024-01-02T15:40:00+00:00","type":"opened","reasons":["A_exceptional_flow"],"private_detail":"DO_NOT_EXPORT_SECRET"}]
    return {"dataset":{"dataset_id":"fixture","mode":mode,"source_version":"synthetic-v1"},"tables":tables,
        "last_run":{"recorded_at":"2024-01-02T15:41:00+00:00","status":"ok"},"watermark":"2024-01-02T15:40:00+00:00",
        "health":{"status":"healthy"},"outbox_counts":{},"deployment_id":"fixture-v1",
        "models":[{"model_version":"0.2.0"}],"policies":[{"reviewed":True,"policy":{"extreme_flow":6.5,"extreme_price":6.5}}],
        "latest_forecast_outcomes":[]}


class PublicSiteTests(unittest.TestCase):
    def test_scenario_expectations_do_not_confuse_context_or_route_a_with_a_miss(self):
        self.assertEqual(expected_outcome("repricing"),"expected_opening")
        self.assertEqual(expected_outcome("quiet_liquid"),"expected_no_alert")
        self.assertEqual(expected_outcome("unsupported_flow"),"expected_no_route_b")
        self.assertEqual(expected_outcome("coverage_faults"),"quality_exclusion_expected")
        self.assertEqual(expected_outcome("market_stress"),"context_only_not_applicable")

    def test_family_miss_explains_preexisting_episode_suppression(self):
        labels=pd.DataFrame([{"event_group":"case-a","instrument_id":"UAL","window_start":"2024-01-02T14:30:00+00:00","public_at":"2024-01-03T14:30:00+00:00"}])
        included=[{"event_group":"case-a","partition":"test","instruments":["UAL"],"case_ids":["a"],
                   "window_start":"2024-01-02T14:30:00+00:00","public_at":"2024-01-03T14:30:00+00:00"}]
        calendar=pd.DataFrame([{"session":"2024-01-02","open_utc":"2024-01-02T14:30:00+00:00","close_utc":"2024-01-02T21:00:00+00:00"}])
        decisions=[{"instrument_id":"UAL","decision_at":"2024-01-02T15:40:00+00:00","active_episode":{"episode_id":"old"},
                    "current_decision_evidence":["A_exceptional_flow"],"transition":[]}]
        result=_family_metrics([],labels,included,{("UAL","2024-01-02")},calendar,decisions=decisions)
        self.assertEqual(result["performance_status"],"FAIL")
        self.assertEqual(result["families"][0]["miss_reason"],"pre_existing_episode_suppressed_new_opening")

    def test_explicit_export_allowlist_and_relative_links(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("shockwatch.publication.read_snapshot",return_value=snapshot()):
                result=export_public_site("unused.db",directory)
            self.assertTrue(result["validation"]["passed"])
            content="".join(p.read_text(encoding="utf-8") for p in Path(directory).rglob("*") if p.is_file())
            self.assertNotIn("DO_NOT_EXPORT_SECRET",content)
            scans=json.loads((Path(directory)/"exports/scans.json").read_text(encoding="utf-8"))
            self.assertEqual(scans[0]["current_decision_evidence"],["A_exceptional_flow"])
            self.assertEqual(scans[0]["original_episode_opening_reasons"],["A_exceptional_flow"])
            self.assertTrue((Path(directory)/".nojekyll").is_file())
            self.assertNotIn(b"\r\r\n",(Path(directory)/"exports/scans.csv").read_bytes())
            for public_file in Path(directory).rglob("*"):
                if public_file.is_file():
                    self.assertNotIn(b"\r\n",public_file.read_bytes(),str(public_file))
            (Path(directory)/"index.html").write_text("<a href='/shockwatch_/scans.html'>bad</a>",encoding="utf-8")
            self.assertFalse(validate_public_site(directory)["passed"])

    def test_real_mode_refused_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("shockwatch.publication.read_snapshot",return_value=snapshot("live")):
                with self.assertRaisesRegex(ValueError,"refuses real modes"):
                    export_public_site("unused.db",directory)
            self.assertEqual(list(Path(directory).iterdir()),[])


if __name__ == "__main__":
    unittest.main()
