"""Shared deterministic decision engine for replay and scheduled operation."""
from __future__ import annotations
from itertools import product
import numpy as np
import pandas as pd
from .common import clean, digest, finite, number, utc
from .detector import initial_state, transition
from .models import enrich, har_forecast, score

def engine_state(model, symbol):
    m = model["symbols"][symbol]
    context = next(r for r in model["context_rows"] if r["instrument_id"] == symbol)
    next_start = context.get("next_interval_start")
    expected = utc(next_start).tz_convert("America/New_York").strftime("%Y-%m-%d") if next_start else None
    return {"detector": initial_state(), "ewma_level": m["initial_ewma_level"], "daily_ewma": m["initial_daily_ewma"],
            "daily_history": m["initial_daily_history"],
            "intraday_history": m.get("initial_intraday_history", []),
            "intraday_ewma": m.get("initial_intraday_ewma", 1e-4), "session": None, "day_slots": [], "day_rv": 0.,
            "overnight": None, "day_valid": True, "expected_session": expected}

def decide(row, model, policy, state):
    if utc(row["decision_at"]) <= utc(model["training_cutoff"]):
        raise ValueError("Monitoring decisions must be after the model training cutoff")
    if str(row["feature_version"]) != model["feature_version"]:
        raise ValueError("Feature version mismatch")
    state = {**state, "daily_history": list(state["daily_history"]),
             "intraday_history": list(state.get("intraday_history", [])), "day_slots": list(state["day_slots"])}
    symbol = row["instrument_id"]
    m = model["symbols"][symbol]
    scores = score(row, model)
    detector, events = transition(state["detector"], row, scores, policy, model["model_id"])
    state["detector"] = detector
    seasonal = m["seasonality"][str(int(row["slot"]))]
    current_forecast = state["ewma_level"]*seasonal*number(row, "minutes")
    rv = number(row, "rv")
    if finite(rv):
        lam = model["config"]["ewma_daily_lambda"]**(number(row, "minutes")/390)
        state["ewma_level"] = lam*state["ewma_level"]+(1-lam)*rv/(seasonal*number(row, "minutes"))
    if state["session"] != row["session"]:
        if state.get("expected_session") and state["expected_session"] != row["session"]:
            state["daily_history"] = []
            state["intraday_history"] = []
        state.update(session=row["session"], day_slots=[], day_rv=0., overnight=row.get("overnight_rv"), day_valid=True)
    state["day_slots"].append(int(row["slot"]))
    state["day_valid"] &= finite(rv)
    state["day_rv"] += rv if finite(rv) else 0.
    daily_forecast, daily_baseline, realized_day = np.nan, np.nan, np.nan
    if row["is_session_end"]:
        complete = state["day_valid"] and state["day_slots"] == list(range(int(row["slot"])+1))
        intraday_day = state["day_rv"] if complete else np.nan
        state["intraday_history"] = (state["intraday_history"]+[intraday_day])[-22:]
        if finite(intraday_day):
            lam = model["config"]["ewma_daily_lambda"]
            state["intraday_ewma"] = lam*state["intraday_ewma"]+(1-lam)*intraday_day
        realized_day = state["day_rv"]+float(state["overnight"]) if complete and finite(state["overnight"]) else np.nan
        state["daily_history"] = (state["daily_history"]+[realized_day])[-22:]
        if finite(realized_day):
            lam = model["config"]["ewma_daily_lambda"]
            state["daily_ewma"] = lam*state["daily_ewma"]+(1-lam)*realized_day
        daily_forecast = har_forecast(m, np.array([np.nan if v is None else v for v in state["daily_history"]], dtype=float))
        daily_baseline = state["daily_ewma"]
        next_start = row.get("next_interval_start")
        state["expected_session"] = utc(next_start).tz_convert("America/New_York").strftime("%Y-%m-%d") if next_start and not pd.isna(next_start) else None
    next_forecast = np.nan
    if finite(number(row, "next_slot")) and finite(number(row, "next_minutes")):
        next_forecast = state["ewma_level"]*m["seasonality"].get(str(int(row["next_slot"])), seasonal)*number(row, "next_minutes")
    result = {"forecast_semantics": "legacy_reconstructed_interval_and_close_to_close_proxy",
              "dataset_id": model.get("dataset_id", "legacy-synthetic" if model["synthetic"] else "legacy-historical"),
              "mode": model.get("mode", "synthetic" if model["synthetic"] else "historical"), "instrument_id": symbol, "session": row["session"], "decision_at": row["decision_at"], "interval_end": row["interval_end"],
              "minutes": row["minutes"], "model_id": model["model_id"], "policy_id": digest(policy)[:16], "scores": scores,
              "cusum_call": detector["cusum_call"], "cusum_put": detector["cusum_put"], "active_episode": detector["active"],
              "current_decision_evidence": detector["current_decision_evidence"],
              "quality_reasons": row["quality_reasons"], "flow_eligible": row["flow_eligible"] and row["cohort_complete"],
              "price_eligible": row["price_eligible"], "rv": rv, "ewma_current_interval_variance": current_forecast,
              "ewma_next_interval_variance": next_forecast, "next_interval_start": row.get("next_interval_start"),
              "next_interval_end": row.get("next_interval_end"), "next_session_har_total_variance": daily_forecast,
              "next_session_date": state["expected_session"] if row["is_session_end"] else None,
              "next_session_ewma_total_variance": daily_baseline, "realized_session_total_variance": realized_day}
    return clean(state), clean(result), clean(events)

