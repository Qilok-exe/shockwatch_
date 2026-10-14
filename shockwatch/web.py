"""Bounded, read-only HTML/JSON/CSV snapshots of committed SQLite records."""
from __future__ import annotations
import csv
import html
import io
import json
from pathlib import Path
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote
from .common import dumps, save_json

LIMIT = 10000
PAGE_SIZE = 100
TABLES = ("decisions", "events", "forecasts", "forecast_outcomes", "episode_outcomes", "runs", "experiments", "experiment_metrics", "data_quality_incidents", "contexts")
NAV = (("index.html", "OVERVIEW"), ("episodes.html", "ALERTS"),
       ("instruments.html", "WATCHLIST"), ("forecasts.html", "FORECASTS"),
       ("experiments.html", "EXPERIMENTS"), ("health.html", "SYSTEM"),
       ("guide.html", "GUIDE"))


def read_snapshot(db_path, limit=LIMIT):
    """Never instantiate Store: opening a report must not migrate or create a DB."""
    if not 1 <= limit <= LIMIT:
        raise ValueError("Snapshot limit must be between 1 and 10000")
    source = Path(db_path).resolve(strict=True)
    con = sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        present = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta = {x[0]: json.loads(x[1]) for x in con.execute("SELECT key,value FROM meta")}
        identity = meta.get("dataset", meta.get("dataset_manifest", meta.get("dataset_identity", {})))
        if "datasets" in present and identity.get("dataset_id"):
            registered = con.execute("SELECT payload FROM datasets WHERE dataset_id=?", (identity["dataset_id"],)).fetchone()
            if registered:
                identity = json.loads(registered[0])
        if not identity:
            identity = {"dataset_id": meta.get("dataset_id", "unbound-legacy"), "mode": meta.get("mode", "unknown"), "source": "legacy inputs"}
        data = {"dataset": {k: identity.get(k) for k in ("dataset_id", "mode", "source", "adapter_version", "generator_version", "source_version", "manifest_hash")},
                "tables": {}, "counts": {}, "limit": limit, "last_run": meta.get("last_run", {}),
                "source_state":meta.get("source_state",{}), "watermark": meta.get("watermark"), "backup_day": meta.get("backup_day"),
                "backup_history": meta.get("backup_history", []), "deployment_id": meta.get("deployment_id"),
                "outbox_counts": {}, "models":[], "policies":[], "schema_version": con.execute("PRAGMA user_version").fetchone()[0]}
        for name in TABLES:
            if name not in present:
                data["counts"][name] = 0
                data["tables"][name] = []
                continue
            data["counts"][name] = con.execute("SELECT count(*) FROM "+name).fetchone()[0]
            rows = con.execute("SELECT * FROM "+name+" ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                if "payload" in value:
                    value = json.loads(value["payload"])
                # Jobs intentionally expose a small safe allowlist, never arbitrary errors/config.
                if name == "runs":
                    value = {k: value.get(k) for k in ("started_at", "recorded_at", "status", "decisions", "events", "imported_rows", "outcomes", "runtime_seconds")}
                for key,default in (("dataset_id","unbound-legacy"),("mode","unknown")):
                    expected=identity.get(key,default)
                    if value.get(key) is not None and value[key] != expected:
                        raise ValueError("Stored record dataset identity disagrees with database")
                    value.setdefault(key,expected)
                result.append(value)
            data["tables"][name] = result
        data["latest_forecast_outcomes"]=[]
        forecast_ids=[x.get("forecast_id") for x in data["tables"]["forecasts"]]
        if "forecast_outcomes" in present:
            for offset in range(0,len(forecast_ids),500):
                chunk=forecast_ids[offset:offset+500]
                placeholders=",".join("?" for _ in chunk)
                sql="SELECT o.payload FROM forecast_outcomes o WHERE o.forecast_id IN ("+placeholders+") AND o.revision=(SELECT max(x.revision) FROM forecast_outcomes x WHERE x.forecast_id=o.forecast_id)"
                data["latest_forecast_outcomes"].extend(json.loads(r[0]) for r in con.execute(sql,chunk))
        data["latest_episode_outcomes"]=[]
        if "episode_outcomes" in present:
            sql="SELECT o.payload FROM episode_outcomes o WHERE o.revision=(SELECT max(x.revision) FROM episode_outcomes x WHERE x.window_id=o.window_id) ORDER BY o.rowid DESC LIMIT ?"
            data["latest_episode_outcomes"]=[json.loads(r[0]) for r in con.execute(sql,(limit,))]
        for key in ("latest_forecast_outcomes","latest_episode_outcomes"):
            for value in data[key]:
                for name,default in (("dataset_id","unbound-legacy"),("mode","unknown")):
                    expected=identity.get(name,default)
                    if value.get(name) is not None and value[name] != expected:
                        raise ValueError("Latest outcome dataset identity disagrees with database")
                    value.setdefault(name,expected)
        if "outbox" in present:
            data["outbox_counts"] = dict(con.execute("SELECT status,count(*) FROM outbox GROUP BY status"))
        if "model_registry" in present:
            for row in con.execute("SELECT payload FROM model_registry ORDER BY rowid DESC LIMIT 5"):
                model=json.loads(row[0])
                data["models"].append({k:model.get(k) for k in ("model_id","model_version","dataset_id","mode","training_cutoff","feature_version","source_version","intraday_target_definition_version","universe")})
        if "policy_registry" in present:
            for row in con.execute("SELECT payload FROM policy_registry ORDER BY rowid DESC LIMIT 5"):
                policy=json.loads(row[0])
                data["policies"].append({k:policy.get(k) for k in ("model_id","dataset_id","mode","validation_start","validation_end","policy","reviewed","reviewed_at")})
        # Strip job report to status/timing fields; endpoint and error text never enter site assets.
        report = data["last_run"]
        data["last_run"] = {k: report.get(k) for k in ("started_at", "recorded_at", "status", "decisions", "events", "imported_rows", "runtime_seconds")}
        health = report.get("data_health", {})
        data["health"] = {k: health.get(k) for k in ("status", "expected_interval_end", "expected", "healthy", "missing", "degraded", "failed_notifications")}
        con.execute("COMMIT")
        return data
    finally:
        con.close()


def esc(value):
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = dumps(value)
    return html.escape(str(value), quote=True)


def table(rows, columns, links=None):
    if not rows:
        return "<p>No stored records for this view.</p>"
    columns=list(columns)
    for key,label in (("dataset_id","Dataset"),("mode","Mode")):
        if all(key in row for row in rows) and key not in {name for name,_ in columns}:
            columns.append((key,label))
    header = "".join("<th scope='col'>"+esc(label)+"</th>" for key, label in columns)
    lines = []
    for row in rows:
        cells = []
        for key, label in columns:
            value = row.get(key)
            if links and key in links and value is not None:
                cells.append("<td><a href='"+esc(links[key](value))+"'>"+esc(value)+"</a></td>")
            elif isinstance(value, (dict, list)):
                cells.append("<td><details><summary>"+esc(len(value))+" fields</summary><pre>"+esc(value)+"</pre></details></td>")
            elif key in {"result", "performance_status"} and value in {"PASS", "FAIL", "NOT_APPLICABLE"}:
                kind = "pass" if value == "PASS" else "fail" if value == "FAIL" else "muted"
                cells.append("<td><strong class='badge "+kind+"'>"+esc(value)+"</strong></td>")
            elif key in {"flow_call_score", "flow_put_score", "price_score"} and isinstance(value, (int, float)):
                threshold = row.get("flow_threshold") if key.startswith("flow_") else row.get("price_threshold")
                crossed = threshold is not None and value >= threshold
                cells.append("<td class='"+("crossed" if crossed else "")+"'>"+esc(round(value, 2))+" / "+esc(threshold)+(" ▲" if crossed else "")+"</td>")
            else:
                cells.append("<td>"+esc(value)+"</td>")
        lines.append("<tr>"+"".join(cells)+"</tr>")
    return "<div class='scroll'><table><thead><tr>"+header+"</tr></thead><tbody>"+"".join(lines)+"</tbody></table></div>"


def page(title, body, snapshot):
    """Shared command shell for local and public committed-record pages."""
    ident = snapshot["dataset"]
    current = {"Overview":"index.html", "Alerts":"episodes.html",
               "Watchlist":"instruments.html", "Forecasts":"forecasts.html",
               "Experiments":"experiments.html", "System":"health.html",
               "How to read SHOCKWATCH":"guide.html"}.get(title)
    nav = "".join("<a"+(" class='current' aria-current='page'" if url == current else "")+
                  " href='"+url+"'>["+str(n)+"] "+label+"</a>" for n,(url,label) in enumerate(NAV,1))
    mode = ident.get("mode", "unknown")
    source_version = ident.get("source_version") or ident.get("generator_version") or "unavailable"
    model_version = (snapshot.get("models") or [{}])[0].get("model_version") or "0.2.0"
    metadata = (("Dataset ID",ident.get("dataset_id")),("Mode",mode),("Source version",source_version),
                ("Model version",model_version),("Build time",snapshot.get("generated_at")),
                ("Data timestamp",snapshot.get("watermark")),("Deployment ID",snapshot.get("deployment_id")))
    detail = "".join("<div><dt>"+esc(k)+"</dt><dd>"+esc(v)+"</dd></div>" for k,v in metadata)
    notice = "SYNTHETIC DATA // SOFTWARE BEHAVIOR ONLY" if mode == "synthetic" else "DATASET EVIDENCE // CHECK COVERAGE"
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>SHOCKWATCH // "+esc(title)+"</title><link rel='stylesheet' href='style.css'></head>"
            "<body><input class='effects-check' id='effects-off' type='checkbox' aria-label='Disable scanlines and glow'>"
            "<div class='terminal-screen'><a class='skip' href='#content'>Skip to content</a><header>"
            "<div class='terminal-head'><h1 class='glow'>SHOCKWATCH TERMINAL // BUILD "+esc(model_version)+"</h1>"
            "<label class='effects-label' for='effects-off'><span class='effects-mark' aria-hidden='true'></span> DISABLE CRT EFFECTS</label></div>"
            "<hr class='terminal-rule'><nav class='command-nav' aria-label='Main commands'>"+nav+"</nav>"
            "<div class='sub-nav'><a href='scans.html'>SCANS</a><a href='scenarios.html'>SCENARIOS</a>"
            "<a href='methods.html'>METHODS</a></div><hr class='terminal-rule'>"
            "<p class='prompt'>COMMAND &gt; <span class='cursor' aria-hidden='true'></span></p></header>"
            "<main id='content'><h2>"+esc(title.upper())+"</h2><div class='screen-meta'>"+esc(notice)+
            " <details><summary>BUILD / DATA DETAILS</summary><dl class='summary'>"+detail+"</dl></details>"
            "<p id='freshness' data-recorded='"+esc(snapshot.get("worker_recorded_at") or "")+"'>"+
            esc(snapshot.get("freshness"))+"</p></div>"+body+"</main><footer>"
            "<p>READ-ONLY RECORD // PAGE REQUESTS NEVER RUN THE WORKER // "
            "<a href='publication.json'>PUBLICATION</a> // <a href='exports/summary.json'>SUMMARY JSON</a></p>"
            "</footer></div><script src='site.js'></script></body></html>")


