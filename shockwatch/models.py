"""Frozen regularized baselines. Scores are scale units, never probabilities."""
from __future__ import annotations
import numpy as np
import pandas as pd
from .common import clean, digest, finite, number, utc
from .features import FLOW, validate_panel

PRICE = ["d_iv30", "d_skew30", "d_term_slope"]

def robust(values, floor=.05):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return {"center": 0., "scale": floor, "n": 0}
    med = np.median(x)
    return {"center": float(med), "scale": float(max(1.4826*np.median(abs(x-med)), floor)), "n": len(x)}

def zscore(value, stats):
    return (value-stats["center"])/stats["scale"] if finite(value) and stats["n"] else np.nan

def ridge_fit(x, y, penalty=10.):
    x, y = np.asarray(x, float), np.asarray(y, float)
    median = np.array([np.median(col[np.isfinite(col)]) if np.isfinite(col).any() else 0. for col in x.T])
    x = np.where(np.isfinite(x), x, median)
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-8] = 1.
    a = np.column_stack([np.ones(len(x)), (x-mean)/scale])
    reg = np.eye(a.shape[1])*penalty
    reg[0, 0] = 0
    beta = np.linalg.solve(a.T@a+reg, a.T@y)
    residual = y-a@beta
    return {"median": median.tolist(), "mean": mean.tolist(), "scale": scale.tolist(), "beta": beta.tolist(),
            "residual": robust(residual), "smearing": float(np.mean(np.exp(np.clip(residual, -20, 20))))}

def ridge_predict(model, x):
    x = np.asarray(x, float)
    x = np.where(np.isfinite(x), x, model["median"])
    return float(np.r_[1., (x-model["mean"])/model["scale"]]@model["beta"])

def enrich(frame, expected_universe=None):
    f = validate_panel(frame)
    f["market_volume"] = f.groupby("decision_at").stock_volume.transform(lambda x: x.sum(min_count=1))
    f["peer_volume"] = f.groupby(["decision_at", "sector"]).stock_volume.transform("median")
    required = set(expected_universe or f.instrument_id.unique())
    cohort = f.groupby("decision_at").instrument_id.agg(lambda x: set(x) == required)
    f["cohort_complete"] = f.decision_at.map(cohort)
    for col in ("iv30", "skew30", "term_slope"):
        f["d_"+col] = f.groupby("instrument_id")[col].diff()
    prior_end = f.groupby("instrument_id").interval_end.shift()
    prior_close = f.groupby("instrument_id").is_session_end.shift().eq(True)
    gap = pd.to_datetime(f.interval_start, utc=True)-pd.to_datetime(prior_end, utc=True)
    adjacent = gap.eq(pd.Timedelta(0)) | (f.slot.eq(0) & prior_close & gap.le(pd.Timedelta(days=4)))
    for col in PRICE:
        f.loc[~adjacent, col] = np.nan
    return f

def flow_x(row, key):
    duration = number(row, "minutes", 60)/60
    log = lambda value: np.log1p(max(value, 0)) if finite(value) else np.nan
    return [float(int(row["slot"]) == i) for i in range(7)]+[
        log(number(row, "stock_volume")/duration), log(number(row, "market_volume")/duration),
        log(number(row, "peer_volume")/duration), log(number(row, "contracts_"+key)),
        number(row, "relative_spread"), number(row, "mean_dte_"+key), number(row, "otm_contract_fraction_"+key)]

def daily_panel(f):
    rows = []
    for (symbol, session), g in f.groupby(["instrument_id", "session"], sort=True):
        g = g.sort_values("slot")
        good = (g.slot.tolist() == list(range(len(g))) and bool(g.is_session_end.iloc[-1]) and g.rv.notna().all() and g.stock_volume.notna().all())
        ov, intraday = g.overnight_rv.iloc[0], float(g.rv.sum())
        next_start = g.iloc[-1].get("next_interval_start")
        next_session = utc(next_start).tz_convert("America/New_York").strftime("%Y-%m-%d") if pd.notna(next_start) else None
        rows.append({"instrument_id": symbol, "session": session, "known_at": g.decision_at.iloc[-1],
                     "next_session_date": next_session,
                     "intraday_rv": intraday if good else np.nan, "total_rv": intraday+float(ov) if good and pd.notna(ov) else np.nan})
    return pd.DataFrame(rows)

