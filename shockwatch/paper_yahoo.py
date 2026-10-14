"""Isolated, observation-only paper Yahoo ledger and scheduled cycle.

The reviewed detector is deliberately not imported: Yahoo snapshots cannot
provide its vendor delta or historical option-flow training series.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from .yahoo_source import YahooSource, YahooSourceError, SOURCE_VERSION, market_schedule, CALENDAR_VERSION

UNIVERSE = ("SPY", "UAL", "LMT")
PAPER_MODEL_VERSION = "paper-stock-ewma-1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (dataset_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode='paper'), source TEXT NOT NULL CHECK(source='yahoo'), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (request_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, requested_at TEXT NOT NULL, received_at TEXT, response_hash TEXT, rows_received INTEGER, rows_accepted INTEGER, rows_rejected INTEGER, metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stock_bars (symbol TEXT NOT NULL, bar_start_utc TEXT NOT NULL, bar_end_utc TEXT NOT NULL, session_date TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(symbol,bar_start_utc));
CREATE TABLE IF NOT EXISTS option_snapshots (symbol TEXT NOT NULL, expiry TEXT NOT NULL, side TEXT NOT NULL, contract_symbol TEXT NOT NULL, fetch_at TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(symbol,expiry,side,contract_symbol,fetch_at));
CREATE TABLE IF NOT EXISTS oi_daily (symbol TEXT NOT NULL, contract_symbol TEXT NOT NULL, session_date TEXT NOT NULL, open_interest INTEGER NOT NULL, fetch_at TEXT NOT NULL, PRIMARY KEY(symbol,contract_symbol,session_date));
CREATE TABLE IF NOT EXISTS oi_comparisons (symbol TEXT NOT NULL, contract_symbol TEXT NOT NULL, session_date TEXT NOT NULL, prior_session_date TEXT NOT NULL, prior_oi INTEGER NOT NULL, current_oi INTEGER NOT NULL, position_difference INTEGER NOT NULL, compared_at TEXT NOT NULL, PRIMARY KEY(symbol,contract_symbol,session_date));
CREATE TABLE IF NOT EXISTS quality_issues (issue_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, row_key TEXT, reason TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (symbol TEXT NOT NULL, interval_end TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(symbol,interval_end));
CREATE TABLE IF NOT EXISTS forecasts (symbol TEXT NOT NULL, issued_at TEXT NOT NULL, interval_end TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(symbol,interval_end));
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT NOT NULL, status TEXT NOT NULL, report_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
PAPER_TABLES = {"identity","requests","stock_bars","option_snapshots","oi_daily","oi_comparisons",
                "quality_issues","decisions","forecasts","runs","state"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state(db, key):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _set(db, key, value):
    db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, sort_keys=True)))


def open_paper_db(path, dataset_id=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    existing_tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if existing_tables and ("identity" not in existing_tables or not existing_tables<=PAPER_TABLES):
        db.close()
        raise ValueError("Refusing to open a non-paper or mixed SQLite database")
    db.executescript(SCHEMA)
    identity = db.execute("SELECT dataset_id,mode,source FROM identity").fetchone()
    if identity is None:
        if not dataset_id or not dataset_id.startswith("paper_yahoo_"):
            db.close()
            raise ValueError("New paper database requires an explicit paper_yahoo_ dataset ID")
        db.execute("INSERT INTO identity VALUES (?,?,?,?)", (dataset_id, "paper", "yahoo", _now()))
        db.commit()
    elif dataset_id and identity != (dataset_id, "paper", "yahoo"):
        db.close()
        raise ValueError("Paper database identity mismatch")
    elif identity[1:] != ("paper", "yahoo"):
        db.close()
        raise ValueError("Database is not a paper-Yahoo dataset")
    return db


def _issue(db, run_id, symbol, kind, key, reason, at):
    issue_key = _hash([run_id, symbol, kind, key, reason])
    db.execute("INSERT OR IGNORE INTO quality_issues VALUES (?,?,?,?,?,?,?)",
               (issue_key, run_id, symbol, kind, key, reason, at))


def _record_response(db, run_id, response):
    accepted = 0
    duplicates = 0
    conflicts = 0
    for item in response.rows:
        payload = json.dumps(item, sort_keys=True, allow_nan=False)
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        if response.kind == "history":
            key = (item["symbol"], item["bar_start_utc"])
            table = "stock_bars"
            existing = db.execute("SELECT payload_json FROM stock_bars WHERE symbol=? AND bar_start_utc=?", key).fetchone()
            if existing:
                # Yahoo may revise intraday data. Record the conflict; never silently rewrite it.
                prior=json.loads(existing[0])
                comparable=lambda value:{k:v for k,v in value.items() if k not in
                    ("received_at","source","source_version","library_version","exchange_calendar","calendar_version")}
                if comparable(prior) != comparable(item):
                    _issue(db, run_id, response.symbol, "history", item["bar_start_utc"], "conflicting_retry_or_revised_bar", response.received_at)
                    conflicts += 1
                else:
                    duplicates += 1
                continue
            db.execute("INSERT INTO stock_bars VALUES (?,?,?,?,?,?)",
                       (item["symbol"], item["bar_start_utc"], item["bar_end_utc"], item["session_date"], payload_hash, payload))
        else:
            key = (item["symbol"], item["expiry"], item["side"], item["contract_symbol"], item["fetch_at"])
            table = "option_snapshots"
            existing = db.execute("SELECT payload_hash FROM option_snapshots WHERE symbol=? AND expiry=? AND side=? AND contract_symbol=? AND fetch_at=?", key).fetchone()
            if existing:
                if existing[0] != payload_hash:
                    _issue(db, run_id, response.symbol, "chain", item["contract_symbol"], "conflicting_retry_or_revised_contract", response.received_at)
                    conflicts += 1
                else:
                    duplicates += 1
                continue
            db.execute("INSERT INTO option_snapshots VALUES (?,?,?,?,?,?,?)", (*key, payload_hash, payload))
        accepted += 1
    for issue in response.issues:
        _issue(db, run_id, response.symbol, response.kind, issue["key"], issue["reason"], response.received_at)
    request_key = _hash([run_id, response.kind, response.symbol, response.request])
    db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (request_key, run_id, response.symbol, response.kind, response.request["requested_at"],
                response.received_at, response.response_hash, response.metadata["rows_received"], accepted,
                len(response.issues)+conflicts, json.dumps({"request": response.request, **response.metadata,
                    "duplicate_unchanged":duplicates,"conflicting_revisions":conflicts}, sort_keys=True)))
    return {"kind": response.kind, "symbol": response.symbol, "request": response.request,
            "received_at": response.received_at, "response_hash": response.response_hash,
            "rows_received": response.metadata["rows_received"], "rows_accepted": accepted,
            "rows_rejected": len(response.issues)+conflicts,"duplicate_unchanged":duplicates,
            "conflicting_revisions":conflicts,
            "issues_by_reason": dict(Counter(i["reason"] for i in response.issues)),
            "metadata": response.metadata}


def _record_expiries(db, run_id, symbol, values, record):
    db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (_hash([run_id, symbol, "expiries"]), run_id, symbol, "expiries", record["requested_at"],
                record["received_at"], record["response_hash"], len(values), len(values), 0,
                json.dumps({"request": {"method": "Ticker.options", "symbol": symbol}, "expiries": list(values)}, sort_keys=True)))


def _latest_complete_interval(db, symbol, now, interval_minutes=60, delay_minutes=10):
    rows = db.execute("SELECT bar_start_utc,session_date FROM stock_bars WHERE symbol=? ORDER BY bar_start_utc DESC LIMIT 100", (symbol,)).fetchall()
    if not rows:
        return None
    for start_text, session_date in rows:
        start = pd.Timestamp(start_text)
        session=market_schedule(symbol,session_date)
        if session is None: continue
        market_open,market_close=session
        elapsed=(start+pd.Timedelta(minutes=5)-market_open).total_seconds()/60
        boundary=max(interval_minutes,math.ceil(elapsed/interval_minutes)*interval_minutes)
        end=min(market_open+pd.Timedelta(minutes=boundary),market_close)
        previous=max(market_open,end-pd.Timedelta(minutes=interval_minutes))
        # A short closing interval starts at the preceding anchored boundary.
        if end==market_close:
            full_slots=math.floor((market_close-market_open).total_seconds()/60/interval_minutes)
            previous=market_open+pd.Timedelta(minutes=full_slots*interval_minutes)
            if previous==market_close: previous-=pd.Timedelta(minutes=interval_minutes)
        if pd.Timestamp(now) >= end+pd.Timedelta(minutes=delay_minutes):
            return {"start":previous.isoformat(),"end":end.isoformat(),
                    "required_bars":int((end-previous).total_seconds()/300)}
    return None


def _forecast(db, symbol, interval_end, now):
    existing=db.execute("SELECT payload_json FROM forecasts WHERE symbol=? AND interval_end=?",(symbol,interval_end)).fetchone()
    if existing:
        return json.loads(existing[0])
    rows = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM stock_bars WHERE symbol=? AND bar_end_utc<=? ORDER BY bar_start_utc DESC LIMIT 500", (symbol, interval_end))]
    rows.reverse()
    returns = []
    for prev, cur in zip(rows, rows[1:]):
        if prev["session_date"] != cur["session_date"] or pd.Timestamp(cur["bar_start_utc"]) != pd.Timestamp(prev["bar_end_utc"]):
            continue
        if prev["adjusted_close"] is None or cur["adjusted_close"] is None:
            continue
        returns.append(math.log(cur["adjusted_close"]/prev["adjusted_close"]))
    if len(returns) < 120:
        return {"symbol":symbol,"status": "INSUFFICIENT_HISTORY", "eligible_returns": len(returns), "required_returns": 120,
                "reason": "At least 120 valid adjacent intraday adjusted five-minute returns are required"}
    sample = returns[-120:]
    variance = sum(x*x for x in sample[:20])/20
    for value in sample[20:]:
        variance = 0.94*variance+0.06*value*value
    result = {"status": "OPERATIONAL", "symbol": symbol, "mode": "paper", "source": "yahoo",
              "model_version": PAPER_MODEL_VERSION, "issued_at": now, "asof_interval_end": interval_end,
              "horizon": "next observed regular-session five-minute adjusted return",
              "predicted_variance": variance, "predicted_volatility": math.sqrt(variance),
              "eligible_returns": len(returns), "lambda": 0.94,
              "limitation": "Research forecast; no predictive market validity established"}
    db.execute("INSERT OR IGNORE INTO forecasts VALUES (?,?,?,?)", (symbol, now, interval_end, json.dumps(result, sort_keys=True)))
    return result


def _decision(db, symbol, now):
    interval = _latest_complete_interval(db, symbol, now)
    if interval is None:
        return {"symbol": symbol, "status": "INSUFFICIENT_HISTORY", "reason": "No completed regular-session decision interval"}
    end=interval["end"]
    existing = db.execute("SELECT payload_json FROM decisions WHERE symbol=? AND interval_end=?", (symbol, end)).fetchone()
    if existing:
        return json.loads(existing[0])
    bar_count = db.execute("SELECT count(*) FROM stock_bars WHERE symbol=? AND bar_end_utc<=? AND bar_start_utc>=?", (symbol,end,interval["start"])).fetchone()[0]
    result = {"symbol": symbol, "interval_end": end, "recorded_at": _now(),
              "interval_start":interval["start"],"stock_coverage_bars": bar_count, "required_bars": interval["required_bars"],
              "status": "WARMING_UP" if bar_count==interval["required_bars"] else "QUALITY_EXCLUDED",
              "flow_score": None, "pricing_score": None, "oi_confirmation": None,
              "routes_evaluated": [], "episode_transition": None,
              "unavailable_reasons": ["No historical Yahoo option-chain series for same-interval flow baseline",
                                      "No dependable vendor delta or validated real-data calibration",
                                      "OI needs two valid daily snapshots of identical contracts"],
              "quality_reasons": [] if bar_count==interval["required_bars"] else ["incomplete_stock_interval"],
              "note": "Unavailable inputs never create an alert"}
    db.execute("INSERT INTO decisions VALUES (?,?,?,?)", (symbol, end, result["status"], json.dumps(result, sort_keys=True)))
    return result


def _audit(db, symbol, now):
    latest = db.execute("SELECT payload_json FROM stock_bars WHERE symbol=? ORDER BY bar_start_utc DESC LIMIT 1", (symbol,)).fetchone()
    latest_bar = json.loads(latest[0]) if latest else None
    latest_fetch = db.execute("SELECT max(fetch_at) FROM option_snapshots WHERE symbol=?", (symbol,)).fetchone()[0]
    poll_started = _state(db,"last_option_fetch_at")
    rows = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM option_snapshots WHERE symbol=? AND fetch_at>=?", (symbol, poll_started))] if latest_fetch and poll_started else []
    counts = Counter(f"{r['side']}:{r['dte_group']}" for r in rows)
    stale = sum(r["last_trade_stale"] for r in rows)
    missing_volume = sum(r["volume"] is None for r in rows)
    missing_oi = sum(r["open_interest"] is None for r in rows)
    issues = dict(db.execute("SELECT reason,count(*) FROM quality_issues WHERE symbol=? GROUP BY reason", (symbol,)))
    hashes = [r[0] for r in db.execute("SELECT response_hash FROM requests WHERE symbol=? AND response_hash IS NOT NULL ORDER BY requested_at DESC LIMIT 100", (symbol,))]
    distinct_oi_days = db.execute("SELECT count(DISTINCT session_date) FROM oi_daily WHERE symbol=?", (symbol,)).fetchone()[0]
    matched_oi_contracts=db.execute("SELECT count(*) FROM oi_comparisons WHERE symbol=?",(symbol,)).fetchone()[0]
    return {"symbol": symbol, "latest_stock_bar_utc": latest_bar["bar_end_utc"] if latest_bar else None,
            "latest_stock_close": latest_bar["close"] if latest_bar else None,
            "exchange_timezone": latest_bar["exchange_timezone"] if latest_bar else None,
            "stock_bars_accepted": db.execute("SELECT count(*) FROM stock_bars WHERE symbol=?",(symbol,)).fetchone()[0],
            "stock_first_bar_utc": db.execute("SELECT min(bar_start_utc) FROM stock_bars WHERE symbol=?",(symbol,)).fetchone()[0],
            "latest_option_fetch_utc": latest_fetch, "option_contract_counts": dict(counts),
            "option_expiry_count": len({r["expiry"] for r in rows}), "stale_last_trades": stale,
            "missing_option_volume": missing_volume, "missing_open_interest": missing_oi,
            "rejections": issues, "response_hashes": hashes, "oi_snapshot_days": distinct_oi_days,
            "oi_status": "UNAVAILABLE_FIRST_SNAPSHOT" if distinct_oi_days<2 else
                         "COMPARISON_AVAILABLE_UNCALIBRATED" if matched_oi_contracts else "COMPARISON_PENDING_OR_NO_MATCH",
            "matched_oi_contracts":matched_oi_contracts,
            "oi_publication_time": "uncertain; Yahoo does not establish when positions were published",
            "source_version": latest_bar.get("source_version") if latest_bar else None,
            "option_source_versions":sorted({r.get("source_version") for r in rows if r.get("source_version")})}


def _compare_daily_oi(db, now):
    """Once after a completed session; levels only, never buyer initiation."""
    local=pd.Timestamp(now).tz_convert("America/New_York")
    session=market_schedule("SPY",local.date().isoformat())
    if session is None or pd.Timestamp(now)<session[1]:
        return {"status":"WAITING_FOR_COMPLETED_TRADING_DAY","matched_contracts":0}
    day=local.date().isoformat()
    if _state(db,"last_oi_compared_day")==day:
        return {"status":"ALREADY_COMPARED","matched_contracts":0}
    matched=0
    for symbol in UNIVERSE:
        prior=db.execute("SELECT max(session_date) FROM oi_daily WHERE symbol=? AND session_date<?",(symbol,day)).fetchone()[0]
        if prior is None: continue
        for contract,current in db.execute("SELECT contract_symbol,open_interest FROM oi_daily WHERE symbol=? AND session_date=?",(symbol,day)):
            previous=db.execute("SELECT open_interest FROM oi_daily WHERE symbol=? AND contract_symbol=? AND session_date=?",(symbol,contract,prior)).fetchone()
            if previous is None: continue
            db.execute("INSERT OR IGNORE INTO oi_comparisons VALUES (?,?,?,?,?,?,?,?)",
                       (symbol,contract,day,prior,previous[0],current,current-previous[0],now))
            matched+=1
    if matched:
        _set(db,"last_oi_compared_day",day)
        return {"status":"COMPARISON_AVAILABLE_UNCALIBRATED","matched_contracts":matched,
                "limitation":"Yahoo publication time uncertain; position difference is not buyer initiation or alert confirmation"}
    return {"status":"INSUFFICIENT_DAILY_MATCHES","matched_contracts":0}


def run_cycle(db_path, dataset_id, source=None, *, now=None, force=False):
    """At most one request per symbol/method/expiry; no endpoint retry loop."""
    now = pd.Timestamp(now or _now()).tz_convert("UTC").isoformat()
    db = open_paper_db(db_path, dataset_id)
    source = source or YahooSource()
    run_id = _hash([dataset_id, now])[:24]
    report = {"run_id": run_id, "dataset_id": dataset_id, "mode": "paper", "source": "yahoo",
              "started_at": now, "source_version": SOURCE_VERSION, "library_version": source.library_version,
              "requests": [], "warnings": [], "status": "running", "decisions": [], "forecasts": [], "audit": {},
              "episodes_opened": 0, "routes_evaluated": 0}
    try:
        db.execute("BEGIN IMMEDIATE")
        last = _state(db, "last_stock_fetch_at")
        local=pd.Timestamp(now).tz_convert("America/New_York")
        session=market_schedule("SPY",local.date().isoformat())
        in_poll_window=bool(session and session[0]<=pd.Timestamp(now)<=session[1]+pd.Timedelta(minutes=15))
        stock_due = force or (in_poll_window and (last is None or (pd.Timestamp(now)-pd.Timestamp(last)).total_seconds()>=300))
        option_last = _state(db, "last_option_fetch_at")
        options_due = force or (in_poll_window and (option_last is None or (pd.Timestamp(now)-pd.Timestamp(option_last)).total_seconds()>=3600))
        if stock_due:
            for symbol in UNIVERSE:
                backfill=db.execute("SELECT count(*) FROM stock_bars WHERE symbol=?",(symbol,)).fetchone()[0]==0
                response = source.history(symbol,backfill=backfill)
                report["requests"].append(_record_response(db, run_id, response))
            _set(db, "last_stock_fetch_at", now)
        if options_due:
            for symbol in UNIVERSE:
                ticker, expiries, request = source.expiries(symbol)
                _record_expiries(db, run_id, symbol, expiries, request)
                report["requests"].append({"kind":"expiries","symbol":symbol,"request":{"method":"Ticker.options","symbol":symbol},
                                           "received_at":request["received_at"],"response_hash":request["response_hash"],
                                           "rows_received":len(expiries),"rows_accepted":len(expiries),"rows_rejected":0})
                for expiry in expiries:
                    response = source.chain(symbol, expiry, ticker)
                    report["requests"].append(_record_response(db, run_id, response))
                    if response.metadata["side_counts"].get("call",0)==0 or response.metadata["side_counts"].get("put",0)==0:
                        _issue(db,run_id,symbol,"chain",expiry,"incomplete_expiry_side_coverage",response.received_at)
            _set(db, "last_option_fetch_at", now)
            # A publication-time uncertain Yahoo snapshot is only a position
            # level. Never infer growth from two intraday fetches.
            local_day = pd.Timestamp(now).tz_convert("America/New_York").date().isoformat()
            for symbol in UNIVERSE:
                if _state(db,"last_option_fetch_at"):
                    for (payload,) in db.execute("SELECT payload_json FROM option_snapshots WHERE symbol=? AND fetch_at>=?",(symbol,now)):
                        item=json.loads(payload)
                        if item["open_interest"] is not None:
                            db.execute("INSERT OR IGNORE INTO oi_daily VALUES (?,?,?,?,?)",(symbol,item["contract_symbol"],local_day,item["open_interest"],item["fetch_at"]))
        report["oi_comparison"]=_compare_daily_oi(db,now)
        previous_decisions=db.execute("SELECT count(*) FROM decisions").fetchone()[0]
        for symbol in UNIVERSE:
            decision = _decision(db,symbol,now)
            report["decisions"].append(decision)
            report["forecasts"].append(_forecast(db,symbol,decision["interval_end"],_now()) if decision.get("interval_end") and decision["status"]=="WARMING_UP" else {"symbol":symbol,"status":"QUALITY_EXCLUDED","reason":"No complete stock decision interval"})
            report["audit"][symbol]=_audit(db,symbol,now)
        report["latest_completed_decision_interval"] = max((x.get("interval_end") for x in report["decisions"] if x.get("interval_end")),default=None)
        report["new_decisions"] = db.execute("SELECT count(*) FROM decisions").fetchone()[0]-previous_decisions
        report["stock_poll_due"] = stock_due
        report["options_poll_due"] = options_due
        report["completed_at"] = _now()
        report["status"] = "ok"
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?)",(run_id,now,report["completed_at"],report["status"],json.dumps(report,sort_keys=True)))
        db.commit()
    except Exception as exc:
        db.rollback()
        report["status"]="source_failed" if isinstance(exc,YahooSourceError) else "failed"
        report["error"]={"type":type(exc).__name__,"message":str(exc)[:500]}
        report["completed_at"]=_now()
        # Failed source fetch never destroys the last committed valid snapshot.
        db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",(run_id,now,report["completed_at"],report["status"],json.dumps(report,sort_keys=True)))
        db.commit()
    finally:
        db.close()
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",required=True)
    parser.add_argument("--dataset-id",required=True)
    parser.add_argument("--evidence",required=True)
    parser.add_argument("--force",action="store_true")
    args=parser.parse_args(argv)
    evidence=Path(args.evidence)
    if (evidence/"FIRST_RUN.json").exists():
        raise ValueError("Refusing to overwrite existing first-run evidence")
    result=run_cycle(args.db,args.dataset_id,force=args.force)
    evidence.mkdir(parents=True,exist_ok=True)
    (evidence/"FIRST_RUN.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("dataset_id","status","started_at","completed_at","latest_completed_decision_interval","error") if k in result},indent=2))
    return 0 if result["status"]=="ok" else 2


if __name__=="__main__":
    raise SystemExit(main())
