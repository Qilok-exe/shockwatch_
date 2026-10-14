"""Offline calendar audit and explicit first-run decision-coverage correction."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shockwatch.paper_yahoo import open_paper_db, _latest_complete_interval, UNIVERSE
from shockwatch.yahoo_source import market_schedule, CALENDAR_VERSION


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",required=True); parser.add_argument("--evidence",required=True)
    args=parser.parse_args(); evidence=Path(args.evidence)
    db=open_paper_db(args.db)
    try:
        issues=[]; total=0; missing=Counter()
        for symbol in UNIVERSE:
            by_day={}
            for start_text,end_text,day in db.execute("SELECT bar_start_utc,bar_end_utc,session_date FROM stock_bars WHERE symbol=? ORDER BY bar_start_utc",(symbol,)):
                total+=1; start=pd.Timestamp(start_text); end=pd.Timestamp(end_text)
                session=market_schedule(symbol,day)
                if session is None or start<session[0] or end>session[1] or end-start!=pd.Timedelta(minutes=5) or (start-session[0]).total_seconds()%300!=0:
                    issues.append({"symbol":symbol,"bar_start_utc":start_text,"reason":"invalid_calendar_bar"})
                by_day.setdefault(day,set()).add(start_text)
            for day,actual in by_day.items():
                session=market_schedule(symbol,day)
                if session is None: continue
                cursor=session[0]
                while cursor<session[1]:
                    if cursor.isoformat() not in actual: missing[symbol]+=1
                    cursor+=pd.Timedelta(minutes=5)
        audit={"calendar":"NYSE for SPY/LMT; NASDAQ for UAL","calendar_version":CALENDAR_VERSION,
               "stock_bars_checked":total,"invalid_calendar_bars":issues[:100],"invalid_count":len(issues),
               "missing_scheduled_five_minute_bars":dict(missing),"network_requests":0,
               "raw_observations_changed":False}
        (evidence/"CALENDAR_AUDIT.json").write_text(json.dumps(audit,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        if issues or any(missing.values()):
            raise ValueError("Calendar audit found invalid or missing stock bars; first-run decision was not reconciled")
        first=db.execute("SELECT run_id,report_json FROM runs WHERE status='ok' ORDER BY completed_at LIMIT 1").fetchone()
        if first is None: raise ValueError("No successful first run")
        first_report=json.loads(first[1]); interval_changes={}
        for symbol in UNIVERSE:
            interval=_latest_complete_interval(db,symbol,first_report["started_at"])
            if interval is None: raise ValueError(f"No completed interval for {symbol}")
            current=db.execute("SELECT payload_json FROM decisions WHERE symbol=? AND interval_end=?",(symbol,interval["end"])).fetchone()
            if current is None: raise ValueError(f"Missing first decision for {symbol}")
            decision=json.loads(current[0])
            count=db.execute("SELECT count(*) FROM stock_bars WHERE symbol=? AND bar_start_utc>=? AND bar_end_utc<=?",(symbol,interval["start"],interval["end"])).fetchone()[0]
            before={"stock_coverage_bars":decision["stock_coverage_bars"],"required_bars":decision["required_bars"]}
            decision.update(interval_start=interval["start"],stock_coverage_bars=count,required_bars=interval["required_bars"],
                            status="WARMING_UP" if count==interval["required_bars"] else "QUALITY_EXCLUDED",
                            quality_reasons=[] if count==interval["required_bars"] else ["incomplete_stock_interval"])
            db.execute("UPDATE decisions SET status=?,payload_json=? WHERE symbol=? AND interval_end=?",
                       (decision["status"],json.dumps(decision,sort_keys=True),symbol,interval["end"]))
            interval_changes[symbol]={"before":before,"after":{"stock_coverage_bars":count,
                                     "required_bars":interval["required_bars"],"interval_start":interval["start"]}}
        for run_id,wire in db.execute("SELECT run_id,report_json FROM runs WHERE status='ok'").fetchall():
            report=json.loads(wire)
            report["decisions"]=[json.loads(db.execute("SELECT payload_json FROM decisions WHERE symbol=? AND interval_end=?",
                (d["symbol"],d["interval_end"])).fetchone()[0]) if d.get("interval_end") else d for d in report["decisions"]]
            report["post_ingestion_calendar_audit"]={"calendar_version":CALENDAR_VERSION,
                "invalid_count":0,"missing_scheduled_bars":0,"interval_coverage_reconciled":True}
            db.execute("UPDATE runs SET report_json=? WHERE run_id=?",(json.dumps(report,sort_keys=True),run_id))
            if run_id==first[0]:
                (evidence/"FIRST_RUN_CALENDAR_AUDITED.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        db.commit()
        audit["interval_coverage_correction"]=interval_changes
        (evidence/"CALENDAR_AUDIT.json").write_text(json.dumps(audit,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        print(json.dumps({"stock_bars_checked":total,"invalid_count":0,"missing_scheduled_bars":0,
                          "interval_coverage_correction":interval_changes},indent=2))
    finally: db.close()


if __name__=="__main__": sys.exit(main())
