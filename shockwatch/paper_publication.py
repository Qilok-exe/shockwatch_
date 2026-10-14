"""Explicit derived-summary allowlist for paper-Yahoo GitHub Pages export."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from .paper_yahoo import UNIVERSE, open_paper_db, PAPER_MODEL_VERSION
from .yahoo_source import market_schedule
from .publication import PUBLIC_FILES as SYNTHETIC_PUBLIC_FILES, PRIVATE_TEXT, validate_public_site
from .theme import CSS

ARCHIVE = "archive/synthetic_20261014"
ROOT_FILES = {"index.html", "first-real-run.html", "guide.html", "style.css", ".nojekyll",
              "publication.json", "publication_manifest.json", "exports/paper-summary.json"}
PUBLIC_FILES = ROOT_FILES | {f"{ARCHIVE}/{name}" for name in SYNTHETIC_PUBLIC_FILES}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _t(value):
    return escape(str(value if value is not None else "UNAVAILABLE"), quote=True)


def _line(label, value):
    return f"<div><dt>{_t(label)}</dt><dd>{_t(value)}</dd></div>"


def _when(value):
    if not value: return "UNAVAILABLE"
    return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _forecast_text(forecast):
    if forecast["status"]!="OPERATIONAL": return forecast["status"]
    variance=forecast["predicted_variance"]
    volatility=forecast["predicted_volatility"]*100
    return f"OPERATIONAL // {variance:.3g} variance; {volatility:.3g}% five-minute volatility; {forecast['horizon']}"


def _page(title, body, active):
    links = [("index.html", "[1] OVERVIEW"), ("first-real-run.html", "[2] FIRST REAL RUN"),
             (f"{ARCHIVE}/index.html", "[3] SYNTHETIC ARCHIVE"), ("guide.html", "[4] GUIDE")]
    nav_parts=[]
    for dest,label in links:
        attr=' class="current" aria-current="page"' if dest==active else ''
        nav_parts.append(f'<a{attr} href="{dest}">{label}</a> ')
    nav="".join(nav_parts)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>SHOCKWATCH // {_t(title)}</title><link rel=\"stylesheet\" href=\"style.css\"></head><body>"
            "<input class=\"effects-check\" id=\"effects-off\" type=\"checkbox\" aria-label=\"Disable scanlines and glow\">"
            "<div class=\"terminal-screen\"><a class=\"skip\" href=\"#content\">Skip to content</a><header>"
            "<div class=\"terminal-head\"><h1 class=\"glow\">SHOCKWATCH TERMINAL // PAPER-YAHOO</h1>"
            "<label class=\"effects-label\" for=\"effects-off\"><span class=\"effects-mark\" aria-hidden=\"true\"></span> DISABLE CRT EFFECTS</label></div>"
            f"<hr class=\"terminal-rule\"><nav class=\"command-nav\" aria-label=\"Main commands\">{nav}</nav>"
            "<hr class=\"terminal-rule\"><p class=\"prompt\">COMMAND &gt; <span class=\"cursor\" aria-hidden=\"true\"></span></p></header>"
            f"<main id=\"content\"><h2>{_t(title)}</h2>{body}</main><footer>OBSERVATION ONLY // NO ORDERS // NO MARKET PREDICTIVE VALIDITY ESTABLISHED</footer></div></body></html>\n")


def _market_state(now):
    from pandas import Timestamp
    local=Timestamp(now).tz_convert("America/New_York")
    session=market_schedule("SPY",local.date().isoformat())
    return "CLOSED" if session is None or not(session[0]<=Timestamp(now)<session[1]) else "OPEN"


def _safe_summary(report):
    """Only named, bounded derived fields can cross the public boundary."""
    if report.get("status")!="ok" or report.get("mode")!="paper" or report.get("source")!="yahoo":
        raise ValueError("Paper exporter requires a successful paper-Yahoo run")
    if not re.fullmatch(r"paper_yahoo_\d{8}T\d{6}Z",report["dataset_id"]):
        raise ValueError("Unexpected paper dataset ID")
    ticker = {}
    for symbol in UNIVERSE:
        source = report["audit"][symbol]
        decision = next(d for d in report["decisions"] if d["symbol"]==symbol)
        forecast = next(f for f in report["forecasts"] if f.get("symbol")==symbol)
        ticker[symbol] = {key:source.get(key) for key in (
            "latest_stock_bar_utc","latest_stock_close","exchange_timezone","stock_bars_accepted",
            "stock_first_bar_utc","latest_option_fetch_utc","option_contract_counts","option_expiry_count",
            "stale_last_trades","missing_option_volume","missing_open_interest","rejections",
            "response_hashes","oi_snapshot_days","oi_status","matched_oi_contracts","oi_publication_time","source_version",
            "option_source_versions")}
        ticker[symbol]["decision"] = {key:decision.get(key) for key in (
            "interval_end","status","stock_coverage_bars","required_bars","flow_score","pricing_score",
            "oi_confirmation","routes_evaluated","episode_transition","unavailable_reasons","quality_reasons")}
        ticker[symbol]["forecast"] = {key:forecast.get(key) for key in (
            "status","model_version","issued_at","asof_interval_end","horizon","predicted_variance",
            "predicted_volatility","eligible_returns","required_returns","reason")}
    requests=[]
    for entry in report["requests"][:300]:
        requests.append({key:entry.get(key) for key in ("kind","symbol","received_at","response_hash",
                                                           "rows_received","rows_accepted","rows_rejected","issues_by_reason")})
        requests[-1]["method"] = entry.get("request",{}).get("method")
        requests[-1]["expiry"] = entry.get("request",{}).get("expiry")
        requests[-1]["requested_at"] = entry.get("request",{}).get("requested_at")
        requests[-1]["parameters"] = {key:entry.get("request",{}).get(key) for key in
                                      ("period","interval","auto_adjust","actions","repair","keepna","prepost","timeout")
                                      if key in entry.get("request",{})}
    if len(report["requests"])>300:
        raise ValueError("Public request ledger exceeds bound")
    return {"dataset_id":report["dataset_id"],"mode":"paper","source":"yahoo",
            "source_version":report["source_version"],"library_version":report["library_version"],
            "model_version":PAPER_MODEL_VERSION,"worker_started_at":report["started_at"],
            "worker_completed_at":report["completed_at"],
            "latest_completed_decision_interval":report.get("latest_completed_decision_interval"),
            "market_state":_market_state(report["completed_at"]),"ticker":ticker,"requests":requests,
            "routes_evaluated":report["routes_evaluated"],"episodes_opened":report["episodes_opened"],
            "warnings":["Yahoo option history and dependable vendor delta are unavailable; no anomaly route was evaluated.",
                        "One option-chain snapshot cannot establish open-interest growth; publication time is uncertain.",
                        "First-run option-count and closing-interval aggregates were audited and corrected from stored records; raw observations were unchanged.",
                        "First-run decision and forecast issue times were conservatively set to run completion, after the last source response; forecast values were unchanged.",
                        "Synthetic software evidence is archived separately; no market predictive validity has been established."],
            "aggregation_revision":report.get("aggregation_revision"),
            "timing_revision":report.get("timing_revision"),
            "post_ingestion_calendar_audit":{key:report.get("post_ingestion_calendar_audit",{}).get(key) for key in
                ("calendar_version","invalid_count","missing_scheduled_bars","interval_coverage_reconciled")},
            "notification_status":"DISABLED // paper research"}


def _overview(data):
    ticker=data["ticker"]
    timestamps=[ticker[s]["latest_stock_bar_utc"] for s in UNIVERSE if ticker[s]["latest_stock_bar_utc"]]
    asof=max(timestamps) if timestamps else None
    total_stale=sum(ticker[s]["stale_last_trades"] for s in UNIVERSE)
    total_missing=sum(ticker[s]["missing_option_volume"]+ticker[s]["missing_open_interest"] for s in UNIVERSE)
    total_excluded=sum(sum(ticker[s]["rejections"].values()) for s in UNIVERSE)
    operational=[s for s in UNIVERSE if ticker[s]["forecast"]["status"]=="OPERATIONAL"]
    summary="".join(_line(a,b) for a,b in (
        ("SYSTEM STATUS","PAPER-YAHOO / EXPERIMENTAL"),("MODE","PAPER // NO ORDERS"),
        ("DATA AS OF",_when(asof)),("WORKER EXECUTION",_when(data["worker_completed_at"])),
        ("MARKET AT WORKER RUN",data["market_state"]),("TICKER COVERAGE",f"{len(timestamps)}/3: SPY, UAL, LMT"),
        ("DATA-SOURCE HEALTH","AVAILABLE WITH QUALITY WARNINGS" if total_stale or total_missing or total_excluded else "AVAILABLE"),
        ("STALE / MISSING / EXCLUDED",f"{total_stale} stale last trades / {total_missing} missing volume or OI fields / {total_excluded} excluded rows"),
        ("OPERATIONAL MODEL FAMILIES", "Stock adjusted-return EWMA: "+(", ".join(operational) if operational else "none")),
        ("WARMING-UP MODEL FAMILIES","Options flow, pricing anomaly, OI confirmation, Route A, Route B"),
        ("LATEST COMPLETED DECISION",_when(data["latest_completed_decision_interval"])),
        ("ACTIVE EPISODES","0"),("NOTIFICATIONS",data["notification_status"]),
        ("OBSERVATION SOURCE VERSION", ", ".join(sorted({ticker[s]["source_version"] or "unavailable" for s in UNIVERSE}))),
        ("WORKER ADAPTER VERSION",data["source_version"]),("DATASET",data["dataset_id"])))
    rows=[]
    for symbol in UNIVERSE:
        x=ticker[symbol]; f=x["forecast"]; d=x["decision"]
        count=sum(x["option_contract_counts"].values())
        close_text=f"${x['latest_stock_close']:,.2f}" if x["latest_stock_close"] is not None else "UNAVAILABLE"
        rows.append(f"<div class=\"record\"><div class=\"record-head\"><strong>{symbol}</strong> <span class=\"secondary\">{_t(_when(x['latest_stock_bar_utc']))}</span></div>"
                    f"<dl>{_line('Latest stock close',close_text)}{_line('Stock history',str(x['stock_bars_accepted'])+' accepted five-minute bars since '+_when(x['stock_first_bar_utc']))}"
                    f"{_line('Options',str(x['option_expiry_count'])+' expiries / '+str(count)+' contracts')}{_line('Latest chain fetch',_when(x['latest_option_fetch_utc']))}"
                    f"{_line('Decision',d['status']+' // '+_when(d['interval_end']))}{_line('Flow / pricing / OI','UNAVAILABLE // no real-data calibration or daily OI comparison')}"
                    f"{_line('Forecast',_forecast_text(f))}</dl></div>")
    return ("<p class=\"finding-title amber\">PAPER-YAHOO / EXPERIMENTAL</p>"
            "<p>Actual Yahoo observations are stored in a separate paper database. No alert route was evaluated and no episode opened."
            " No market predictive validity has been established.</p>"
            "<section class=\"section-rule\"><h3>LATEST FINDINGS</h3><dl class=\"summary\">"+summary+"</dl>"
            "<p class=\"finding-title\">NO CALIBRATED ANOMALY SCORE AVAILABLE</p>"
            "<p>Five-minute stock history supports a separate variance forecast where coverage is sufficient. Yahoo provides current option-chain snapshots,"
            " but this first run cannot establish historical options-flow baselines or open-interest growth. Missing evidence does not become a zero score or an alert.</p>"
            "<a class=\"command-link\" href=\"first-real-run.html\">VIEW FIRST REAL RUN</a></section>"
            "<section class=\"section-rule\"><h3>INSTRUMENT COVERAGE</h3>"+"".join(rows)+"</section>"
            "<section class=\"section-rule\"><h3>SYNTHETIC EVIDENCE</h3><p>Previous synthetic software verification is retained as a separate dated archive.</p>"
            f"<a class=\"command-link\" href=\"{ARCHIVE}/index.html\">OPEN SYNTHETIC ARCHIVE</a></section>")


def _first_run(data):
    totals=Counter()
    kind_totals=Counter()
    rows=[]
    for req in data["requests"]:
        for key in ("rows_received","rows_accepted","rows_rejected"):
            totals[key]+=req[key] or 0
        kind_totals[req["kind"]]+=req["rows_accepted"] or 0
        label=f"{req['symbol']} // {req['method']}"+(f" // {req['expiry']}" if req.get("expiry") else "")
        if req.get("parameters"):
            label+=" // "+json.dumps(req["parameters"],sort_keys=True,separators=(",",":"))
        rows.append("<tr>"+"".join(f"<td>{_t(v)}</td>" for v in (label,req["requested_at"],req["received_at"],req["rows_received"],req["rows_accepted"],req["rows_rejected"],req["response_hash"]))+"</tr>")
    models=[]
    for symbol in UNIVERSE:
        x=data["ticker"][symbol];d=x["decision"];f=x["forecast"]
        models.append(f"<div class=\"record\"><h3>{symbol}</h3><dl>"
                      +_line("Stock feature",str(x["stock_bars_accepted"])+" accepted bars")
                      +_line("Options feature",str(x["option_expiry_count"])+" expiries; vendor delta unavailable")
                      +_line("Decision",d["status"]+" // "+_when(d["interval_end"])+" // "+str(d["stock_coverage_bars"])+"/"+str(d["required_bars"])+" scheduled stock bars")
                      +_line("Routes evaluated",len(d["routes_evaluated"]))
                      +_line("Forecast",_forecast_text(f))
                      +_line("OI",x["oi_status"]+" // publication time uncertain")
                      +_line("Stale last trades",x["stale_last_trades"])
                      +_line("Missing volume / OI",str(x["missing_option_volume"])+" / "+str(x["missing_open_interest"]))
                      +_line("Rejected by reason",json.dumps(x["rejections"],sort_keys=True))
                      +"</dl><details><summary>[WHY SCORES ARE UNAVAILABLE]</summary><ul>"+"".join(f"<li>{_t(reason)}</li>" for reason in d["unavailable_reasons"])+"</ul></details></div>")
    return ("<p>PAPER-YAHOO / EXPERIMENTAL. This page records one manual ingestion cycle. The source adapter made exactly the requests listed below;"
            " no order or notification was sent.</p><dl class=\"summary\">"
            +"".join(_line(a,b) for a,b in (("Dataset",data["dataset_id"]),("Worker started",_when(data["worker_started_at"])),
                                        ("Worker completed",_when(data["worker_completed_at"])),("Latest decision",_when(data["latest_completed_decision_interval"])),
                                        ("Stock bars accepted",kind_totals["history"]),("Option contracts accepted",kind_totals["chain"]),
                                        ("Expiry identifiers accepted",kind_totals["expiries"]),
                                        ("Total response rows received",totals["rows_received"]),("Total response rows accepted",totals["rows_accepted"]),
                                        ("Rows rejected",totals["rows_rejected"]),("Routes evaluated",data["routes_evaluated"]),
                                        ("Episodes opened",data["episodes_opened"]),("Yahoo library",data["library_version"]),
                                        ("Adapter version",data["source_version"]),
                                        ("Timing audit","PASS // issue time follows last source response" if data.get("timing_revision") else "UNAVAILABLE"),
                                        ("Calendar audit","PASS // post-ingestion; interval coverage reconciled" if data["post_ingestion_calendar_audit"].get("interval_coverage_reconciled") else "UNAVAILABLE")))+"</dl>"
            "<section class=\"section-rule\"><h3>FEATURES AND MODEL DECISIONS</h3>"+"".join(models)+"</section>"
            "<section class=\"section-rule\"><h3>WARNINGS AND LIMITATIONS</h3><ul>"+"".join(f"<li>{_t(w)}</li>" for w in data["warnings"])+"</ul></section>"
            "<section class=\"section-rule\"><h3>EXACT SOURCE REQUESTS // BOUNDED RESPONSE AUDIT</h3>"
            "<div class=\"table-wrap\"><table><thead><tr><th>REQUEST</th><th>REQUESTED UTC</th><th>RECEIVED UTC</th><th>ROWS IN</th><th>ACCEPTED</th><th>REJECTED</th><th>RESPONSE SHA-256</th></tr></thead><tbody>"
            +"".join(rows)+"</tbody></table></div></section>"
            "<a class=\"command-link\" href=\"exports/paper-summary.json\">DERIVED JSON EXPORT</a>")


def _guide():
    return ("<p>Read the status and latest findings first. Then check the first-run source audit, per-instrument coverage, quality exclusions,"
            " and stock forecasts. The archived synthetic pages document software tests only.</p>"
            "<dl class=\"summary\">"
            +"".join(_line(a,b) for a,b in (
                ("Paper mode","Observes Yahoo market data without orders or notifications."),
                ("Stock forecast","EWMA variance of the next observed regular-session five-minute adjusted return. This is separate from alerts."),
                ("Flow / pricing score","Unavailable until historical real option observations support independent calibration."),
                ("OI confirmation","Requires at least two valid daily snapshots of the same contract; Yahoo release time is uncertain."),
                ("Decision interval","A completed regular-session hour plus processing delay. Missing bars exclude the decision."),
                ("Stale last trade","Last recorded option trade exceeded the freshness limit; Yahoo quote time itself is uncertain."),
                ("Synthetic archive","Earlier artificial scenarios; no evidence of market prediction.")))+"</dl>"
            "<p>Scores, if introduced after real-data calibration, measure anomalies, not insider-trading probabilities."
            " No current paper score or alert is shown.</p>")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.ids=set()
    def handle_starttag(self,tag,attrs):
        for key,value in attrs:
            if key in ("href","src") and value: self.links.append(value)
            if key=="id" and value: self.ids.add(value)


def validate_paper_site(directory):
    root=Path(directory).resolve(strict=True)
    files={p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    missing=sorted(PUBLIC_FILES-files); unexpected=sorted(files-PUBLIC_FILES)
    broken=[]; external=[]; private=[]
    for rel in sorted(files):
        path=root/rel
        if path.is_symlink(): broken.append(rel); continue
        if path.suffix in (".html",".json",".css",".csv"):
            content=path.read_text(encoding="utf-8")
            if PRIVATE_TEXT.search(content): private.append(rel)
            if path.suffix==".html":
                parser=_Links(); parser.feed(content)
                for link in parser.links:
                    parsed=urlsplit(link)
                    if parsed.scheme or parsed.netloc or link.startswith("/"):
                        external.append(f"{rel}: {link}"); continue
                    target=(path.parent/unquote(parsed.path)).resolve() if parsed.path else path
                    if root not in target.parents and target!=root or not target.is_file():
                        broken.append(f"{rel}: {link}")
                    elif parsed.fragment and target.suffix==".html":
                        target_parser=_Links(); target_parser.feed(target.read_text(encoding="utf-8"))
                        if unquote(parsed.fragment) not in target_parser.ids: broken.append(f"{rel}: {link}")
    archived=validate_public_site(root/ARCHIVE) if (root/ARCHIVE).is_dir() else {"passed":False}
    manifest=json.loads((root/"publication_manifest.json").read_text(encoding="utf-8")) if (root/"publication_manifest.json").is_file() else {}
    expected=manifest.get("file_hashes",{})
    actual={rel:hashlib.sha256((root/rel).read_bytes()).hexdigest() for rel in files if rel!="publication_manifest.json"}
    hash_mismatch=sorted(rel for rel in set(expected)|set(actual) if expected.get(rel)!=actual.get(rel))
    return {"passed":not(missing or unexpected or broken or external or private or hash_mismatch) and archived["passed"],
            "files":len(files),"missing":missing,"unexpected":unexpected,"broken":broken,"non_relative":external,
            "private_patterns":private,"hash_mismatch":hash_mismatch,"synthetic_archive":archived}


def export_paper_site(db_path, docs, synthetic_site):
    db=open_paper_db(db_path)
    try:
        first=db.execute("SELECT report_json FROM runs WHERE status='ok' ORDER BY completed_at ASC LIMIT 1").fetchone()
        latest=db.execute("SELECT report_json FROM runs WHERE status='ok' ORDER BY completed_at DESC LIMIT 1").fetchone()
        if not first or not latest: raise ValueError("No successful paper-Yahoo run")
        def with_request_times(wire):
            report=json.loads(wire)
            for entry in report["requests"]:
                request=entry.setdefault("request",{})
                if not request.get("requested_at"):
                    row=db.execute("SELECT requested_at FROM requests WHERE run_id=? AND kind=? AND symbol=?",
                                   (report["run_id"],entry["kind"],entry["symbol"])).fetchone()
                    if row is None: raise ValueError("Request timestamp absent from paper ledger")
                    request["requested_at"]=row[0]
            return report
        first_data=_safe_summary(with_request_times(first[0]))
        data=_safe_summary(with_request_times(latest[0]))
    finally:
        db.close()
    root=Path(docs); archive=root/ARCHIVE
    if not archive.exists():
        source=Path(synthetic_site)
        validation=validate_public_site(source)
        if not validation["passed"]: raise ValueError("Synthetic archive source failed validation: "+json.dumps(validation))
        archive.mkdir(parents=True)
        for rel in SYNTHETIC_PUBLIC_FILES:
            target=archive/rel; target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((source/rel).read_bytes())
    elif not validate_public_site(archive)["passed"]:
        raise ValueError("Existing synthetic archive is invalid")
    # The validated archive now owns the old synthetic root. Remove only
    # allowlisted old root files before writing the new paper landing page.
    for rel in SYNTHETIC_PUBLIC_FILES-ROOT_FILES:
        (root/rel).unlink(missing_ok=True)
    present={p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if present-PUBLIC_FILES: raise ValueError("Unexpected file in public directory")
    _write(root/"style.css",CSS)
    _write(root/"index.html",_page("OVERVIEW",_overview(data),"index.html"))
    _write(root/"first-real-run.html",_page("FIRST REAL RUN",_first_run(first_data),"first-real-run.html"))
    _write(root/"guide.html",_page("HOW TO READ SHOCKWATCH",_guide(),"guide.html"))
    _write(root/".nojekyll","")
    _write(root/"exports/paper-summary.json",json.dumps({"current":data,"first_run":first_data},indent=2,sort_keys=True,allow_nan=False)+"\n")
    _write(root/"publication.json",json.dumps({"dataset_id":data["dataset_id"],"mode":"paper","source":"yahoo",
                                                 "data_asof":max((data["ticker"][s]["latest_stock_bar_utc"] or "") for s in UNIVERSE),
                                                 "worker_completed_at":data["worker_completed_at"]},indent=2)+"\n")
    hashes={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file() and p.resolve()!= (root/"publication_manifest.json").resolve()}
    manifest={"built_at":datetime.now(timezone.utc).isoformat(),"dataset_id":data["dataset_id"],
              "mode":"paper","source":"yahoo","source_version":data["source_version"],
              "model_version":PAPER_MODEL_VERSION,"file_hashes":hashes}
    _write(root/"publication_manifest.json",json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    validation=validate_paper_site(root)
    if not validation["passed"]: raise ValueError("Paper publication failed validation: "+json.dumps(validation))
    return {"manifest":manifest,"validation":validation}


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    export=sub.add_parser("export")
    export.add_argument("--db",required=True); export.add_argument("--docs",required=True)
    export.add_argument("--synthetic-site",required=True)
    validate=sub.add_parser("validate")
    validate.add_argument("--docs",required=True)
    args=parser.parse_args(argv)
    result=export_paper_site(args.db,args.docs,args.synthetic_site) if args.command=="export" else validate_paper_site(args.docs)
    print(json.dumps(result if args.command=="validate" else {"dataset_id":result["manifest"]["dataset_id"],
          "validation":result["validation"]},indent=2))
    return 0 if (result["validation"]["passed"] if args.command=="export" else result["passed"]) else 2


if __name__=="__main__":
    raise SystemExit(main())
