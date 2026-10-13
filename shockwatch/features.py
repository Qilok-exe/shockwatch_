"""Strict canonical CSV adapter with separate pricing and flow quality gates."""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from .common import clean, iso, utc

SCHEMAS = {
    "sessions": ["session", "open_utc", "close_utc"],
    "batches": ["instrument_id", "sector", "interval_start", "interval_end", "available_at", "expected_contracts", "complete", "source", "schema_version"],
    "bars": ["instrument_id", "bar_start", "bar_end", "open", "high", "low", "close", "volume", "previous_close_factor", "available_at"],
    "options": ["instrument_id", "contract_id", "interval_end", "expiry_utc", "last_trade_utc", "put_call", "strike", "multiplier", "standard_deliverable", "bid", "ask", "quote_time", "iv", "delta", "interval_volume", "available_at"],
    "oi": ["contract_id", "position_date", "open_interest", "published_at", "available_at"]
}
TIME_COLS = {"open_utc", "close_utc", "interval_start", "interval_end", "available_at", "bar_start", "bar_end", "expiry_utc", "last_trade_utc", "quote_time", "published_at"}
FLOW = [f"{side}_{band}" for side in ("call", "put") for band in ("7_30", "31_90")]

def read_tables(directory):
    out = {}
    for name, columns in SCHEMAS.items():
        frame = pd.read_csv(Path(directory)/f"{name}.csv", keep_default_na=True, float_precision="round_trip")
        missing = set(columns)-set(frame.columns)
        if missing:
            raise ValueError(f"{name}: missing columns {sorted(missing)}")
        for col in set(columns) & TIME_COLS:
            frame[col] = pd.to_datetime([pd.NaT if col == "quote_time" and pd.isna(v) else utc(v) for v in frame[col]], utc=True)
        out[name] = frame
    return out

def session_intervals(sessions):
    result = []
    for row in sessions.sort_values("open_utc").to_dict("records"):
        start, close = utc(row["open_utc"]), utc(row["close_utc"])
        if close <= start or (close-start).total_seconds() % 300:
            raise ValueError("Invalid session boundaries")
        slot = 0
        while start < close:
            end = min(start+pd.Timedelta(hours=1), close)
            result.append({"session": str(row["session"]), "interval_start": start, "interval_end": end,
                           "slot": slot, "minutes": (end-start).total_seconds()/60, "is_session_end": end == close})
            start, slot = end, slot+1
    return result

def _surface(chain, decision, cfg, days, absolute_delta, side):
    q = chain[chain.put_call.eq(side)].copy()
    age = (decision-q.quote_time).dt.total_seconds()
    mid = (q.bid+q.ask)/2
    spread = (q.ask-q.bid)/mid.clip(lower=.01)
    good = ((q.bid > 0) & (q.ask >= q.bid) & (q.iv > 0) & (q.iv <= 5)
            & age.between(0, cfg["max_quote_age_seconds"]) & (spread <= cfg["max_relative_spread"])
            & (q.delta.abs() > 0) & (q.delta.abs() < 1))
    good &= q.delta.gt(0) if side == "C" else q.delta.lt(0)
    q = q[good]
    points = []
    for expiry, g in q.groupby("expiry_utc", sort=True):
        t = (expiry-decision).total_seconds()/(365*86400)
        if t <= 0:
            continue
        g = g.assign(ad=g.delta.abs()).sort_values("ad").groupby("ad", as_index=False).iv.median()
        x = g.ad.to_numpy()
        if len(x) >= 2 and x[0] <= absolute_delta <= x[-1]:
            iv = float(np.interp(absolute_delta, x, g.iv))
            points.append((t, iv*iv*t))
    target = days/365
    if len(points) < 2:
        return np.nan, len(q)
    x, w = np.array(points).T
    if target < x.min() or target > x.max():
        return np.nan, len(q)
    return float(np.sqrt(np.interp(target, x, w)/target)), len(q)

