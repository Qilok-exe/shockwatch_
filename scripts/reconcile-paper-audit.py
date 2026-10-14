"""Recompute the first Yahoo report aggregation from stored rows only; no network."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from shockwatch.paper_yahoo import _audit, _state, open_paper_db, UNIVERSE


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",required=True)
    parser.add_argument("--evidence",required=True)
    args=parser.parse_args()
    evidence=Path(args.evidence)
    original=evidence/"FIRST_RUN.json"
    if not original.is_file(): raise ValueError("Original first-run report required")
    db=open_paper_db(args.db)
    try:
        row=db.execute("SELECT run_id,report_json FROM runs WHERE status='ok' ORDER BY completed_at LIMIT 1").fetchone()
        if row is None: raise ValueError("No successful first run")
        run_id,wire=row; report=json.loads(wire)
        if json.loads(original.read_text(encoding="utf-8"))["run_id"]!=run_id:
            raise ValueError("Evidence and database run identity mismatch")
        if report.get("aggregation_revision"):
            raise ValueError("First run already reconciled")
        poll_started=_state(db,"last_option_fetch_at")
        if poll_started!=report["started_at"]:
            raise ValueError("A later option poll prevents first-run reconstruction")
        day=report["started_at"][:10]
        added=0
        for symbol in UNIVERSE:
            for (payload,) in db.execute("SELECT payload_json FROM option_snapshots WHERE symbol=? AND fetch_at>=?",(symbol,poll_started)):
                item=json.loads(payload)
                if item["open_interest"] is None: continue
                cursor=db.execute("INSERT OR IGNORE INTO oi_daily VALUES (?,?,?,?,?)",(symbol,item["contract_symbol"],day,item["open_interest"],item["fetch_at"]))
                added+=cursor.rowcount
        old_counts={s:report["audit"][s]["option_contract_counts"] for s in UNIVERSE}
        report["audit"]={symbol:_audit(db,symbol,report["completed_at"]) for symbol in UNIVERSE}
        report["new_decisions"]=db.execute("SELECT count(*) FROM decisions").fetchone()[0]
        report["aggregation_revision"]="all-expiries-audit-1"
        db.execute("UPDATE runs SET report_json=? WHERE run_id=?",(json.dumps(report,sort_keys=True),run_id))
        db.commit()
        reconciliation={"run_id":run_id,"revision":"all-expiries-audit-1","network_requests":0,
                        "prior_final_expiry_only_counts":old_counts,
                        "corrected_all_expiry_counts":{s:report["audit"][s]["option_contract_counts"] for s in UNIVERSE},
                        "oi_snapshot_rows_added":added,"raw_observations_changed":False}
        (evidence/"RECONCILIATION.json").write_text(json.dumps(reconciliation,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        (evidence/"FIRST_RUN_CORRECTED.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        print(json.dumps(reconciliation,indent=2))
    finally: db.close()


if __name__=="__main__":
    sys.exit(main())
