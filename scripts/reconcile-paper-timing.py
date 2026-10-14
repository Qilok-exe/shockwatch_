"""Offline conservative timing correction for the first paper run; no network."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shockwatch.paper_yahoo import open_paper_db, UNIVERSE


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",required=True); parser.add_argument("--evidence",required=True)
    args=parser.parse_args(); evidence=Path(args.evidence)
    db=open_paper_db(args.db)
    try:
        row=db.execute("SELECT run_id,report_json FROM runs WHERE status='ok' ORDER BY completed_at LIMIT 1").fetchone()
        if row is None: raise ValueError("No successful first run")
        run_id,wire=row; first=json.loads(wire)
        if first.get("timing_revision"): raise ValueError("First run already reconciled")
        prior=evidence/"FIRST_RUN_CALENDAR_AUDITED.json"
        if not prior.is_file() or json.loads(prior.read_text(encoding="utf-8"))["run_id"]!=run_id:
            raise ValueError("Prior calendar audit evidence required")
        corrected=first["completed_at"]
        latest_response=max(pd.Timestamp(x["received_at"]) for x in first["requests"])
        if pd.Timestamp(corrected)<latest_response:
            raise ValueError("Run completion precedes received source data")
        changes={}
        for symbol in UNIVERSE:
            forecast=db.execute("SELECT interval_end,payload_json FROM forecasts WHERE symbol=? ORDER BY interval_end LIMIT 1",(symbol,)).fetchone()
            decision=db.execute("SELECT interval_end,payload_json FROM decisions WHERE symbol=? ORDER BY interval_end LIMIT 1",(symbol,)).fetchone()
            if forecast is None or decision is None or forecast[0]!=decision[0]:
                raise ValueError(f"First-run decision/forecast mismatch for {symbol}")
            f=json.loads(forecast[1]); d=json.loads(decision[1])
            if pd.Timestamp(f["issued_at"])>=latest_response:
                raise ValueError("Forecast does not have the documented first-run backdating issue")
            changes[symbol]={"forecast_issue_before":f["issued_at"],"decision_recorded_before":d["recorded_at"],
                             "forecast_issue_after":corrected,"decision_recorded_after":corrected,
                             "predicted_variance_unchanged":f["predicted_variance"]}
            f["issued_at"]=corrected; d["recorded_at"]=corrected
            db.execute("UPDATE forecasts SET issued_at=?,payload_json=? WHERE symbol=? AND interval_end=?",
                       (corrected,json.dumps(f,sort_keys=True),symbol,forecast[0]))
            db.execute("UPDATE decisions SET payload_json=? WHERE symbol=? AND interval_end=?",
                       (json.dumps(d,sort_keys=True),symbol,decision[0]))
        for current_run_id,current_wire in db.execute("SELECT run_id,report_json FROM runs WHERE status='ok'").fetchall():
            report=json.loads(current_wire)
            report["decisions"]=[json.loads(db.execute("SELECT payload_json FROM decisions WHERE symbol=? AND interval_end=?",
                (d["symbol"],d["interval_end"])).fetchone()[0]) if d.get("interval_end") else d for d in report["decisions"]]
            report["forecasts"]=[json.loads(db.execute("SELECT payload_json FROM forecasts WHERE symbol=? AND interval_end=?",
                (f["symbol"],f["asof_interval_end"])).fetchone()[0]) if f.get("status")=="OPERATIONAL" else f for f in report["forecasts"]]
            if current_run_id==run_id:
                report["timing_revision"]="completed-run-upper-bound-1"
                (evidence/"FIRST_RUN_TIMING_AUDITED.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8")
            db.execute("UPDATE runs SET report_json=? WHERE run_id=?",(json.dumps(report,sort_keys=True),current_run_id))
        db.commit()
        result={"run_id":run_id,"network_requests":0,"latest_source_response_at":latest_response.isoformat(),
                "conservative_corrected_time":corrected,"changes":changes,"raw_observations_changed":False,
                "forecast_values_changed":False}
        (evidence/"TIMING_RECONCILIATION.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        print(json.dumps(result,indent=2))
    finally:db.close()


if __name__=="__main__":sys.exit(main())