def har_x(history):
    history = np.asarray(history, dtype=float)
    return np.log(np.maximum([history[-1], history[-5:].mean(), history[-22:].mean()], 1e-12)).tolist()

def fit(frame, cutoff, cfg):
    cutoff = utc(cutoff)
    f = validate_panel(frame)
    f = f[pd.to_datetime(f.decision_at, utc=True) <= cutoff].copy()
    if f.empty:
        raise ValueError("No training observations at or before cutoff")
    universe = sorted(cfg.get("universe", f.instrument_id.unique().tolist()))
    if set(universe) != set(f.instrument_id.unique()):
        raise ValueError("Configured universe must match eligible training instruments")
    f = enrich(f, universe)
    models = {}
    for symbol, g in f.groupby("instrument_id"):
        entry = {"flow": {}, "raw": {}, "price": {}, "oi": {}}
        for key in FLOW:
            h = g[g.flow_eligible & g.cohort_complete & g["volume_"+key].notna()]
            if len(h) < cfg["min_training_rows"]:
                raise ValueError(f"{symbol}/{key}: insufficient eligible training rows")
            y = np.log1p(h["volume_"+key].to_numpy()/(h.minutes.to_numpy()/60))
            entry["flow"][key] = ridge_fit([flow_x(r, key) for r in h.to_dict("records")], y, cfg["ridge_penalty"])
            entry["raw"][key] = {str(slot): robust(np.log1p(v["volume_"+key]/(v.minutes/60))) for slot, v in h.groupby("slot")}
        for name in PRICE:
            h = g[g.price_eligible]
            entry["price"][name] = {str(slot): robust(v[name], .001) for slot, v in h.groupby("slot")}
        entry["cusum_center"] = {}
        for side in ("call", "put"):
            maxima = []
            for r in g[g.flow_eligible & g.cohort_complete].to_dict("records"):
                zs = []
                for key in FLOW:
                    if key.startswith(side) and finite(number(r, "volume_"+key)):
                        y = np.log1p(number(r, "volume_"+key)/(number(r, "minutes")/60))
                        zs.append(zscore(y-ridge_predict(entry["flow"][key], flow_x(r, key)), entry["flow"][key]["residual"]))
                if zs:
                    maxima.append(max(zs))
            entry["cusum_center"][side] = float(np.mean(maxima)) if maxima else 0.
        for side in ("call", "put"):
            h = g[g.oi_token.notna()].drop_duplicates("oi_token")
            entry["oi"][side] = robust(h["oi_"+side], .001)
        entry["seasonality"] = {str(slot): float(max(np.nanmean(v.rv/v.minutes), 1e-12)) for slot, v in g.groupby("slot")}
        level = 1.
        for r in g.sort_values("decision_at").to_dict("records"):
            if finite(number(r, "rv")):
                lam = cfg["ewma_daily_lambda"]**(number(r, "minutes")/390)
                normalized = number(r, "rv")/(entry["seasonality"][str(int(r["slot"]))]*number(r, "minutes"))
                level = lam*level+(1-lam)*normalized
        entry["initial_ewma_level"] = level
        models[symbol] = entry
    daily = daily_panel(f)
    for symbol, entry in models.items():
        d = daily[daily.instrument_id.eq(symbol)].sort_values("session") if len(daily) else pd.DataFrame()
        values = d.total_rv.to_numpy() if len(d) else np.array([])
        xs, ys = [], []
        for i in range(22, len(values)-1):
            history, target = values[:i+1], values[i+1]
            if np.isfinite(history[-22:]).all() and finite(target):
                xs.append(har_x(history))
                ys.append(np.log(max(target, 1e-12)))
        entry["har"] = ridge_fit(xs, ys, cfg["ridge_penalty"]) if len(xs) >= 30 else None
        entry["har_training_pairs"] = len(xs)
        entry["initial_daily_history"] = clean(values[-22:].tolist())
        entry["initial_daily_ewma"] = float(np.nanmean(values[-22:])) if np.isfinite(values).any() else 1e-4
        intraday_values = d.intraday_rv.to_numpy() if len(d) else np.array([])
        intraday_x, intraday_y = [], []
        for i in range(22, len(intraday_values)-1):
            history, target = intraday_values[:i+1], intraday_values[i+1]
            consecutive = all(d.iloc[j].next_session_date == str(d.iloc[j+1].session)
                              for j in range(i-21, i+1))
            if consecutive and np.isfinite(history[-22:]).all() and finite(target):
                intraday_x.append(har_x(history))
                intraday_y.append(np.log(max(target, 1e-12)))
        entry["intraday_har"] = ridge_fit(intraday_x, intraday_y, cfg["ridge_penalty"]) if len(intraday_x) >= 30 else None
        entry["intraday_har_training_pairs"] = len(intraday_x)
        tail_start = max(0, len(intraday_values)-22)
        for j in range(tail_start, len(intraday_values)-1):
            if d.iloc[j].next_session_date != str(d.iloc[j+1].session):
                tail_start = j+1
        entry["initial_intraday_history"] = clean(intraday_values[tail_start:].tolist())
        tail = intraday_values[-22:]
        entry["initial_intraday_ewma"] = float(np.nanmean(tail)) if np.isfinite(tail).any() else 1e-4
    mode = cfg.get("mode", "synthetic" if cfg.get("synthetic", False) else "historical")
    if mode not in ("synthetic", "historical", "prospective"):
        raise ValueError("Unknown dataset mode")
    if bool(cfg.get("synthetic", False)) != (mode == "synthetic"):
        raise ValueError("Config mode and synthetic flag disagree")
    dataset_id = cfg.get("dataset_id", "legacy-"+mode)
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("Dataset ID is required")
    for name, expected in (("dataset_id", dataset_id), ("mode", mode)):
        if not dataset_id.startswith("legacy-") and name not in f:
            raise ValueError("Named dataset training requires identity columns")
        if name in f and (f[name].isna().any() or set(f[name].astype(str)) != {expected}):
            raise ValueError("Training panel dataset identity mismatch")
    artifact = {"model_version": "0.2.0", "dataset_id": dataset_id, "mode": mode,
                "source_version": cfg.get("source_version", "legacy-schema-1"),
                "intraday_target_definition_version": "next_session_intraday_v1", "training_cutoff": cutoff.isoformat(), "universe": universe,
                "feature_version": cfg["feature_version"], "config": cfg, "symbols": models,
                "training_rows": len(f), "synthetic": bool(cfg.get("synthetic", False)),
                "context_rows": clean(f.sort_values("decision_at").groupby("instrument_id").tail(1).to_dict("records"))}
    artifact["model_id"] = digest(artifact)[:16]
    return clean(artifact)

