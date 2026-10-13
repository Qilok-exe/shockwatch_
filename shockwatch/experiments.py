"""Frozen chronological research comparisons; synthetic results are software checks only.

The runner fits on training, chooses episode policies on validation, and retains
all test candidates. It never promotes a model or uses case truth as a feature.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import platform
import sys

import numpy as np
import pandas as pd

from .common import clean, config as default_config, digest, dumps, finite, iso, number, save_json, utc
from .detector import initial_state, transition
from .features import FLOW, session_intervals, validate_panel
from .models import enrich, fit, flow_x, ridge_fit, ridge_predict, robust, score, zscore

VERSION = "chronological-comparisons-v1"
CANDIDATES = (
    "stock_only_hourly", "flow_only_hourly", "pricing_only_hourly",
    "flow_pricing_hourly", "full_hourly", "full_without_persistence_hourly",
    "full_without_oi_hourly", "full_daily", "stock_only_daily",
)



def _ready_at(row):
    values=[utc(row["decision_at"])]
    for key in ("source_ready_at","available_at","input_watermark"):
        value=row.get(key)
        if value is not None and pd.notna(value):values.append(utc(value))
    return max(values)


def _timely_rows(panel):
    return panel[[ _ready_at(r)<=utc(r["decision_at"]) for r in panel.to_dict("records") ]].copy()



def _calendar_frame(calendar):
    if not {"session","open_utc","close_utc"}.issubset(calendar):
        raise ValueError("Calendar requires explicit timezone-aware opens/closes")
    calendar=calendar.copy()
    calendar["session"]=calendar.session.astype(str)
    for key in ("open_utc","close_utc"):calendar[key]=[iso(t) for t in calendar[key]]
    calendar=calendar.sort_values("open_utc").reset_index(drop=True)
    session_intervals(calendar)  # Apply the shared calendar validity contract.
    return calendar


def _records_hash(frame):
    return digest(clean(frame.where(pd.notna(frame), None).to_dict("records")))


def freeze_manifest(panel, calendar, labels, *, dataset_id, mode="synthetic", seed=731,
                    train_sessions=100, validation_sessions=30, test_sessions=30,
                    block_length=5, bootstrap_samples=400, raw_source_hash=None, config=None):
    """Freeze the specification before examining test results; no randomized split."""
    calendar=_calendar_frame(calendar)
    for key,value in (("dataset_id",dataset_id),("mode",mode)):
        if key not in panel or panel[key].isna().any() or set(panel[key].astype(str))!={value}:
            raise ValueError("Experiment inputs require matching dataset_id/mode columns")
    dates = sorted(calendar.session.astype(str).unique())
    needed = train_sessions + validation_sessions + test_sessions
    if needed > len(dates) or min(train_sessions, validation_sessions, test_sessions) < 1:
        raise ValueError("Chronological split exceeds available calendar history")
    if mode not in {"synthetic", "historical", "prospective"} or not dataset_id:
        raise ValueError("A dataset identity and explicit mode are required")
    fold = {"fold_id": "fold_001", "train_start": dates[0], "train_end": dates[train_sessions-1],
            "validation_start": dates[train_sessions],
            "validation_end": dates[train_sessions+validation_sessions-1],
            "test_start": dates[train_sessions+validation_sessions], "test_end": dates[needed-1]}
    defaults=default_config()
    supplied=config or {}
    fit_keys=("schema_version","feature_version","decision_delay_minutes","max_quote_age_seconds",
              "max_relative_spread","min_quote_coverage","min_training_rows","ridge_penalty","ewma_daily_lambda")
    fitted_config={k:supplied.get(k,defaults[k]) for k in fit_keys}
    fitted_config["policy"]={k:supplied.get("policy",{}).get(k,v) for k,v in defaults["policy"].items()}
    fitted_config["source_version"]=supplied.get("source_version","legacy-schema-1")
    fitted_config.update(dataset_id=dataset_id,mode=mode,synthetic=mode=="synthetic",universe=sorted(panel.instrument_id.unique().tolist()))
    manifest = {
        "experiment_version": VERSION, "dataset_id": dataset_id, "mode": mode,
        "evidence_label": "Artificial software exercise; no market-performance evidence" if mode == "synthetic" else mode,
        "source_hashes": {"panel": _records_hash(validate_panel(panel)), "calendar": _records_hash(calendar),
                          "labels": _records_hash(labels), "raw_bars": raw_source_hash},
        "universe": sorted(panel.instrument_id.unique().tolist()),
        "calendar": "supplied explicit session calendar; synthetic calendar is artificial" if mode == "synthetic" else "supplied explicit session calendar",
        "availability_convention": "first-release inputs; rows received after their recorded decision_at are excluded from fitting/detector state and common coverage; outcome prices may mature later",
        "folds": [fold], "final_holdout": {"start": fold["test_start"], "end": fold["test_end"], "usage": "test once per frozen specification"},
        "history_design": "synthetic 100/30/30 software exercise" if mode == "synthetic" and (train_sessions, validation_sessions, test_sessions) == (100,30,30) else "explicit reduced-history experiment",
        "feature_version": str(panel.feature_version.iloc[0]),
        "target_definition": "next_session_intraday_v1: calendar open-to-close sum of complete interval five-minute RV; target starts after issuance",
        "variance_floor": 1e-12, "warm_state": "continuous",
        "purge": {"training_outcome_horizon_sessions": 1, "family_crossing_boundary": "exclude whole family and its instrument/window observations; partition label windows end at next local midnight"},
        "fit_configuration": fitted_config,
        "candidates": list(CANDIDATES), "model_grids": {"ridge_penalty": [fitted_config["ridge_penalty"]], "ewma_daily_lambda": [fitted_config["ewma_daily_lambda"]],
             "stock_combination": "maximum positive seasonal volume residual and log-RV surprise; own instrument excluded from market/sector volume context; absolute standardized interval return when supplied"},
        "policy_grid": {"extreme": [3.5,4.5,5.5,6.5], "persistence": [6.,10.], "clear_trading_minutes": [195,390]},
        "policy_base": fitted_config["policy"],
        "selection_rule": "validation closest new episodes per portfolio week to 1, then higher extreme threshold; no label recall optimization",
        "primary_metrics": ["paired QLIKE difference", "strict family detection", "new-or-updated family detection", "new episodes per portfolio week"],
        "resampling": {"unit": "contiguous market-date blocks retaining all instruments", "block_length": block_length,
                       "samples": bootstrap_samples, "seed": seed, "case_unit": "independent event families"},
        "software": {"python": platform.python_version(), "implementation":platform.python_implementation(), "platform":platform.platform(), "numpy": np.__version__, "pandas": pd.__version__, "runner": VERSION},
        "random_seed": seed, "labels": clean(labels.to_dict("records")),
        "coverage_rule": "paired complete calendar instrument-days for stock, flow and pricing; at least 80% common validation coverage; report broader method coverage separately",
        "label_usage": "retrospective/synthetic evaluation only; no case-label supervision; public-disclosure timing would be mandatory for a deployable supervised simulation",
        "promotion": "none; all candidates retained including failures and neutral ablations",
    }
    included,excluded=case_families(labels,fold,calendar)
    manifest["label_groups"]={fold["fold_id"]:{"included":included,"excluded":excluded}}
    manifest["manifest_id"] = digest(manifest)
    return clean(manifest)


def _validate_manifest(manifest, panel, calendar, labels):
    if manifest.get("experiment_version") != VERSION:
        raise ValueError("Experiment specification version mismatch")
    frozen = {k:v for k,v in manifest.items() if k != "manifest_id"}
    if digest(frozen) != manifest.get("manifest_id"):
        raise ValueError("Manifest changed after freezing")
    for name, frame in (("panel", validate_panel(panel)), ("calendar", calendar), ("labels", labels)):
        if _records_hash(frame) != manifest["source_hashes"][name]:
            raise ValueError(f"Frozen {name} source hash mismatch")
    if manifest["warm_state"] not in {"continuous", "reset"}:
        raise ValueError("Specify continuous or reset warm state")
    if manifest["resampling"]["block_length"] < 1 or manifest["resampling"]["samples"] < 2:
        raise ValueError("Invalid prespecified resampling settings")
    previous = None
    dates = set(calendar.session.astype(str))
    for fold in manifest["folds"]:
        ordered = [fold[k] for k in ("train_start","train_end","validation_start","validation_end","test_start","test_end")]
        if not (ordered[0] <= ordered[1] < ordered[2] <= ordered[3] < ordered[4] <= ordered[5]):
            raise ValueError("Folds must be non-overlapping chronological windows")
        if not set(ordered).issubset(dates):
            raise ValueError("Fold endpoints must be calendar sessions")
        if previous is not None and fold["test_start"] <= previous:
            raise ValueError("Test folds cannot overlap")
        previous = fold["test_end"]
    if sorted(panel.instrument_id.unique()) != manifest["universe"]:
        raise ValueError("Frozen universe mismatch")
    if not labels.empty:
        if not {"case_id","event_group","instrument_id","window_start","public_at","status"}.issubset(labels.columns):
            raise ValueError("Case labels require explicit event families and timestamps")
        if labels[["case_id","event_group","instrument_id","window_start","public_at","status"]].isna().any().any():
            raise ValueError("Case labels require non-null family, identity and availability fields")
        if not labels.instrument_id.isin(manifest["universe"]).all():
            raise ValueError("Case label instrument is outside the frozen universe")
        allowed = {"synthetic"} if manifest["mode"] == "synthetic" else {"verified_documented"}
        if not labels.status.isin(allowed).all():
            raise ValueError("Research leads or mode-mismatched labels cannot score performance")
        if any(utc(r.window_start) >= utc(r.public_at) for r in labels.itertuples()):
            raise ValueError("Case windows must precede public events")


def case_families(labels, fold, calendar):
    """A family spanning any partition boundary is excluded as a whole."""
    if labels.empty:
        return [], []
    cal = calendar.set_index(calendar.session.astype(str))
    partitions = {}
    for name in ("train", "validation", "test"):
        start = utc(cal.loc[fold[name+"_start"], "open_utc"])
        # Include the final session evening, and exclude the next civil day.
        end = (pd.Timestamp(fold[name+"_end"],tz="America/New_York")+pd.DateOffset(days=1)).tz_convert("UTC")
        partitions[name] = (start, end)
    included, excluded = [], []
    for group, g in labels.groupby("event_group", sort=True):
        start = min(utc(x) for x in g.window_start)
        end = max(utc(x) for x in g.public_at)
        names = [name for name,(a,b) in partitions.items() if a <= start and end < b]
        entry = {"event_group": str(group), "window_start": iso(start), "public_at": iso(end),
                 "instruments": sorted(g.instrument_id.unique().tolist()), "case_ids": g.case_id.tolist()}
        if len(names) == 1:
            included.append({**entry,"partition": names[0]})
        else:
            excluded.append({**entry,"reason": "event_family_crosses_fold_boundary_or_outside_fold"})
    return included, excluded


def _purge_families(frame, excluded):
    keep = np.ones(len(frame), dtype=bool)
    times = pd.to_datetime(frame.decision_at, utc=True)
    for family in excluded:
        # Also remove the source interval whose delayed decision falls after public_at.
        ends = pd.to_datetime(frame.interval_end, utc=True)
        keep &= ~(frame.instrument_id.isin(family["instruments"]) &
                  (ends >= utc(family["window_start"])) & (times <= utc(family["public_at"])+pd.Timedelta(hours=1)))
    return frame.loc[keep].copy()


def aggregate_daily(panel, calendar):
    """Sum true interval volume once; use close surfaces and last available OI.

    Completeness is against the calendar, so an entirely absent session remains
    absent and cannot be disguised by observed-row fractions.
    """
    panel = validate_panel(panel)
    expected = {}
    for interval in session_intervals(calendar):
        expected.setdefault(interval["session"], []).append((iso(interval["interval_start"]), iso(interval["interval_end"])))
    rows = []
    for (symbol, session), g in panel.groupby(["instrument_id","session"], sort=True):
        g = g.sort_values("interval_end")
        complete = list(zip(g.interval_start, g.interval_end)) == expected.get(str(session), [])
        last = g.iloc[-1].to_dict()
        row = {**last, "slot": 0, "minutes": float(g.minutes.sum()), "interval_start": g.interval_start.iloc[0],
               "decision_at":iso(max(utc(t) for t in g.decision_at)),
               "underlying_complete":bool(complete and g.rv.notna().all() and ("underlying_complete" not in g or g.underlying_complete.eq(True).all())),
               "is_session_end": True, "session_complete": complete,
               "stock_volume": float(g.stock_volume.sum()) if complete and g.stock_volume.notna().all() else np.nan,
               "rv": float(g.rv.sum()) if complete and g.rv.notna().all() else np.nan,
               "flow_eligible": bool(complete and g.flow_eligible.all()),
               "price_eligible": bool(complete and last["price_eligible"]),
               "data_ok": bool(complete and g.data_ok.all())}
        oi=g[g.oi_token.notna()]
        if not oi.empty:
            latest_oi=oi.sort_values("decision_at").iloc[-1]
            for key in ("oi_token","oi_call","oi_put","oi_coverage"):
                if key in oi:row[key]=latest_oi[key]
        for key in FLOW:
            row["volume_"+key] = float(g["volume_"+key].sum()) if complete and g["volume_"+key].notna().all() else np.nan
            # Closing population descriptors; summing contracts would count them repeatedly.
        if "underlying_start_price" in g:
            row["underlying_start_price"]=g.underlying_start_price.iloc[0] if complete else np.nan
        if "underlying_end_price" in g:
            row["underlying_end_price"]=g.underlying_end_price.iloc[-1] if complete else np.nan
        if "interval_return" in g:
            row["interval_return"] = float(g.interval_return.sum()) if complete and g.interval_return.notna().all() else np.nan
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    daily = pd.DataFrame(rows).sort_values(["decision_at","instrument_id"]).reset_index(drop=True)
    daily["market_volume"] = daily.groupby("decision_at").stock_volume.transform(lambda x:x.sum(min_count=1))
    daily["peer_volume"] = daily.groupby(["decision_at","sector"]).stock_volume.transform("median")
    universe = set(panel.instrument_id.unique())
    daily["cohort_complete"] = daily.groupby("decision_at").instrument_id.transform(lambda x:set(x)==universe)
    positions = {str(x):i for i,x in enumerate(calendar.session.astype(str))}
    for _, g in daily.groupby("instrument_id"):
        prior = None
        for idx, row in g.iterrows():
            adjacent = prior is not None and positions[str(row.session)] == positions[str(prior.session)]+1
            for key in ("iv30","skew30","term_slope"):
                daily.loc[idx,"d_"+key] = float(row[key])-float(prior[key]) if adjacent and pd.notna(row[key]) and pd.notna(prior[key]) else np.nan
            prior = row
    return daily



def _stock_context(frame):
    frame=frame.copy()
    if "interval_return" not in frame and {"underlying_start_price","underlying_end_price"}.issubset(frame):
        first=pd.to_numeric(frame.underlying_start_price,errors="coerce")
        last=pd.to_numeric(frame.underlying_end_price,errors="coerce")
        valid=np.isfinite(first)&np.isfinite(last)&first.gt(0)&last.gt(0)
        frame["interval_return"]=np.nan
        frame.loc[valid,"interval_return"]=np.log(last[valid]/first[valid])
    frame["stock_sector_peer_volume"]=np.nan
    for _,g in frame.groupby(["decision_at","sector"]):
        for idx,row in g.iterrows():
            peers=g.loc[g.instrument_id.ne(row.instrument_id),"stock_volume"].dropna()
            if not peers.empty:frame.loc[idx,"stock_sector_peer_volume"]=float(peers.median())
    return frame


def _stock_x(row):
    log = lambda value: np.log1p(max(value,0)) if finite(value) else np.nan
    duration = number(row,"minutes")/60
    return [float(int(row["slot"])==i) for i in range(7)] + [
        log((number(row,"market_volume")-number(row,"stock_volume"))/duration),
        log(number(row,"stock_sector_peer_volume")/duration)]


def fit_stock_model(training, penalty=10.):
    """All imputation, residual scales and seasonal normalization use training."""
    training=_stock_context(training)
    result = {}
    for symbol,g in training.groupby("instrument_id"):
        valid = g[g.stock_volume.notna() & g.rv.notna()]
        if "underlying_complete" in valid:
            valid=valid[valid.underlying_complete.eq(True)]
        if len(valid) < 20:
            raise ValueError(f"{symbol}: insufficient stock-only training coverage")
        volume = np.log1p(valid.stock_volume.to_numpy()/(valid.minutes.to_numpy()/60))
        entry = {"volume": ridge_fit([_stock_x(r) for r in valid.to_dict("records")],volume,penalty),
                 "rv": {str(slot):robust(np.log(np.maximum(v.rv/v.minutes,1e-12)),.05) for slot,v in valid.groupby("slot")}}
        if "interval_return" in valid:
            entry["return"] = {str(slot):robust(v.interval_return,.0001) for slot,v in valid.groupby("slot")}
        result[symbol] = entry
    return clean(result)


def _stock_score(row, fitted):
    entry = fitted[row["instrument_id"]]
    if not bool(row.get("underlying_complete",True)) or not finite(number(row,"stock_volume")) or not finite(number(row,"rv")):
        return np.nan
    volume = np.log1p(number(row,"stock_volume")/(number(row,"minutes")/60))
    residual = volume-ridge_predict(entry["volume"],_stock_x(row))
    values = [zscore(residual,entry["volume"]["residual"]),
              zscore(np.log(max(number(row,"rv")/number(row,"minutes"),1e-12)),entry["rv"].get(str(int(row["slot"])),{"n":0}))]
    if "return" in entry:
        values.append(abs(zscore(number(row,"interval_return"),entry["return"].get(str(int(row["slot"])),{"n":0}))))
    return max([v for v in values if finite(v)],default=np.nan)


def _fit_daily_options(training, penalty=10.):
    """Same ridge/robust primitives as hourly, fit independently at daily frequency."""
    symbols = {}
    for symbol,g in training.groupby("instrument_id"):
        entry = {"flow":{},"price":{},"oi":{},"cusum_center":{}}
        for key in FLOW:
            h = g[g.flow_eligible & g.cohort_complete & g["volume_"+key].notna()]
            if len(h) < 20:
                raise ValueError(f"{symbol}: insufficient complete daily flow training")
            y = np.log1p(h["volume_"+key].to_numpy()/(h.minutes.to_numpy()/60))
            entry["flow"][key] = ridge_fit([flow_x(r,key) for r in h.to_dict("records")],y,penalty)
        for name in ("d_iv30","d_skew30","d_term_slope"):
            entry["price"][name] = robust(g.loc[g.price_eligible,name],.001)
        for side in ("call","put"):
            unique = g[g.oi_token.notna()].drop_duplicates("oi_token")
            entry["oi"][side] = robust(unique["oi_"+side],.001)
            zs = []
            for row in g[g.flow_eligible & g.cohort_complete].to_dict("records"):
                vals=[]
                for key in FLOW:
                    if key.startswith(side):
                        y = np.log1p(number(row,"volume_"+key)/(number(row,"minutes")/60))
                        vals.append(zscore(y-ridge_predict(entry["flow"][key],flow_x(row,key)),entry["flow"][key]["residual"]))
                zs.append(max(vals))
            entry["cusum_center"][side] = float(np.mean(zs)) if zs else 0.
        symbols[symbol] = entry
    return clean(symbols)


def _daily_score(row, fitted):
    m=fitted[row["instrument_id"]]
    result={"flow_call":np.nan,"flow_put":np.nan,"price":np.nan,
            "oi_call":zscore(number(row,"oi_call"),m["oi"]["call"]),"oi_put":zscore(number(row,"oi_put"),m["oi"]["put"]),
            "flow_reference_call":m["cusum_center"]["call"],"flow_reference_put":m["cusum_center"]["put"]}
    if row["flow_eligible"] and row["cohort_complete"]:
        for side in ("call","put"):
            vals=[]
            for key in FLOW:
                if key.startswith(side):
                    y=np.log1p(number(row,"volume_"+key)/(number(row,"minutes")/60))
                    vals.append(zscore(y-ridge_predict(m["flow"][key],flow_x(row,key)),m["flow"][key]["residual"]))
            result["flow_"+side]=max(vals)
    if row["price_eligible"]:
        vals=[abs(zscore(number(row,k),m["price"][k])) for k in m["price"]]
        result["price"]=max([v for v in vals if finite(v)],default=np.nan)
    return result


def _candidate_scores(rows, candidate, fitted, stock):
    result=[]
    for row in rows:
        if candidate.startswith("stock_only"):
            z=_stock_score(row,stock)
            s={"flow_call":z,"flow_put":z,"price":z,"oi_call":np.nan,"oi_put":np.nan,
               "flow_reference_call":0.,"flow_reference_put":0.}
        else:
            s=_daily_score(row,fitted) if candidate.endswith("daily") else score(row,fitted)
            s={k:v for k,v in s.items() if k != "components"}
            if candidate.startswith("flow_only"):
                s["price"]=0. if finite(s["flow_call"]) and finite(s["flow_put"]) else np.nan
            if candidate.startswith("pricing_only"):
                s["flow_call"]=s["flow_put"]=0. if finite(s["price"]) else np.nan
            if candidate in {"flow_only_hourly","pricing_only_hourly","flow_pricing_hourly","full_without_oi_hourly"}:
                s["oi_call"]=s["oi_put"]=np.nan
        result.append((row,s))
    return result


def _policy(base, candidate, extreme, persistence, clear):
    p={**base,"extreme_flow":extreme,"extreme_price":extreme,"cusum_trigger":persistence,
       "clear_trading_minutes":clear,"target_episodes_per_week":1.}
    if candidate.startswith(("stock_only","flow_only","pricing_only")) or "without_persistence" in candidate:
        # Route B is a flow-with-support route; single-channel comparators use Route A.
        p["cusum_trigger"]=1e100
    return p


def _play(scored, policy, model_id, start, end, warm_state="continuous", common=None):
    states={}
    decisions=[]
    events=[]
    for row,scores in scored:
        day=str(row["session"])
        if day > end:
            continue
        if warm_state == "reset" and day < start:
            continue
        symbol=row["instrument_id"]
        if common is not None and (symbol,day) not in common:
            # Missing/unusable days cannot reset an episode or provide quiet time.
            continue
        state,new=transition(states.get(symbol,initial_state()),row,scores,policy,model_id)
        states[symbol]=state
        if start <= day <= end:
            decisions.append({"instrument_id":symbol,"session":day,"decision_at":row["decision_at"],
                              "minutes":row["minutes"],"scores":clean(scores),"active_episode":state["active"],
                              "cusum_call":state["cusum_call"],"cusum_put":state["cusum_put"],
                              "current_decision_evidence":state["current_decision_evidence"],
                              "transition":[event["type"] for event in new]})
            events.extend(new)
    return decisions,events


def _burden(decisions, events, calendar, start, end, common, universe):
    sessions=[str(d) for d in calendar.session if start <= str(d) <= end]
    openings=[e for e in events if e["type"]=="opened"]
    updates=[e for e in events if e["type"]=="updated"]
    days=sum((s,d) in common for s in universe for d in sessions)
    by_name=Counter(e["instrument_id"] for e in openings)
    duration=[]
    episodes={e["episode_id"]:e for e in openings}
    newly_opened=set(episodes)
    for decision in decisions:
        active=decision.get("active_episode")
        if active:episodes.setdefault(active["episode_id"],{**active,"instrument_id":decision["instrument_id"],"decision_at":active["started_at"]})
    for event in events:
        if event["type"]=="closed":episodes.setdefault(event["episode_id"],{**event,"decision_at":event["started_at"]})
    for event in episodes.values():
        closed=next((e for e in events if e["type"]=="closed" and e["episode_id"]==event["episode_id"]),None)
        end_at=closed["decision_at"] if closed else (max((d["decision_at"] for d in decisions),default=event["decision_at"]))
        duration.append({"episode_id":event["episode_id"],"wall_hours":(utc(end_at)-utc(event["decision_at"])).total_seconds()/3600,
                         "trading_minutes":sum(float(d["minutes"]) for d in decisions if d["instrument_id"]==event["instrument_id"] and utc(event["decision_at"])<=utc(d["decision_at"])<=utc(end_at)),
                         "left_censored":event["episode_id"] not in newly_opened,
                         "right_censored":closed is None,"trading_minutes_definition":"observed trading minutes inside the reporting window"})
    return {"calendar_sessions":len(sessions),"eligible_instrument_days":days,"new_episodes":len(openings),"material_updates":len(updates),
            "carried_in_episodes":len(set(episodes)-newly_opened),
            "episodes_per_portfolio_week":len(openings)/(len(sessions)/5) if sessions else None,
            "alerts_per_1000_eligible_instrument_days":1000*(len(openings)+len(updates))/days if days else None,
            "name_concentration":{s:n/max(len(openings),1) for s,n in sorted(by_name.items())},"episode_durations":duration}


def block_uncertainty(rows, value_key, *, block_length, samples, seed, calendar_dates=None):
    """Circular moving market-date blocks; all instruments remain together."""
    if not rows:
        return {"mean":None,"lower":None,"upper":None,"samples":0,"block_length":block_length}
    if block_length < 1 or samples < 2:
        raise ValueError("Invalid block bootstrap specification")
    dates=sorted(set(str(r["session"]) for r in rows)) if calendar_dates is None else list(calendar_dates)
    by_date={d:[float(r[value_key]) for r in rows if str(r["session"])==d] for d in dates}
    if not dates:
        raise ValueError("Bootstrap needs market dates")
    rng=np.random.default_rng(seed)
    means=[]
    for _ in range(samples):
        indexes=[]
        while len(indexes)<len(dates):
            start=int(rng.integers(0,len(dates)))
            indexes.extend((start+j)%len(dates) for j in range(block_length))
        values=[v for i in indexes[:len(dates)] for v in by_date[dates[i]]]
        if values:
            means.append(float(np.mean(values)))
    return {"mean":float(np.mean([r[value_key] for r in rows])),"lower":float(np.quantile(means,.025)) if means else None,
            "upper":float(np.quantile(means,.975)) if means else None,"samples":len(means),"block_length":block_length,
            "method":"circular contiguous market-date block bootstrap; instruments kept together"}


def paired_forecast_losses(forecasts, outcomes, *, floor=1e-12, block_length=5, samples=400, seed=731, calendar_dates=None):
    """Join only immutable, genuinely future forecasts on identical target IDs."""
    if floor <= 0:
        raise ValueError("Variance floor must be positive")
    expected={}
    for f in forecasts:
        if utc(f["target_start"]) < utc(f["issued_at"]):
            raise ValueError("Forecast target started before issuance")
        key=(f["target_id"],f["model_name"])
        if key in expected:
            raise ValueError("Duplicate forecast for same model and target")
        expected[key]=f
    latest={}
    for outcome in outcomes:
        target=outcome["target_id"]
        if target not in latest or int(outcome.get("version",1))>int(latest[target].get("version",1)):
            latest[target]=outcome
    names=sorted({f["model_name"] for f in forecasts})
    if len(names)!=2:
        return {"status":"unavailable","reason":"Exactly two forecast candidates required", "paired_targets":0,"rows":[]}
    baseline=next((n for n in ("seasonal_intraday_ewma","seasonal_ewma") if n in names),names[0])
    challenger=next(n for n in names if n!=baseline)
    pairs=[]
    clipped={baseline:0,challenger:0,"outcome":0}
    incomplete=0;unpaired=0
    for target in sorted({f["target_id"] for f in forecasts}):
        outcome=latest.get(target)
        if outcome is None or outcome.get("status") not in {"complete","revised"} or not finite(outcome.get("realized_variance")):
            incomplete+=1
            continue
        a=expected.get((target,baseline)); b=expected.get((target,challenger))
        if a is None or b is None or not finite(a.get("variance")) or not finite(b.get("variance")):
            unpaired+=1
            continue
        if any(a.get(k)!=b.get(k) for k in ("target_start","target_end","instrument_id","target_definition")):
            raise ValueError("Paired target contracts disagree")
        if any(outcome.get(k,a.get(k))!=a.get(k) for k in ("target_start","target_end","instrument_id","target_definition")):
            raise ValueError("Outcome contract disagrees with forecast")
        if utc(outcome.get("evaluated_at",a["target_end"]))<utc(a["target_end"]):
            raise ValueError("Outcome evaluated before target ended")
        y=float(outcome["realized_variance"])
        if y<0:
            raise ValueError("Negative realized variance")
        values={baseline:float(a["variance"]),challenger:float(b["variance"])}
        for name in values:
            clipped[name]+=int(values[name]<floor); values[name]=max(values[name],floor)
        clipped["outcome"]+=int(y<floor)
        loss={name:float(np.log(v)+y/v) for name,v in values.items()}
        pairs.append({"target_id":target,"instrument_id":a["instrument_id"],"session":a["session"],
                      "target_definition":a["target_definition"],"regime":a.get("regime","unclassified"),
                      "baseline_qlike":loss[baseline],"challenger_qlike":loss[challenger],
                      "paired_difference":loss[challenger]-loss[baseline],
                      "baseline_log_rv_error":float(np.log(max(y,floor))-np.log(values[baseline])),
                      "challenger_log_rv_error":float(np.log(max(y,floor))-np.log(values[challenger]))})
    breakdown={}
    for key in ("instrument_id","regime"):
        breakdown[key]={group:{"n":len(g),"paired_difference":float(np.mean([r["paired_difference"] for r in g])),
                                      "mean_baseline_qlike":float(np.mean([r["baseline_qlike"] for r in g])),
                                      "mean_challenger_qlike":float(np.mean([r["challenger_qlike"] for r in g]))}
                        for group in sorted({r[key] for r in pairs}) for g in [[r for r in pairs if r[key]==group]]}
    return clean({"status":"complete" if pairs else "unavailable","baseline":baseline,"challenger":challenger,
                  "paired_targets":len(pairs),"incomplete_targets":incomplete,"unpaired_or_unavailable_forecast_targets":unpaired,
                  "forecast_availability":{n:{"issued":sum(f["model_name"]==n and finite(f.get("variance")) for f in forecasts),"unavailable":sum(f["model_name"]==n and not finite(f.get("variance")) for f in forecasts)} for n in names},
                  "outcome_statuses":dict(Counter(o.get("status","missing") for o in latest.values())),"variance_floor":floor,"clipping":clipped,
                  "mean_baseline_qlike":float(np.mean([r["baseline_qlike"] for r in pairs])) if pairs else None,
                  "mean_challenger_qlike":float(np.mean([r["challenger_qlike"] for r in pairs])) if pairs else None,
                  "paired_difference_uncertainty":block_uncertainty(pairs,"paired_difference",block_length=block_length,samples=samples,seed=seed,calendar_dates=calendar_dates),
                  "log_rv_error_diagnostic":{name:{"mean":float(np.mean([r[key] for r in pairs])) if pairs else None,
                                                     "rmse":float(np.sqrt(np.mean([r[key]**2 for r in pairs]))) if pairs else None}
                                               for name,key in ((baseline,"baseline_log_rv_error"),(challenger,"challenger_log_rv_error"))},
                  "breakdown":breakdown,"rows":pairs})


def _family_metrics(events, labels, included, common, calendar, partition="test", decisions=None):
    results=[];excluded=[]
    for family in included:
        if family["partition"]!=partition:
            continue
        group=labels[labels.event_group.astype(str)==family["event_group"]]
        sessions=[str(r.session) for r in calendar.itertuples() if utc(r.close_utc)>=utc(family["window_start"]) and utc(r.open_utc)<utc(family["public_at"])]
        if not all((s,d) in common for s in family["instruments"] for d in sessions):
            excluded.append({"event_group":family["event_group"],"reason":"common_coverage_exclusion"});continue
        matching=[e for e in events for c in group.to_dict("records") if e["instrument_id"]==c["instrument_id"] and utc(c["window_start"])<=utc(e["decision_at"])<utc(c["public_at"]) and e["type"] in {"opened","updated"}]
        window_decisions=[d for d in (decisions or []) if d["instrument_id"] in family["instruments"] and utc(family["window_start"])<=utc(d["decision_at"])<utc(family["public_at"])]
        opened=[e for e in matching if e["type"]=="opened"]
        first=min((utc(e["decision_at"]) for e in opened),default=None)
        any_first=min((utc(e["decision_at"]) for e in matching),default=None)
        public=utc(family["public_at"])
        trading=0.
        if first is not None:
            trading=sum(max(0.,(min(public,utc(r.close_utc))-max(first,utc(r.open_utc))).total_seconds()/60) for r in calendar.itertuples())
        active_at_start=bool(window_decisions and window_decisions[0].get("active_episode") and "opened" not in window_decisions[0].get("transition",[]))
        crossings=sum(bool(d.get("current_decision_evidence")) for d in window_decisions)
        miss_reason=(None if opened else "pre_existing_episode_suppressed_new_opening" if active_at_start and crossings else
                     "current_route_evidence_without_new_opening" if crossings else
                     "no_route_threshold_crossed" if window_decisions else "no_eligible_decisions")
        results.append({"event_group":family["event_group"],"case_ids":family["case_ids"],"strict_new_episode":bool(opened),
                        "new_or_updated":bool(matching),"lead_wall_hours":(public-first).total_seconds()/3600 if first else None,
                        "lead_trading_minutes":trading if first else None,"new_or_updated_lead_hours":(public-any_first).total_seconds()/3600 if any_first else None,
                        "detection_latency_wall_minutes":(first-utc(family["window_start"])).total_seconds()/60 if first else None,
                        "eligible_decisions":len(window_decisions),"route_crossings":crossings,"active_at_window_start":active_at_start,
                        "updates":sum(e["type"]=="updated" for e in matching),"miss_reason":miss_reason})
    return {"eligible_families":len(results),"strict_detected":sum(r["strict_new_episode"] for r in results),
            "new_or_updated_detected":sum(r["new_or_updated"] for r in results),
            "missed_families":[r["event_group"] for r in results if not r["strict_new_episode"]],"coverage_exclusions":excluded,"families":results,
            "expected_positive_families":len(results),"detected_positive_families":sum(r["strict_new_episode"] for r in results),
            "missed_positive_families":sum(not r["strict_new_episode"] for r in results),
            "expected_negative_families":0,"false_openings":None,
            "negative_family_note":"No independently labelled negative family falls in this test fold; unlabelled openings cannot be called false positives.",
            "updates":sum(r["updates"] for r in results),"detection_latency_wall_minutes":[r["detection_latency_wall_minutes"] for r in results if r["detection_latency_wall_minutes"] is not None],
            "performance_status":"PASS" if results and all(r["strict_new_episode"] for r in results) else "FAIL",
            "precision_note":"Unlabeled alerts are not false positives; this is documented/synthetic-label detection, not population crime precision."}


def _case_paired_uncertainty(a,b,*,samples,seed):
    left={r["event_group"]:r for r in a["families"]};right={r["event_group"]:r for r in b["families"]}
    groups=sorted(set(left)&set(right))
    delta=np.array([int(right[g]["strict_new_episode"])-int(left[g]["strict_new_episode"]) for g in groups])
    if not len(delta):
        return {"families":0,"mean":None,"lower":None,"upper":None}
    rng=np.random.default_rng(seed)
    boot=[float(np.mean(delta[rng.integers(0,len(delta),len(delta))])) for _ in range(samples)]
    return {"families":len(groups),"mean":float(delta.mean()),"lower":float(np.quantile(boot,.025)),"upper":float(np.quantile(boot,.975)),"unit":"independent event family"}


def _common_days(panel, calendar, universe):
    expected={}
    for r in session_intervals(calendar):expected.setdefault(r["session"],[]).append((iso(r["interval_start"]),iso(r["interval_end"])))
    common=set();excluded=[]
    for s in universe:
        for day in calendar.session.astype(str):
            g=panel[panel.instrument_id.eq(s)&panel.session.eq(day)].sort_values("interval_end")
            reason=[]
            if list(zip(g.interval_start,g.interval_end))!=expected[day]:reason.append("missing_or_mismatched_calendar_intervals")
            if g.empty or not (g.stock_volume.notna().all() and g.rv.notna().all() and ("underlying_complete" not in g or g.underlying_complete.eq(True).all())):reason.append("stock_features_unusable")
            if g.empty or not (g.flow_eligible.all() and g.cohort_complete.all() and g[["volume_"+key for key in FLOW]].notna().all().all()):reason.append("flow_features_unusable")
            if g.empty or not (g.price_eligible.all() and g[["d_iv30","d_skew30","d_term_slope"]].notna().any(axis=1).all()):reason.append("surface_features_unusable")
            if reason:excluded.append({"instrument_id":s,"session":day,"reasons":reason})
            else:common.add((s,day))
    return common,excluded


def _coverage(panel,calendar,universe):
    expected=len(session_intervals(calendar))*len(universe)
    return {"expected_intervals":expected,"received_intervals":len(panel),
            "expected_instrument_sessions":len(calendar)*len(universe),"received_instrument_sessions":len(panel[["instrument_id","session"]].drop_duplicates()),
            "feature_family_usable_intervals":{"stock":int((panel.rv.notna()&panel.stock_volume.notna()&(panel.underlying_complete.eq(True) if "underlying_complete" in panel else True)).sum()),
                                               "flow":int((panel.flow_eligible&panel.cohort_complete&panel[["volume_"+key for key in FLOW]].notna().all(axis=1)).sum()),
                                               "pricing":int((panel.price_eligible&panel[["d_iv30","d_skew30","d_term_slope"]].notna().any(axis=1)).sum()),
                                               "oi_unique_releases":int(len(panel[panel.oi_token.notna()][["instrument_id","oi_token"]].drop_duplicates()))},
            "quality_exclusions":dict(Counter(reason for text in panel.quality_reasons for reason in str(text).split(";") if reason))}


def _forecast_ledger(panel, calendar, model, fold, manifest, regime_thresholds, outcome_panel=None):
    """Issue via the common future-target contract, then evaluate separately."""
    if manifest["mode"]=="prospective":
        raise ValueError("Prospective experiments require the original immutable forecast and outcome records; reconstruction would backdate issuance")
    from .engine import engine_state, decide
    from .outcomes import build_forecasts, calculate_forecast_outcome
    states={s:engine_state(model,s) for s in model["universe"]}
    forecasts=[]
    issued_records=pd.Timestamp.now(tz="UTC")
    for row in panel.to_dict("records"):
        if utc(row["decision_at"])<=utc(model["training_cutoff"]) or str(row["session"])>fold["test_end"]:continue
        s=row["instrument_id"]
        states[s],_,_=decide(row,model,model["config"]["policy"],states[s])
        issued=build_forecasts(row,states[s],model,model["config"]["policy"],calendar,dataset_id=manifest["dataset_id"],
                              mode=manifest["mode"],issued_at=iso(_ready_at(row)),recorded_at=issued_records,run_id=manifest["manifest_id"])
        for f in issued:
            if f.get("target_definition_version")!="next_session_intraday_v1":continue
            session=utc(f["target_start"]).tz_convert("America/New_York").strftime("%Y-%m-%d")
            if fold["test_start"]<=session<=fold["test_end"]:
                name=f.get("model_name",f.get("method",f.get("forecast_method")))
                thresholds=regime_thresholds.get(s,[])
                level=states[s].get("intraday_ewma")
                regime=("low" if level<=thresholds[0] else "medium" if level<=thresholds[1] else "high") if len(thresholds)==2 and finite(level) else "unclassified"
                forecasts.append({**f,"model_name":name,"session":session,"variance":f.get("predicted_variance"),"target_definition":f["target_definition_version"],"regime":regime})
    outcomes=[]
    outcome_panel=panel if outcome_panel is None else outcome_panel
    for target_id in sorted({f["target_id"] for f in forecasts}):
        f=next(f for f in forecasts if f["target_id"]==target_id)
        g=outcome_panel[outcome_panel.instrument_id.eq(f["instrument_id"])&outcome_panel.session.eq(f["session"])]
        evaluated=max([utc(f["target_end"])]+[_ready_at(r) for r in g.to_dict("records")])
        outcome=calculate_forecast_outcome(f,outcome_panel.to_dict("records"),calendar,evaluated)
        outcomes.append({**outcome,"instrument_id":f["instrument_id"],"target_start":f["target_start"],"target_end":f["target_end"],
                         "target_definition":f["target_definition_version"],"version":1,"realized_variance":outcome["observed_variance"]})
    return clean(forecasts),clean(outcomes)



def _normalize_ledger(forecasts,outcomes,manifest):
    from .outcomes import validate_forecast
    prepared=[]
    for record in forecasts:
        if record.get("target_definition_version",record.get("target_definition"))!="next_session_intraday_v1":continue
        record=validate_forecast(record)
        if record.get("method") not in {"seasonal_intraday_ewma","intraday_har"}:
            raise ValueError("Forecast ledger method is outside the prespecified seasonal EWMA/HAR comparison")
        if any(record.get(k)!=manifest[k] for k in ("dataset_id","mode")):
            raise ValueError("Forecast ledger dataset/mode differs from frozen experiment")
        session=utc(record["target_start"]).tz_convert("America/New_York").strftime("%Y-%m-%d")
        prepared.append({**record,"model_name":record["method"],"session":session,
                         "variance":record["predicted_variance"],"target_definition":record["target_definition_version"],"regime":record.get("regime","unclassified")})
    known={r["forecast_id"]:r for r in prepared}
    latest={}
    for record in outcomes:
        if record.get("target_definition_version",record.get("target_definition"))!="next_session_intraday_v1":continue
        record=dict(record)
        for key in ("target_start","target_end","evaluated_at"):
            record[key]=iso(record[key])
        if record.get("outcome_available_at"):
            record["outcome_available_at"]=iso(record["outcome_available_at"])
        if any(record.get(k)!=manifest[k] for k in ("dataset_id","mode")):
            raise ValueError("Outcome ledger dataset/mode differs from frozen experiment")
        forecast=known.get(record.get("forecast_id"))
        if forecast is None:
            raise ValueError("Outcome is not linked to a known immutable forecast")
        for key in ("target_id","target_start","target_end","instrument_id"):
            if record.get(key)!=forecast[key]:raise ValueError("Outcome target contract disagrees with linked forecast")
        if record.get("target_definition_version")!=forecast["target_definition_version"]:
            raise ValueError("Outcome target definition disagrees with linked forecast")
        if record.get("status") in {"complete","revised"}:
            if record.get("target_coverage")!=1 or not finite(record.get("observed_variance")) or record["observed_variance"]<0:
                raise ValueError("Complete outcome lacks complete nonnegative target evidence")
            if not record.get("outcome_available_at") or utc(record["outcome_available_at"])>utc(record["evaluated_at"]) or utc(record["evaluated_at"])<utc(forecast["target_end"]):
                raise ValueError("Outcome was scored before full target evidence became available")
            if not record.get("source_revision") or not record.get("source_evidence_hash"):
                raise ValueError("Complete outcome requires revision/evidence provenance")
        version=int(record.get("version",record.get("revision",1)))
        prior=latest.get(record["forecast_id"])
        if prior is None or version>prior[0]:latest[record["forecast_id"]]=(version,record)
        elif version==prior[0] and record!=prior[1]:raise ValueError("Conflicting same-revision forecast outcomes")
    by_target={}
    for version,record in latest.values():by_target.setdefault(record["target_id"],[]).append((version,record))
    matured=[]
    for target,records in by_target.items():
        # Revision counters belong to individual forecasts, not shared targets.
        # Pair only an identical observed source version across their latest outcomes.
        evidence={("complete" if r.get("status") in {"complete","revised"} else r.get("status"),r.get("observed_variance"),r.get("source_revision"),r.get("source_evidence_hash")) for _,r in records}
        chosen=max((r for _,r in records),key=lambda r:utc(r["evaluated_at"]))
        prepared_outcome={**chosen,"realized_variance":chosen.get("observed_variance"),"version":1,
                          "target_definition":chosen["target_definition_version"],
                          "matched_forecast_ids":[r["forecast_id"] for _,r in records],
                          "paired_model_outcome_evidence":[{"revision":v,**r} for v,r in records]}
        if len(evidence)>1:
            prepared_outcome.update(status="incomplete_data",realized_variance=None,
                                    comparison_exclusion="paired_models_latest_outcomes_have_different_source_evidence")
        matured.append(prepared_outcome)
    return prepared,matured


def _measurement_forecast_losses(measurement,forecasts,outcomes,settings,floor,calendar_dates):
    lookup={(r["instrument_id"],r["session"]):r for r in measurement.get("rows",[])}
    eligible=[f for f in forecasts if (f["instrument_id"],f["session"]) in lookup]
    targets={f["target_id"] for f in eligible}
    fine=[o for o in outcomes if o["target_id"] in targets]
    coarse=[]
    by_target={f["target_id"]:lookup[(f["instrument_id"],f["session"])] for f in eligible}
    for outcome in fine:
        coarse.append({**outcome,"realized_variance":by_target[outcome["target_id"]]["coarse_hourly_rv"],
                       "measurement":"hourly actual endpoint returns on the same target"})
    args={"floor":floor,"block_length":settings["block_length"],"samples":settings["samples"],"seed":settings["seed"],"calendar_dates":calendar_dates}
    return {"five_minute":paired_forecast_losses(eligible,fine,**args),
            "coarse_hourly":paired_forecast_losses(eligible,coarse,**args),
            "prediction_contract":"identical immutable pre-target forecasts; only the realized measurement changes"}


def measurement_comparison(bars,calendar,fold):
    """Compare actual five-minute and hourly endpoint returns on identical sessions."""
    if bars is None or bars.empty:
        return {"status":"unavailable","reason":"No five-minute price path supplied; interval RV cannot reconstruct coarse returns"}
    required={"instrument_id","bar_start","bar_end","open","close"}
    if not required.issubset(bars):return {"status":"failed","reason":"Raw bars missing required price/time fields"}
    intervals=session_intervals(calendar);rows=[];excluded=[]
    for (s,day),g in bars.assign(session=[utc(t).tz_convert("America/New_York").strftime("%Y-%m-%d") for t in bars.bar_end]).groupby(["instrument_id","session"]):
        if not fold["test_start"]<=day<=fold["test_end"]:continue
        g=g.sort_values("bar_end");expected=[i for i in intervals if i["session"]==day]
        chunks=[];fine=[];ok=bool(expected)
        for i in expected:
            h=g[[utc(t)>utc(i["interval_start"]) and utc(t)<=utc(i["interval_end"]) for t in g.bar_end]]
            ends=[utc(t) for t in h.bar_end]
            required_ends=list(pd.date_range(utc(i["interval_start"])+pd.Timedelta(minutes=5),utc(i["interval_end"]),freq="5min"))
            if ends!=required_ends or any(utc(a)!=b-pd.Timedelta(minutes=5) for a,b in zip(h.bar_start,ends)) or not np.isfinite(h[["open","close"]].to_numpy(dtype=float)).all() or (h[["open","close"]]<=0).any().any():ok=False;break
            # Intraday returns use actual consecutive endpoints; apply only explicit
            # previous-close adjustment factors when a corporate action changes them.
            previous=g[[utc(t)==utc(i["interval_start"]) for t in g.bar_end]]
            prior=float(previous.close.iloc[-1]) if not previous.empty else float(h.open.iloc[0]);returns=[]
            for r in h.itertuples():
                factor=float(getattr(r,"previous_close_factor",1.))
                if not previous.empty or returns:
                    if not np.isfinite(factor) or factor<=0:ok=False;break
                else:
                    factor=1.
                returns.append(np.log(float(r.close)/(prior*factor)));prior=float(r.close)
            fine.extend(returns);chunks.append(float(np.sum(returns)))
        if not ok:excluded.append({"instrument_id":s,"session":day,"reason":"incomplete_five_minute_price_path"});continue
        rows.append({"instrument_id":s,"session":day,"five_minute_rv":float(np.sum(np.square(fine))),"coarse_hourly_rv":float(np.sum(np.square(chunks))),
                     "measurement_difference":float(np.sum(np.square(chunks))-np.sum(np.square(fine)))})
    return {"status":"complete" if rows else "unavailable","horizon":"same calendar open-to-close intraday sessions",
            "note":"Measurement sensitivity only; no proxy is asserted to be the true latent variance", "paired_instrument_sessions":len(rows),"coverage_exclusions":excluded,"rows":rows}


def run_experiment(panel,calendar,labels,manifest,config=None,forecast_records=None,outcome_records=None,bars=None):
    """Run every prespecified candidate; failed candidates remain in the packet."""
    panel=validate_panel(panel);calendar=_calendar_frame(calendar);_validate_manifest(manifest,panel,calendar,labels)
    if bars is not None and manifest["source_hashes"].get("raw_bars") != _records_hash(bars):
        raise ValueError("Frozen raw-bar source hash mismatch")
    cfg=dict(manifest["fit_configuration"])
    for key in ("min_training_rows","ridge_penalty","ewma_daily_lambda","feature_version"):
        if config and key in config and config[key]!=cfg[key]:
            raise ValueError("Runtime configuration differs from frozen fit configuration: "+key)
    cfg.update(synthetic=manifest["mode"]=="synthetic",dataset_id=manifest["dataset_id"],mode=manifest["mode"],
               universe=manifest["universe"],feature_version=manifest["feature_version"],ridge_penalty=manifest["model_grids"]["ridge_penalty"][0],
               ewma_daily_lambda=manifest["model_grids"]["ewma_daily_lambda"][0])
    cfg["policy"]=manifest["policy_base"]
    packet={"manifest":manifest,"dataset_id":manifest["dataset_id"],"mode":manifest["mode"],"evidence_label":manifest["evidence_label"],"folds":[],
            "promotion":"none", "reliability":{"status":"not_measured_by_offline_runner","note":"Worker delay/retry/outage/backup metrics belong to committed operational jobs; no invented reliability numbers"}}
    timely=_timely_rows(panel)
    if timely.empty:raise ValueError("No timely first-release observations available")
    all_features=_stock_context(enrich(timely,manifest["universe"]))
    daily=_stock_context(aggregate_daily(timely,calendar))
    common,coverage_exclusions=_common_days(all_features,calendar,manifest["universe"])
    packet["coverage"]={**_coverage(enrich(panel,manifest["universe"]),calendar,manifest["universe"]),
                        "late_transport_excluded_intervals":len(panel)-len(timely),"common_instrument_sessions":len(common),"exclusions":coverage_exclusions}
    for fold in manifest["folds"]:
        included,excluded=case_families(labels,fold,calendar)
        filtered=_purge_families(all_features,excluded)
        filtered_daily=_purge_families(daily,excluded)
        train=filtered[(filtered.session>=fold["train_start"])&(filtered.session<=fold["train_end"])]
        # Conservatively purge the final training session: its one-session forward
        # outcome window reaches validation. Shared fitting sees only earlier rows.
        dates=sorted(train.session.unique());purged_dates=dates[-manifest["purge"]["training_outcome_horizon_sessions"]:] if manifest["purge"]["training_outcome_horizon_sessions"] else []
        train=train[~train.session.isin(purged_dates)]
        if train.empty:raise ValueError("No training rows after horizon purge")
        cutoff=max(train.decision_at)
        hourly_model=fit(train,cutoff,cfg)
        hourly_stock=fit_stock_model(enrich(train,manifest["universe"]),cfg["ridge_penalty"])
        daily_train=filtered_daily[(filtered_daily.session>=fold["train_start"])&(filtered_daily.session<=fold["train_end"])&~filtered_daily.session.isin(purged_dates)]
        regime_thresholds={s:np.quantile(g.rv.dropna(),[1/3,2/3]).tolist() for s,g in daily_train.groupby("instrument_id") if not g.rv.dropna().empty}
        hourly_rows=filtered[pd.to_datetime(filtered.decision_at,utc=True)>utc(cutoff)].to_dict("records")
        daily_rows=filtered_daily[pd.to_datetime(filtered_daily.decision_at,utc=True)>utc(cutoff)].to_dict("records")
        daily_models=None;daily_stock=None;daily_failure=None
        try:daily_models=_fit_daily_options(daily_train,cfg["ridge_penalty"]);daily_stock=fit_stock_model(daily_train,cfg["ridge_penalty"])
        except (ValueError,np.linalg.LinAlgError) as exc:daily_failure=str(exc)
        # Fair paired evaluation excludes all removed family windows and incomplete
        # calendar days for every method, while operational coverage stays visible.
        fold_common=common & set(zip(filtered.instrument_id,filtered.session))
        for family in excluded:
            for s in family["instruments"]:
                for day in calendar.session.astype(str):
                    row=calendar[calendar.session.astype(str).eq(day)].iloc[0]
                    if utc(row.close_utc)>=utc(family["window_start"]) and utc(row.open_utc)<=utc(family["public_at"]):fold_common.discard((s,day))
        result={"fold":fold,"training_cutoff":cutoff,"training_rows":len(train),"purged_training_sessions":purged_dates,
                "family_assignment":included,"family_exclusions":excluded,"candidates":[],
                "artifacts":{"hourly_model":hourly_model,"hourly_stock":hourly_stock,"daily_options":daily_models,"daily_stock":daily_stock,"regime_thresholds":regime_thresholds}}
        for candidate in manifest["candidates"]:
            if candidate not in CANDIDATES:
                result["candidates"].append({"candidate":candidate,"status":"failed","reason":"Unknown prespecified candidate"});continue
            if candidate.endswith("daily") and daily_failure:
                result["candidates"].append({"candidate":candidate,"status":"failed","reason":daily_failure});continue
            try:
                rows=daily_rows if candidate.endswith("daily") else hourly_rows
                fitted=daily_models if candidate.endswith("daily") else hourly_model
                stocks=daily_stock if candidate.endswith("daily") else hourly_stock
                scored=_candidate_scores(rows,candidate,fitted,stocks)
                policies=[]
                for extreme in manifest["policy_grid"]["extreme"]:
                    for persistence in manifest["policy_grid"]["persistence"]:
                        for clear in manifest["policy_grid"]["clear_trading_minutes"]:
                            p=_policy(manifest["policy_base"],candidate,extreme,persistence,clear)
                            d,e=_play(scored,p,digest([candidate,fitted,stocks])[:16],fold["validation_start"],fold["validation_end"],manifest["warm_state"],fold_common)
                            metrics=_burden(d,e,calendar,fold["validation_start"],fold["validation_end"],fold_common,manifest["universe"])
                            policies.append({"policy":p,"validation_metrics":metrics})
                valid_days=sum((s,day) in fold_common for s in manifest["universe"] for day in calendar.session.astype(str) if fold["validation_start"]<=day<=fold["validation_end"])
                expected_days=len(manifest["universe"])*sum(fold["validation_start"]<=str(day)<=fold["validation_end"] for day in calendar.session)
                if expected_days<5*len(manifest["universe"]) or valid_days/max(expected_days,1)<.8:raise ValueError("Insufficient common validation coverage for fair episode calibration")
                policies.sort(key=lambda r:(abs(r["validation_metrics"]["episodes_per_portfolio_week"]-1.),-r["policy"]["extreme_flow"]))
                selected=policies[0]["policy"]
                d,e=_play(scored,selected,digest([candidate,fitted,stocks])[:16],fold["test_start"],fold["test_end"],manifest["warm_state"],fold_common)
                broader={(r["instrument_id"],str(r["session"])) for r,s in scored if any(finite(s[k]) for k in ("flow_call","flow_put","price"))}
                operational_d,operational_e=_play(scored,selected,digest([candidate,fitted,stocks])[:16],fold["test_start"],fold["test_end"],manifest["warm_state"],None)
                result["candidates"].append({"candidate":candidate,"status":"complete","policy":selected,"policy_calibration_candidates":policies,
                    "test_metrics":_burden(d,e,calendar,fold["test_start"],fold["test_end"],fold_common,manifest["universe"]),
                    "operational_coverage":{"eligible_instrument_sessions":sum(fold["test_start"]<=day<=fold["test_end"] for s,day in broader),
                                            "definition":"at least one usable method-specific observation in the instrument/session; not the common complete-day sample"},
                    "operational_metrics":_burden(operational_d,operational_e,calendar,fold["test_start"],fold["test_end"],broader,manifest["universe"]),
                    "operational_case_metrics":_family_metrics(operational_e,labels,included,broader,calendar,decisions=operational_d),
                    "operational_events":operational_e,
                    "case_metrics":_family_metrics(e,labels,included,fold_common,calendar,decisions=d),"decisions":d,"events":e})
            except (ValueError,KeyError,np.linalg.LinAlgError) as exc:
                result["candidates"].append({"candidate":candidate,"status":"failed","reason":str(exc)})
        settings=manifest["resampling"]
        try:
            if (forecast_records is None)!=(outcome_records is None):raise ValueError("Provide both immutable forecasts and outcomes")
            f,o=_normalize_ledger(forecast_records,outcome_records,manifest) if forecast_records is not None else _forecast_ledger(filtered,calendar,hourly_model,fold,manifest,regime_thresholds,outcome_panel=panel)
            f=[r for r in f if fold["test_start"]<=str(r["session"])<=fold["test_end"]]
            test_dates=[str(d) for d in calendar.session if fold["test_start"]<=str(d)<=fold["test_end"]]
            result["forecast_records"]=f;result["outcome_records"]=o
            result["forecasts"]=paired_forecast_losses(f,o,floor=manifest["variance_floor"],block_length=settings["block_length"],samples=settings["samples"],seed=settings["seed"],calendar_dates=test_dates)
        except (ImportError,AttributeError,ValueError,KeyError) as exc:
            result["forecasts"]={"status":"failed","reason":str(exc),"paired_targets":0}
        lookup={r["candidate"]:r for r in result["candidates"] if r["status"]=="complete"}
        comparisons=[]
        for a,b in (("stock_only_hourly","full_hourly"),("full_daily","full_hourly"),("flow_pricing_hourly","full_hourly"),("full_without_persistence_hourly","full_hourly")):
            if a in lookup and b in lookup:
                delta=lookup[b]["test_metrics"]["new_episodes"]-lookup[a]["test_metrics"]["new_episodes"]
                comparisons.append({"baseline":a,"challenger":b,"new_episode_difference":delta,
                    "family_detection_uncertainty":_case_paired_uncertainty(lookup[a]["case_metrics"],lookup[b]["case_metrics"],samples=settings["samples"],seed=settings["seed"]),
                    "interpretation":"neutral episode count" if delta==0 else "synthetic software behavior only" if manifest["mode"]=="synthetic" else "held-out comparison; inspect uncertainty and burden"})
        result["detector_comparisons"]=comparisons
        result["measurement_comparison"]=measurement_comparison(bars,calendar,fold)
        if result["measurement_comparison"]["status"]=="complete" and "forecast_records" in result:
            result["measurement_comparison"]["forecast_loss_sensitivity"]=_measurement_forecast_losses(result["measurement_comparison"],result["forecast_records"],result["outcome_records"],settings,manifest["variance_floor"],test_dates)
        packet["folds"].append(result)
    packet["completion"]=packet_completion(packet,required_candidates=manifest["candidates"])
    packet["assessments"]={"code_execution_completed":packet["completion"]["complete"],
        "invariant_passed":packet["completion"]["complete"],
        "invariant_scope":"Experiment packet integrity and paired-measurement checks; end-to-end replay, forecast timing and duplicate invariants appear in VERIFICATION.json",
        "expected_scenario_response_passed":None,
        "detector_performance_passed":all(next((c.get("case_metrics",{}).get("performance_status")=="PASS" for c in f["candidates"] if c["candidate"]=="full_hourly"),False) for f in packet["folds"]),
        "detector_performance_failed":any(next((c.get("case_metrics",{}).get("performance_status")!="PASS" for c in f["candidates"] if c["candidate"]=="full_hourly"),True) for f in packet["folds"]),
        "interpretation":"Execution and invariant checks are separate from labelled-family detector performance; synthetic software behavior is not market validity."}
    packet["packet_id"]=digest(packet)
    return clean(packet)


def packet_completion(packet, required_candidates=None):
    """Check the executed matrix and paired measurement evidence, not just status flags.

    The default is the full nine-detector delivery matrix. Callers persisting an
    intentional reduced specification can pass its declared candidate list.
    Incomplete and failed packets remain valid artifacts; this returns evidence
    about their completion rather than rejecting or removing their results.
    """
    required=list(CANDIDATES if required_candidates is None else required_candidates)
    checks=[]
    def check(fold_id,name,ok,detail):
        checks.append({"fold_id":fold_id,"check":name,"ok":bool(ok),"detail":detail})
    check(None,"required_candidates",bool(required) and len(required)==len(set(required)),
          "The required matrix must contain distinct, explicitly named candidates")
    folds=packet.get("folds",[])
    check(None,"nonempty_folds",bool(folds),"At least one chronological test fold must execute")

    def forecast_check(fold_id,name,summary):
        rows=summary.get("rows",[])
        ids=[r.get("target_id") for r in rows]
        expected_methods={"seasonal_intraday_ewma","intraday_har"}
        actual_methods={summary.get("baseline"),summary.get("challenger")}
        check(fold_id,name+"_methods",actual_methods==expected_methods,
              "The actual seasonal EWMA and intraday HAR methods must be paired")
        valid_pairs=(summary.get("status")=="complete" and
                     summary.get("paired_targets",0)>0 and
                     len(rows)==summary.get("paired_targets") and
                     all(ids) and len(ids)==len(set(ids)))
        check(fold_id,name+"_targets",valid_pairs,
              "A complete status requires nonempty, unique paired target rows matching its denominator")
        uncertainty=summary.get("paired_difference_uncertainty",{})
        valid_uncertainty=(uncertainty.get("samples",0)>0 and
                           all(finite(uncertainty.get(k)) for k in ("mean","lower","upper")) and
                           uncertainty.get("lower",0)<=uncertainty.get("upper",0))
        check(fold_id,name+"_uncertainty",valid_uncertainty,
              "The paired difference requires executed finite dependent-resampling evidence")
        check(fold_id,name+"_losses",
              all(finite(summary.get(k)) for k in ("mean_baseline_qlike","mean_challenger_qlike")),
              "Both mean forecast losses must be finite")
        return set(ids) if all(ids) else set()

    for index,fold in enumerate(folds):
        fold_id=fold.get("fold",{}).get("fold_id",f"fold_{index+1}")
        candidates=fold.get("candidates",[])
        ids=[c.get("candidate") for c in candidates]
        check(fold_id,"exact_detector_matrix",Counter(ids)==Counter(required),
              "Each required detector must execute exactly once; missing, duplicate and extra IDs fail completion")
        check(fold_id,"detector_statuses",bool(candidates) and all(c.get("status")=="complete" for c in candidates),
              "Every declared detector must complete; failed and neutral candidates remain recorded")
        primary_ids=forecast_check(fold_id,"primary_forecasts",fold.get("forecasts",{}))
        measurement=fold.get("measurement_comparison",{})
        measurements=measurement.get("rows",[])
        measurement_ids=[(r.get("instrument_id"),r.get("session")) for r in measurements]
        valid_measurement=(measurement.get("status")=="complete" and
                           measurement.get("paired_instrument_sessions",0)>0 and
                           len(measurements)==measurement.get("paired_instrument_sessions") and
                           len(measurement_ids)==len(set(measurement_ids)) and
                           all(s and d and finite(r.get("five_minute_rv")) and
                               finite(r.get("coarse_hourly_rv")) and r["five_minute_rv"]>=0 and
                               r["coarse_hourly_rv"]>=0
                               for (s,d),r in zip(measurement_ids,measurements)))
        check(fold_id,"actual_measurement_paths",valid_measurement,
              "Fine/coarse RV must come from nonempty, unique actual price paths with finite nonnegative measurements")
        sensitivity=measurement.get("forecast_loss_sensitivity",{})
        fine_ids=forecast_check(fold_id,"fine_measurement_forecasts",sensitivity.get("five_minute",{}))
        coarse_ids=forecast_check(fold_id,"coarse_measurement_forecasts",sensitivity.get("coarse_hourly",{}))
        check(fold_id,"measurement_common_targets",bool(fine_ids) and fine_ids==coarse_ids and fine_ids.issubset(primary_ids),
              "Fine and coarse loss comparisons must use identical paired target IDs from the primary immutable forecasts")
    failures=[c for c in checks if not c["ok"]]
    return {"complete":not failures,"required_candidates":required,"fold_count":len(folds),
            "checks":checks,"failures":failures}


def save_packet(packet,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    existing=directory/"results.json"
    if existing.exists() and json.loads(existing.read_text(encoding="utf-8")).get("packet_id")!=packet["packet_id"]:
        raise ValueError("Refusing to overwrite a different experiment packet; use a new versioned directory")
    prior_manifest=directory/"manifest.json"
    if prior_manifest.exists() and json.loads(prior_manifest.read_text(encoding="utf-8")).get("manifest_id")!=packet["manifest"]["manifest_id"]:
        raise ValueError("Refusing to overwrite another frozen experiment specification")
    save_json(directory/"manifest.json",packet["manifest"])
    save_json(directory/"results.json",packet)
    forecast=[];detectors=[];measurement=[]
    for fold in packet["folds"]:
        forecast.extend({"dataset_id":packet["dataset_id"],"mode":packet["mode"],"fold_id":fold["fold"]["fold_id"],**r} for r in fold.get("forecasts",{}).get("rows",[]))
        detectors.extend({"dataset_id":packet["dataset_id"],"mode":packet["mode"],"fold_id":fold["fold"]["fold_id"],"candidate":r["candidate"],"status":r["status"],"reason":r.get("reason"),**{k:v for k,v in r.get("test_metrics",{}).items() if not isinstance(v,(dict,list))}} for r in fold["candidates"])
        measurement.extend({"dataset_id":packet["dataset_id"],"mode":packet["mode"],"fold_id":fold["fold"]["fold_id"],**r} for r in fold["measurement_comparison"].get("rows",[]))
    pd.DataFrame(forecast,columns=["dataset_id","mode","fold_id","target_id","instrument_id","session","target_definition","regime","baseline_qlike","challenger_qlike","paired_difference","baseline_log_rv_error","challenger_log_rv_error"]).to_csv(directory/"paired_forecast_losses.csv",index=False)
    pd.DataFrame(detectors).to_csv(directory/"detector_comparisons.csv",index=False)
    pd.DataFrame(measurement,columns=["dataset_id","mode","fold_id","instrument_id","session","five_minute_rv","coarse_hourly_rv","measurement_difference"]).to_csv(directory/"measurement_comparison.csv",index=False)
    (directory/"README.md").write_text("# SHOCKWATCH frozen experiment packet\n\n"+packet["evidence_label"]+".\n\nDataset: "+packet["dataset_id"]+"; mode: "+packet["mode"]+".\n\nManifest: "+packet["manifest"]["manifest_id"]+". All prespecified candidates, failures, neutral results and exclusions remain in results.json. No model was promoted. Forecast predictions were issued before targets began; outcomes are separate later records.\n",encoding="utf-8")
    return directory


def synthetic_experiment(directory,seed=731):
    from .synthetic import generate
    root=Path(directory)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Synthetic experiment output must be a new or empty directory")
    panel,calendar,labels=generate(root/"inputs",160,seed)
    panel["dataset_id"]=f"synthetic_experiment_{seed}"
    panel["mode"]="synthetic"
    panel.to_csv(root/"inputs"/"panel.csv",index=False)
    manifest=freeze_manifest(panel,calendar,labels,dataset_id=f"synthetic_experiment_{seed}",seed=seed)
    save_json(root/"manifest.json",manifest)
    packet=run_experiment(panel,calendar,labels,manifest)
    save_packet(packet,root)
    return packet


def persist_packet(db_path, packet):
    """Persist an immutable packet in a store already bound to the same dataset."""
    from .store import Store
    expected={"dataset_id":packet["dataset_id"],"mode":packet["mode"]}
    if {k:packet["manifest"][k] for k in expected}!=expected:
        raise ValueError("Packet and frozen manifest identities disagree")
    if digest({k:v for k,v in packet.items() if k!="packet_id"})!=packet["packet_id"]:
        raise ValueError("Packet changed after finalization")
    if not Path(db_path).is_file():
        raise ValueError("Experiment persistence requires an existing bound database")
    store=Store(db_path)
    try:
        if store.identity()!=expected:
            raise ValueError("Experiment dataset/mode does not match operational store")
        experiment_id=packet["manifest"]["manifest_id"]
        payload=dumps(packet)
        with store.transaction():
            store.db.execute("CREATE TABLE IF NOT EXISTS experiments (experiment_id TEXT PRIMARY KEY,dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),mode TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL)")
            store.db.execute("CREATE TABLE IF NOT EXISTS experiment_metrics (metric_id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),payload TEXT NOT NULL)")
            for table in ("experiments","experiment_metrics"):
                for operation in ("UPDATE","DELETE"):
                    store.db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Frozen experiment records are immutable'); END")
            prior=store.db.execute("SELECT payload FROM experiments WHERE experiment_id=?",(experiment_id,)).fetchone()
            if prior and prior[0]!=payload:
                raise ValueError("A frozen experiment ID already has different results; retain the old packet and version the specification")
            status="complete" if packet_completion(packet,required_candidates=packet["manifest"]["candidates"])["complete"] else "complete_with_exclusions_or_failures"
            store.db.execute("INSERT OR IGNORE INTO experiments VALUES(?,?,?,?,?)",(experiment_id,expected["dataset_id"],expected["mode"],status,payload))
            metrics=[]
            for f in packet["folds"]:
                fold_id=f["fold"]["fold_id"]
                for c in f["candidates"]:
                    metrics.append({**expected,"fold_id":fold_id,"kind":"detector","candidate":c["candidate"],"status":c["status"],"reason":c.get("reason"),"test_metrics":c.get("test_metrics"),"case_metrics":c.get("case_metrics"),"operational_coverage":c.get("operational_coverage"),"operational_metrics":c.get("operational_metrics"),"operational_case_metrics":c.get("operational_case_metrics")})
                metrics.append({**expected,"fold_id":fold_id,"kind":"forecasts",**{k:v for k,v in f["forecasts"].items() if k!="rows"}})
                metrics.append({**expected,"fold_id":fold_id,"kind":"measurement",**{k:v for k,v in f["measurement_comparison"].items() if k!="rows"}})
            for metric in metrics:
                metric_id=digest([experiment_id,metric])
                value=dumps(metric)
                old=store.db.execute("SELECT payload FROM experiment_metrics WHERE metric_id=?",(metric_id,)).fetchone()
                if old and old[0]!=value:raise ValueError("Immutable experiment metric collision")
                store.db.execute("INSERT OR IGNORE INTO experiment_metrics VALUES(?,?,?)",(metric_id,experiment_id,value))
        return experiment_id
    finally:
        store.db.close()


def source_hash(frame):
    """Canonical content hash used when freezing optional raw-bar input."""
    if {"decision_at","feature_version","volume_call_7_30"}.issubset(frame):
        frame=validate_panel(frame)
    return _records_hash(frame)
