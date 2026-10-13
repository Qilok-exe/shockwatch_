from __future__ import annotations
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from shockwatch.common import dumps
from shockwatch.store import Store
from shockwatch.web import render_site, read_snapshot

class WebsiteTests(unittest.TestCase):
    def test_snapshot_reads_committed_rows_escapes_text_and_preserves_database(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); db=root/"monitor.db"
            store=Store(db,{"dataset_id":"test-synthetic","mode":"synthetic","source":"artificial fixture"})
            hostile="<img src=x onerror=alert(1)>"
            row={"instrument_id":"SPY","decision_at":"2024-03-11T14:40:00+00:00","interval_end":"2024-03-11T14:30:00+00:00","quality_reasons":[hostile],"scores":{"price":2.1},"active_episode":None,"forecast_semantics":"legacy_diagnostic"}
            store.db.execute("INSERT INTO decisions VALUES(?,?,?,?)",("SPY",row["interval_end"],row["decision_at"],dumps(row)))
            store.set("secret_configuration",{"SHOCKWATCH_TELEGRAM_TOKEN":"DO_NOT_EXPORT_SECRET","endpoint":"PRIVATE_ENDPOINT"})
            store.set("last_run",{"started_at":row["decision_at"],"recorded_at":"2024-03-11T14:40:00+00:00","status":"ok","errors":["PRIVATE_ERROR"]})
            store.close()
            before=hashlib.sha256(db.read_bytes()).hexdigest()
            manifest=render_site(db,root/"site")
            self.assertEqual(before,hashlib.sha256(db.read_bytes()).hexdigest())
            site=root/"site"/manifest["snapshot_id"]
            export=json.loads((site/"exports/scans.json").read_text(encoding="utf-8"))
            self.assertEqual(export[0]["scores"]["price"],2.1)
            self.assertEqual(export[0]["dataset_id"],"test-synthetic")
            self.assertEqual(export[0]["mode"],"synthetic")
            page=(site/"scans.html").read_text(encoding="utf-8")
            self.assertIn("&lt;img",page)
            self.assertNotIn(hostile,page)
            self.assertIn("STALE",page)
            assets="".join(p.read_text(encoding="utf-8") for p in site.rglob("*") if p.is_file())
            for secret in ("DO_NOT_EXPORT_SECRET","PRIVATE_ENDPOINT","PRIVATE_ERROR"):
                self.assertNotIn(secret,assets)
            self.assertTrue((site/"forecasts.html").exists())
            self.assertTrue((site/"episodes.html").exists())
    def test_report_cannot_create_database_and_bounds_queries(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with self.assertRaises(FileNotFoundError):
                read_snapshot(root/"missing.db")
            self.assertFalse((root/"missing.db").exists())
            store=Store(root/"db.sqlite"); store.close()
            with self.assertRaises(ValueError):
                read_snapshot(root/"db.sqlite",10001)
    def test_mismatched_experiment_refused_without_switching_published_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); db=root/"monitor.db"
            store=Store(db,{"dataset_id":"one","mode":"synthetic","source":"fixture"});store.close()
            first=render_site(db,root/"site")
            before=(root/"site/index.html").read_bytes()
            wrong=root/"packet.json";wrong.write_text(dumps({"dataset_id":"two","mode":"synthetic"}),encoding="utf-8")
            with self.assertRaises(ValueError):
                render_site(db,root/"site",experiment_file=wrong)
            self.assertEqual(before,(root/"site/index.html").read_bytes())

    def test_latest_outcomes_join_to_displayed_forecasts_despite_revision_limit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=root/"monitor.db"
            store=Store(db,{"dataset_id":"test-synthetic","mode":"synthetic","source":"fixture"})
            # The query/view risk is tested with a minimal SQL ledger. Other
            # contract tests exercise full forecast insertion validation.
            store.db.execute("PRAGMA foreign_keys=OFF")
            for i in range(2):
                f={"forecast_id":f"f{i}","dataset_id":"test-synthetic","mode":"synthetic","forecast_status":"issued","predicted_variance":.1}
                store.db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(f"f{i}","test-synthetic","synthetic","run","SPY","model","policy","2024-01-01T00:00:00Z","2024-01-02T00:00:00Z","2024-01-02T01:00:00Z","next_complete_interval","intraday","issued",.1,dumps(f),str(i)))
            for i,(fid,version) in enumerate((("f1",1),("f0",1),("f0",2))):
                outcome={"forecast_id":fid,"revision":version,"status":"complete","observed_variance":.02,"dataset_id":"test-synthetic","mode":"synthetic"}
                store.db.execute("INSERT INTO forecast_outcomes VALUES(?,?,?,?,?,?,?,?)",(f"o{i}",fid,version,"complete","2024-01-02T02:00:00Z","2024-01-02T01:10:00Z",dumps(outcome),str(i)))
            store.close()
            result=read_snapshot(db,limit=1)
            self.assertEqual(result["tables"]["forecasts"][0]["forecast_id"],"f1")
            self.assertEqual(result["tables"]["forecast_outcomes"][0]["forecast_id"],"f0")
            self.assertEqual(result["latest_forecast_outcomes"][0]["forecast_id"],"f1")

if __name__ == "__main__":
    unittest.main()
