"""Explicitly allowlisted, synthetic-only GitHub Pages export and link validation."""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
from datetime import datetime, timezone

from .common import finite
from .theme import CSS
from .web import _csv, page, read_snapshot
from .site_views import build_pages

PUBLIC_SCORE_FIELDS = ("flow_call", "flow_put", "price", "oi_call", "oi_put")
PUBLIC_FILES = {"index.html", "guide.html", "scans.html", "instruments.html", "episodes.html", "forecasts.html",
                "experiments.html", "scenarios.html", "health.html", "methods.html", "style.css", "site.js",
                "publication.json", "publication_manifest.json", ".nojekyll", "exports/summary.json",
                "exports/scans.json", "exports/scans.csv", "exports/episodes.json", "exports/episodes.csv",
                "exports/forecasts.json", "exports/forecasts.csv", "exports/experiments.json",
                "exports/experiments.csv", "exports/scenarios.json", "exports/scenarios.csv"}
PRIVATE_TEXT = re.compile(r"(?i)(?:[A-Z]:[\\/]|/Users/|/home/|127\.0\.0\.1|localhost|\.env|telegram|api[_-]?key|secret|password|token|@[\w.-]+\.[a-z]{2,})")


def _write_utf8(path, value):
    """Keep public bytes identical to manifest hashes and Git blobs on Windows."""
    path.write_bytes(value.encode("utf-8"))


def _identity(value):
    text = str(value or "unavailable")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,150}", text):
        raise ValueError("Public identity contains non-allowlisted characters")
    return text


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.links.append(value)
            if key == "id" and value:
                self.ids.add(value)


def validate_public_site(directory):
    root = Path(directory).resolve(strict=True)
    files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    missing = sorted(PUBLIC_FILES - files)
    unexpected = sorted(files - PUBLIC_FILES)
    broken = []
    external = []
    hash_mismatch = []
    manifest_path=root/"publication_manifest.json"
    if manifest_path.is_file():
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_hashes=manifest.get("file_hashes") or {}
        actual_hashes={item:hashlib.sha256((root/item).read_bytes()).hexdigest() for item in files if item!="publication_manifest.json"}
        hash_mismatch=sorted(item for item in set(actual_hashes)|set(expected_hashes) if actual_hashes.get(item)!=expected_hashes.get(item))
    for item in sorted(files):
        path = root / item
        if path.is_symlink():
            broken.append(f"symlink: {item}")
            continue
        if path.suffix.lower() in {".html", ".json", ".csv", ".css"}:
            content = path.read_text(encoding="utf-8")
            if PRIVATE_TEXT.search(content):
                broken.append(f"private-pattern: {item}")
            if path.suffix.lower() == ".html":
                parser = _Links()
                parser.feed(content)
                for link in parser.links:
                    parsed = urlsplit(link)
                    if parsed.scheme or parsed.netloc or link.startswith("/"):
                        external.append(f"{item}: {link}")
                        continue
                    target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
                    if root not in target.parents and target != root or not target.is_file():
                        broken.append(f"{item}: {link}")
                    elif parsed.fragment and target.suffix.lower() == ".html":
                        target_parser = _Links()
                        target_parser.feed(target.read_text(encoding="utf-8"))
                        if unquote(parsed.fragment) not in target_parser.ids:
                            broken.append(f"{item}: missing fragment {link}")
    return {"passed": not (missing or unexpected or broken or external or hash_mismatch), "files": len(files),
            "missing": missing, "unexpected": unexpected, "broken_or_private": broken, "non_relative_links": external,
            "hash_mismatch":hash_mismatch}


