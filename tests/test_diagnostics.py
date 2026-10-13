import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from shockwatch.common import config, digest, save_json
from shockwatch.diagnostics import doctor
from shockwatch.features import SCHEMAS
from shockwatch.sources import file_hash


class SourceDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.bundle = self.root / "bundle"
        self.runtime.mkdir()
        (self.bundle / "raw").mkdir(parents=True)
        (self.bundle / "source_archive").mkdir()
        self.identity = {"dataset_id": "synthetic_doctor_fixture", "mode": "synthetic"}
        self.cfg = config()
        self.cfg.update(self.identity)
        self.cfg["synthetic"] = True
        self.cfg["notifications"]["enabled"] = False
        save_json(self.runtime / "config.json", self.cfg)
        save_json(self.runtime / "model.json", {**self.identity, "model_id": "fixture", "intraday_target_definition_version": "next_session_intraday_v1"})
        save_json(self.runtime / "policy.json", {**self.identity, "model_id": "fixture", "reviewed": True})
        calendar = "session,open_utc,close_utc\n2024-01-02,2024-01-02T14:30:00Z,2024-01-02T21:00:00Z\n"
        (self.runtime / "sessions.csv").write_text(calendar)
        for name, headers in SCHEMAS.items():
            content = ",".join(headers) + "\n"
            if name == "sessions":
                content = calendar
            elif name == "batches":
                content += "SPY,market,2024-01-02T14:30:00Z,2024-01-02T15:30:00Z,2024-01-02T15:30:45Z,1,True,SYNTHETIC,1\n"
            (self.bundle / "raw" / (name + ".csv")).write_text(content)
        (self.bundle / "panel.csv").write_text("dataset_id,mode\nsynthetic_doctor_fixture,synthetic\n")
        (self.bundle / "source_archive/public_context.csv").write_text("context_id,instrument_id,public_at,available_at\n")
        self.files = ["raw/" + name + ".csv" for name in SCHEMAS] + ["panel.csv", "source_archive/public_context.csv"]
        builder_keys = ("schema_version", "feature_version", "decision_delay_minutes", "max_quote_age_seconds", "max_relative_spread", "min_quote_coverage")
        source_identity = {**self.identity, "builder_config": {key: self.cfg[key] for key in builder_keys}, "content_hashes": {name: file_hash(self.bundle / name) for name in self.files}}
        self.manifest = {**self.identity, "identity": source_identity, "manifest_id": digest(source_identity)}
        save_json(self.bundle / "manifest.json", self.manifest)
        save_json(self.runtime / "dataset.json", self.manifest)
        save_json(self.runtime / "deployment.json", {**self.identity, "bundle": str(self.bundle)})

    def tearDown(self):
        self.tmp.cleanup()

    def check_by_name(self, report, name):
        return next(row for row in report["checks"] if row["name"] == name)

    def publish_checkpoint(self):
        (self.runtime / "inbox").mkdir()
        filename = "cohort_20240102T153000Z.csv"
        path = self.runtime / "inbox" / filename
        path.write_text("instrument_id,interval_end\nSPY,2024-01-02T15:30:00Z\n")
        cursor = {**self.identity, "manifest_id": self.manifest["manifest_id"], "emitted": {"2024-01-02T15:30:00+00:00": file_hash(path)}, "virtual_asof": "2024-01-02T15:40:00Z", "wall_processed_at": "2026-10-13T00:00:00Z"}
        save_json(self.runtime / "source_checkpoint.json", cursor)
        return path, filename, cursor

    def test_intact_source_is_read_only_and_known_cursor_passes(self):
        _, _, _ = self.publish_checkpoint()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = doctor(self.runtime)
        self.assertEqual(report["status"], "ok", report["checks"])
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.runtime / "monitor.db").exists())

    def test_changed_raw_content_and_removed_bundle_are_reported(self):
        raw = self.bundle / "raw/options.csv"
        raw.write_text(raw.read_text() + "\n")
        report = doctor(self.runtime)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(self.check_by_name(report, "source content hashes")["ok"])
        deployment = {**self.identity, "bundle": str(self.root / "absent")}
        save_json(self.runtime / "deployment.json", deployment)
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source bundle")["ok"])

    def test_nonobject_json_produces_diagnostic_without_exception(self):
        for name in ("config.json", "deployment.json"):
            with self.subTest(name=name):
                path = self.runtime / name
                original = path.read_bytes()
                path.write_text("[]")
                report = doctor(self.runtime)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(self.check_by_name(report, name)["detail"], "Expected a JSON object")
                path.write_bytes(original)

    def test_manifest_and_panel_identity_and_builder_disagreement(self):
        changed = json.loads(json.dumps(self.manifest))
        changed["manifest_id"] = "0" * 64
        save_json(self.bundle / "manifest.json", changed)
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source manifest hash")["ok"])
        save_json(self.bundle / "manifest.json", self.manifest)
        cfg = {**self.cfg, "decision_delay_minutes": 20}
        save_json(self.runtime / "config.json", cfg)
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source builder configuration")["ok"])
        save_json(self.runtime / "config.json", self.cfg)
        (self.bundle / "panel.csv").write_text("dataset_id,mode\nforeign,synthetic\n")
        changed = json.loads(json.dumps(self.manifest))
        changed["identity"]["content_hashes"]["panel.csv"] = file_hash(self.bundle / "panel.csv")
        changed["manifest_id"] = digest(changed["identity"])
        save_json(self.bundle / "manifest.json", changed)
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source panel identity")["ok"])

    def test_checkpoint_identity_hash_and_clock_corruption_are_reported(self):
        path, _, cursor = self.publish_checkpoint()
        cursor["manifest_id"] = "wrong"
        save_json(self.runtime / "source_checkpoint.json", cursor)
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source checkpoint identity")["ok"])
        cursor["manifest_id"] = self.manifest["manifest_id"]
        cursor["virtual_asof"] = "2024-01-02T14:30:00Z"
        save_json(self.runtime / "source_checkpoint.json", cursor)
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source checkpoint clock")["ok"])
        cursor["virtual_asof"] = "2024-01-02T15:40:00Z"
        save_json(self.runtime / "source_checkpoint.json", cursor)
        path.write_text(path.read_text() + "changed\n")
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source checkpoint deliveries")["ok"])

    def test_archived_cohort_is_verified_against_accepted_ledger(self):
        path, filename, cursor = self.publish_checkpoint()
        connection = sqlite3.connect(self.runtime / "monitor.db")
        connection.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE files(hash TEXT PRIMARY KEY,name TEXT,imported_at TEXT);")
        connection.execute("INSERT INTO meta VALUES(?,?)", ("dataset_identity", json.dumps(self.identity)))
        connection.execute("INSERT INTO files VALUES(?,?,?)", (next(iter(cursor["emitted"].values())), filename, "2024-01-02T15:40:00Z"))
        connection.commit(); connection.close()
        path.unlink()
        report = doctor(self.runtime)
        self.assertEqual(report["status"], "ok", report["checks"])
        self.assertTrue(self.check_by_name(report, "source checkpoint deliveries")["ok"])

    def test_manifest_path_cannot_escape_its_bundle(self):
        outside = self.root / "outside.csv"
        outside.write_text("outside")
        changed = json.loads(json.dumps(self.manifest))
        changed["identity"]["content_hashes"]["../outside.csv"] = file_hash(outside)
        changed["manifest_id"] = digest(changed["identity"])
        save_json(self.bundle / "manifest.json", changed)
        report = doctor(self.runtime)
        self.assertFalse(self.check_by_name(report, "source content hashes")["ok"])


if __name__ == "__main__":
    unittest.main()
