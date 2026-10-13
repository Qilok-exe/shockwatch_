from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import pandas as pd
from .common import config, dumps, save_json, utc
from .audit import coverage
from .engine import calibrate, case_metrics, evaluation, replay
from .features import SCHEMAS, build_panel
from .models import fit
from .store import Store
from .synthetic import generate, raw_fixture
from .worker import run_once

def load(path):
    return json.loads(Path(path).read_text())

def read_panel(path):
    return pd.read_csv(path, float_precision="round_trip")

def write_replay(out, decisions, events, metrics):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out/"decisions.jsonl").write_text("".join(dumps(x)+"\n" for x in decisions))
    (out/"episodes.jsonl").write_text("".join(dumps(x)+"\n" for x in events))
    save_json(out/"metrics.json", metrics)

def demo(directory):
    path = Path(directory)
    if path.exists() and any(path.iterdir()):
        raise ValueError("Demo output must be a new or empty directory")
    cfg = config()
    cfg["synthetic"] = True
    panel, calendar, labels = generate(path/"data")
    panel = read_panel(path/"data/panel.csv")
    cutoff = str(calendar.iloc[99].session)+"T23:59:00Z"
    val_start, val_end = str(calendar.iloc[100].session)+"T00:00:00Z", str(calendar.iloc[129].session)+"T23:59:00Z"
    test_start, test_end = str(calendar.iloc[130].session)+"T00:00:00Z", str(calendar.iloc[-1].session)+"T23:59:00Z"
    print("Fitting synthetic training data...", flush=True)
    model = fit(panel, cutoff, cfg)
    save_json(path/"model.json", model)
    print("Calibrating both alert paths on synthetic validation data...", flush=True)
    selection = calibrate(panel, model, val_start, val_end, 1., labels)
    selection.update(reviewed=True, review_note="Synthetic software demo only; not a deployment approval.")
    save_json(path/"policy.json", selection)
    decisions, events = replay(panel, model, selection["policy"])
    metrics = {"SYNTHETIC_ONLY": True, "validation": evaluation(decisions, events, val_start, val_end),
               "test": evaluation(decisions, events, test_start, test_end), "test_cases": case_metrics(events, labels, test_start, test_end)}
    write_replay(path/"replay", decisions, events, metrics)
    print("Checking raw adapter and restart behavior...", flush=True)
    built = build_panel(raw_fixture(path/"raw_fixture"), cfg)
    built.to_csv(path/"adapter_panel.csv", index=False)
    inbox = path/"runtime/inbox"
    inbox.mkdir(parents=True)
    shutil.copyfile(path/"data/panel.csv", inbox/"synthetic_panel.csv")
    first = run_once(inbox, path/"runtime/monitor.db", model, selection, path/"data/sessions.csv", cfg, test_end)
    second = run_once(inbox, path/"runtime/monitor.db", model, selection, path/"data/sessions.csv", cfg, test_end)
    metrics["adapter"] = {"rows": len(built), "healthy_rows": int(built.data_ok.sum())}
    metrics["worker"] = {"first_decisions": first["decisions"], "second_decisions": second["decisions"], "first_events": first["events"],
                         "second_events": second["events"], "health": second["data_health"], "notifications_enabled": False}
    store = Store(path/"runtime/monitor.db")
    actual = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM decisions ORDER BY decision_at,instrument")]
    store.close()
    metrics["worker"]["replay_equal"] = actual == decisions
    save_json(path/"VERIFICATION.json", metrics)
    if second["decisions"] != 0 or actual != decisions or int(built.data_ok.sum()) != 7:
        raise RuntimeError("Synthetic integration verification failed; see VERIFICATION.json")
    print(json.dumps(metrics, indent=2))