def replay(frame, model, policy=None, end=None):
    policy = policy or model["config"]["policy"]
    f = enrich(frame, model["universe"])
    f = f[pd.to_datetime(f.decision_at, utc=True) > utc(model["training_cutoff"])]
    if end is not None:
        f = f[pd.to_datetime(f.decision_at, utc=True) <= utc(end)]
    states = {s: engine_state(model, s) for s in model["universe"]}
    decisions, events = [], []
    for row in f.to_dict("records"):
        s = row["instrument_id"]
        states[s], result, new = decide(row, model, policy, states[s])
        decisions.append(result)
        events.extend(new)
    return decisions, events

def evaluation(decisions, events, start=None, end=None):
    in_window = lambda r: (start is None or utc(r["decision_at"]) >= utc(start)) and (end is None or utc(r["decision_at"]) <= utc(end))
    d = [r for r in decisions if in_window(r)]
    e = [r for r in events if r["type"] == "opened" and in_window(r)]
    alerts = [r for r in events if r["type"] in ("opened", "updated") and in_window(r)]
    days = len(set(r["session"] for r in d))
    eligible = [r for r in d if r["flow_eligible"] or r["price_eligible"]]
    qlike = lambda y, v: float(np.mean(np.log(v)+y/v)) if len(y) else None
    rows = [r for r in d if finite(r["rv"]) and finite(r["ewma_current_interval_variance"])]
    actual = np.array([r["rv"] for r in rows])
    forecast = np.maximum([r["ewma_current_interval_variance"] for r in rows], 1e-12)
    daily_pairs = []
    for s in set(r["instrument_id"] for r in decisions):
        previous = None
        for r in [x for x in decisions if x["instrument_id"] == s]:
            if r["next_session_ewma_total_variance"] is None:
                continue
            if previous and previous.get("next_session_date") == r["session"] and r in d and finite(r["realized_session_total_variance"]):
                daily_pairs.append((r["realized_session_total_variance"], previous["next_session_ewma_total_variance"], previous["next_session_har_total_variance"]))
            previous = r
    paired = [(y, v, h) for y, v, h in daily_pairs if finite(v) and finite(h)]
    return {"forecast_semantics": "legacy_diagnostics_not_fully_prospective_accuracy", "sessions": days, "decisions": len(d), "eligible_fraction": len(eligible)/max(len(d), 1),
            "new_episodes": len(e), "episodes_per_portfolio_week": len(e)/(days/5) if days else None,
            "urgent_messages": len(alerts), "urgent_messages_per_portfolio_week": len(alerts)/(days/5) if days else None,
            "intraday_ewma_qlike": qlike(actual, forecast), "daily_paired_n": len(paired),
            "daily_ewma_qlike": qlike(np.array([x[0] for x in paired]), np.maximum([x[1] for x in paired], 1e-12)),
            "daily_har_qlike": qlike(np.array([x[0] for x in paired]), np.maximum([x[2] for x in paired], 1e-12))}