def _oi_evidence(chain, positions, decision, session_dates):
    ids = set(chain.contract_id)
    known = positions[positions.contract_id.isin(ids) & (positions.available_at <= decision) & (positions.published_at <= decision)].copy()
    empty = {"oi_token": None, "oi_call": np.nan, "oi_put": np.nan, "oi_coverage": 0.}
    if known.empty:
        return empty
    latest = str(known.position_date.max())
    earlier = [s for s in session_dates if s < latest]
    if not earlier:
        return empty
    previous = earlier[-1]
    pivot = known[known.position_date.astype(str).isin([previous, latest])].pivot(index="contract_id", columns="position_date", values="open_interest")
    if previous not in pivot or latest not in pivot:
        return empty
    p = pivot[[previous, latest]].dropna().join(chain.set_index("contract_id")["put_call"])
    p["change"] = p[latest]-p[previous]
    values = {}
    for side, name in [("C", "call"), ("P", "put")]:
        s = p[p.put_call.eq(side)]
        denom = float(s[previous].sum())
        values[f"oi_{name}"] = float(s.change.sum()/denom) if denom > 0 else np.nan
    return {**values, "oi_token": latest, "oi_coverage": len(p)/max(len(chain), 1)}

def build_panel(directory, cfg, asof=None):
    tables = read_tables(directory)
    sessions, batches, bars, options, positions = [tables[k] for k in SCHEMAS]
    for name, frame, keys in [("sessions", sessions, ["session"]), ("batches", batches, ["instrument_id", "interval_end"]),
                              ("bars", bars, ["instrument_id", "bar_end"]), ("options", options, ["contract_id", "interval_end"]),
                              ("oi", positions, ["contract_id", "position_date"])]:
        if frame.duplicated(keys).any():
            raise ValueError(f"{name}: duplicate key or revision; first-release input required")
    for name, endcol in [("bars", "bar_end"), ("options", "interval_end"), ("batches", "interval_end")]:
        if (tables[name].available_at < tables[name][endcol]).any():
            raise ValueError(f"{name}: completed interval cannot be available before its end")
    if not positions.empty and ((positions.open_interest < 0).any() or not np.isfinite(positions.open_interest).all() or (positions.open_interest % 1 != 0).any()):
        raise ValueError("OI cannot be negative, fractional or missing")
    ordered = session_intervals(sessions)
    for i, item in enumerate(ordered):
        nxt = ordered[i+1] if i+1 < len(ordered) else None
        item.update(next_interval_start=iso(nxt["interval_start"]) if nxt else None,
                    next_interval_end=iso(nxt["interval_end"]) if nxt else None,
                    next_slot=nxt["slot"] if nxt else None, next_minutes=nxt["minutes"] if nxt else None)
    intervals = {iso(x["interval_end"]): x for x in ordered}
    session_dates = sorted(sessions.session.astype(str).tolist())
    opt_groups = {k: g for k, g in options.groupby(["instrument_id", "interval_end"])}
    bar_groups = {k: g.sort_values("bar_end") for k, g in bars.groupby("instrument_id")}
    rows = []
    for b in batches.sort_values(["interval_end", "instrument_id"]).to_dict("records"):
        interval = intervals.get(iso(b["interval_end"]))
        if interval is None or b["interval_start"] != interval["interval_start"]:
            raise ValueError("Batch does not match the supplied exchange calendar")
        decision = interval["interval_end"]+pd.Timedelta(minutes=cfg["decision_delay_minutes"])
        if asof is not None and decision > utc(asof):
            continue
        symbol, reasons = b["instrument_id"], []
        row = {**interval, "instrument_id": symbol, "sector": b["sector"], "decision_at": decision,
               "source": b["source"], "feature_version": cfg["feature_version"], "schema_version": str(b["schema_version"])}
        if str(b["schema_version"]) not in ("1", "1.0"):
            raise ValueError("Unsupported source schema version")
        chain = opt_groups.get((symbol, b["interval_end"]), options.iloc[:0]).copy()
        received = chain.available_at <= decision
        count = int(b["expected_contracts"])
        if float(b["expected_contracts"]) != count:
            raise ValueError("Expected contract count must be an integer")
        manifest_ok = str(b["complete"]).lower() in ("true", "1")
        manifest_ok &= b["available_at"] <= decision and count > 0 and count == len(chain) and received.all()
        if not manifest_ok:
            reasons.append("incomplete_or_late_chain")
        chain = chain[received & (chain.last_trade_utc > interval["interval_start"])]
        chain["dte"] = (chain.expiry_utc-decision).dt.total_seconds()/86400
        standard = chain.standard_deliverable.astype(str).str.lower().isin(["true", "1"])
        chain = chain[standard & (chain.multiplier > 0) & chain.put_call.isin(["C", "P"])]
        main = chain[(chain.dte >= 7) & (chain.dte <= 90)].copy()
        flow_ok = bool(manifest_ok and len(main) > 0 and np.isfinite(main.interval_volume).all() and (main.interval_volume >= 0).all())
        if not flow_ok and manifest_ok:
            reasons.append("empty_main_chain" if not len(main) else "invalid_or_missing_main_volume")
        for side, label in [("C", "call"), ("P", "put")]:
            for low, high, band in [(7, 30, "7_30"), (30, 90, "31_90")]:
                g = main[(main.put_call == side) & (main.dte >= low if low == 7 else main.dte > low) & (main.dte <= high)]
                row[f"volume_{label}_{band}"] = float(g.interval_volume.sum()) if flow_ok and len(g) else np.nan
                row[f"contracts_{label}_{band}"] = len(g)
                row[f"mean_dte_{label}_{band}"] = float(g.dte.mean()) if len(g) else np.nan
                delta_known = g.delta.abs().between(.0001, .9999) & (decision-g.quote_time).dt.total_seconds().between(0, cfg["max_quote_age_seconds"])
                row[f"otm_contract_fraction_{label}_{band}"] = float(g.loc[delta_known, "delta"].abs().lt(.35).mean()) if delta_known.any() else np.nan
        for low, high, name in [(0, 7, "research_0_6"), (90, 180.000001, "research_91_180")]:
            g = chain[(chain.dte >= low if low == 0 else chain.dte > low) & (chain.dte < high)]
            row[f"volume_{name}"] = float(g.interval_volume.sum()) if manifest_ok and len(g) and np.isfinite(g.interval_volume).all() and (g.interval_volume >= 0).all() else np.nan
        mid = (main.bid+main.ask)/2
        age = (decision-main.quote_time).dt.total_seconds()
        valid = ((main.bid > 0) & (main.ask >= main.bid) & main.iv.between(.0001, 5) & age.between(0, cfg["max_quote_age_seconds"])
                 & ((main.ask-main.bid)/mid.clip(lower=.01) <= cfg["max_relative_spread"]))
        row["quote_coverage"] = float(valid.mean()) if len(main) else 0.
        row["relative_spread"] = float(((main.ask-main.bid)/mid.clip(lower=.01))[valid].median()) if valid.any() else np.nan
        ivc, _ = _surface(main, decision, cfg, 30, .50, "C")
        ivp, _ = _surface(main, decision, cfg, 30, .50, "P")
        put25, _ = _surface(main, decision, cfg, 30, .25, "P")
        call25, _ = _surface(main, decision, cfg, 30, .25, "C")
        iv60, _ = _surface(main, decision, cfg, 60, .50, "C")
        row.update(iv30=(ivc+ivp)/2, skew30=put25-call25, term_slope=iv60-ivc)
        pricing_ok = manifest_ok and row["quote_coverage"] >= cfg["min_quote_coverage"] and np.isfinite(row["iv30"])
        if not pricing_ok:
            row.update(iv30=np.nan, skew30=np.nan, term_slope=np.nan)
            reasons.append("surface_unavailable")
        row.update(_oi_evidence(main, positions, decision, session_dates))
        if not flow_ok or row["oi_coverage"] < .80:
            row.update(oi_call=np.nan, oi_put=np.nan, oi_token=None)
        allbars = bar_groups.get(symbol, bars.iloc[:0])
        g = allbars[(allbars.bar_start >= interval["interval_start"]) & (allbars.bar_end <= interval["interval_end"]) & (allbars.available_at <= decision)].copy()
        expected = pd.date_range(interval["interval_start"]+pd.Timedelta(minutes=5), interval["interval_end"], freq="5min")
        bars_ok = len(g) == len(expected) and list(g.bar_end) == list(expected)
        bars_ok &= bool(((g.bar_end-g.bar_start).dt.total_seconds() == 300).all())
        bars_ok &= bool((g[["open", "high", "low", "close"]] > 0).all().all()
                        and (g.high >= g[["open", "close", "low"]].max(axis=1)).all()
                        and (g.low <= g[["open", "close", "high"]].min(axis=1)).all()
                        and (g.volume >= 0).all() and (g.previous_close_factor > 0).all())
        bars_ok &= bool(np.isfinite(g[["open", "high", "low", "close", "volume", "previous_close_factor"]]).all().all())
        if bars_ok:
            prev = allbars[(allbars.bar_end == interval["interval_start"]) & (allbars.available_at <= decision)]
            ref = g.close.shift(1).to_numpy()
            ref[0] = float(prev.close.iloc[-1]) if len(prev) else float(g.open.iloc[0])
            factors = g.previous_close_factor.to_numpy(dtype=float, copy=True)
            if not len(prev):
                factors[0] = 1.
            returns = np.log(g.close.to_numpy()/(ref*factors))
            row.update(rv=float(returns@returns), stock_volume=float(g.volume.sum()))
            overnight = np.nan
            if interval["slot"] == 0:
                session_index = session_dates.index(interval["session"])
                if session_index > 0:
                    previous_close = utc(sessions[sessions.session.astype(str).eq(session_dates[session_index-1])].close_utc.iloc[0])
                    old = allbars[(allbars.bar_end == previous_close) & (allbars.available_at <= decision)]
                    if len(old):
                        overnight = float(np.log(g.open.iloc[0]/(old.close.iloc[-1]*g.previous_close_factor.iloc[0]))**2)
            row["overnight_rv"] = overnight
        else:
            reasons.append("missing_or_invalid_5min_bars")
            row.update(rv=np.nan, stock_volume=np.nan, overnight_rv=np.nan)
        row.update(flow_eligible=bool(flow_ok and bars_ok), price_eligible=bool(pricing_ok and bars_ok),
                   data_ok=bool(flow_ok and pricing_ok and bars_ok), quality_reasons=";".join(reasons))
        rows.append(clean(row))
    return pd.DataFrame(rows)