def _csv(rows):
    keys = sorted({k for r in rows for k in r})
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, keys, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    return buf.getvalue()


def render_site(db_path, directory, scenario_file=None, experiment_file=None, limit=LIMIT):
    snapshot = read_snapshot(db_path, limit)
    snapshot["generated_at"] = datetime.now(timezone.utc).isoformat()
    recorded = snapshot["last_run"].get("recorded_at")
    # Legacy started_at may be virtual; never mistake it for wall-clock receipt.
    if not recorded and snapshot["dataset"].get("mode") != "synthetic":
        recorded = snapshot["last_run"].get("started_at")
    snapshot["worker_recorded_at"] = recorded
    age = None
    if recorded:
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(recorded.replace("Z", "+00:00"))).total_seconds()
    snapshot["freshness"] = "Worker timestamp unavailable; freshness unverified" if age is None else "STALE: worker last ran more than 15 minutes ago" if age > 900 else "Worker ran within 15 minutes; inspect data health separately"
    target = Path(directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    unrelated=[p.name for p in target.iterdir() if p.name not in ("index.html","current.json") and not (p.is_dir() and p.name.startswith("snapshot-"))]
    if unrelated:
        raise ValueError("Website output requires a dedicated directory, not a runtime/source folder")
    snapshot_id = "snapshot-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:8]
    staging = target/(snapshot_id+".partial")
    final = target/snapshot_id
    staging.mkdir()
    (staging/"exports").mkdir()
    rows = snapshot["tables"]
    ident = snapshot["dataset"]
    experiment = json.loads(Path(experiment_file).read_text(encoding="utf-8")) if experiment_file else None
    if experiment and (experiment.get("dataset_id",experiment.get("manifest",{}).get("dataset_id")) != ident.get("dataset_id") or experiment.get("mode",experiment.get("manifest",{}).get("mode")) != ident.get("mode")):
        raise ValueError("Experiment/site identity mismatch")
    current_policy = next((p.get("policy") for p in snapshot["policies"] if p.get("reviewed") and p.get("policy")), None) or next((p.get("policy") for p in snapshot["policies"] if p.get("policy")), {})
    latest_by_instrument = {}
    for decision in rows["decisions"]:
        latest_by_instrument.setdefault(decision["instrument_id"],decision)
    active_now = [d for d in latest_by_instrument.values() if d.get("active_episode")]
    from .common import finite
    def anomaly(decision):
        scores=decision.get("scores") or {}
        return max((scores.get(k) for k in ("flow_call","flow_put","price") if finite(scores.get(k))),default=None)
    highest=max(((anomaly(d),d["instrument_id"]) for d in latest_by_instrument.values() if anomaly(d) is not None),default=(None,None))
    forecast_note="No frozen comparison supplied"
    if experiment and experiment.get("folds"):
        summary=experiment["folds"][0].get("forecasts") or {}
        baseline=summary.get("mean_baseline_qlike"); challenger=summary.get("mean_challenger_qlike")
        interval=summary.get("paired_difference_uncertainty") or {}
        if baseline is not None and challenger is not None:
            forecast_note=f"Seasonal EWMA {baseline:.4f}; HAR {challenger:.4f}; {summary.get('paired_targets')} paired targets. Lower QLIKE is better. " + ("Bootstrap interval crosses zero; no reliable winner." if interval.get("lower") is not None and interval["lower"]<=0<=interval.get("upper",-1) else "Inspect the paired uncertainty interval.")
    latest_transition=next((e for e in rows["events"] if e.get("type") in ("opened","updated","closed")),None)
    successful_run=next((r.get("recorded_at") for r in rows["runs"] if r.get("status")=="ok" and r.get("recorded_at")),None)
    if successful_run is None and snapshot["last_run"].get("status")=="ok":
        successful_run=snapshot["last_run"].get("recorded_at")
    notification_status="Disabled or no delivery record" if not snapshot["outbox_counts"] else ", ".join(f"{k}: {v}" for k,v in snapshot["outbox_counts"].items())
    summary_items={"Mode":ident.get("mode"),"Data timestamp":snapshot.get("watermark"),"Last successful worker run":successful_run or "No confirmed successful run in bounded snapshot", "Data health":snapshot["health"].get("status"),"Active episodes":len(active_now),"Highest current anomaly":f"{highest[1]} {highest[0]:.2f}" if highest[0] is not None else None,"Latest episode transition":f"{latest_transition.get('type')} / {latest_transition.get('instrument_id')} / {latest_transition.get('decision_at')}" if latest_transition else None,"Forecast-model comparison":forecast_note,"Notification status":notification_status,"Deployment/version":snapshot.get("deployment_id") or (snapshot["models"][0].get("model_version") if snapshot["models"] else None)}
    body = "<h3>Current state</h3><dl class='summary'>"+"".join("<div><dt>"+esc(k)+"</dt><dd>"+esc(v)+"</dd></div>" for k,v in summary_items.items())+"</dl>"
    body += "<section><h3>What am I looking at?</h3><p>A scan is one decision for one instrument. An episode groups related suspicious scans. Severity is an anomaly score, not an insider-trading probability. Forecasts are separate from anomaly alerts. Synthetic results establish software behavior only, not market predictive value.</p></section>"
    body += "<details><summary>Dataset, model and source details</summary><dl>"+"".join("<dt>"+esc(k)+"</dt><dd>"+esc(v)+"</dd>" for k,v in {"Dataset":ident.get("dataset_id"),"Source version":ident.get("source_version") or ident.get("generator_version"),"Model":snapshot["models"],"Reviewed policies":snapshot["policies"],"Source state":snapshot.get("source_state"),"Schema":snapshot["schema_version"]}.items())+"</dl></details>"
    body += table([{"table":n,"stored":snapshot["counts"][n],"exported":len(rows[n])} for n in TABLES], [("table","Records"),("stored","Stored count"),("exported","Exported count")])
    pages = {"index.html": ("Status", body)}
    events_by_scan={}
    openings={e.get("episode_id"):e for e in rows["events"] if e.get("type")=="opened"}
    for event in rows["events"]:
        events_by_scan.setdefault((event.get("instrument_id"),event.get("decision_at")),[]).append(event.get("type"))
    scans=[]
    for original in rows["decisions"]:
        active=original.get("active_episode") or {}
        transition=events_by_scan.get((original.get("instrument_id"),original.get("decision_at")),[])
        scores=original.get("scores") or {}
        flow=max((scores.get(k) for k in ("flow_call","flow_put") if finite(scores.get(k))),default=float("-inf"))
        price=scores.get("price")
        legacy_evidence=(["A_exceptional_flow"] if flow>=current_policy.get("extreme_flow",float("inf")) else [])+(["A_exceptional_repricing"] if finite(price) and price>=current_policy.get("extreme_price",float("inf")) else [])
        current=original.get("current_decision_evidence")
        if current is None:
            current=legacy_evidence+["Legacy scan: Route B current evidence unavailable"]
        opening=openings.get(active.get("episode_id")) or {}
        scans.append({**original,"episode_id":active.get("episode_id"),"flow_call_score":scores.get("flow_call"),"flow_put_score":scores.get("flow_put"),"price_score":price,"flow_threshold":current_policy.get("extreme_flow"),"price_threshold":current_policy.get("extreme_price"),"current_decision_evidence":current,"original_episode_opening_reasons":opening.get("reasons") if opening else ("Opening outside bounded export" if active else None),"current_episode_state":"active" if active else "none","transition_at_scan":transition})
    scan_cols = [("decision_at","Decision time"),("instrument_id","Instrument"),("flow_call_score","Call flow / threshold"),("flow_put_score","Put flow / threshold"),("price_score","Price / threshold"),("current_decision_evidence","Current decision evidence"),("original_episode_opening_reasons","Original episode-opening reasons"),("current_episode_state","Current episode state"),("transition_at_scan","Transition at this scan"),("episode_id","Episode"),("quality_reasons","Quality"),("scores","All score details")]
    def slug(value):
        return uuid.uuid5(uuid.NAMESPACE_URL, str(value)).hex
    def paginated(name, title, values, columns, links=None):
        total = len(values)
        pages_count = max(1, (total+PAGE_SIZE-1)//PAGE_SIZE)
        for p in range(pages_count):
            filename = name+".html" if p == 0 else name+"-"+str(p+1)+".html"
            navigation = " · ".join("<a href='"+(name+".html" if n == 0 else name+"-"+str(n+1)+".html")+"'>"+str(n+1)+"</a>" for n in range(pages_count))
            chunk = values[p*PAGE_SIZE:(p+1)*PAGE_SIZE]
            body = "<p>"+str(total)+" records in this bounded snapshot; database total "+str(snapshot["counts"].get({"scans":"decisions"}.get(name,name), total))+". Maximum export "+str(limit)+".</p><p><label>Filter this page <input id='filter' type='search'></label></p><p>Pages: "+navigation+"</p>"+table(chunk, columns, links)
            body += "<p>Every exported row retains dataset and mode. <a href='exports/"+name+".json'>JSON</a> · <a href='exports/"+name+".csv'>CSV</a></p>"
            pages[filename] = (title, body)
        (staging/"exports"/(name+".json")).write_text(dumps(values)+"\n", encoding="utf-8")
        (staging/"exports"/(name+".csv")).write_text(_csv(values), encoding="utf-8")
    paginated("scans", "All scans, including ordinary periods", scans, scan_cols, {"episode_id":lambda x:"episode-"+slug(x)+".html"})
    instruments = sorted({r["instrument_id"] for r in rows["decisions"]})
    pages["instruments.html"] = ("Instruments", "<ul>"+"".join("<li><a href='instrument-"+slug(i)+".html'>"+esc(i)+"</a></li>" for i in instruments)+"</ul>")
    for symbol in instruments:
        chosen = [r for r in scans if r["instrument_id"] == symbol]
        pages["instrument-"+slug(symbol)+".html"] = ("Instrument "+symbol, "<p>Source: "+esc(ident.get("source"))+". Stable source identifier: "+esc(symbol)+". "+str(len(chosen))+" exported intervals.</p>"+table(chosen[:PAGE_SIZE], scan_cols)+"<p>Full bounded history is available in the scans export. Nonempty quality reasons above remain unresolved input qualifications.</p>")
    grouped = {}
    for event in reversed(rows["events"]):
        grouped.setdefault(event["episode_id"], []).append(event)
    for scan in scans:
        active=scan.get("active_episode") or {}
        episode_id=active.get("episode_id")
        if episode_id and episode_id not in grouped:
            grouped[episode_id]=[{**active,"instrument_id":scan["instrument_id"],"decision_at":max(r["decision_at"] for r in scans if r.get("episode_id") == episode_id),"type":"opening_outside_export_window","dataset_id":ident.get("dataset_id"),"mode":ident.get("mode"),"quality_reasons":["Original event outside bounded export; retained active state shown"]}]
    episodes = []
    for eid, timeline in grouped.items():
        first, last = timeline[0], timeline[-1]
        summary = {"dataset_id":ident.get("dataset_id"),"mode":ident.get("mode"),"episode_id":eid,"instrument_id":first.get("instrument_id"),"original_start":first.get("started_at",first.get("decision_at")),"state":"closed" if last.get("type") == "closed" else "open", "latest_update":last.get("decision_at"), "age_in_dataset_clock_hours":max(0.,(datetime.fromisoformat((last["decision_at"] if last.get("type") == "closed" else snapshot.get("watermark") or last["decision_at"]).replace("Z","+00:00"))-datetime.fromisoformat(first.get("started_at",first["decision_at"]).replace("Z","+00:00"))).total_seconds()/3600), "reasons":last.get("reasons"), "severity":last.get("notified_severity",last.get("severity"))}
        episodes.append(summary)
        outcomes = [o for o in snapshot["latest_episode_outcomes"] if o.get("episode_id") == eid]
        context=[]
        for outcome in outcomes:
            for item in outcome.get("public_events",[]):
                if item not in context:
                    context.append(item)
        evidence_heading="<h3>Retained active state</h3><p>Original transition is outside this bounded export; increase the limit to inspect it.</p>" if first.get("type") == "opening_outside_export_window" else "<h3>Original recorded evidence</h3>"
        pages["episode-"+slug(eid)+".html"] = ("Episode "+eid, "<p>Original evidence is immutable. Contract-side activity does not identify trader intent. Forward returns have no implicit trade direction.</p>"+evidence_heading+"<pre>"+esc(first)+"</pre><h3>Transitions</h3>"+table(timeline, [("decision_at","Time"),("type","State change"),("reasons","Routes"),("severity","Severity"),("scores","Evidence")])+"<h3>Predefined later outcomes</h3>"+table(outcomes, [("horizon_id","Window"),("revision","Latest revision"),("evaluated_at","Evaluated"),("status","Status"),("signed_underlying_return","Signed return"),("absolute_underlying_return","Absolute return"),("realized_variance","Realized variance"),("target_coverage","Coverage")])+"<h3>Timestamped public context</h3>"+table(context,[("available_at","Available to worker"),("public_at","Public event time"),("occurrence_status","Observed / scheduled"),("kind","Context"),("source","Source")])+"<p>Contract references and public explanations are available only when supplied and retained by the source. Synthetic scheduled event context is shown when available. No real news or verified case registry is connected.</p>")
    paginated("episodes", "Episodes", episodes, [("episode_id","Episode"),("instrument_id","Instrument"),("original_start","Original start"),("state","State"),("age_in_dataset_clock_hours","Age in dataset clock hours"),("latest_update","Latest transition"),("reasons","Accumulated episode reasons"),("severity","Opening / notified severity")], {"episode_id":lambda x:"episode-"+slug(x)+".html"})
    latest = {}
    for o in snapshot["latest_forecast_outcomes"]:
        latest[o.get("forecast_id")] = o
    forecasts = []
    for f in rows["forecasts"]:
        outcome = latest.get(f.get("forecast_id"), {})
        forecasts.append({**f,"outcome_status":outcome.get("status", "pending"),"observed_variance":outcome.get("observed_variance"),"qlike":outcome.get("qlike",(outcome.get("loss") or {}).get("qlike")),"target_coverage":outcome.get("target_coverage"),"outcome_available_at":outcome.get("outcome_available_at")})
    paginated("forecasts", "Immutable forecasts and later outcomes", forecasts, [("instrument_id","Instrument"),("issued_at","Issued in dataset clock"),("recorded_at","Recorded wall time"),("target_start","Future target start"),("target_end","Target end"),("horizon_id","Horizon"),("method","Method"),("forecast_status","Forecast state"),("predicted_variance","Forecast variance"),("observed_variance","Observed variance"),("outcome_status","Outcome state"),("target_coverage","Coverage"),("qlike","QLIKE")])
    # Keep the scheduled local renderer on the same read-only command shell.
    from .site_views import guide, overview
    local_scans=[]
    for row in scans:
        copy=dict(row)
        transition=copy.get("transition_at_scan") or []
        copy["transition_at_scan"]=transition[0] if isinstance(transition,list) and transition else transition or None
        if copy.get("episode_id"):
            copy["_local_episode_page"]="episode-"+slug(copy["episode_id"])+".html"
        local_scans.append(copy)
    local_episodes=[{"instrument_id":e.get("instrument_id"),"latest_transition":"closed" if e.get("state")=="closed" else "updated",
                     "latest_transition_at":e.get("latest_update")} for e in episodes]
    local_summary={"mode":ident.get("mode"),"data_timestamp":snapshot.get("watermark"),
                   "data_health":snapshot["health"].get("status"),"active_episodes":len(active_now),
                   "forecast_model_comparison":forecast_note,"notification_status":notification_status,
                   "last_successful_worker_run":successful_run,"deployment_id":snapshot.get("deployment_id"),
                   "model_version":snapshot["models"][0].get("model_version") if snapshot["models"] else "0.2.0"}
    pages["index.html"]=("Overview",overview(local_scans,local_episodes,forecasts,local_summary,snapshot))
    pages["guide.html"]=("How to read SHOCKWATCH",guide())
    if experiment:
        save_json(staging/"exports/experiment.json",experiment)
        save_json(staging/"exports/experiment_manifest.json",experiment["manifest"])
        evidence_note="These artificial results do not establish market predictive value." if experiment.get("mode") == "synthetic" else "Interpret these results under the recorded source availability and coverage assumptions."
        body="<p>All prespecified candidates, failures, neutral results and coverage exclusions are retained. "+esc(evidence_note)+"</p><p><a href='exports/experiment_manifest.json'>Frozen manifest</a> · <a href='exports/experiment.json'>Complete result packet</a></p>"
        for fold in experiment.get("folds",[]):
            body+="<h3>"+esc(fold.get("fold",{}).get("fold_id"))+"</h3><p>"+esc(fold.get("fold"))+"</p>"
            comparisons=[]
            for candidate in fold.get("candidates",[]):
                metrics=candidate.get("test_metrics") or {}
                cases=candidate.get("case_metrics") or {}
                comparisons.append({"method":candidate.get("candidate"),"status":candidate.get("status"),
                    "new_episodes":metrics.get("new_episodes"),"updates":metrics.get("material_updates"),
                    "weekly_rate":metrics.get("episodes_per_portfolio_week"),
                    "eligible_days":metrics.get("eligible_instrument_days"),
                    "case_families":cases.get("eligible_families"),"detected":cases.get("strict_detected"),
                    "new_or_updated":cases.get("new_or_updated_detected"),"performance_status":cases.get("performance_status"),
                    "closures":sum(e.get("type")=="closed" for e in candidate.get("events",[])),
                    "miss_reasons":[{"family":f["event_group"],"reason":f["miss_reason"]} for f in cases.get("families",[]) if f.get("miss_reason")],
                    "reason":candidate.get("reason")})
            body+=table(comparisons,[("method","Comparator / ablation"),("status","Execution"),
                ("new_episodes","New episodes"),("updates","Updates"),("weekly_rate","Episodes / portfolio week"),
                ("eligible_days","Common instrument-days"),("case_families","Eligible labelled families"),
                ("detected","New-episode detections"),("new_or_updated","New or updated"),("closures","Closures"),("performance_status","Performance"),("miss_reasons","Miss reasons"),("reason","Failure qualification")])
            forecasts=fold.get("forecasts") or {}
            forecast_summary={key:forecasts.get(key) for key in ("status","baseline","challenger","paired_targets",
                "incomplete_targets","mean_baseline_qlike","mean_challenger_qlike","paired_difference_uncertainty","variance_floor")}
            interval=forecasts.get("paired_difference_uncertainty") or {}
            baseline_loss=forecasts.get("mean_baseline_qlike"); challenger_loss=forecasts.get("mean_challenger_qlike")
            winner="Seasonal EWMA has lower mean QLIKE" if baseline_loss is not None and challenger_loss is not None and baseline_loss<challenger_loss else "HAR has lower mean QLIKE" if baseline_loss is not None and challenger_loss is not None and challenger_loss<baseline_loss else "No mean difference available"
            significance="; bootstrap interval crosses zero, so no statistically reliable winner is established" if interval.get("lower") is not None and interval["lower"]<=0<=interval.get("upper",-1) else "; inspect the paired uncertainty interval"
            body+="<h3>Paired next-session intraday variance forecasts</h3><p>Lower QLIKE is better. "+esc(winner)+" across "+esc(forecasts.get("paired_targets"))+" synthetic paired targets"+esc(significance)+".</p>"+table(
                [{"metric":key,"value":value} for key,value in forecast_summary.items()],[("metric","Metric"),("value","Stored result")])
            measurement=fold.get("measurement_comparison") or {}
            body+="<p>Actual fine/coarse price-path comparison: "+esc(measurement.get("status"))+"; paired instrument-sessions: "+esc(measurement.get("paired_instrument_sessions"))+". Detailed losses and exclusions remain in the packet.</p>"
        coverage=experiment.get("coverage") or {}
        body+="<h3>Coverage</h3>"+table([{ "metric":key,"value":value} for key,value in coverage.items() if key != "exclusions"],[("metric","Coverage metric"),("value","Stored result")])
        pages["experiments.html"] = ("Frozen historical / synthetic experiments",body)
    else:
        pages["experiments.html"] = ("Experiments", "<p>No comparison packet has been supplied for this snapshot. Proposed research is not a measured result.</p>"+table(rows["experiments"],[("experiment_id","ID"),("status","State"),("manifest","Frozen specification")]))
    truth = None
    if scenario_file:
        truth = json.loads(Path(scenario_file).read_text(encoding="utf-8"))
        if isinstance(truth,dict) and truth.get("dataset_id") not in (None,ident.get("dataset_id")):
            raise ValueError("Scenario/site dataset mismatch")
    scenario_body="<p>Scenario truth is evaluation-only and never a detector input. A planned scenario is not a passed test. Alert counts are artificial software responses, not market precision or recall.</p>"
    if isinstance(truth,dict) and "checks" in truth:
        scenario_body+="<p>Generator "+esc(truth.get("generator_version"))+" · Data invariants passed: "+esc(truth.get("all_measured_checks_passed"))+" · Expected scenario responses passed: "+esc(truth.get("scenario_response_passed"))+". Independent reset-state responses are scored separately from chronological shared episodes.</p>"
        scenario_body+=table(truth["checks"],[("check","Invariant"),("passed","Passed"),("denominator","Checked count"),("evidence","Evidence")])
        scenario_body+="<h3>Observed scenario responses</h3>"+table(truth.get("responses",[]),[("scenario_id","Scenario"),("expected_outcome","Expected outcome"),("result","Result"),("reason","Reason"),("instrument_id","Instrument"),("stored_decisions","Scans"),("isolated_opened_events","Isolated openings"),("isolated_updated_events","Isolated updates"),("isolated_closed_events","Isolated closures"),("opened_events","Chronological openings"),("updated_events","Chronological updates"),("closed_events","Chronological closures"),("flow_eligible_rows","Flow coverage"),("price_eligible_rows","Pricing coverage")])
        scenario_body+="<p><a href='exports/scenarios.json'>Complete scenario evidence</a></p>"
    else:
        scenario_body+="<p>No measured scenario results supplied. Any catalogue remains proposed test coverage.</p>"
    pages["scenarios.html"] = ("Artificial scenarios",scenario_body)
    if truth:
        save_json(staging/"exports/scenarios.json",{"dataset_id":ident.get("dataset_id"),"mode":ident.get("mode"),"evaluation":truth})
    pages["health.html"] = ("Data quality, jobs and recovery", "<dl>"+"".join("<dt>"+esc(k)+"</dt><dd>"+esc(v)+"</dd>" for k,v in {"Data health":snapshot["health"],"Outbox states":snapshot["outbox_counts"],"Last local backup":snapshot["backup_day"],"Retained local backup dates":snapshot["backup_history"]}.items())+"</dl><p>Local backups do not establish off-device recovery. Independent watchdog and physical lock/sleep/reboot checks require actual execution evidence.</p>"+table(rows["runs"][:PAGE_SIZE],[("recorded_at","Recorded wall time"),("started_at","Dataset clock"),("status","Job state"),("decisions","Scans"),("events","Transitions"),("runtime_seconds","Runtime seconds")])+table(rows["data_quality_incidents"][:PAGE_SIZE],[("instrument_id","Instrument"),("interval_end","Interval"),("severity","Severity"),("quality_reasons","Input qualifications"),("reason","Ingest reason")]))
    pages["methods.html"] = ("Methods and version log", "<p>Route A opens on eligible exceptional flow or repricing. Route B requires current moderate same-side flow, accumulated CUSUM and pricing or one fresh daily OI token. There is no weekly cap. Scores are robust scale units, never insider-trading probabilities.</p><p>Fully future forecasts select the first configured interval beginning at or after issuance. Session forecasts use refitted open-to-close five-minute intraday variance targets. Legacy reconstructed interval and close-to-close quantities remain diagnostic proxies and are excluded from fully prospective accuracy.</p><p>Outcomes remain separate from immutable forecasts. Pending and incomplete targets are retained; corrections append versioned evaluations. Synthetic software response does not establish empirical market value.</p><p>Current package 0.2.0; feature schema 0.1.0 / raw schema 1; future target definitions are versioned in each record. The local website reads committed bounded records. No feed or notification service is implied by a page being reachable.</p>")
    for filename,(title,body) in pages.items():
        (staging/filename).write_text(page(title,body,snapshot),encoding="utf-8")
    from .theme import CSS
    (staging/"style.css").write_text(CSS,encoding="utf-8")
    (staging/"site.js").write_text("const f=document.getElementById('filter');if(f)f.addEventListener('input',()=>{for(const r of document.querySelectorAll('tbody tr'))r.hidden=!r.textContent.toLowerCase().includes(f.value.toLowerCase());});const s=document.getElementById('freshness');if(s&&s.dataset.recorded){const age=Date.now()-Date.parse(s.dataset.recorded);if(age>900000)s.textContent='STALE: worker last ran more than 15 minutes ago. This page is a dated snapshot.';}",encoding="utf-8")
    summary = {k:v for k,v in snapshot.items() if k != "tables"}
    save_json(staging/"exports/summary.json",summary)
    for name in TABLES:
        export_name = "issued_forecasts" if name == "forecasts" else name
        save_json(staging/"exports"/(export_name+".json"),rows[name])
        (staging/"exports"/(export_name+".csv")).write_text(_csv(rows[name]),encoding="utf-8")
    import hashlib
    hashes = {str(p.relative_to(staging)).replace("\\","/"):hashlib.sha256(p.read_bytes()).hexdigest() for p in staging.rglob("*") if p.is_file()}
    publication = {"snapshot_id":snapshot_id,"generated_at":snapshot["generated_at"],"data_asof":snapshot.get("watermark"),"dataset_id":ident.get("dataset_id"),"mode":ident.get("mode"),"counts":snapshot["counts"],"export_limit":limit,"visibility":"local-only","content_hashes":hashes}
    save_json(staging/"publication.json",publication)
    staging.rename(final)
    # Atomic publication pointer: readers see an entirely completed immutable directory.
    pointer = target/"index.html.partial"
    pointer.write_text("<!doctype html><html lang='en'><meta charset='utf-8'><meta http-equiv='refresh' content='0; url="+snapshot_id+"/index.html'><title>SHOCKWATCH local results</title><a href='"+snapshot_id+"/index.html'>Open current stored results</a></html>",encoding="utf-8")
    pointer.replace(target/"index.html")
    save_json(target/"current.json",publication)
    return publication


class ReadOnlyHandler(SimpleHTTPRequestHandler):
    def do_POST(self):
        self.send_error(405,"Read-only preview")
    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    def list_directory(self,path):
        self.send_error(403,"Directory listing disabled")
        return None
    def send_head(self):
        requested = Path(self.translate_path(self.path))
        base = Path(self.directory).resolve()
        try:
            resolved = requested.resolve()
            relative=resolved.relative_to(base)
            allowed_root={"index.html","current.json"}
            if relative.parts and relative.parts[0] not in allowed_root and not relative.parts[0].startswith("snapshot-"):
                raise ValueError("Unpublished file")
            if resolved.is_file() and resolved.suffix.lower() not in {".html",".json",".csv",".css",".js"}:
                raise ValueError("Non-public artifact")
            if requested.is_symlink() or any(p.is_symlink() for p in requested.parents if p != base):
                raise ValueError("Symlink")
            if ".partial" in str(resolved.relative_to(base)):
                raise ValueError("Unpublished snapshot")
        except (ValueError,OSError):
            self.send_error(403,"Outside published snapshot")
            return None
        return super().send_head()
    def log_message(self,fmt,*args):
        # Requests may contain arbitrary source text. No private query strings in console.
        pass


def serve_site(directory,port=8765):
    import functools
    source = Path(directory).resolve(strict=True)
    if not (source/"current.json").is_file():
        raise ValueError("Serve only a rendered SHOCKWATCH site directory")
    handler = functools.partial(ReadOnlyHandler,directory=str(source))
    with ThreadingHTTPServer(("127.0.0.1",port),handler) as server:
        print(f"Local read-only preview: http://127.0.0.1:{server.server_port}/ ; Ctrl+C stops it",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
