"""Two paths, one episode state machine. No quota or hourly OI recounting."""
from __future__ import annotations
import numpy as np
from .common import clean, digest, finite

def initial_state():
    return {"cusum_call": 0., "cusum_put": 0., "oi_token": None, "active": None, "quiet_minutes": 0., "last_decision": None,
            "current_decision_evidence": []}

def transition(state, row, scores, policy, model_id):
    state, events = dict(state), []
    minutes = float(row["minutes"])
    fresh_oi = row.get("oi_token") is not None and row.get("oi_token") != state.get("oi_token")
    if fresh_oi:
        state["oi_token"] = row["oi_token"]
    for side in ("call", "put"):
        z = scores["flow_"+side]
        if finite(z):
            reference = scores.get("flow_reference_"+side, 0.)
            state["cusum_"+side] = max(0., state["cusum_"+side]+(z-reference-policy["cusum_drift"])*minutes/60)
    flow = max([scores[k] for k in ("flow_call", "flow_put") if finite(scores[k])], default=-np.inf)
    price = scores["price"] if finite(scores["price"]) else -np.inf
    reasons = []
    if flow >= policy["extreme_flow"]:
        reasons.append("A_exceptional_flow")
    if price >= policy["extreme_price"]:
        reasons.append("A_exceptional_repricing")
    for side in ("call", "put"):
        z, oi = scores["flow_"+side], scores["oi_"+side]
        support = price >= policy["price_support"] or (fresh_oi and finite(oi) and oi >= policy["oi_support"])
        if finite(z) and z >= policy["moderate_flow"] and state["cusum_"+side] >= policy["cusum_trigger"] and support:
            reasons.append("B_persistent_"+side)
    # This scan's route evidence is distinct from reasons retained by an episode.
    state["current_decision_evidence"] = reasons
    if reasons and state["active"] is None:
        event_id = digest([row["instrument_id"], row["decision_at"], model_id])[:20]
        severity = max(flow/policy["extreme_flow"], price/policy["extreme_price"], .01)
        state["active"] = {"episode_id": event_id, "started_at": row["decision_at"], "reasons": reasons, "notified_severity": severity}
        state["quiet_minutes"] = 0
        events.append({"type": "opened", **state["active"], "decision_at": row["decision_at"], "instrument_id": row["instrument_id"], "scores": clean(scores)})
    elif reasons and state["active"] is not None:
        old = set(state["active"]["reasons"])
        added = sorted(set(reasons)-old)
        severity = max(flow/policy["extreme_flow"], price/policy["extreme_price"], .01)
        escalated = severity >= policy.get("escalation_multiplier", 2.)*state["active"].get("notified_severity", severity)
        if added or escalated:
            state["active"] = {**state["active"], "reasons": sorted(old | set(reasons)), "notified_severity": max(severity, state["active"]["notified_severity"])}
            events.append({"type": "updated", **state["active"], "new_reasons": added, "severity_escalated": escalated,
                           "decision_at": row["decision_at"], "instrument_id": row["instrument_id"], "scores": clean(scores)})
        state["quiet_minutes"] = 0
    elif state["active"] is not None:
        full_evidence = all(finite(scores[k]) for k in ("flow_call", "flow_put", "price"))
        # CUSUM remembers old excess flow and must not block a quiet closure.
        # Only fully observed, current low-score trading intervals count.
        quiet = full_evidence and max(flow, price) < policy["clear_score"]
        if quiet:
            state["quiet_minutes"] += minutes
        elif full_evidence:
            state["quiet_minutes"] = 0
        if state["quiet_minutes"] >= policy["clear_trading_minutes"]:
            events.append({"type": "closed", **state["active"], "decision_at": row["decision_at"], "instrument_id": row["instrument_id"], "closure": "qualified_quiet_trading_time"})
            state["active"] = None
            state["quiet_minutes"] = 0
    state["last_decision"] = row["decision_at"]
    return clean(state), events