def case_metrics(events, labels, start=None, end=None):
    allowed = {"verified_documented", "synthetic"}
    opened = [e for e in events if e["type"] == "opened"]
    updates = [e for e in events if e["type"] == "updated"]
    results = []
    if "event_group" not in labels:
        raise ValueError("Case labels require event_group to keep related trades together")
    excluded_groups = set()
    for c in labels.to_dict("records"):
        if c.get("status") not in allowed:
            raise ValueError("Unverified research leads cannot be performance labels")
        if (start and utc(c["window_start"]) < utc(start)) or (end and utc(c["public_at"]) > utc(end)):
            excluded_groups.add(c["event_group"])
    for c in labels.to_dict("records"):
        first, public = utc(c["window_start"]), utc(c["public_at"])
        if first >= public:
            raise ValueError("Case window must precede public event")
        if c["event_group"] in excluded_groups:
            continue
        candidates = [e for e in opened if e["instrument_id"] == c["instrument_id"] and first <= utc(e["decision_at"]) < public]
        earliest = min([utc(e["decision_at"]) for e in candidates], default=None)
        revised = [e for e in updates if e["instrument_id"] == c["instrument_id"] and first <= utc(e["decision_at"]) < public]
        first_update = min([utc(e["decision_at"]) for e in revised], default=None)
        results.append({"case_id": c["case_id"], "event_group": c["event_group"], "detected": earliest is not None,
                        "lead_hours": (public-earliest).total_seconds()/3600 if earliest else None,
                        "updated_existing_episode": first_update is not None,
                        "first_update_lead_hours": (public-first_update).total_seconds()/3600 if first_update else None,
                        "status": c["status"]})
    groups = {r["event_group"] for r in results}
    hits = {r["event_group"] for r in results if r["detected"]}
    notified = {r["event_group"] for r in results if r["detected"] or r["updated_existing_episode"]}
    return {"case_count": len(groups), "detected_cases": len(hits), "cases_with_new_or_updated_alert": len(notified), "cases": results}

def calibrate(frame, model, start, end, target=1., labels=None):
    if utc(start) <= utc(model["training_cutoff"]):
        raise ValueError("Validation must begin after the training cutoff")
    candidates = []
    for extreme, persistence in product((3.5, 4.5, 5.5, 6.5), (6., 10.)):
        for clear_minutes in (195, 390):
            p = {**model["config"]["policy"], "extreme_flow": extreme, "extreme_price": extreme,
                 "cusum_trigger": persistence, "target_episodes_per_week": target, "clear_trading_minutes": clear_minutes}
            decisions, events = replay(frame, model, p, end)
            metrics = evaluation(decisions, events, start, end)
            if metrics["sessions"] < 5 or metrics["eligible_fraction"] < .8:
                raise ValueError("Too little validation coverage to calibrate alert volume")
            cases = case_metrics(events, labels, start, end) if labels is not None else None
            candidates.append({"policy": p, "metrics": metrics, "documented_cases": cases})
    candidates.sort(key=lambda c: (abs(c["metrics"]["episodes_per_portfolio_week"]-target), -c["policy"]["extreme_flow"]))
    return {"dataset_id": model.get("dataset_id", "legacy-synthetic" if model["synthetic"] else "legacy-historical"),
            "mode": model.get("mode", "synthetic" if model["synthetic"] else "historical"),
            "model_id": model["model_id"], "validation_start": start, "validation_end": end,
            "selection_rule": "closest_observed_episode_rate_then_higher_threshold", "requires_review": True,
            "policy": candidates[0]["policy"], "candidates": candidates}
