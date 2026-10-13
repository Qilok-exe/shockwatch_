"""Deterministic software fixtures. These are not real market data."""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from .common import clean, iso, save_json
from .features import FLOW, SCHEMAS, session_intervals

def generate(directory, sessions=160, seed=731):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=sessions)
    calendar = []
    for i, d in enumerate(dates):
        day = d.strftime("%Y-%m-%d")
        start = pd.Timestamp(day+" 09:30", tz="America/New_York")
        close = pd.Timestamp(day+(" 13:00" if i == 40 else " 16:00"), tz="America/New_York")
        calendar.append({"session": day, "open_utc": start.tz_convert("UTC"), "close_utc": close.tz_convert("UTC")})
    calendar = pd.DataFrame(calendar)
    calendar.to_csv(path/"sessions.csv", index=False)
    intervals = session_intervals(calendar)
    day_index = {x.strftime("%Y-%m-%d"): i for i, x in enumerate(dates)}
    symbols = {"SPY": "market", "UAL": "airlines", "LMT": "defense"}
    levels = {s: .25+z*.06 for z, s in enumerate(symbols)}
    rows, labels, daily_oi = [], [], {}
    for i, interval in enumerate(intervals):
        d, slot, minutes = day_index[interval["session"]], interval["slot"], interval["minutes"]
        common = rng.normal(0, .18)
        for k, (symbol, sector) in enumerate(symbols.items()):
            own = rng.normal(0, .20)
            intensity = 1.35 if slot in (0, 6) else .85
            stock_volume = float(1e6/(1+k)*minutes/60*intensity*np.exp(common+own))
            base_rv = (.00018+k*.00007)*minutes/390*intensity
            rv = float(base_rv*np.exp(common+rng.normal(0, .30)))
            levels[symbol] += .04*(.25+k*.06-levels[symbol])+rng.normal(0, .0012)
            price_event = symbol == "UAL" and d in (115, 145) and slot == 2
            persistent = symbol == "LMT" and d in (112, 143) and slot in (1, 2, 3, 4)
            if price_event:
                levels[symbol] += .04
            if persistent:
                levels[symbol] += .004
            if slot == 0:
                daily_oi[symbol] = {side: float(rng.normal(.0003, .002)) for side in ("call", "put")}
            row = {**interval, "instrument_id": symbol, "sector": sector, "decision_at": interval["interval_end"]+pd.Timedelta(minutes=10),
                   "stock_volume": stock_volume, "rv": rv, "overnight_rv": float(base_rv*np.exp(rng.normal(0, .4))) if slot == 0 else None,
                   "iv30": levels[symbol], "skew30": .025+rng.normal(0, .0010), "term_slope": .005+rng.normal(0, .0010),
                   "relative_spread": .06+abs(rng.normal(0, .02)), "oi_token": dates[d-1].strftime("%Y-%m-%d") if d else None,
                   "oi_call": daily_oi[symbol]["call"], "oi_put": daily_oi[symbol]["put"], "oi_coverage": 1., "quote_coverage": 1.,
                   "flow_eligible": True, "price_eligible": True, "data_ok": True, "quality_reasons": "", "feature_version": "0.1.0",
                   "schema_version": "1", "source": "SYNTHETIC_SOFTWARE_FIXTURE"}
            for group in FLOW:
                volume = 200/(1+k)*minutes/60*intensity*np.exp(.8*(common+own)+rng.normal(0, .18))
                if (price_event or persistent) and group.startswith("call"):
                    volume *= 18 if price_event else 2.7
                row["volume_"+group] = float(round(volume))
                row["contracts_"+group] = 12
                row["mean_dte_"+group] = 20. if group.endswith("7_30") else 55.
                row["otm_contract_fraction_"+group] = .5
            for name in ("research_0_6", "research_91_180"):
                row["volume_"+name] = float(rng.poisson(60))
            nxt = intervals[i+1] if i+1 < len(intervals) else None
            row.update(next_interval_start=iso(nxt["interval_start"]) if nxt else None, next_interval_end=iso(nxt["interval_end"]) if nxt else None,
                       next_slot=nxt["slot"] if nxt else None, next_minutes=nxt["minutes"] if nxt else None)
            rows.append(clean(row))
    for d, symbol, name in [(112, "LMT", "validation_persistent"), (115, "UAL", "validation_extreme"), (143, "LMT", "test_persistent"), (145, "UAL", "test_extreme")]:
        if d+1 < sessions:
            labels.append({"case_id": name, "instrument_id": symbol, "event_group": name, "window_start": iso(calendar.iloc[d].open_utc),
                           "public_at": iso(calendar.iloc[d+1].open_utc), "status": "synthetic", "source_url": "synthetic://fixture"})
    panel = pd.DataFrame(rows)
    panel.to_csv(path/"panel.csv", index=False)
    pd.DataFrame(labels).to_csv(path/"cases.csv", index=False)
    save_json(path/"SYNTHETIC_NOTICE.json", {"synthetic": True, "seed": seed, "sessions": sessions,
              "calendar": "Weekday fixture with an artificial early close. Not a valid exchange calendar.",
              "purpose": "Verify code paths, timing and recovery. Not market evidence."})
    return panel, calendar, pd.DataFrame(labels)

def raw_fixture(directory):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    day = "2024-03-11"
    calendar = pd.DataFrame([{"session": day, "open_utc": pd.Timestamp(day+"T13:30:00Z"), "close_utc": pd.Timestamp(day+"T20:00:00Z")}])
    intervals = session_intervals(calendar)
    options, bars, batches = [], [], []
    rng = np.random.default_rng(24)
    price = 100.
    for b in intervals:
        for end in pd.date_range(b["interval_start"]+pd.Timedelta(minutes=5), b["interval_end"], freq="5min"):
            new = price*np.exp(rng.normal(0, .0008))
            bars.append({"instrument_id": "SPY", "bar_start": end-pd.Timedelta(minutes=5), "bar_end": end, "open": price,
                         "high": max(price, new), "low": min(price, new), "close": new, "volume": 10000,
                         "previous_close_factor": 1., "available_at": end+pd.Timedelta(seconds=20)})
            price = new
        for days in (20, 45, 75):
            expiry = calendar.iloc[0].close_utc+pd.Timedelta(days=days)
            for side in ("C", "P"):
                for delta in (.15, .25, .50, .70):
                    options.append({"instrument_id": "SPY", "contract_id": f"SYNTH_{days}_{side}_{delta}", "interval_end": b["interval_end"],
                                    "expiry_utc": expiry, "last_trade_utc": expiry, "put_call": side,
                                    "strike": 100+(1 if side == "C" else -1)*(.5-delta)*30, "multiplier": 100, "standard_deliverable": True,
                                    "bid": 2., "ask": 2.1, "quote_time": b["interval_end"], "iv": .25+.02*(.5-delta)+days*.0001,
                                    "delta": delta if side == "C" else -delta, "interval_volume": 20,
                                    "available_at": b["interval_end"]+pd.Timedelta(seconds=30)})
        batches.append({"instrument_id": "SPY", "sector": "market", "interval_start": b["interval_start"], "interval_end": b["interval_end"],
                        "available_at": b["interval_end"]+pd.Timedelta(seconds=30), "expected_contracts": 24, "complete": True, "source": "SYNTHETIC", "schema_version": "1"})
    for name, frame in [("sessions", calendar), ("batches", pd.DataFrame(batches)), ("bars", pd.DataFrame(bars)), ("options", pd.DataFrame(options)), ("oi", pd.DataFrame(columns=SCHEMAS["oi"]))]:
        frame.to_csv(path/(name+".csv"), index=False)
    return path