def har_forecast(entry, history):
    if entry["har"] is None or len(history) < 22 or not np.isfinite(history[-22:]).all():
        return np.nan
    m = entry["har"]
    return float(np.exp(np.clip(ridge_predict(m, har_x(history)), -30, 5))*m["smearing"])

def score(row, model):
    entry = model["symbols"].get(row["instrument_id"])
    if entry is None:
        raise ValueError(f"Unknown instrument: {row['instrument_id']}")
    scores = {"flow_call": np.nan, "flow_put": np.nan, "price": np.nan, "oi_call": np.nan, "oi_put": np.nan,
              "flow_reference_call": entry["cusum_center"]["call"], "flow_reference_put": entry["cusum_center"]["put"]}
    details = {}
    if row["flow_eligible"] and row["cohort_complete"]:
        for key in FLOW:
            vol = number(row, "volume_"+key)
            y = np.log1p(vol/(number(row, "minutes")/60)) if finite(vol) else np.nan
            residual = y-ridge_predict(entry["flow"][key], flow_x(row, key))
            conditional = zscore(residual, entry["flow"][key]["residual"])
            raw = zscore(y, entry["raw"][key].get(str(int(row["slot"])), {"n": 0}))
            details[key] = {"conditional": conditional, "raw": raw, "volume": vol}
        for side in ("call", "put"):
            candidates = [details[k]["conditional"] for k in FLOW if k.startswith(side)]
            scores["flow_"+side] = max([v for v in candidates if finite(v)], default=np.nan)
    if row["price_eligible"]:
        candidates = []
        for name in PRICE:
            z = zscore(number(row, name), entry["price"][name].get(str(int(row["slot"])), {"n": 0}))
            details[name] = z
            if finite(z):
                candidates.append(abs(z))
        scores["price"] = max(candidates, default=np.nan)
    for side in ("call", "put"):
        scores["oi_"+side] = zscore(number(row, "oi_"+side), entry["oi"][side])
    return {**scores, "components": clean(details)}