def validate_panel(frame):
    required = {"instrument_id", "sector", "session", "interval_start", "interval_end", "decision_at", "slot", "minutes", "is_session_end", "rv", "stock_volume", "overnight_rv", "iv30", "skew30", "term_slope", "oi_token", "oi_call", "oi_put", "relative_spread", "flow_eligible", "price_eligible", "data_ok", "quality_reasons", "feature_version"}
    required |= {f"{prefix}_{x}" for x in FLOW for prefix in ("volume", "contracts", "mean_dte", "otm_contract_fraction")}
    if not required.issubset(frame.columns):
        raise ValueError(f"Panel missing columns: {sorted(required-set(frame.columns))}")
    frame = frame.copy()
    for col in ("interval_start", "interval_end", "decision_at"):
        frame[col] = [iso(x) for x in frame[col]]
    for col in ("flow_eligible", "price_eligible", "data_ok", "is_session_end"):
        if not frame[col].astype(str).str.lower().isin(["true", "false", "1", "0"]).all():
            raise ValueError(f"Invalid boolean: {col}")
        frame[col] = frame[col].astype(str).str.lower().isin(["true", "1"])
    if frame.duplicated(["instrument_id", "interval_end"]).any():
        raise ValueError("Panel has duplicate keys")
    if (pd.to_datetime(frame.decision_at) < pd.to_datetime(frame.interval_end)).any():
        raise ValueError("A decision cannot precede its completed interval")
    if not frame.minutes.between(1, 60).all() or not frame.slot.between(0, 6).all():
        raise ValueError("Invalid interval duration or slot")
    for col in ["rv", "stock_volume"]+[f"volume_{x}" for x in FLOW]:
        values = pd.to_numeric(frame[col], errors="raise")
        if (values.dropna() < 0).any() or np.isinf(values).any():
            raise ValueError(f"Invalid nonnegative quantity: {col}")
    for col in ("session", "oi_token"):
        frame[col] = frame[col].apply(lambda x: str(x) if pd.notna(x) else None)
    frame["quality_reasons"] = frame.quality_reasons.fillna("")
    return frame.sort_values(["decision_at", "instrument_id"]).reset_index(drop=True)