def main(argv=None):
    parser = argparse.ArgumentParser(description="SHOCKWATCH research and monitoring pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo", help="Synthetic end-to-end verification; sends no notifications")
    p.add_argument("--out", required=True)
    p = sub.add_parser("templates", help="Create empty canonical CSV templates")
    p.add_argument("--out", required=True)
    p = sub.add_parser("map-csv", help="Rename mapped vendor fields and apply declared scales")
    p.add_argument("--input", required=True); p.add_argument("--mapping", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("build-features", help="Canonical bundle to hourly panel")
    p.add_argument("--input", required=True); p.add_argument("--out", required=True); p.add_argument("--config"); p.add_argument("--asof")
    p = sub.add_parser("fit", help="Fit only data and matured targets available by cutoff")
    p.add_argument("--panel", required=True); p.add_argument("--cutoff", required=True); p.add_argument("--out", required=True); p.add_argument("--config")
    p = sub.add_parser("audit", help="Compare coverage with an explicit calendar")
    p.add_argument("--panel", required=True); p.add_argument("--calendar", required=True); p.add_argument("--universe", nargs="+", required=True)
    p.add_argument("--start", required=True); p.add_argument("--end", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("calibrate", help="Compare complete alert policies on validation only")
    p.add_argument("--panel", required=True); p.add_argument("--model", required=True); p.add_argument("--start", required=True); p.add_argument("--end", required=True)
    p.add_argument("--target", type=float, default=1.); p.add_argument("--cases"); p.add_argument("--out", required=True)
    p = sub.add_parser("review-policy", help="Record an explicit local review of a policy")
    p.add_argument("--input", required=True); p.add_argument("--note", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("replay", help="Replay a frozen model and policy")
    p.add_argument("--panel", required=True); p.add_argument("--model", required=True); p.add_argument("--policy", required=True); p.add_argument("--out", required=True)
    p.add_argument("--start"); p.add_argument("--end"); p.add_argument("--cases")
    p = sub.add_parser("run-once", help="One persistent scheduled monitoring cycle")
    p.add_argument("--inbox", required=True); p.add_argument("--db", required=True); p.add_argument("--model", required=True)
    p.add_argument("--policy", required=True); p.add_argument("--calendar", required=True); p.add_argument("--config")
    p = sub.add_parser("status", help="Read worker and outbox status")
    p.add_argument("--db", required=True)
    p = sub.add_parser("backup", help="Online SQLite backup")
    p.add_argument("--db", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("generate-scenarios", help="Seeded canonical raw bundle; evaluation truth is isolated")
    p.add_argument("--out", required=True); p.add_argument("--sessions",type=int,default=160); p.add_argument("--seed",type=int,default=731)
    p.add_argument("--dataset-id"); p.add_argument("--no-faults",action="store_true")
    p = sub.add_parser("source-once", help="Atomically deliver newly available synthetic cohorts")
    p.add_argument("--bundle",required=True); p.add_argument("--runtime",required=True); p.add_argument("--asof",required=True); p.add_argument("--config"); p.add_argument("--max-cohorts",type=int)
    p = sub.add_parser("synthetic-prepare", help="Fit/freeze the raw synthetic demonstration")
    p.add_argument("--out",required=True); p.add_argument("--seed",type=int,default=731); p.add_argument("--sessions",type=int,default=160)
    p = sub.add_parser("synthetic-cycle", help="One combined synthetic source, worker, outcomes and website wake")
    p.add_argument("--runtime",required=True); p.add_argument("--asof")
    p = sub.add_parser("synthetic-demo", help="Complete finite raw synthetic integration/recovery/comparison exercise")
    p.add_argument("--out",required=True); p.add_argument("--seed",type=int,default=731); p.add_argument("--sessions",type=int,default=160); p.add_argument("--skip-experiment",action="store_true")
    p.add_argument("--source-bundle",help="Reuse an immutable preflight-checked fixture with the same specification")
    p = sub.add_parser("evaluate-outcomes", help="Append target readiness/outcomes without rewriting forecasts")
    p.add_argument("--db",required=True); p.add_argument("--calendar",required=True); p.add_argument("--now")
    p = sub.add_parser("freeze-experiment", help="Save chronological split/comparison specification before test evaluation")
    p.add_argument("--panel",required=True); p.add_argument("--calendar",required=True); p.add_argument("--cases"); p.add_argument("--dataset-id",required=True)
    p.add_argument("--mode",choices=("synthetic","historical","prospective"),required=True); p.add_argument("--out",required=True); p.add_argument("--bars"); p.add_argument("--config")
    p = sub.add_parser("experiment", help="Execute an already frozen chronological specification")
    p.add_argument("--panel",required=True); p.add_argument("--calendar",required=True); p.add_argument("--cases"); p.add_argument("--manifest",required=True)
    p.add_argument("--out",required=True); p.add_argument("--bars"); p.add_argument("--db"); p.add_argument("--forecasts"); p.add_argument("--outcomes")
    p = sub.add_parser("render-site", help="Render read-only bounded HTML/JSON/CSV from SQLite")
    p.add_argument("--db",required=True); p.add_argument("--out",required=True); p.add_argument("--scenarios"); p.add_argument("--experiment"); p.add_argument("--limit",type=int,default=10000)
    p = sub.add_parser("serve", help="Local read-only static preview on 127.0.0.1")
    p.add_argument("--site",required=True); p.add_argument("--port",type=int,default=8765)
    p = sub.add_parser("doctor", help="Read-only identity/path/configuration checks without secrets")
    p.add_argument("--runtime",required=True)
    p = sub.add_parser("restore-drill", help="Restore to a separate path and verify duplicate catch-up")
    p.add_argument("--runtime",required=True); p.add_argument("--out",required=True)
    for name in ("stop", "resume"):
        p = sub.add_parser(name, help="Pause/resume a synthetic workflow through its STOP sentinel")
        p.add_argument("--runtime",required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            demo(args.out)
        elif args.command == "templates":
            Path(args.out).mkdir(parents=True, exist_ok=True)
            for name, cols in SCHEMAS.items():
                target = Path(args.out)/(name+".csv")
                if target.exists():
                    raise ValueError(f"Refusing to overwrite {target.name}")
                pd.DataFrame(columns=cols).to_csv(target, index=False)
        elif args.command == "map-csv":
            mapping, source = load(args.mapping), pd.read_csv(args.input)
            result = pd.DataFrame({canonical: source[vendor] for canonical, vendor in mapping["columns"].items()})
            for col, scale in mapping.get("scales", {}).items():
                result[col] = pd.to_numeric(result[col])*float(scale)
            for col, value in mapping.get("constants", {}).items():
                result[col] = value
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            result.to_csv(args.out, index=False)
        elif args.command == "build-features":
            f = build_panel(args.input, config(args.config), args.asof)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(args.out).with_suffix(".partial")
            f.to_csv(tmp, index=False)
            tmp.replace(args.out)
            print(dumps({"rows": len(f), "healthy": int(f.data_ok.sum()) if len(f) else 0}))
        elif args.command == "fit":
            model = fit(read_panel(args.panel), args.cutoff, config(args.config))
            save_json(args.out, model)
            print(dumps({"model_id": model["model_id"], "training_rows": model["training_rows"]}))
        elif args.command == "audit":
            calendar = pd.read_csv(args.calendar)
            for col in ("open_utc", "close_utc"):
                calendar[col] = [utc(x) for x in calendar[col]]
            result = coverage(read_panel(args.panel), calendar, args.universe, args.start, args.end)
            save_json(args.out, result)
            print(json.dumps(result, indent=2))
        elif args.command == "calibrate":
            result = calibrate(read_panel(args.panel), load(args.model), args.start, args.end, args.target, pd.read_csv(args.cases) if args.cases else None)
            save_json(args.out, result)
            print(dumps({"candidate_count": len(result["candidates"]), "requires_review": True}))
        elif args.command == "review-policy":
            result = load(args.input)
            result.update(reviewed=True, review_note=args.note, reviewed_at=pd.Timestamp.now(tz="UTC").isoformat())
            save_json(args.out, result)
        elif args.command == "replay":
            model, policy = load(args.model), load(args.policy)
            if policy["model_id"] != model["model_id"]:
                raise ValueError("Policy/model mismatch")
            decisions, events = replay(read_panel(args.panel), model, policy["policy"], args.end)
            metrics = evaluation(decisions, events, args.start, args.end)
            if args.cases:
                metrics["documented_cases"] = case_metrics(events, pd.read_csv(args.cases), args.start, args.end)
            write_replay(args.out, decisions, events, metrics)
            print(json.dumps(metrics, indent=2))
        elif args.command == "run-once":
            result = run_once(args.inbox, args.db, load(args.model), load(args.policy), args.calendar, config(args.config))
            print(json.dumps(result, indent=2))
            if result["status"] != "ok":
                sys.exit(2)
        elif args.command == "generate-scenarios":
            from .scenarios import generate_scenarios
            result=generate_scenarios(args.out,sessions=args.sessions,seed=args.seed,dataset_id=args.dataset_id,faults=not args.no_faults)
            print(dumps({k:result[k] for k in ("dataset_id","mode","manifest_id","seed","sessions")}))
        elif args.command == "source-once":
            from .sources import SyntheticSource
            cfg=config(args.config or str(Path(args.runtime)/"config.json"))
            print(dumps(SyntheticSource(args.bundle,args.runtime,cfg).deliver(args.asof,args.max_cohorts)))
        elif args.command in ("synthetic-prepare","synthetic-cycle","synthetic-demo","restore-drill"):
            from .operations import prepare_synthetic,synthetic_cycle,synthetic_demo,restore_drill
            if args.command == "synthetic-prepare":
                result=prepare_synthetic(args.out,args.seed,args.sessions)
            elif args.command == "synthetic-cycle":
                result=synthetic_cycle(args.runtime,args.asof)
            elif args.command == "restore-drill":
                result=restore_drill(args.runtime,args.out)
            else:
                result=synthetic_demo(args.out,args.seed,args.sessions,with_experiment=not args.skip_experiment,source_bundle=args.source_bundle)
            print(dumps(result))
        elif args.command == "evaluate-outcomes":
            from .outcomes import evaluate_outcomes
            if not Path(args.db).is_file():
                raise ValueError("Database does not exist")
            calendar=pd.read_csv(args.calendar)
            store=Store(args.db)
            try:
                if not store.identity():
                    raise ValueError("Outcomes require a bound dataset")
                result=evaluate_outcomes(store,calendar,args.now or pd.Timestamp.now(tz="UTC"))
                print(dumps(result))
            finally:
                store.close()
        elif args.command in ("freeze-experiment","experiment"):
            from .experiments import freeze_manifest,run_experiment,save_packet,persist_packet,source_hash
            panel=read_panel(args.panel);calendar=pd.read_csv(args.calendar)
            labels=pd.read_csv(args.cases) if args.cases else pd.DataFrame(columns=["case_id","event_group","instrument_id","window_start","public_at","status"])
            bars=pd.read_csv(args.bars) if args.bars else None
            if args.command == "freeze-experiment":
                result=freeze_manifest(panel,calendar,labels,dataset_id=args.dataset_id,mode=args.mode,config=config(args.config),raw_source_hash=source_hash(bars) if bars is not None else None)
                if Path(args.out).exists():
                    raise ValueError("Refusing to overwrite frozen experiment manifest")
                save_json(args.out,result)
                print(dumps({"manifest_id":result["manifest_id"],"dataset_id":result["dataset_id"],"mode":result["mode"]}))
            else:
                specification=load(args.manifest)
                if bool(args.forecasts) != bool(args.outcomes):
                    raise ValueError("Provide both immutable forecasts and outcomes, or neither")
                if specification.get("mode") == "prospective" and not (args.forecasts and args.outcomes):
                    raise ValueError("Prospective experiments require original immutable forecast/outcome records")
                result=run_experiment(panel,calendar,labels,specification,bars=bars,
                                      forecast_records=load(args.forecasts) if args.forecasts else None,outcome_records=load(args.outcomes) if args.outcomes else None)
                save_packet(result,args.out)
                if args.db:
                    persist_packet(args.db,result)
                failures=[{"fold_id":f["fold"]["fold_id"],"candidate":c["candidate"],"reason":c.get("reason")} for f in result["folds"] for c in f["candidates"] if c["status"] != "complete"]
                failures += [{"fold_id":f["fold"]["fold_id"],"candidate":"forecasts","reason":f["forecasts"].get("reason")} for f in result["folds"] if f["forecasts"]["status"] != "complete"]
                print(dumps({"packet_id":result["packet_id"],"dataset_id":result["dataset_id"],"mode":result["mode"],"retained_failures":failures}))
                if failures:
                    sys.exit(2)
        elif args.command == "render-site":
            from .web import render_site
            print(dumps(render_site(args.db,args.out,scenario_file=args.scenarios,experiment_file=args.experiment,limit=args.limit)))
        elif args.command == "serve":
            from .web import serve_site
            serve_site(args.site,args.port)
        elif args.command == "doctor":
            from .diagnostics import doctor
            result=doctor(args.runtime);print(dumps(result))
            if result["status"] != "ok":
                sys.exit(2)
        elif args.command in ("stop","resume"):
            deployment=load(Path(args.runtime)/"deployment.json")
            if deployment.get("mode") != "synthetic":
                raise ValueError("This stop/resume command is restricted to synthetic workflows")
            sentinel=Path(args.runtime)/"STOP"
            if args.command == "stop":
                sentinel.write_text("Owner stopped synthetic processing\n",encoding="utf-8")
            else:
                sentinel.unlink(missing_ok=True)
            print(dumps({"mode":"synthetic","status":"stopped" if sentinel.exists() else "resumed"}))
        elif args.command in ("status", "backup"):
            if not Path(args.db).is_file():
                raise ValueError("Database does not exist")
            if args.command == "backup":
                store=Store(args.db)
                try:
                    store.backup(args.out)
                finally:
                    store.close()
            else:
                from .web import read_snapshot
                snapshot=read_snapshot(args.db,limit=1)
                last=snapshot["last_run"]
                timestamp=last.get("recorded_at") or (last.get("started_at") if snapshot["dataset"].get("mode") != "synthetic" else None)
                age=(pd.Timestamp.now(tz="UTC")-utc(timestamp)).total_seconds() if timestamp else None
                print(dumps({"dataset":snapshot["dataset"],"last_run":last,"last_run_age_seconds":age,
                             "worker_stale":age is None or age>900,"data_asof":snapshot["watermark"],"outbox":snapshot["outbox_counts"],"counts":snapshot["counts"]}))
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")
