from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from shockwatch.yahoo_source import YahooSource, SourceResponse, YahooSourceError
from shockwatch.paper_yahoo import open_paper_db, run_cycle, _forecast, _audit, _compare_daily_oi, _record_response, _set, _latest_complete_interval
from shockwatch.yahoo_source import market_schedule
from shockwatch.paper_publication import _safe_summary, export_paper_site, validate_paper_site, ARCHIVE


FIXTURE = json.loads((Path(__file__).parent/"fixtures"/"yahoo_small.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)


class FakeTicker:
    history_metadata = {"exchangeTimezoneName":"America/New_York"}
    options = (FIXTURE["expiry"],)
    def history(self, **kwargs):
        self.history_kwargs = kwargs
        return pd.DataFrame([{k:v for k,v in row.items() if k!="at"} for row in FIXTURE["history"]],
                            index=pd.DatetimeIndex([row["at"] for row in FIXTURE["history"]]))
    def option_chain(self, expiry):
        return SimpleNamespace(calls=pd.DataFrame(FIXTURE["calls"]), puts=pd.DataFrame(FIXTURE["puts"]),
                               underlying={"regularMarketPrice":100.5})


class YahooAdapterTests(unittest.TestCase):
    def test_fixture_validates_ohlc_bid_ask_missing_values_and_delta(self):
        source=YahooSource(ticker_factory=lambda symbol:FakeTicker(),clock=lambda:NOW)
        stock=source.history("SPY")
        self.assertEqual(stock.metadata["rows_received"],4)
        self.assertEqual(len(stock.rows),3)
        self.assertEqual(stock.issues[0]["reason"],"inconsistent_ohlc")
        self.assertIsNone(stock.rows[1]["volume"])
        self.assertNotEqual(stock.rows[0]["close"],stock.rows[0]["adjusted_close"])
        self.assertEqual(stock.rows[0]["exchange_timezone"],"America/New_York")
        ticker,expiries,_=source.expiries("SPY")
        chain=source.chain("SPY",expiries[0],ticker)
        self.assertEqual(len(chain.rows),2)
        self.assertEqual(chain.issues[0]["reason"],"bid_exceeds_ask")
        self.assertIsNone(chain.rows[0]["volume"])
        self.assertIsNone(chain.rows[1]["open_interest"])
        self.assertTrue(all(r["vendor_delta"] is None for r in chain.rows))
        self.assertEqual(chain.rows[0]["dte_group"],"research_0_6")

    def test_current_bar_is_excluded(self):
        source=YahooSource(ticker_factory=lambda symbol:FakeTicker(),clock=lambda:datetime(2026,10,13,13,32,tzinfo=timezone.utc))
        result=source.history("SPY")
        self.assertEqual(result.rows,[])
        self.assertIn("incomplete_current_or_future_bar",{i["reason"] for i in result.issues})

    def test_exchange_calendar_blocks_holiday_and_recognizes_early_close(self):
        self.assertIsNone(market_schedule("SPY","2026-07-03"))
        early=market_schedule("SPY","2026-11-27")
        self.assertEqual(early[1].isoformat(),"2026-11-27T18:00:00+00:00")

    def test_noninteger_counts_and_nonfinite_iv_are_excluded(self):
        class BadTicker(FakeTicker):
            def history(self,**kwargs):
                frame=super().history(**kwargs)
                frame.iloc[0,frame.columns.get_loc("Volume")]=1.5
                return frame
            def option_chain(self,expiry):
                chain=super().option_chain(expiry)
                chain.calls.loc[0,"impliedVolatility"]=float("inf")
                return chain
        source=YahooSource(ticker_factory=lambda symbol:BadTicker(),clock=lambda:NOW)
        stock=source.history("SPY")
        self.assertIn("noninteger_volume",{x["reason"] for x in stock.issues})
        chain=source.chain("SPY",FIXTURE["expiry"])
        self.assertIn("nonfinite_option_field",{x["reason"] for x in chain.issues})


class FakeSource:
    library_version="fixture"
    def __init__(self): self.calls=[]
    def history(self,symbol,*,backfill=True):
        self.calls.append(("history",symbol))
        rows=[]
        start=pd.Timestamp("2026-10-13T13:30:00Z")
        for i in range(12):
            at=start+pd.Timedelta(minutes=5*i)
            rows.append({"symbol":symbol,"bar_start_utc":at.isoformat(),"bar_end_utc":(at+pd.Timedelta(minutes=5)).isoformat(),
                         "session_date":"2026-10-13","exchange_timezone":"America/New_York", "open":100.0,"high":101.0,
                         "low":99.0,"close":100.1,"adjusted_close":100.1,"volume":10,"dividends":0.0,"split_ratio":0.0,
                         "received_at":"2026-10-13T16:00:00+00:00","source":"yahoo","source_version":"yahoo-adapter-1","library_version":"fixture"})
        return SourceResponse(symbol,"history",{"method":"Ticker.history","symbol":symbol,"requested_at":"2026-10-13T16:00:00+00:00"},
                              "2026-10-13T16:00:00+00:00","a"*64,rows,[],{"rows_received":12,"exchange_timezone":"America/New_York"})
    def expiries(self,symbol):
        self.calls.append(("expiries",symbol))
        return object(),("2026-10-16",),{"requested_at":"2026-10-13T16:00:00+00:00","received_at":"2026-10-13T16:00:00+00:00","response_hash":"b"*64}
    def chain(self,symbol,expiry,ticker=None):
        self.calls.append(("chain",symbol,expiry))
        return SourceResponse(symbol,"chain",{"method":"Ticker.option_chain","symbol":symbol,"expiry":expiry,"requested_at":"2026-10-13T16:00:00+00:00"},
                              "2026-10-13T16:00:00+00:00","c"*64,[],[],{"rows_received":0,"expiry":expiry,"side_counts":{"call":0,"put":0},"dte_group":"research_0_6"})


class PaperLedgerTests(unittest.TestCase):
    def test_failed_site_regeneration_keeps_previous_valid_snapshot(self):
        script=Path(__file__).resolve().parents[1]/"scripts"/"run-paper-yahoo-task.py"
        spec=importlib.util.spec_from_file_location("paper_task_test",script)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            runtime=Path(temp); site=runtime/"site"; site.mkdir()
            (site/"index.html").write_text("prior-valid-site",encoding="utf-8")
            with patch.object(module,"export_paper_site",side_effect=ValueError("render failed")):
                with self.assertRaisesRegex(ValueError,"render failed"):
                    module._regenerate_site(runtime)
            self.assertEqual((site/"index.html").read_text(encoding="utf-8"),"prior-valid-site")
            self.assertFalse(list(runtime.glob("site.staging-*")))

    def test_existing_synthetic_database_is_never_modified(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"monitor.db"
            db=sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE synthetic_decisions (payload TEXT)")
                db.commit()
            finally: db.close()
            with self.assertRaisesRegex(ValueError,"non-paper or mixed"):
                open_paper_db(path,"paper_yahoo_20261013T160000Z")
            db=sqlite3.connect(path)
            try:
                self.assertEqual([x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")],
                                 ["synthetic_decisions"])
            finally: db.close()

    def test_source_block_stops_after_one_request_and_retains_failure(self):
        class BlockedSource:
            library_version="fixture"
            calls=0
            def history(self,symbol,*,backfill=True):
                self.calls+=1
                raise YahooSourceError("SPY stock history: HTTP 429")
        with tempfile.TemporaryDirectory() as temp:
            source=BlockedSource(); db_path=Path(temp)/"paper.db"
            report=run_cycle(db_path,"paper_yahoo_20261013T160000Z",source,now="2026-10-13T16:00:00Z")
            self.assertEqual(report["status"],"source_failed")
            self.assertEqual(source.calls,1)
            db=open_paper_db(db_path)
            try:
                self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0],"source_failed")
                self.assertEqual(db.execute("SELECT count(*) FROM stock_bars").fetchone()[0],0)
            finally: db.close()

    def test_operational_forecast_issue_follows_last_received_input(self):
        class FullSource(FakeSource):
            def history(self,symbol,*,backfill=True):
                rows=[]
                for day in ("2026-10-12","2026-10-13"):
                    start=pd.Timestamp(day+"T13:30:00Z")
                    for i in range(78):
                        at=start+pd.Timedelta(minutes=5*i)
                        rows.append({"symbol":symbol,"bar_start_utc":at.isoformat(),"bar_end_utc":(at+pd.Timedelta(minutes=5)).isoformat(),
                                     "session_date":day,"exchange_timezone":"America/New_York","open":100.0,"high":101.0,
                                     "low":99.0,"close":100.1,"adjusted_close":100.1+i*0.001,"volume":10,
                                     "dividends":0.0,"split_ratio":0.0,"received_at":"2026-10-13T20:00:00+00:00",
                                     "source":"yahoo","source_version":"fixture","library_version":"fixture"})
                return SourceResponse(symbol,"history",{"method":"Ticker.history","symbol":symbol,"requested_at":"2026-10-13T19:59:00+00:00"},
                                      "2026-10-13T20:00:00+00:00","d"*64,rows,[],{"rows_received":len(rows)})
        with tempfile.TemporaryDirectory() as temp:
            report=run_cycle(Path(temp)/"paper.db","paper_yahoo_20261013T201500Z",FullSource(),now="2026-10-13T20:15:00Z")
            self.assertEqual(report["status"],"ok")
            last_received=max(pd.Timestamp(r["received_at"]) for r in report["requests"])
            for decision,forecast in zip(report["decisions"],report["forecasts"]):
                self.assertEqual(forecast["status"],"OPERATIONAL")
                self.assertGreaterEqual(pd.Timestamp(forecast["issued_at"]),last_received)
                self.assertGreaterEqual(pd.Timestamp(decision["recorded_at"]),last_received)

    def test_first_run_separate_idempotent_and_never_opens_unavailable_route(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path=Path(temp)/"paper.db"
            source=FakeSource(); dataset="paper_yahoo_20261013T160000Z"
            first=run_cycle(db_path,dataset,source,now="2026-10-13T16:00:00Z")
            self.assertEqual(first["status"],"ok")
            self.assertEqual(first["routes_evaluated"],0)
            self.assertEqual(first["episodes_opened"],0)
            self.assertTrue(all(x["flow_score"] is None for x in first["decisions"]))
            self.assertTrue(all(x["status"]=="INSUFFICIENT_HISTORY" for x in first["forecasts"]))
            self.assertTrue(all(x["oi_status"]=="UNAVAILABLE_FIRST_SNAPSHOT" for x in first["audit"].values()))
            calls=len(source.calls)
            second=run_cycle(db_path,dataset,source,now="2026-10-13T16:01:00Z")
            self.assertEqual(len(source.calls),calls)
            self.assertFalse(second["stock_poll_due"])
            self.assertFalse(second["options_poll_due"])
            db=open_paper_db(db_path,dataset)
            try:
                self.assertEqual(db.execute("SELECT count(*) FROM stock_bars").fetchone()[0],36)
                self.assertEqual(db.execute("SELECT count(*) FROM decisions").fetchone()[0],3)
                self.assertEqual(db.execute("SELECT mode FROM identity").fetchone()[0],"paper")
                self.assertEqual(db.execute("SELECT count(*) FROM oi_daily").fetchone()[0],0)
            finally: db.close()
            public=_safe_summary(first)
            self.assertEqual(public["mode"],"paper")
            self.assertNotIn("stock_bars",public)

    def test_public_export_is_bounded_and_rejects_unlisted_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path=Path(temp)/"paper.db"
            dataset="paper_yahoo_20261013T160000Z"
            self.assertEqual(run_cycle(db_path,dataset,FakeSource(),now="2026-10-13T16:00:00Z")["status"],"ok")
            archive=Path(__file__).resolve().parents[1]/"docs"/ARCHIVE
            site=Path(temp)/"site"
            result=export_paper_site(db_path,site,archive)
            self.assertTrue(result["validation"]["passed"])
            self.assertNotIn("contract_symbol",(site/"exports/paper-summary.json").read_text(encoding="utf-8"))
            (site/"raw.db").write_bytes(b"private")
            self.assertFalse(validate_paper_site(site)["passed"])
            self.assertIn("raw.db",validate_paper_site(site)["unexpected"])

    def test_ewma_uses_only_contiguous_adjusted_history(self):
        with tempfile.TemporaryDirectory() as temp:
            db=open_paper_db(Path(temp)/"paper.db","paper_yahoo_20261013T160000Z")
            start=pd.Timestamp("2026-10-12T13:30:00Z")
            for i in range(131):
                # Continue into a second test session with no overnight return.
                at=start+pd.Timedelta(minutes=5*i)
                if i>=78: at=pd.Timestamp("2026-10-13T13:30:00Z")+pd.Timedelta(minutes=5*(i-78))
                item={"session_date":at.tz_convert("America/New_York").date().isoformat(),
                      "bar_start_utc":at.isoformat(),"bar_end_utc":(at+pd.Timedelta(minutes=5)).isoformat(),
                      "adjusted_close":100.0+i*0.01}
                db.execute("INSERT INTO stock_bars VALUES (?,?,?,?,?,?)",("SPY",item["bar_start_utc"],item["bar_end_utc"],item["session_date"],"h",json.dumps(item)))
            result=_forecast(db,"SPY","2026-10-13T17:55:00+00:00","2026-10-13T18:00:00+00:00")
            self.assertEqual(result["status"],"OPERATIONAL")
            self.assertGreater(result["predicted_variance"],0)
            repeated=_forecast(db,"SPY","2026-10-13T17:55:00+00:00","2026-10-13T19:00:00+00:00")
            self.assertEqual(repeated,result)
            db.close()

    def test_stock_retry_receipt_time_is_idempotent_and_conflict_is_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            db=open_paper_db(Path(temp)/"paper.db","paper_yahoo_20261013T160000Z")
            source=FakeSource(); first=source.history("SPY")
            _record_response(db,"run1",first)
            copied=[dict(x,received_at="2026-10-13T16:05:00+00:00") for x in first.rows]
            second=SourceResponse("SPY","history",{"method":"Ticker.history","symbol":"SPY","requested_at":"2026-10-13T16:05:00+00:00"},
                                  "2026-10-13T16:05:00+00:00","b"*64,copied,[],{"rows_received":12})
            result=_record_response(db,"run2",second)
            self.assertEqual(result["rows_accepted"],0)
            self.assertEqual(result["duplicate_unchanged"],12)
            self.assertEqual(result["rows_rejected"],0)
            self.assertEqual(db.execute("SELECT count(*) FROM quality_issues").fetchone()[0],0)
            copied[0]["close"]=200.0
            third=SourceResponse("SPY","history",{"method":"Ticker.history","symbol":"SPY","requested_at":"2026-10-13T16:10:00+00:00"},
                                 "2026-10-13T16:10:00+00:00","c"*64,copied,[],{"rows_received":12})
            changed=_record_response(db,"run3",third)
            self.assertEqual(changed["rows_rejected"],1)
            self.assertEqual(changed["conflicting_revisions"],1)
            self.assertEqual(db.execute("SELECT reason FROM quality_issues").fetchone()[0],"conflicting_retry_or_revised_bar")
            db.close()

    def test_all_expiries_are_counted_and_oi_only_matches_completed_days(self):
        with tempfile.TemporaryDirectory() as temp:
            db=open_paper_db(Path(temp)/"paper.db","paper_yahoo_20261013T160000Z")
            _set(db,"last_option_fetch_at","2026-10-13T20:00:00+00:00")
            for expiry,contract,fetch in (("2026-10-16","SPY261016C00100000","2026-10-13T20:00:01+00:00"),
                                          ("2026-11-20","SPY261120P00100000","2026-10-13T20:00:02+00:00")):
                item={"symbol":"SPY","expiry":expiry,"side":"call" if "C" in contract else "put",
                      "contract_symbol":contract,"fetch_at":fetch,"dte_group":"research_0_6" if expiry.startswith("2026-10") else "main_7_90",
                      "last_trade_stale":False,"volume":None,"open_interest":20}
                db.execute("INSERT INTO option_snapshots VALUES (?,?,?,?,?,?,?)",
                           ("SPY",expiry,item["side"],contract,fetch,"hash",json.dumps(item)))
                db.execute("INSERT INTO oi_daily VALUES (?,?,?,?,?)",("SPY",contract,"2026-10-13",20,fetch))
            audit=_audit(db,"SPY","2026-10-13T21:00:00+00:00")
            self.assertEqual(audit["option_expiry_count"],2)
            self.assertEqual(sum(audit["option_contract_counts"].values()),2)
            self.assertEqual(_compare_daily_oi(db,"2026-10-13T21:00:00+00:00")["status"],"INSUFFICIENT_DAILY_MATCHES")
            db.execute("INSERT INTO oi_daily VALUES (?,?,?,?,?)",("SPY","SPY261016C00100000","2026-10-14",23,"2026-10-14T20:00:00+00:00"))
            compared=_compare_daily_oi(db,"2026-10-14T21:00:00+00:00")
            self.assertEqual(compared["matched_contracts"],1)
            self.assertEqual(db.execute("SELECT position_difference FROM oi_comparisons").fetchone()[0],3)
            self.assertEqual(_audit(db,"SPY","2026-10-14T21:00:00+00:00")["oi_status"],"COMPARISON_AVAILABLE_UNCALIBRATED")
            self.assertEqual(_compare_daily_oi(db,"2026-10-14T21:01:00+00:00")["status"],"ALREADY_COMPARED")
            db.close()

    def test_short_closing_interval_requires_only_its_scheduled_bars(self):
        with tempfile.TemporaryDirectory() as temp:
            db=open_paper_db(Path(temp)/"paper.db","paper_yahoo_20261127T190000Z")
            start=pd.Timestamp("2026-11-27T17:30:00Z")
            for i in range(6):
                at=start+pd.Timedelta(minutes=5*i)
                db.execute("INSERT INTO stock_bars VALUES (?,?,?,?,?,?)",
                           ("SPY",at.isoformat(),(at+pd.Timedelta(minutes=5)).isoformat(),"2026-11-27","h","{}"))
            interval=_latest_complete_interval(db,"SPY","2026-11-27T18:15:00Z")
            self.assertEqual(interval["start"],"2026-11-27T17:30:00+00:00")
            self.assertEqual(interval["end"],"2026-11-27T18:00:00+00:00")
            self.assertEqual(interval["required_bars"],6)
            db.close()
