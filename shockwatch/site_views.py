"""Present allowlisted, committed records as a compact read-only terminal."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from math import isfinite

from .web import esc


def utc(value):
    if not value:
        return "UNAVAILABLE"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def number(value, places=2):
    return f"{value:.{places}f}" if isinstance(value, (int, float)) and isfinite(value) else "UNAVAILABLE"


def detail(row):
    return "<details class='raw'><summary>VIEW RAW RECORD</summary><pre>"+esc(json.dumps(row, sort_keys=True, indent=2, ensure_ascii=False))+"</pre></details>"


def term(label, value):
    return "<div><dt>"+esc(label)+":</dt><dd>"+esc(value)+"</dd></div>"


def anchor(text, href, css="command-link"):
    return "<a class='"+css+"' href='"+esc(href)+"'>"+esc(text)+"</a>"


def score(value, threshold=None):
    crossed = isinstance(value, (int, float)) and threshold is not None and value >= threshold
    label = number(value)+(f" / {number(threshold)}" if threshold is not None else "")
    return "<span class='"+("crossed" if crossed else "")+"'>"+esc(label)+( "  ** ABOVE **" if crossed else "")+"</span>"


def evidence(value):
    if not value:
        return "NONE"
    if isinstance(value, list):
        return ", ".join(str(x).replace("_", " ") for x in value)
    return str(value)


def table(rows, columns, *, raw=False):
    if not rows:
        return "<p class='dim'>NO STORED RECORDS</p>"
    head = "".join("<th scope='col'>"+esc(label)+"</th>" for label,_ in columns)
    if raw:
        head += "<th scope='col'>RECORD</th>"
    body = []
    for row in rows:
        cells = "".join("<td>"+render(row)+"</td>" for _,render in columns)
        if raw:
            cells += "<td>"+detail(row)+"</td>"
        row_id = " id='"+esc(row["_id"])+"'" if row.get("_id") else ""
        body.append("<tr"+row_id+">"+cells+"</tr>")
    return "<div class='scroll' role='region' aria-label='Scrollable records' tabindex='0'><table><thead><tr>"+head+"</tr></thead><tbody>"+"".join(body)+"</tbody></table></div>"


def latest_rows(scans):
    latest = {}
    for row in scans:
        latest.setdefault(row["instrument_id"], row)
    return latest


def top_score(rows, keys):
    options = [(row[key], row, key) for row in rows for key in keys if isinstance(row.get(key), (int,float)) and isfinite(row[key])]
    return max(options, key=lambda item: (item[0],item[1]["instrument_id"],item[2])) if options else None


def score_name(key):
    return {"flow_call_score":"call flow", "flow_put_score":"put flow", "price_score":"pricing"}.get(key,key)


def notification_label(value):
    if isinstance(value, dict) and "blocked_synthetic" in value:
        return "BLOCKED IN SYNTHETIC MODE ("+str(value["blocked_synthetic"])+" RECORDED)"
    return value or "NO DELIVERY RECORD"


def scan_link(row):
    return "scans.html#scan-"+esc(row["instrument_id"])+"-"+esc(row["decision_at"].replace(":", "-"))


def episode_link(row):
    return row.get("_local_episode_page") or "episodes.html#episode-"+esc(row["episode_id"])


def overview(scans, episodes, forecasts, summary, snapshot):
    recent = latest_rows(scans)
    current = list(recent.values())
    latest_time = max((r["decision_at"] for r in current), default=None)
    current_transitions = [r for r in current if r["decision_at"] == latest_time and r.get("transition_at_scan")]
    active = [r for r in current if r.get("current_episode_state") == "active"]
    top = top_score(current, ("flow_call_score","flow_put_score","price_score"))
    flow = top_score(current, ("flow_call_score","flow_put_score"))
    pricing = top_score(current, ("price_score",))
    source = snapshot.get("source_state") or {}
    feed = "SYNTHETIC / FINITE SOURCE EXHAUSTED" if source.get("exhausted") else "SYNTHETIC / STORED SNAPSHOT"
    health = (summary.get("data_health") or "unknown").upper()
    status = "CRITICAL" if health in {"failed", "critical"} else "ATTENTION" if health not in {"HEALTHY", "OK"} else "NOMINAL"
    if status == "NOMINAL" and source.get("exhausted"):
        status = "SYNTHETIC SNAPSHOT"
    latest_episode = max(episodes,key=lambda r:r.get("latest_transition_at") or "") if episodes else None
    transition_label = (str(latest_episode.get("latest_transition")).upper()+" / "+str(latest_episode.get("instrument_id"))+" / "+utc(latest_episode.get("latest_transition_at"))) if latest_episode else "NONE RECORDED"
    notifications = notification_label(summary.get("notification_status"))
    forecast_status = ("SEASONAL EWMA LOWER MEAN QLIKE / NO RELIABLE WINNER" if "Seasonal EWMA" in str(summary.get("forecast_model_comparison")) and "no statistically reliable winner" in str(summary.get("forecast_model_comparison")) else summary.get("forecast_model_comparison"))
    fields = (("SYSTEM STATUS",status),("MODE",summary["mode"].upper()),
              ("DATA AS OF",utc(summary.get("data_timestamp"))),
              ("LAST SUCCESSFUL SCAN",utc(latest_time)),("DATA HEALTH",health+" // "+feed),
              ("ACTIVE EPISODES",summary["active_episodes"]),
              ("LATEST TRANSITION",transition_label),
              ("TOP CURRENT ANOMALY",f"{top[1]['instrument_id']} {score_name(top[2])} {number(top[0])}" if top else "UNAVAILABLE"),
              ("FORECAST STATUS",forecast_status),
              ("NOTIFICATION STATUS",notifications))
    body = "<section><h3>LATEST FINDINGS</h3><div class='summary'>"+"".join(term(k,v) for k,v in fields)+"</div></section>"
    if not current:
        body += "<section class='section-rule'><h3 class='finding-title'>NO STORED SCANS</h3><p>No current detector decision can be summarized.</p></section>"
    else:
        chosen = next((r for r in current_transitions if r.get("transition_at_scan") == "opened"),None) or (active[0] if active else None)
        if chosen:
            why = evidence(chosen.get("current_decision_evidence"))
            route = "ROUTE B" if "B_" in str(chosen.get("current_decision_evidence")) else "ROUTE A" if "A_" in str(chosen.get("current_decision_evidence")) else "RECORDED"
            headline = ("NEW EPISODE" if chosen.get("transition_at_scan") == "opened" else "ACTIVE EPISODE")+" // "+chosen["instrument_id"]+" // "+route
            body += "<section class='section-rule'><h3 class='finding-title alert'>"+esc(headline)+"</h3>"
            body += "<p class='finding-copy'>"+esc(chosen["instrument_id"])+" has current decision evidence: "+esc(why)+". This indicates unusual options-market activity under the stored rules. It does not identify a trader, establish intent, or prove insider trading.</p>"
            body += "<div class='finding-lines'><p>CALL FLOW: "+score(chosen.get("flow_call_score"),chosen.get("flow_threshold"))+"</p><p>PUT FLOW: "+score(chosen.get("flow_put_score"),chosen.get("flow_threshold"))+"</p><p>PRICING: "+score(chosen.get("price_score"),chosen.get("price_threshold"))+"</p><p>DECISION: "+esc(utc(chosen["decision_at"]))+"</p></div>"
            body += anchor("OPEN EPISODE RECORD",episode_link(chosen)) if chosen.get("episode_id") else anchor("OPEN SUPPORTING SCAN",scan_link(chosen))
            body += "</section>"
        else:
            below = all(r.get(k) is None or (r.get("flow_threshold" if k.startswith("flow") else "price_threshold") is not None and
                        r[k] < r["flow_threshold" if k.startswith("flow") else "price_threshold"])
                        for r in current for k in ("flow_call_score","flow_put_score","price_score"))
            heading = "NO ACTIONABLE ANOMALY DETECTED" if below else "NO ACTIVE EPISODE AT LATEST SCAN"
            body += "<section class='section-rule'><h3 class='finding-title'>"+heading+"</h3>"
            body += "<p class='finding-copy'>"+("All "+str(len(current))+" monitored instruments are below their exceptional flow and pricing thresholds. " if below else "A current score crossed an exceptional threshold without an active episode; inspect eligibility and evidence. ")
            if top:
                threshold = top[1]["flow_threshold" if top[2].startswith("flow") else "price_threshold"]
                body += "The highest current anomaly is "+esc(top[1]["instrument_id"])+" "+esc(score_name(top[2]))+" at "+number(top[0])+" robust anomaly units. "
                body += (("This is "+("below" if top[0]<threshold else "at or above")+" the "+number(threshold)+" exceptional threshold.") if threshold is not None else "The exceptional threshold is unavailable in this snapshot.")
            episode_target = episode_link(latest_episode) if latest_episode and latest_episode.get("episode_id") else "episodes.html"
            episode_label = "LATEST EPISODE RECORD" if latest_episode and latest_episode.get("episode_id") else "EPISODE RECORDS"
            body += "</p>"+anchor("INSPECT SUPPORTING SCANS","scans.html")+anchor(episode_label,episode_target)+"</section>"
    previous = {}
    for row in scans:
        if row["instrument_id"] in recent and row is not recent[row["instrument_id"]]:
            previous.setdefault(row["instrument_id"],row)
    body += "<section class='section-rule'><h3>WHAT CHANGED SINCE THE PREVIOUS SCAN</h3>"
    if current_transitions:
        body += "<p>"+esc(", ".join(r["instrument_id"]+" "+str(r["transition_at_scan"]) for r in current_transitions))+" at the latest decision time.</p>"
    else:
        body += "<p>No opening, update, escalation, or closure was recorded at the latest decision time.</p>"
    if top and top[1]["instrument_id"] in previous:
        old=previous[top[1]["instrument_id"]].get(top[2])
        body += "<p>Top score, "+esc(top[1]["instrument_id"])+" "+esc(score_name(top[2]))+": "+number(old)+" previously; "+number(top[0])+" now.</p>"
    body += "</section><section class='section-rule'><h3>EVIDENCE / FORECASTS</h3><div class='summary'>"
    body += term("HIGHEST CURRENT FLOW",f"{flow[1]['instrument_id']} {score_name(flow[2])} {number(flow[0])} / {number(flow[1].get('flow_threshold'))}" if flow else "UNAVAILABLE")
    body += term("HIGHEST CURRENT PRICING",f"{pricing[1]['instrument_id']} {number(pricing[0])} / {number(pricing[1].get('price_threshold'))}" if pricing else "UNAVAILABLE")
    oi = next((r for r in scans if any("oi" in str(x).lower() for x in (r.get("current_decision_evidence") or []))),None)
    body += term("LATEST OI CONFIRMATION",f"{oi['instrument_id']} / {utc(oi['decision_at'])}" if oi else "NONE IN RETAINED DECISION EVIDENCE")
    latest_forecast = max(forecasts,key=lambda r:(r.get("issued_at") or "",r.get("instrument_id") or "")) if forecasts else None
    body += term("LATEST STORED FORECAST",f"{latest_forecast['instrument_id']} / {latest_forecast['method']} / {number(latest_forecast.get('predicted_variance'),8)} variance / target {utc(latest_forecast.get('target_start'))} to {utc(latest_forecast.get('target_end'))} / {latest_forecast.get('outcome_status')}" if latest_forecast else "NONE")
    body += term("WORKER LAST SUCCEEDED",utc(summary.get("last_successful_worker_run")))
    deployment=str(summary.get("deployment_id") or "UNAVAILABLE")
    body += term("DEPLOYMENT / MODEL",(deployment[:12]+"…" if len(deployment)>12 else deployment)+" / "+str(summary.get("model_version")))
    body += "</div>"+anchor("WATCHLIST","instruments.html")+anchor("FORECAST RECORDS","forecasts.html")+"</section>"
    body += "<section class='section-rule'><h3>WHAT AM I LOOKING AT?</h3><p>A scan is one decision for one instrument. An episode groups related suspicious scans. Severity is an anomaly score, not an insider-trading probability. Forecasts are separate from anomaly alerts. Synthetic results test software behavior, not market predictive value.</p>"+anchor("READ THE GUIDE","guide.html")+"</section>"
    return body


def guide():
    return ("<section><h3>READING ORDER</h3><ol><li>Check system status and data timestamp.</li>"
            "<li>Read latest findings.</li><li>Look for new episode transitions.</li>"
            "<li>Inspect evidence beside each threshold.</li><li>Check data quality and coverage.</li>"
            "<li>Review forecasts separately from anomaly alerts.</li></ol></section>"
            "<section class='section-rule'><h3>CORE TERMS</h3><dl class='summary'>"+
            "".join(term(k,v) for k,v in (
                ("Scan","One stored decision for one instrument at one time."),
                ("Episode","Related suspicious scans grouped into one alert record."),
                ("Opening","The first scan that starts an episode."),
                ("Update","New material evidence added to an open episode."),
                ("Escalation","An update that raises recorded severity."),
                ("Closure","An episode ends after enough valid, quiet trading minutes."),
                ("Route A","Eligible exceptional flow or repricing evidence at the displayed threshold."),
                ("Route B","Moderate same-side flow plus CUSUM persistence and pricing or a fresh OI release."),
                ("Flow score","How unusual current call or put flow is versus its reference behavior."),
                ("Pricing score","How unusual the current options repricing is."),
                ("OI confirmation","Fresh daily open-interest evidence that can support Route B; an OI score alone is not confirmation."),
                ("CUSUM persistence","Accumulated same-side evidence across valid scans."),
                ("Severity","Alert anomaly magnitude, not a probability."),
                ("Forecast variance","Predicted amount of future price variation for the stated horizon."),
                ("QLIKE","A forecast loss measure; lower is better."),
                ("Coverage","Whether needed observations were complete and eligible."),
                ("Synthetic mode","Artificial input used to test software behavior, not market predictive value.")))+
            "</dl></section><section class='section-rule'><h3>INTERPRET SCORES</h3>"
            "<p>Robust anomaly units measure distance from historical behavior. They are not probabilities or automatically Gaussian standard deviations. No score proves insider trading.</p></section>"
            "<section class='section-rule'><h3>WHERE TO LOOK</h3><p>Overview shows the latest finding and changes. Alerts groups episodes; Scans shows current route evidence beside original opening reasons. Watchlist shows each instrument now. Forecasts shows separate future-variance estimates and later outcomes. Experiments and Scenarios test synthetic behavior. System shows worker, source, data and delivery state. Methods records rule semantics.</p></section>"
            "<section class='section-rule'><h3>DISPLAY KEY</h3><p>Green = normal stored information or PASS. Amber = threshold crossing, stale data or a warning. Red = actual FAIL or critical state. NONE means no matching record; UNAVAILABLE means the value was not stored here. Raw records and full identifiers are in expandable details and exports.</p></section>")


def build_pages(scans, episodes, forecasts, comparisons, scenarios, summary, snapshot):
    recent=latest_rows(scans)
    pages={"index.html":("Overview",overview(scans,episodes,forecasts,summary,snapshot)),
           "guide.html":("How to read SHOCKWATCH",guide())}
    scan_cols=[("UTC",lambda r:esc(utc(r.get("decision_at")))),("TICKER",lambda r:esc(r.get("instrument_id"))),
               ("CALL / LIMIT",lambda r:score(r.get("flow_call_score"),r.get("flow_threshold"))),
               ("PUT / LIMIT",lambda r:score(r.get("flow_put_score"),r.get("flow_threshold"))),
               ("PRICE / LIMIT",lambda r:score(r.get("price_score"),r.get("price_threshold"))),
               ("CURRENT DECISION EVIDENCE",lambda r:esc(evidence(r.get("current_decision_evidence")))),
               ("EPISODE STATE",lambda r:esc(r.get("current_episode_state"))),
               ("TRANSITION",lambda r:esc(r.get("transition_at_scan") or "NONE")),
               ("OPENING REASONS (HISTORICAL)",lambda r:esc(evidence(r.get("original_episode_opening_reasons")))),
               ("QUALITY",lambda r:esc(evidence(r.get("quality_reasons"))))]
    scan_rows=[]
    for row in scans[:300]:
        copy=dict(row)
        copy["_id"]="scan-"+row["instrument_id"]+"-"+row["decision_at"].replace(":","-")
        scan_rows.append(copy)
    scan_table=table(scan_rows,scan_cols,raw=True)
    # Latest per-instrument records are in the watchlist; all rows remain downloadable.
    scan_lead="<section><h3>LATEST SCAN EVIDENCE</h3>"
    for row in sorted(recent.values(),key=lambda r:r["instrument_id"]):
        scan_lead += ("<article class='record'><div class='record-head'><strong>"+esc(row["instrument_id"])+"</strong><span>"+esc(utc(row["decision_at"]))+"</span><span class='secondary'>"+esc(row.get("current_episode_state"))+"</span></div><dl>"+
                      term("CURRENT DECISION EVIDENCE",evidence(row.get("current_decision_evidence")))+
                      term("ORIGINAL OPENING REASONS",evidence(row.get("original_episode_opening_reasons")))+
                      term("TRANSITION AT THIS SCAN",row.get("transition_at_scan") or "NONE")+
                      "<div><dt>CALL FLOW / THRESHOLD:</dt><dd>"+score(row.get("flow_call_score"),row.get("flow_threshold"))+"</dd></div>"+
                      "<div><dt>PUT FLOW / THRESHOLD:</dt><dd>"+score(row.get("flow_put_score"),row.get("flow_threshold"))+"</dd></div>"+
                      "<div><dt>PRICING / THRESHOLD:</dt><dd>"+score(row.get("price_score"),row.get("price_threshold"))+"</dd></div></dl>"+detail(row)+"</article>")
    scan_lead+="</section>"
    pages["scans.html"]=("Scans",scan_lead+"<section class='section-rule'><h3>RECENT SCAN LEDGER</h3><p>Latest decisions first. Scores show reviewed exceptional thresholds. Opening reasons are historical; only CURRENT DECISION EVIDENCE describes this scan.</p>"+
                         scan_table+"<p>300 latest rows shown. "+anchor("SCANS JSON","exports/scans.json")+anchor("SCANS CSV","exports/scans.csv")+"</p></section>")
    watch=[]
    for row in sorted(recent.values(),key=lambda r:r["instrument_id"]):
        watch.append("<article class='record'><div class='record-head'><strong>"+esc(row["instrument_id"])+"</strong><span>"+esc(row.get("current_episode_state"))+"</span><span class='secondary'>"+esc(utc(row["decision_at"]))+"</span></div><dl>"+
                     term("CALL FLOW / THRESHOLD",number(row.get("flow_call_score"))+" / "+number(row.get("flow_threshold")))+
                     term("PUT FLOW / THRESHOLD",number(row.get("flow_put_score"))+" / "+number(row.get("flow_threshold")))+
                     term("PRICING / THRESHOLD",number(row.get("price_score"))+" / "+number(row.get("price_threshold")))+
                     term("CURRENT EVIDENCE",evidence(row.get("current_decision_evidence")))+
                     term("LATEST TRANSITION",row.get("transition_at_scan") or "NONE")+"</dl>"+
                     anchor("OPEN SCANS","scans.html")+detail(row)+"</article>")
    pages["instruments.html"]=("Watchlist","<p>Latest committed decision for each monitored instrument. The source is synthetic.</p>"+"".join(watch))
    episode_records=[]
    for row in episodes:
        eid=row["episode_id"]
        episode_records.append("<article class='record' id='episode-"+esc(eid)+"'><div class='record-head'><strong>"+esc(row["instrument_id"])+"</strong><span class='"+("amber" if row.get("state")=="active" else "")+"'>"+esc(row.get("state","unknown").upper())+"</span><span>SEVERITY "+esc(number(row.get("severity")))+"</span><span class='secondary'>"+esc(utc(row.get("started_at")))+"</span></div><dl>"+
                               term("ORIGINAL OPENING REASON",evidence(row.get("original_opening_reasons")))+
                               term("LATEST TRANSITION",str(row.get("latest_transition"))+" / "+utc(row.get("latest_transition_at")))+
                               "</dl><p class='secondary'>EPISODE <a class='secondary' href='#episode-"+esc(eid)+"'>"+esc(eid)+"</a></p>"+detail(row)+"</article>")
    pages["episodes.html"]=("Alerts","<p>Episodes group suspicious scans. Severity is an anomaly score, never a probability or finding of misconduct. Latest episodes first.</p>"+
                            ("".join(episode_records) if episode_records else "<p>NO STORED EPISODES</p>")+
                            anchor("EPISODES JSON","exports/episodes.json")+anchor("EPISODES CSV","exports/episodes.csv"))
    latest_forecasts=forecasts[:300]
    conclusion=summary.get("forecast_model_comparison") or "No frozen forecast comparison supplied."
    pages["forecasts.html"]=("Forecasts","<section><h3>FORECAST CONCLUSION</h3><p>"+esc(conclusion)+"</p><p>Forecast variance concerns a stated future horizon. Forecasts are separate from anomaly alerts; outcomes attach only after their target period closes.</p></section>"+
                             table(latest_forecasts,[("ISSUED UTC",lambda r:esc(utc(r.get("issued_at")))),("TICKER",lambda r:esc(r.get("instrument_id"))),
                             ("METHOD",lambda r:esc(r.get("method"))),("TARGET START",lambda r:esc(utc(r.get("target_start")))),("TARGET END",lambda r:esc(utc(r.get("target_end")))),
                             ("FORECAST VARIANCE",lambda r:esc(number(r.get("predicted_variance"),8))),("OUTCOME",lambda r:esc(r.get("outcome_status"))),("QLIKE",lambda r:esc(number(r.get("qlike"),4)))],raw=True)+
                             anchor("FORECASTS JSON","exports/forecasts.json")+anchor("FORECASTS CSV","exports/forecasts.csv"))
    pages["experiments.html"]=("Experiments","<p>Execution, invariants, scenario response, and detector performance are separate results. "+esc(conclusion)+" Synthetic comparisons do not establish market predictive value.</p>"+
                               table(comparisons,[("DETECTOR",lambda r:esc(r.get("method"))),("EXECUTION",lambda r:esc(r.get("execution"))),
                               ("PERFORMANCE",lambda r:"<strong class='badge "+("fail" if r.get("performance_status")=="FAIL" else "pass")+"'>"+esc(r.get("performance_status"))+"</strong>"),
                               ("DETECTED / EXPECTED",lambda r:esc(str(r.get("detected_positive_families"))+" / "+str(r.get("expected_positive_families")))),
                               ("MISSED",lambda r:esc(r.get("missed_positive_families"))),("FALSE OPENINGS",lambda r:esc(r.get("false_openings"))),
                               ("UPDATES",lambda r:esc(r.get("updates"))),("CLOSURES",lambda r:esc(r.get("closures")))],raw=True)+
                               anchor("EXPERIMENTS JSON","exports/experiments.json")+anchor("EXPERIMENTS CSV","exports/experiments.csv"))
    counts={k:sum(r.get("result")==k for r in scenarios) for k in ("PASS","FAIL","NOT_APPLICABLE")}
    failed=[r for r in scenarios if r.get("result")=="FAIL"]
    scenario_intro="<p class='statusline'><strong>PASS "+str(counts["PASS"])+"</strong><strong class='"+("failure" if failed else "dim")+"'>FAIL "+str(counts["FAIL"])+"</strong><strong>NOT APPLICABLE "+str(counts["NOT_APPLICABLE"])+"</strong></p><p>Independent reset-state scenarios test software behavior, not real-market predictive value. Chronological episodes may span adjacent fixtures.</p>"
    if failed:
        scenario_intro += "<h3 class='failure'>FAILED SCENARIOS</h3>"+"".join("<p class='failure'>"+esc(r.get("scenario_id"))+": "+esc(r.get("reason"))+"</p>" for r in failed)
    pages["scenarios.html"]=("Synthetic scenarios",scenario_intro+table(scenarios,[("RESULT",lambda r:"<strong class='badge "+("fail" if r.get("result")=="FAIL" else "pass" if r.get("result")=="PASS" else "muted")+"'>"+esc(r.get("result"))+"</strong>"),
                                ("SCENARIO",lambda r:esc(r.get("scenario_id"))),("EXPECTED",lambda r:esc(r.get("expected_outcome"))),("REASON",lambda r:esc(r.get("reason"))),
                                ("OPEN / UPDATE / CLOSE",lambda r:esc(str(r.get("isolated_opened_events"))+" / "+str(r.get("isolated_updated_events"))+" / "+str(r.get("isolated_closed_events"))))],raw=True)+
                                anchor("SCENARIOS JSON","exports/scenarios.json")+anchor("SCENARIOS CSV","exports/scenarios.csv"))
    source=snapshot.get("source_state") or {}
    worker="LAST RECORDED RUN SUCCEEDED" if summary.get("last_successful_worker_run") else "NO CONFIRMED SUCCESS"
    source_status="FINITE SYNTHETIC SOURCE EXHAUSTED" if source.get("exhausted") else "SYNTHETIC SOURCE"
    health="<section><h3>OPERATING STATE</h3><div class='summary'>"+"".join(term(k,v) for k,v in (("WORKER",worker+" / "+utc(summary.get("last_successful_worker_run"))),("SOURCE",source_status),("DATA",summary.get("data_health") or "UNKNOWN"),("NOTIFICATIONS",notification_label(summary.get("notification_status"))),("DATA AS OF",utc(summary.get("data_timestamp")))))+"</div></section>"
    health+="<section class='section-rule'><h3>DETAILS</h3><p>Worker success is a stored run result. A dated synthetic feed is not a live market feed. Publication file hashes are available in the build manifest.</p>"+detail({"data_health":snapshot.get("health"),"source_state":{k:source.get(k) for k in ("finite","exhausted","pending_cohorts","virtual_asof")},"notification_counts":snapshot.get("outbox_counts"),"deployment_id":summary.get("deployment_id"),"dataset_id":summary.get("dataset_id")})+"<details class='raw'><summary>BUILD MANIFEST / FILE HASHES</summary><p>"+anchor("OPEN MANIFEST","publication_manifest.json")+"</p></details></section>"
    pages["health.html"]=("System",health)
    pages["methods.html"]=("Methods","<p>Route A evaluates eligible exceptional flow or repricing against the reviewed threshold. Route B requires current moderate same-side flow, CUSUM persistence and pricing or fresh daily OI confirmation. Quiet closure counts fully observed low-score trading minutes; missing intervals freeze the timer.</p><p>Robust-scale scores are anomalies, not probabilities or proof of misconduct. Forecasts and their later outcomes are separate from alert decisions.</p>"+anchor("READ GUIDE","guide.html"))
    return pages
