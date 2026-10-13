"""Seeded raw software scenarios. No market-performance claims are implied.

The calendar is an explicit weekday/DST/closure fixture, not an exchange calendar.
European Black-Scholes prices merely exercise quality filters for equity options.
Scenario truth and event outcomes are stored separately and never loaded by the
source adapter, feature builder, worker or detector.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from statistics import NormalDist
import numpy as np
import pandas as pd

from .common import config, digest, iso, utc
from .features import SCHEMAS, session_intervals
from .sources import ADAPTER_VERSION, CumulativeVolume, atomic_json, build_source_panel, file_hash

GENERATOR_VERSION = "raw-scenarios-2"
SYMBOLS = {"SPY": "market", "UAL": "airlines", "LMT": "defense"}
# Frozen weekly selection anchors; wings retain real delta brackets after drift.
DELTA_GRID = (.02, .15, .35, .50, .75, .98)
CHAIN_SELECTION_VERSION = "weekly-grid-2"
CATALOGUE = [
    ("quiet_liquid", "Quiet liquid trading", "ordinary computation; no forced alert"),
    ("seasonal_spike", "Intraday seasonal spike", "opening/closing activity is an explicit baseline"),
    ("market_stress", "Broad market stress", "common stock/options evidence remains visible"),
    ("exceptional_call", "Isolated exceptional call activity", "flow spike with stale surface quotes"),
    ("exceptional_put", "Isolated exceptional put activity", "put/call symmetry"),
    ("repricing", "Exceptional repricing without volume spike", "independent pricing evidence"),
    ("persistent_price", "Persistent moderate flow plus pricing", "multiple intervals with fresh price support"),
    ("persistent_oi", "Persistent moderate flow plus new daily OI", "release-time gating and one token per position date"),
    ("unsupported_flow", "Persistent flow without support", "no forced pricing/OI injection"),
    ("flat_oi", "Large trade with flat OI", "gross volume persists despite flat end positions"),
    ("offsetting_roll", "Offsetting OI changes and contract roll", "contract-level gross/net differences retained"),
    ("public_event", "Scheduled public event", "timestamped context remains separate from detector inputs"),
    ("quote_faults", "Stale crossed and one-sided quotes", "surface rejected while volume retained"),
    ("coverage_faults", "Missing volume empty chain and omitted session", "missing differs from observed zero"),
    ("partial_release", "Partial cohort and OI publication", "no premature completeness or confirmation"),
    ("late_arrival", "Delay and out-of-order arrival", "availability and chronological atomic publication"),
    ("cumulative_faults", "Cumulative reset first difference correction", "ambiguous intervals quarantined"),
    ("corporate_lifecycle", "Split expiry and nonstandard deliverable", "share-basis adjustment and explicit exclusions"),
    ("calendar_boundaries", "Early close DST and multi-day closure", "declared session boundaries only"),
    ("restart_backfill", "Crash restart duplicate file and late backfill", "durable checkpoint and immutable first release"),
    ("two_events_week", "Two qualifying events in one week", "no weekly cap in shared detector"),
    ("conflicting_outcomes", "Conflicting upward/downward outcomes", "both signed paths retained for evaluation"),
]

POSITIVE_SCENARIOS = {"exceptional_call", "exceptional_put", "repricing", "persistent_price",
                      "persistent_oi", "flat_oi", "public_event", "two_events_week"}
NEGATIVE_SCENARIOS = {"quiet_liquid"}
QUALITY_SCENARIOS = {"quote_faults", "coverage_faults", "partial_release", "late_arrival"}

def expected_outcome(scenario_family):
    """Prespecified software expectations; context-only paths remain unscored."""
    if scenario_family in POSITIVE_SCENARIOS:
        return "expected_opening"
    if scenario_family in NEGATIVE_SCENARIOS:
        return "expected_no_alert"
    if scenario_family == "unsupported_flow":
        return "expected_no_route_b"
    if scenario_family in QUALITY_SCENARIOS:
        return "quality_exclusion_expected"
    return "context_only_not_applicable"


def _bsm(spot, strike, years, volatility, side, rate=.02):
    if years <= 0:
        return max((spot - strike) if side == "C" else (strike - spot), 0.), 0.
    root = volatility * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + .5 * volatility ** 2) * years) / root
    d2 = d1 - root
    normal = NormalDist()
    discount = math.exp(-rate * years)
    if side == "C":
        return spot * normal.cdf(d1) - strike * discount * normal.cdf(d2), normal.cdf(d1)
    return strike * discount * normal.cdf(-d2) - spot * normal.cdf(-d1), normal.cdf(d1) - 1.


def _calendar(sessions):
    # Deliberately omit two weekday dates to test a multi-day calendar closure.
    candidates = pd.bdate_range("2024-01-02", periods=sessions + 4)
    dates = [d for d in candidates if d.strftime("%Y-%m-%d") not in {"2024-01-15", "2024-01-16"}][:sessions]
    rows = []
    for i, day in enumerate(dates):
        label = day.strftime("%Y-%m-%d")
        opened = pd.Timestamp(label + " 09:30", tz="America/New_York").tz_convert("UTC")
        closed = pd.Timestamp(label + (" 13:00" if i == min(40, sessions - 2) else " 16:00"), tz="America/New_York").tz_convert("UTC")
        rows.append({"session": label, "open_utc": opened, "close_utc": closed})
    return pd.DataFrame(rows)


def _scenario_schedule(calendar):
    """Pure source schedule: independent faults remain observable in long runs.

    A complete bundle keeps whole-day absence away from other scenario inputs,
    OI support positions, public context boundaries, and paired outcome dates.
    Small fixtures explicitly retain compressed/colliding scenarios for fast
    filter tests; complete demonstrations use at least 160 sessions.
    """
    sessions = len(calendar)
    if sessions < 8:
        raise ValueError("At least eight synthetic sessions are required")
    base = 105 if sessions >= 140 else max(1, int(.60 * sessions))
    room = max(sessions - base - 2, 1)
    scenario_day = {key: min(base + i % room, sessions - 2) for i, (key, _, _) in enumerate(CATALOGUE)}

    def same_week_pair(target):
        candidates = list(range(min(target, sessions - 3), max(min(target, sessions - 3) - 5, -1), -1))
        candidates += list(range(min(target + 1, sessions - 3), min(target + 6, sessions - 2)))
        for candidate in candidates:
            first = pd.Timestamp(calendar.iloc[candidate].session)
            second = pd.Timestamp(calendar.iloc[candidate + 2].session)
            if first.isocalendar()[:2] == second.isocalendar()[:2]:
                return candidate
        raise ValueError("Calendar fixture cannot fit two activity sessions in one week")

    scenario_day["two_events_week"] = same_week_pair(scenario_day["two_events_week"])
    scenario_day["conflicting_outcomes"] = scenario_day["two_events_week"]
    instances = {key: [day] for key, day in scenario_day.items()}
    if sessions >= 160:
        for key, day in (("exceptional_call", 145), ("exceptional_put", 147), ("repricing", 155), ("persistent_price", 143), ("persistent_oi", 149), ("flat_oi", 151), ("public_event", 153), ("two_events_week", same_week_pair(140)), ("conflicting_outcomes", same_week_pair(140))):
            instances[key].append(day)
    reserved = {day for dates in instances.values() for day in dates}
    reserved.update(day + 2 for day in instances["two_events_week"])
    reserved.update(day + offset for day in instances["conflicting_outcomes"] for offset in (1, 3))
    reserved.update(day - 1 for day in instances["persistent_oi"])
    reserved.update(day + offset for day in instances["public_event"] for offset in (-1, 1))
    reserved = {day for day in reserved if 0 <= day < sessions}
    omitted = min(scenario_day["coverage_faults"] + 1, sessions - 1)
    if sessions >= 140 and omitted in reserved:
        # Use a quiet post-training session before the dense fault catalogue.
        # This also permits daily history to mature before the held-out period.
        candidates = list(range(base - 1, 99, -1)) + list(range(base, sessions - 1))
        omitted = next((day for day in candidates if day not in reserved), None)
        if omitted is None:
            raise ValueError("No independent post-training session available for whole-day omission")
    return {"scenario_day": scenario_day, "instances": instances, "split_day": scenario_day["corporate_lifecycle"], "omit_day": omitted, "reserved_days": sorted(reserved)}


def generate_scenarios(directory, sessions=160, seed=731, dataset_id=None, faults=True, cfg=None):
    """Generate canonical raw tables, immutable builder cache and isolated truth.

    Reusing a nonempty path is allowed only for exactly the same specification
    and unchanged hashed contents. Use a new directory for a changed experiment.
    """
    if not isinstance(sessions, int) or sessions < 8:
        raise ValueError("At least eight synthetic sessions are required")
    cfg = cfg or config()
    builder_keys = ("schema_version", "feature_version", "decision_delay_minutes", "max_quote_age_seconds", "max_relative_spread", "min_quote_coverage")
    builder_cfg = {k: cfg[k] for k in builder_keys}
    dataset_id = dataset_id or f"synthetic_raw_{GENERATOR_VERSION}_{seed}_{sessions}_{int(faults)}"
    spec = {"generator_version": GENERATOR_VERSION, "sessions": sessions, "seed": int(seed), "faults": bool(faults), "dataset_id": dataset_id, "builder_config": builder_cfg, "delta_grid": list(DELTA_GRID), "chain_selection_version": CHAIN_SELECTION_VERSION}
    path = Path(directory)
    existing = path / "manifest.json"
    if existing.exists():
        manifest = json.loads(existing.read_text(encoding="utf-8"))
        if manifest["identity"]["generation_spec"] != spec:
            raise ValueError("Existing raw bundle has a different immutable generation specification")
        for relative, expected in manifest["identity"]["content_hashes"].items():
            if file_hash(path / relative) != expected:
                raise ValueError("Changed first-release source content: " + relative)
        return manifest
    if path.exists() and any(path.iterdir()):
        raise ValueError("Refusing to overwrite a nonempty unmanifested bundle")
    raw = path / "raw"
    truth = path / "truth"
    archive = path / "source_archive"
    for folder in (raw, truth, archive):
        folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    calendar = _calendar(sessions)
    intervals = session_intervals(calendar)
    day_index = {str(row.session): i for i, row in calendar.iterrows()}
    schedule = _scenario_schedule(calendar)
    scenario_day = schedule["scenario_day"]
    instances = schedule["instances"]
    split_day = schedule["split_day"]
    omit_day = schedule["omit_day"]
    prices = {"SPY": 470., "UAL": 43., "LMT": 455.}
    volatilities = {"SPY": .20, "UAL": .34, "LMT": .27}
    sv = {s: 0. for s in SYMBOLS}
    contracts, metadata, positions, last_oi = {}, [], [], {}
    bars, options, batches, cumulative_archive = [], [], [], []
    converter = CumulativeVolume()
    cumulative_counters = {}

    def active(day, key):
        return faults and day in instances[key]

    def make_contracts(symbol, day, spot):
        regime = int(faults and symbol == "SPY" and day >= split_day)
        vintage = day // 5
        key = (symbol, vintage, regime)
        if key in contracts:
            return contracts[key]
        anchor_day = max(vintage * 5, split_day if regime else 0)
        anchor_close = calendar.iloc[anchor_day].close_utc
        chain = []
        for maturity in (3, 25, 50, 85, 120):
            expiry = anchor_close + pd.Timedelta(days=maturity)
            for side in ("C", "P"):
                for ad in DELTA_GRID:
                    years = maturity / 365
                    vol = volatilities[symbol]
                    target = ad if side == "C" else 1 - ad
                    strike = round(spot * math.exp((.02 + .5 * vol ** 2) * years - NormalDist().inv_cdf(target) * vol * math.sqrt(years)), 6)
                    contract_id = f"SYN_{seed}_{symbol}_{vintage}_{regime}_{maturity}_{side}_{int(ad * 100)}"
                    item = {"contract_id": contract_id, "expiry_utc": expiry, "last_trade_utc": expiry, "put_call": side, "strike": strike, "multiplier": 100, "standard_deliverable": True, "target_delta": ad, "maturity_days": maturity}
                    chain.append(item)
                    metadata.append({**item, "instrument_id": symbol, "issuer_id": "SYNTHETIC_" + symbol, "currency": "USD", "exercise_style": "EUROPEAN_FIXTURE", "settlement_style": "PHYSICAL_FIXTURE", "listed_at": iso(calendar.iloc[max(0, anchor_day - 1)].open_utc), "selected_from": iso(calendar.iloc[anchor_day].open_utc), "selection_version": CHAIN_SELECTION_VERSION})
                    last_oi[contract_id] = int(rng.integers(700, 1600))
                    if day:
                        prior = calendar.iloc[day - 1]
                        positions.append({"contract_id": contract_id, "position_date": prior.session, "open_interest": last_oi[contract_id], "published_at": calendar.iloc[day].open_utc + pd.Timedelta(minutes=15), "available_at": calendar.iloc[day].open_utc + pd.Timedelta(minutes=20)})
        contracts[key] = chain
        return chain

    for day, session_row in calendar.iterrows():
        chains = {}
        for symbol in SYMBOLS:
            if faults and symbol == "SPY" and day == split_day:
                prices[symbol] *= .5
            chains[symbol] = make_contracts(symbol, day, prices[symbol])
        session_volumes = {item["contract_id"]: 0 for chain in chains.values() for item in chain}
        day_intervals = [x for x in intervals if x["session"] == session_row.session]
        for interval in day_intervals:
            slot = interval["slot"]
            seasonal = 1.8 if slot == 0 else 1.6 if interval["is_session_end"] else .8
            common_activity = float(rng.normal(0, .12))
            shared_returns = rng.normal(0, 1, int(interval["minutes"] / 5))
            if active(day, "market_stress"):
                shared_returns *= 4
                common_activity += 1.1
            for k, (symbol, sector) in enumerate(SYMBOLS.items()):
                sv[symbol] = .95 * sv[symbol] + float(rng.normal(0, .06))
                own_activity = float(rng.normal(0, .15))
                activity = math.exp(common_activity + own_activity)
                for j, end in enumerate(pd.date_range(interval["interval_start"] + pd.Timedelta(minutes=5), interval["interval_end"], freq="5min")):
                    sigma = volatilities[symbol] * math.exp(.5 * sv[symbol]) / math.sqrt(252 * 78)
                    shock = sigma * (.60 * shared_returns[j] + .80 * rng.normal()) * math.sqrt(seasonal)
                    if faults and symbol == "UAL" and any(day in (event_day + 1, event_day + 3) for event_day in instances["conflicting_outcomes"]) and slot == 0 and j == 0:
                        shock += .035 if any(day == event_day + 1 for event_day in instances["conflicting_outcomes"]) else -.045
                    new_price = prices[symbol] * math.exp(shock)
                    spread = abs(float(rng.normal(0, sigma * .25)))
                    bar = {"instrument_id": symbol, "bar_start": end - pd.Timedelta(minutes=5), "bar_end": end, "open": prices[symbol], "high": max(prices[symbol], new_price) * (1 + spread), "low": min(prices[symbol], new_price) / (1 + spread), "close": new_price, "volume": int(12000 / (1 + k) * seasonal * activity), "previous_close_factor": .5 if faults and symbol == "SPY" and day == split_day and slot == 0 and j == 0 else 1., "available_at": end + pd.Timedelta(seconds=20)}
                    prices[symbol] = new_price
                    if not (faults and day == omit_day):
                        bars.append(bar)
                observed = []
                for item in chains[symbol]:
                    if item["last_trade_utc"] <= interval["interval_start"]:
                        continue
                    side = item["put_call"]
                    years = (item["expiry_utc"] - interval["interval_end"]).total_seconds() / (365 * 86400)
                    iv = volatilities[symbol] + .025 * (side == "P") + .008 * (years - 30 / 365) + .004 * sv[symbol] + float(rng.normal(0, .0005))
                    if active(day, "market_stress"):
                        iv += .055
                    if symbol == "UAL" and active(day, "repricing") and slot == 2:
                        iv += .12
                    if symbol == "LMT" and active(day, "persistent_price") and slot in (1, 2, 3, 4):
                        iv += .010 * slot
                    mid, delta = _bsm(prices[symbol], item["strike"], max(years, 1e-7), iv, side)
                    halfspread = max(mid * .03, .001)
                    volume = int(rng.poisson(max(1., 24 / (1 + k) * seasonal * activity * interval["minutes"] / 60)))
                    exceptional = symbol == "UAL" and slot == 2 and ((active(day, "exceptional_call") and side == "C") or (active(day, "exceptional_put") and side == "P"))
                    if exceptional:
                        volume *= 24
                    if symbol == "LMT" and slot in (1, 2, 3, 4) and side == "C" and any(active(day, name) for name in ("persistent_price", "persistent_oi", "unsupported_flow")):
                        volume *= 4
                    if symbol == "UAL" and side == "C" and slot == 2 and (active(day, "flat_oi") or active(day, "public_event") or (faults and any(day in (event_day, event_day + 2) for event_day in instances["two_events_week"]))):
                        volume *= 25
                    quote_time = interval["interval_end"]
                    bid, ask = max(.0001, mid - halfspread), mid + halfspread
                    if symbol == "UAL" and exceptional and active(day, "exceptional_call"):
                        quote_time -= pd.Timedelta(minutes=30)
                    if symbol == "SPY" and active(day, "quote_faults"):
                        if slot == 1:
                            quote_time -= pd.Timedelta(minutes=30)
                        elif slot == 2:
                            bid, ask = ask, bid
                        elif slot == 3:
                            bid = np.nan
                        elif slot == 4:
                            quote_time += pd.Timedelta(minutes=20)
                    interval_volume = volume
                    if symbol == "UAL" and active(day, "coverage_faults") and slot == 1:
                        interval_volume = np.nan
                    if symbol == "SPY" and active(day, "cumulative_faults") and item["maturity_days"] == 25 and item["put_call"] == "C" and item["target_delta"] == .15 and slot <= 3:
                        key = (item["contract_id"], session_row.session)
                        previous = cumulative_counters.get(key, 0)
                        counter = previous + volume if slot < 2 else 5 if slot == 2 else 2
                        result = converter.observe(item["contract_id"], session_row.session, interval["interval_end"], counter, reset=slot == 2)
                        cumulative_counters[key] = counter
                        interval_volume = result["interval_volume"]
                        cumulative_archive.append({"contract_id": item["contract_id"], "session": session_row.session, "event_time": iso(interval["interval_end"]), "cumulative_volume": counter, "reset_declared": slot == 2, **result})
                    standard = item["standard_deliverable"]
                    if symbol == "SPY" and active(day, "corporate_lifecycle") and item["maturity_days"] == 25 and item["put_call"] == "C" and item["target_delta"] == .15:
                        standard = False
                    available = interval["interval_end"] + pd.Timedelta(seconds=45)
                    if symbol == "LMT" and active(day, "late_arrival") and slot == 2:
                        available += pd.Timedelta(minutes=90)
                    record = {"instrument_id": symbol, **{name: item[name] for name in ("contract_id", "expiry_utc", "last_trade_utc", "put_call", "strike", "multiplier")}, "standard_deliverable": standard, "interval_end": interval["interval_end"], "bid": bid, "ask": ask, "quote_time": quote_time, "iv": iv, "delta": delta, "interval_volume": interval_volume, "available_at": available}
                    session_volumes[item["contract_id"]] += volume
                    observed.append(record)
                expected = len(observed)
                if symbol == "UAL" and active(day, "coverage_faults") and slot == 2:
                    observed = []
                omit_symbol = symbol == "LMT" and active(day, "partial_release") and slot == 1
                if not (faults and day == omit_day) and not omit_symbol:
                    options.extend(observed)
                    batches.append({"instrument_id": symbol, "sector": sector, "interval_start": interval["interval_start"], "interval_end": interval["interval_end"], "available_at": max([record["available_at"] for record in observed] or [interval["interval_end"] + pd.Timedelta(seconds=45)]), "expected_contracts": expected, "complete": bool(len(observed) == expected and expected > 0), "source": "SYNTHETIC_RAW_SOFTWARE_FIXTURE", "schema_version": "1"})
        for symbol, chain in chains.items():
            for n, item in enumerate(chain):
                contract_id = item["contract_id"]
                if item["last_trade_utc"] <= session_row.open_utc:
                    continue
                volume = session_volumes[contract_id]
                # Gross observed volume is distinct from net new contracts.
                change = int(rng.binomial(volume, .10) - rng.binomial(volume, .085))
                if symbol == "UAL" and active(day, "flat_oi"):
                    change = 0
                if symbol == "LMT" and faults and any(day == max(event_day - 1, 0) for event_day in instances["persistent_oi"]) and item["put_call"] == "C":
                    change = min(volume, int(volume * .5))
                if active(day, "offsetting_roll"):
                    change = int(volume * .15) * (1 if n % 2 else -1)
                last_oi[contract_id] = max(0, last_oi[contract_id] + change)
                next_open = calendar.iloc[day + 1].open_utc if day + 1 < sessions else session_row.close_utc + pd.Timedelta(days=1)
                published = next_open + pd.Timedelta(minutes=15)
                if faults and day + 1 == scenario_day["partial_release"] and n % 2:
                    published += pd.Timedelta(hours=3)
                positions.append({"contract_id": contract_id, "position_date": session_row.session, "open_interest": last_oi[contract_id], "published_at": published, "available_at": published + pd.Timedelta(minutes=5)})

    tables = {"sessions": calendar, "batches": pd.DataFrame(batches, columns=SCHEMAS["batches"]), "bars": pd.DataFrame(bars, columns=SCHEMAS["bars"]), "options": pd.DataFrame(options, columns=SCHEMAS["options"]), "oi": pd.DataFrame(positions, columns=SCHEMAS["oi"])}
    for name, frame in tables.items():
        frame.to_csv(raw / (name + ".csv"), index=False)
    pd.DataFrame(metadata).to_csv(archive / "contracts.csv", index=False)
    pd.DataFrame(cumulative_archive, columns=["contract_id", "session", "event_time", "cumulative_volume", "reset_declared", "interval_volume", "reason"]).to_csv(archive / "cumulative_volume.csv", index=False)
    panel = build_source_panel(raw, cfg, dataset_id, "synthetic")
    panel.to_csv(path / "panel.csv", index=False)
    rows, case_rows = [], []
    detection_families = {"exceptional_call", "exceptional_put", "repricing", "persistent_price", "persistent_oi", "flat_oi", "public_event"}
    for key, title, purpose in CATALOGUE:
        for instance_index, day in enumerate(instances[key]):
            interval = [x for x in intervals if x["session"] == calendar.iloc[day].session][0]
            instance_id = key if instance_index == 0 else "test_" + key
            symbol = "SPY" if key in ("market_stress", "quote_faults", "cumulative_faults", "corporate_lifecycle", "calendar_boundaries") else "LMT" if key in ("persistent_price", "persistent_oi", "unsupported_flow", "partial_release", "late_arrival") else "UAL"
            end_day = min(day + (3 if key in ("two_events_week", "conflicting_outcomes") else 1), sessions - 1)
            rows.append({"scenario_id": instance_id, "scenario_family": key, "title": title, "instrument_id": symbol, "window_start": iso(interval["interval_start"]), "window_end": iso(calendar.iloc[end_day].close_utc), "purpose": purpose, "status": "synthetic", "expected_outcome": expected_outcome(key), "mechanism": "seeded baseline" if key in ("quiet_liquid", "seasonal_spike") else "source/adapter acceptance fixture" if key in ("calendar_boundaries", "restart_backfill") else "raw injection" if faults else "catalogue only; faults disabled"})
            if faults and key in detection_families:
                prefix = "validation_" if instance_index == 0 else "test_"
                case_rows.append({"case_id": prefix + key, "instrument_id": symbol, "event_group": prefix + key, "window_start": iso(calendar.iloc[day].open_utc), "public_at": iso(calendar.iloc[min(day + 1, sessions - 1)].open_utc), "status": "synthetic", "source_url": "synthetic://raw-scenario", "label_definition": "Injected artificial activity before a declared next-session evaluation boundary; not a market event label"})
            if faults and key == "two_events_week":
                prefix = "validation_" if instance_index == 0 else "test_"
                for offset in (0, 2):
                    event_day = min(day + offset, sessions - 2)
                    case_rows.append({"case_id": prefix + "two_events_" + str(offset), "instrument_id": "UAL", "event_group": prefix + "two_event_outcome_family", "window_start": iso(calendar.iloc[event_day].open_utc), "public_at": iso(calendar.iloc[event_day + 1].open_utc), "status": "synthetic", "source_url": "synthetic://raw-scenario", "label_definition": "Two related artificial activity/outcome paths counted as one family"})
    pd.DataFrame(rows).to_csv(truth / "scenarios.csv", index=False)
    pd.DataFrame(case_rows, columns=["case_id", "instrument_id", "event_group", "window_start", "public_at", "status", "source_url", "label_definition"]).to_csv(truth / "cases.csv", index=False)
    events = []
    if faults:
        for instance_index, day in enumerate(instances["public_event"]):
            events.append({"context_id": digest([dataset_id, "scheduled_public_event", instance_index])[:24], "event_id": "synthetic_public_event_" + str(instance_index), "instrument_id": "UAL", "public_at": iso(calendar.iloc[min(day + 1, sessions - 1)].open_utc), "available_at": iso(calendar.iloc[max(day - 1, 0)].close_utc), "event_type": "scheduled_public_event", "title": "Scheduled synthetic public event", "kind": "SYNTHETIC_SCHEDULED_PUBLIC_EVENT", "source": "synthetic://scenario", "source_reference": "synthetic://scenario", "dataset_id": dataset_id, "mode": "synthetic"})
    context_frame = pd.DataFrame(events, columns=["context_id", "event_id", "instrument_id", "public_at", "available_at", "event_type", "title", "kind", "source", "source_reference", "dataset_id", "mode"])
    context_frame.to_csv(truth / "context.csv", index=False)
    context_frame.to_csv(archive / "public_context.csv", index=False)
    fault_report = {"dataset_id": dataset_id, "mode": "synthetic", "faults_enabled": bool(faults), "injections": rows, "omitted_raw_session": str(calendar.iloc[omit_day].session) if faults else None, "timing_note": "Delayed rows are first releases; past decisions never receive late values. Changed backfills are rejected by source hashes and Store.ingest.", "scenario_response_note": "Injected paths exercise software behavior; scenario alert counts are measured by evaluation, never asserted market recall.", "recovery_checks": "tests/test_sources.py and integration tests; process kill/scheduler behavior require separate operation evidence", "reduced_history_note": "Scenarios may share sessions when fewer than 140 sessions are requested."}
    fault_report["observed_feature_gates"] = {"panel_rows": len(panel), "flow_eligible_rows": int(panel.flow_eligible.sum()), "price_eligible_rows": int(panel.price_eligible.sum()), "both_eligible_rows": int(panel.data_ok.sum()), "partial_cohort_intervals": int(sum(set(g.instrument_id) != set(SYMBOLS) for _, g in panel.groupby("decision_at"))), "surface_unavailable_rows": int(panel.quality_reasons.str.contains("surface_unavailable").sum()), "invalid_volume_rows": int(panel.quality_reasons.str.contains("invalid_or_missing_main_volume").sum()), "incomplete_chain_rows": int(panel.quality_reasons.str.contains("incomplete_or_late_chain").sum()), "late_delivery_rows": int((pd.to_datetime(panel.source_ready_at, utc=True) > pd.to_datetime(panel.decision_at, utc=True)).sum()), "split_adjustments": int(tables["bars"].previous_close_factor.ne(1.).sum()), "cumulative_quarantined_intervals": int(sum(row["interval_volume"] is None for row in cumulative_archive))}
    atomic_json(truth / "fault_report.json", fault_report)
    files = ["raw/" + name + ".csv" for name in SCHEMAS] + ["panel.csv", "source_archive/contracts.csv", "source_archive/cumulative_volume.csv", "source_archive/public_context.csv"]
    identity = {"generation_spec": spec, "builder_config": builder_cfg, "content_hashes": {name: file_hash(path / name) for name in files}, "universe": list(SYMBOLS), "mode": "synthetic", "dataset_id": dataset_id, "source": "SYNTHETIC_RAW_SOFTWARE_FIXTURE", "schema_version": "1", "adapter_version": ADAPTER_VERSION}
    manifest = {"manifest_id": digest(identity), "identity": identity, "dataset_id": dataset_id, "mode": "synthetic", "source": identity["source"], "schema_version": "1", "adapter_version": ADAPTER_VERSION, "extracted_at": iso(pd.Timestamp.now(tz="UTC")), "generator_version": GENERATOR_VERSION, "seed": int(seed), "sessions": sessions, "raw_directory": "raw", "truth_directory": "truth", "calendar": "Weekday fixture; Jan 15-16 omitted and one artificial early close; America/New_York DST. Not a verified exchange calendar.", "clock": "UTC simulated market availability; actual wall processing recorded separately in source checkpoints and job records.", "chain_selection": "Declared weekly grid: five expiries and six fixed signed-delta anchors per call/put (.02, .15, .35, .50, .75, .98). Expired contracts leave the grid; nonstandard deliverables remain archived and are excluded by the builder. This is deliberately not a full real-market chain.", "delta_anchors": list(DELTA_GRID), "chain_selection_version": CHAIN_SELECTION_VERSION, "coverage_limits": "Three synthetic instruments and weekly contract grids; all listed requested grid contracts are counted before filtering. No licensed data or real identifiers.", "units": {"underlying_volume": "shares per 5-minute bar", "option_volume": "contracts per interval; missing values remain missing", "iv": "annualized decimal", "delta": "positive call and negative put", "open_interest": "nonnegative integer outstanding contracts per position date", "prices": "raw USD with explicit split factor; outcome basis columns adjust once"}, "cumulative_volume_rule": "Difference within contract/session only; absent initial baseline, declared reset and negative correction produce missing/quarantined interval volumes.", "oi_convention": "Net end-of-session open contracts, published next fixture session at 09:45 and available 09:50 Eastern; partial releases retain later availability. Per-contract net changes bounded by daily volume; observed gross volume remains distinct.", "greek_method": "European Black-Scholes, fixed 2% annual risk-free rate, no dividends; a software pricing fixture, not a production American option engine.", "instrument_mapping": [{"instrument_id": symbol, "issuer_id": "SYNTHETIC_" + symbol, "valid_from": str(calendar.session.iloc[0]), "valid_to": str(calendar.session.iloc[-1])} for symbol in SYMBOLS], "requested_received": {"requested_sessions": sessions, "received_bar_sessions": int(tables["bars"].bar_end.dt.date.nunique()), "requested_cohort_rows": len(intervals) * len(SYMBOLS), "received_batch_rows": len(batches), "received_options_rows": len(options), "received_bars_rows": len(bars)}, "missing_codes": {"CSV empty": "missing, never observed zero"}, "revision_policy": "Immutable first-release hashes; corrections require a new isolated dataset", "entitlement_limits": "Artificial locally generated software fixture; no real feed or notifications"}
    atomic_json(path / "manifest.json", manifest)
    return manifest


def evaluate_scenarios(bundle, db_path=None):
    """Read-only measured fault/response report with explicit invariant checks.

    Alert counts are diagnostics, never forced passes for random quiet paths or
    proxies for real-market precision/recall. SQLite is optional so source gates
    remain independently inspectable before worker integration.
    """
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    panel = pd.read_csv(bundle / "panel.csv", float_precision="round_trip")
    catalogue = pd.read_csv(bundle / "truth/scenarios.csv")
    fault_report = json.loads((bundle / "truth/fault_report.json").read_text(encoding="utf-8"))
    from .features import read_tables
    tables = read_tables(bundle / "raw")
    checks = []

    def check(name, passed, denominator, evidence):
        checks.append({"check": name, "passed": bool(passed), "denominator": int(denominator), "evidence": evidence})

    forbidden = {"scenario_id", "case_id", "event_group", "public_at", "future_price", "injection_flag"}
    check("truth_isolated_from_detector_inputs", not forbidden.intersection(panel.columns), len(panel), sorted(forbidden.intersection(panel.columns)))
    for table in tables.values():
        if forbidden.intersection(table.columns):
            checks[-1]["passed"] = False
    check("input_cutoff_respected", (pd.to_datetime(panel.input_watermark, utc=True) <= pd.to_datetime(panel.decision_at, utc=True)).all(), len(panel), "Every included-input watermark is at or before the fixed decision cutoff")
    check("dataset_and_mode_persist", panel.dataset_id.eq(manifest["dataset_id"]).all() and panel["mode"].eq("synthetic").all(), len(panel), manifest["dataset_id"])
    check("first_release_keys_unique", all(not tables[name].duplicated(keys).any() for name, keys in (("sessions", ["session"]), ("batches", ["instrument_id", "interval_end"]), ("bars", ["instrument_id", "bar_end"]), ("options", ["contract_id", "interval_end"]), ("oi", ["contract_id", "position_date"]))), sum(len(t) for t in tables.values()), "Canonical schema-1 keys; revisions remain isolated")
    check("all_canonical_schemas_preserved", all(list(tables[name].columns) == columns for name, columns in SCHEMAS.items()), len(SCHEMAS), "Exact schema-1 headers")
    late = panel[pd.to_datetime(panel.source_ready_at, utc=True) > pd.to_datetime(panel.decision_at, utc=True)]
    if len(late):
        check("late_options_do_not_rewrite_cutoff", (~late.flow_eligible & ~late.price_eligible).all(), len(late), "Late full-chain releases remain unavailable to their earlier decision")
    omitted = fault_report.get("omitted_raw_session")
    if omitted:
        present = tables["sessions"].session.astype(str).eq(omitted).any()
        emitted = any(pd.to_datetime(tables[name][column], utc=True).dt.strftime("%Y-%m-%d").eq(omitted).any() for name, column in (("bars", "bar_end"), ("options", "interval_end"), ("batches", "interval_end")))
        check("omitted_session_retains_calendar_fault", present and not emitted, 1, omitted)
    cumulative = pd.read_csv(bundle / "source_archive/cumulative_volume.csv")
    if len(cumulative):
        ambiguous = cumulative[cumulative.reason.ne("observed_difference")]
        check("ambiguous_cumulative_volume_is_missing", ambiguous.interval_volume.isna().all(), len(ambiguous), ambiguous.reason.value_counts().to_dict())
    splits = tables["bars"][tables["bars"].previous_close_factor.ne(1)]
    for row in splits.to_dict("records"):
        chosen = panel[panel.instrument_id.eq(row["instrument_id"]) & pd.to_datetime(panel.interval_start, utc=True).eq(row["bar_start"])]
        check("split_has_no_manufactured_return", len(chosen) == 1 and chosen.rv.notna().all() and chosen.rv.lt(.10).all(), len(chosen), {"instrument_id": row["instrument_id"], "bar_start": iso(row["bar_start"]), "factor": row["previous_close_factor"]})
    for scenario, symbol, slots, column, expected in (("quote_faults", "SPY", (1, 2, 3, 4), "price_eligible", False), ("coverage_faults", "UAL", (1, 2), "flow_eligible", False)):
        item = catalogue[catalogue.scenario_id.eq(scenario)]
        if fault_report["faults_enabled"] and len(item):
            day = utc(item.window_start.iloc[0]).strftime("%Y-%m-%d")
            chosen = panel[panel.instrument_id.eq(symbol) & panel.session.eq(day) & panel.slot.isin(slots)]
            check(scenario + "_quality_gate", len(chosen) == len(slots) and chosen[column].eq(expected).all(), len(chosen), {"session": day, "slots": list(slots), "column": column})

    if fault_report["faults_enabled"]:
        partial = catalogue[catalogue.scenario_id.eq("partial_release")]
        day = utc(partial.window_start.iloc[0]).strftime("%Y-%m-%d")
        morning = panel[panel.session.eq(day) & panel.slot.eq(0)]
        check("partial_oi_release_cannot_confirm", len(morning) > 0 and morning.oi_token.isna().all(), len(morning), {"session": day, "publication": "half-chain delayed until 12:45 Eastern"})
    # End-position changes cannot exceed the contracts actually traded. Skip
    # rows with missing interval volumes, whose gross total is unobservable.
    options = tables["options"].copy()
    options["position_date"] = options.interval_end.dt.strftime("%Y-%m-%d")
    totals = options.groupby(["contract_id", "position_date"]).interval_volume.agg(["sum", "count", "size"])
    positions = tables["oi"].sort_values(["contract_id", "position_date"]).copy()
    positions["net_change"] = positions.groupby("contract_id").open_interest.diff()
    reconciled = positions.set_index(["contract_id", "position_date"]).join(totals)
    reconciled = reconciled[reconciled.net_change.notna() & reconciled["count"].eq(reconciled["size"])]
    check("oi_net_change_respects_observed_volume", (reconciled.net_change.abs() <= reconciled["sum"]).all(), len(reconciled), "Only fully observed contract-session volume totals enter reconciliation")

    case_labels = pd.read_csv(bundle / "truth/cases.csv")
    paired = case_labels[case_labels.event_group.str.contains("two_event_outcome_family", na=False)]
    if len(paired):
        groups = list(paired.groupby("event_group"))
        check("two_events_share_actual_calendar_week", all(len(g) == 2 and len({utc(x).isocalendar()[:2] for x in g.window_start}) == 1 for _, g in groups), len(groups), "Two activity occurrences remain one related family without a weekly quota")

    decisions, events, forecasts, outcomes = [], [], [], []
    evaluation_policy = None
    if db_path is not None:
        import sqlite3
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            present = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            for table, target in (("decisions", decisions), ("events", events), ("forecasts", forecasts), ("forecast_outcomes", outcomes)):
                if table in present:
                    target.extend(json.loads(row[0]) for row in connection.execute("SELECT payload FROM " + table))
        finally:
            connection.close()
        policy_file = Path(db_path).parent / "policy.json"
        if policy_file.is_file():
            evaluation_policy = json.loads(policy_file.read_text(encoding="utf-8"))["policy"]
        if forecasts:
            ordered = all(utc(f["input_watermark"]) <= utc(f["asof_at"]) <= utc(f["issued_at"]) <= utc(f["target_start"]) for f in forecasts)
            check("stored_forecasts_fully_future", ordered, len(forecasts), "Included input <= asof <= issue <= target start")
        if decisions:
            check("stored_decisions_share_dataset", all(d.get("dataset_id") == manifest["dataset_id"] and d.get("mode") == "synthetic" for d in decisions), len(decisions), manifest["dataset_id"])
    responses = []
    for item in catalogue.to_dict("records"):
        first, last = utc(item["window_start"]), utc(item["window_end"])
        matched = [d for d in decisions if d["instrument_id"] == item["instrument_id"] and first <= utc(d["decision_at"]) <= last]
        event_rows = [e for e in events if e["instrument_id"] == item["instrument_id"] and first <= utc(e["decision_at"]) <= last]
        feature_rows = panel[panel.instrument_id.eq(item["instrument_id"]) & pd.to_datetime(panel.decision_at, utc=True).between(first, last)]
        family = item.get("scenario_family") or item["scenario_id"].removeprefix("test_")
        expected = item.get("expected_outcome") or expected_outcome(family)
        isolated_events = []
        isolated_route_b = 0
        if evaluation_policy is not None:
            from .detector import initial_state, transition
            tokens = {utc(r.decision_at): (r.oi_token if pd.notna(r.oi_token) else None) for r in feature_rows.itertuples()}
            state = initial_state()
            for decision in sorted(matched, key=lambda d: d["decision_at"]):
                state, new = transition(state, {"instrument_id": decision["instrument_id"], "decision_at": decision["decision_at"],
                    "minutes": decision["minutes"], "oi_token": tokens.get(utc(decision["decision_at"]))},
                    decision["scores"], evaluation_policy, decision["model_id"])
                isolated_events.extend(new)
                isolated_route_b += sum(reason.startswith("B_") for reason in state["current_decision_evidence"])
        isolated_opened = sum(e["type"] == "opened" for e in isolated_events)
        isolated_updated = sum(e["type"] == "updated" for e in isolated_events)
        isolated_closed = sum(e["type"] == "closed" for e in isolated_events)
        if evaluation_policy is None and expected in {"expected_opening", "expected_no_alert", "expected_no_route_b"}:
            result, reason = "NOT_APPLICABLE", "No operational policy supplied for detector-response measurement"
        elif not matched and expected in {"expected_opening", "expected_no_alert", "expected_no_route_b"}:
            result, reason = "FAIL", "No committed decisions in a scored scenario window"
        elif expected == "expected_opening" and evaluation_policy is not None:
            result = "PASS" if isolated_opened else "FAIL"
            reason = "Independent scenario replay opened an episode" if isolated_opened else "No opening when the scenario was replayed from an empty episode state"
        elif expected == "expected_no_alert" and evaluation_policy is not None:
            result = "PASS" if not isolated_opened and not isolated_updated else "FAIL"
            reason = "Independent scenario replay had no alert" if result == "PASS" else "Independent scenario replay alerted on an expected quiet/unsupported path"
        elif expected == "expected_no_route_b" and evaluation_policy is not None:
            result = "PASS" if isolated_route_b == 0 else "FAIL"
            reason = "No unsupported Route B decision; exceptional Route A may still open" if result == "PASS" else "Route B crossed without price or fresh OI support"
        elif expected == "quality_exclusion_expected":
            if family == "partial_release":
                excluded = bool(feature_rows.oi_token.isna().any())
            elif family == "quote_faults":
                excluded = int(feature_rows.price_eligible.sum()) < len(feature_rows)
            elif family == "coverage_faults":
                excluded = int(feature_rows.flow_eligible.sum()) < len(feature_rows)
            else:
                excluded = int((feature_rows.flow_eligible & feature_rows.price_eligible).sum()) < len(feature_rows)
            result = "PASS" if excluded else "FAIL"
            reason = "Expected input qualification observed" if excluded else "Expected input qualification absent"
        else:
            result, reason = "NOT_APPLICABLE", "Context scenario has no prespecified alert target" if expected == "context_only_not_applicable" else "No operational policy available for isolated replay"
        responses.append({"scenario_id": item["scenario_id"], "scenario_family": family, "instrument_id": item["instrument_id"], "window_start": item["window_start"], "window_end": item["window_end"],
            "expected_outcome": expected, "expected_opening": expected == "expected_opening", "expected_update": False,
            "expected_closure": False, "expected_no_alert": expected == "expected_no_alert", "quality_exclusion_expected": expected == "quality_exclusion_expected",
            "result": result, "reason": reason, "feature_rows": len(feature_rows), "flow_eligible_rows": int(feature_rows.flow_eligible.sum()),
            "price_eligible_rows": int(feature_rows.price_eligible.sum()), "stored_decisions": len(matched),
            "opened_events": sum(e["type"] == "opened" for e in event_rows), "updated_events": sum(e["type"] == "updated" for e in event_rows),
            "closed_events": sum(e["type"] == "closed" for e in event_rows), "isolated_opened_events": isolated_opened,
            "isolated_updated_events": isolated_updated, "isolated_closed_events": isolated_closed,"isolated_route_b_crossings":isolated_route_b,
            "continuity_note": "Original chronological events may share an existing episode; result uses independent reset-state replay and does not credit prior-scenario transitions."})
    response_summary = {"pass": sum(r["result"] == "PASS" for r in responses), "fail": sum(r["result"] == "FAIL" for r in responses),
                        "not_applicable": sum(r["result"] == "NOT_APPLICABLE" for r in responses)}
    scored=[r for r in responses if r["expected_outcome"]!="context_only_not_applicable"]
    positives=[r for r in responses if r["expected_outcome"]=="expected_opening"]
    response_passed=db_path is not None and bool(positives) and all(r["result"]=="PASS" for r in scored)
    return {"dataset_id": manifest["dataset_id"], "mode": "synthetic", "generator_version": GENERATOR_VERSION, "manifest_id": manifest["manifest_id"], "database_supplied": db_path is not None,
            "checks": checks, "all_measured_checks_passed": all(c["passed"] for c in checks), "scenario_response_passed": response_passed,
            "response_summary": response_summary, "responses": responses, "observed_feature_gates": fault_report["observed_feature_gates"],
            "forecast_rows": len(forecasts), "outcome_rows": len(outcomes), "limits": "Synthetic software responses only. Physical process kill, Windows scheduling and actual feeds are separate unexecuted operational checks unless documented elsewhere."}