def export_public_site(db_path, directory, scenario_file=None, experiment_file=None):
    snapshot = read_snapshot(db_path)
    ident = snapshot["dataset"]
    if ident.get("mode") != "synthetic":
        raise ValueError("Public export refuses real modes until an approved derived-summary publication policy exists")
    dataset = _identity(ident.get("dataset_id"))
    source_version = _identity(ident.get("source_version") or ident.get("generator_version"))
    models = snapshot["models"]
    model_version = _identity(models[0].get("model_version") or models[0].get("model_id")) if models else "unavailable"
    policies = snapshot["policies"]
    policy = next((p["policy"] for p in policies if p.get("reviewed") and p.get("policy")), None) or {}
    if not policy:
        raise ValueError("No reviewed public detector policy")
    scenarios = json.loads(Path(scenario_file).read_text(encoding="utf-8")) if scenario_file else None
    experiment = json.loads(Path(experiment_file).read_text(encoding="utf-8")) if experiment_file else None
    for item in (scenarios, experiment):
        if item and item.get("dataset_id") != dataset:
            raise ValueError("Public input dataset mismatch")
    generated = datetime.now(timezone.utc).isoformat()
    snapshot["generated_at"] = generated
    snapshot["worker_recorded_at"] = snapshot["last_run"].get("recorded_at")
    snapshot["freshness"] = "Dated synthetic snapshot; inspect data timestamp and worker run separately"
    raw = snapshot["tables"]
    opening_reasons = {e.get("episode_id"): e.get("reasons",[]) for e in raw["events"] if e.get("type")=="opened"}
    transitions = {(e.get("instrument_id"),e.get("decision_at")):e.get("type") for e in raw["events"]}
    scans=[]
    for d in raw["decisions"]:
        scores=d.get("scores") or {}
        current=d.get("current_decision_evidence")
        if current is None:
            flow=max((scores.get(k) for k in ("flow_call","flow_put") if finite(scores.get(k))),default=float("-inf"))
            price=scores.get("price")
            current=(["A_exceptional_flow"] if flow>=policy["extreme_flow"] else [])+(["A_exceptional_repricing"] if finite(price) and price>=policy["extreme_price"] else [])
            current.append("Legacy Route B evidence unavailable")
        active=d.get("active_episode") or {}
        eid=active.get("episode_id")
        scans.append({"dataset_id":dataset,"mode":"synthetic","decision_at":d.get("decision_at"),"instrument_id":d.get("instrument_id"),
            "flow_call_score":scores.get("flow_call"),"flow_put_score":scores.get("flow_put"),"price_score":scores.get("price"),
            "oi_call_score":scores.get("oi_call"),"oi_put_score":scores.get("oi_put"),
            "flow_threshold":policy["extreme_flow"],"price_threshold":policy["extreme_price"],
            "current_decision_evidence":current,"original_episode_opening_reasons":opening_reasons.get(eid,"outside bounded export" if eid else []),
            "current_episode_state":"active" if eid else "none","transition_at_scan":transitions.get((d.get("instrument_id"),d.get("decision_at"))),
            "episode_id":eid,"quality_reasons":d.get("quality_reasons")})
    episodes=[]
    event_by_id={}
    for e in raw["events"]:
        event_by_id.setdefault(e.get("episode_id"),[]).append(e)
    for eid, group in event_by_id.items():
        if not eid:
            continue
        group.sort(key=lambda e:e.get("decision_at",""))
        first=next((e for e in group if e.get("type")=="opened"),group[0])
        episodes.append({"dataset_id":dataset,"mode":"synthetic","episode_id":eid,"instrument_id":first.get("instrument_id"),
            "started_at":first.get("started_at") or first.get("decision_at"),"original_opening_reasons":first.get("reasons",[]),
            "latest_transition":group[-1].get("type"),"latest_transition_at":group[-1].get("decision_at"),
            "state":"closed" if group[-1].get("type")=="closed" else "active",
            "severity":group[-1].get("notified_severity")})
    episodes.sort(key=lambda r:r["started_at"] or "",reverse=True)
    forecasts=[]
    outcomes={o.get("forecast_id"):o for o in snapshot["latest_forecast_outcomes"]}
    for f in raw["forecasts"]:
        o=outcomes.get(f.get("forecast_id"),{})
        forecasts.append({"dataset_id":dataset,"mode":"synthetic","instrument_id":f.get("instrument_id"),"issued_at":f.get("issued_at"),
            "target_start":f.get("target_start"),"target_end":f.get("target_end"),"method":f.get("method"),
            "predicted_variance":f.get("predicted_variance"),"outcome_status":o.get("status","pending"),
            "observed_variance":o.get("observed_variance"),"qlike":o.get("qlike",(o.get("loss") or {}).get("qlike"))})
    comparisons=[]
    forecast_note="No frozen comparison supplied"
    if experiment:
        for fold in experiment.get("folds",[]):
            for candidate in fold.get("candidates",[]):
                metrics=candidate.get("case_metrics") or {}
                burden=candidate.get("test_metrics") or {}
                comparisons.append({"dataset_id":dataset,"mode":"synthetic","fold":fold["fold"]["fold_id"],"method":candidate.get("candidate"),
                    "execution":candidate.get("status"),"performance_status":metrics.get("performance_status"),
                    "expected_positive_families":metrics.get("eligible_families"),"detected_positive_families":metrics.get("strict_detected"),
                    "missed_positive_families":metrics.get("missed_families"),"expected_negative_families":metrics.get("expected_negative_families"),
                    "false_openings":metrics.get("false_openings"),"updates":burden.get("material_updates"),
                    "closures":sum(e.get("type")=="closed" for e in candidate.get("events",[])),
                    "episode_rate_per_portfolio_week":burden.get("episodes_per_portfolio_week"),
                    "coverage_exclusions":metrics.get("coverage_exclusions"),
                    "miss_reasons":[{"family":f.get("event_group"),"reason":f.get("miss_reason")} for f in metrics.get("families",[]) if f.get("miss_reason")],
                    "detection_latency_wall_minutes":metrics.get("detection_latency_wall_minutes")})
            f=fold.get("forecasts") or {}
            interval=f.get("paired_difference_uncertainty") or {}
            a=f.get("mean_baseline_qlike"); b=f.get("mean_challenger_qlike")
            if a is not None and b is not None:
                forecast_note=f"Seasonal EWMA {a:.4f}; HAR {b:.4f}; {f.get('paired_targets')} paired targets. Lower QLIKE is better. " + ("Bootstrap interval crosses zero; no statistically reliable winner." if interval.get("lower") is not None and interval["lower"]<=0<=interval.get("upper",-1) else "Inspect paired interval.")
    scenario_rows=[]
    if scenarios:
        for r in scenarios.get("responses",[]):
            scenario_rows.append({"dataset_id":dataset,"mode":"synthetic","scenario_id":r.get("scenario_id"),"expected_outcome":r.get("expected_outcome"),
                "result":r.get("result"),"reason":r.get("reason"),"isolated_opened_events":r.get("isolated_opened_events"),
                "isolated_updated_events":r.get("isolated_updated_events"),"isolated_closed_events":r.get("isolated_closed_events"),
                "chronological_openings":r.get("opened_events"),"chronological_updates":r.get("updated_events"),
                "quality_exclusion_expected":r.get("quality_exclusion_expected")})
    recent={}
    for d in scans:
        recent.setdefault(d["instrument_id"],d)
    active=sum(r["current_episode_state"]=="active" for r in recent.values())
    best=max(((max((r[k] for k in ("flow_call_score","flow_put_score","price_score") if finite(r[k])),default=float("-inf")),r["instrument_id"]) for r in recent.values()),default=(None,None))
    latest=next((e for e in raw["events"] if e.get("type") in {"opened","updated","closed"}),None)
    outbox=snapshot["outbox_counts"]
    successful_run=next((r.get("recorded_at") for r in raw["runs"] if r.get("status")=="ok" and r.get("recorded_at")),None)
    if successful_run is None and snapshot["last_run"].get("status")=="ok":
        successful_run=snapshot["last_run"].get("recorded_at")
    summary={"dataset_id":dataset,"mode":"synthetic","data_timestamp":snapshot.get("watermark"),"generated_at":generated,
        "last_successful_worker_run":successful_run,
        "data_health":snapshot["health"].get("status"),"active_episodes":active,"highest_current_anomaly":f"{best[1]} · {best[0]:.2f}" if best[0] is not None else None,
        "latest_transition":f"{latest.get('type')} · {latest.get('instrument_id')} · {latest.get('decision_at')}" if latest else None,
        "forecast_model_comparison":forecast_note,"notification_status":outbox or "disabled or no delivery record",
        "deployment_id":_identity(snapshot.get("deployment_id")),"source_version":source_version,"model_version":model_version,
        "synthetic_limit":"Software behavior only; market predictive value unmeasured"}
    root=Path(directory).resolve()
    root.mkdir(parents=True,exist_ok=True)
    present={p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if present-PUBLIC_FILES:
        raise ValueError("Public directory contains non-allowlisted files")
    (root/"exports").mkdir(exist_ok=True)
    pages=build_pages(scans,episodes,forecasts,comparisons,scenario_rows,summary,snapshot)
    for filename,(title,body) in pages.items():
        _write_utf8(root/filename,page(title,body,snapshot))
    for name,values in (("scans",scans),("episodes",episodes),("forecasts",forecasts),("experiments",comparisons),("scenarios",scenario_rows)):
        _write_utf8(root/"exports"/(name+".json"),json.dumps(values,sort_keys=True,indent=2,allow_nan=False)+"\n")
        _write_utf8(root/"exports"/(name+".csv"),_csv(values))
    _write_utf8(root/"exports/summary.json",json.dumps(summary,sort_keys=True,indent=2)+"\n")
    _write_utf8(root/"style.css",CSS)
    _write_utf8(root/"site.js","")
    _write_utf8(root/".nojekyll","")
    _write_utf8(root/"publication.json",json.dumps({"dataset_id":dataset,"mode":"synthetic","generated_at":generated,"data_asof":summary["data_timestamp"]},indent=2)+"\n")
    hashes={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file() and p.name!="publication_manifest.json"}
    manifest={"built_at":generated,"dataset_id":dataset,"mode":"synthetic","source_version":source_version,"model_version":model_version,"file_hashes":hashes}
    _write_utf8(root/"publication_manifest.json",json.dumps(manifest,sort_keys=True,indent=2)+"\n")
    validation=validate_public_site(root)
    if not validation["passed"]:
        raise ValueError("Public site validation failed: "+json.dumps(validation))
    return {"manifest":manifest,"validation":validation}


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="action",required=True)
    export=sub.add_parser("export")
    export.add_argument("--db",required=True)
    export.add_argument("--docs",required=True)
    export.add_argument("--scenarios")
    export.add_argument("--experiment")
    check=sub.add_parser("validate")
    check.add_argument("--docs",required=True)
    args=parser.parse_args()
    result=export_public_site(args.db,args.docs,args.scenarios,args.experiment) if args.action=="export" else validate_public_site(args.docs)
    print(json.dumps(result,indent=2))
    if args.action=="validate" and not result["passed"]:
        raise SystemExit(1)


if __name__=="__main__":
    main()
