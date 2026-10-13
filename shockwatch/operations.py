"""One synthetic source/worker/outcome/publication workflow and reproducible drill."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import time
import uuid
import pandas as pd
from .common import config, digest, dumps, iso, save_json, utc
from .engine import calibrate, replay, engine_state, decide
from .models import fit, enrich
from .features import session_intervals
from .scenarios import generate_scenarios
from .sources import SyntheticSource, source_lock
from .store import Store
from .worker import run_once
from .outcomes import build_forecasts
from .diagnostics import doctor, logical_digest, numerical_digest
from .web import render_site


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prepare_synthetic(directory, seed=731, sessions=160, source_bundle=None):
    """Prepare frozen artifacts; routine cycles never fit or tune models."""
    out = Path(directory).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("Synthetic output must be a new or empty directory")
    if sessions < 160:
        raise ValueError("Complete demonstration requires at least 160 sessions (100/30/30)")
    started = time.perf_counter()
    cfg = config()
    cfg.update(synthetic=True, mode="synthetic", forecast_issue_clock="processing")
    cfg["notifications"]["enabled"] = False
    bundle = out/"bundle"
    if source_bundle is not None:
        supplied = Path(source_bundle).resolve(strict=True)
        if supplied == out or supplied in out.parents or out in supplied.parents:
            raise ValueError("Reused source bundle must be separate from the demonstration output")
        if not (supplied/"manifest.json").is_file():
            raise ValueError("Prepared source bundle requires its immutable manifest")
        # Reuse a preflight-checked fixture without generating it twice. The
        # generator verifies the full specification and hashes before use.
        shutil.copytree(supplied, bundle)
    manifest = generate_scenarios(bundle, sessions=sessions, seed=seed, cfg=cfg)
    dataset_id = manifest["dataset_id"]
    cfg.update(dataset_id=dataset_id, source_version=manifest["adapter_version"], dataset_manifest=manifest)
    panel = pd.read_csv(bundle/"panel.csv",float_precision="round_trip")
    calendar = pd.read_csv(bundle/"raw/sessions.csv")
    # Labels evaluate scenario responses only. They never enter fitting inputs.
    scenarios = pd.read_csv(bundle/"truth/scenarios.csv")
    detection_scenarios={"exceptional_call","exceptional_put","repricing","persistent_price","persistent_oi","flat_oi","two_events_week"}
    labels = pd.read_csv(bundle/"truth/cases.csv") if (bundle/"truth/cases.csv").exists() else scenarios[scenarios.scenario_id.isin(detection_scenarios)].rename(columns={"scenario_id":"case_id","window_end":"public_at"}).copy()
    if "event_group" not in labels:
        labels["event_group"] = labels["case_id"]
    labels["status"] = "synthetic"
    labels.to_csv(out/"synthetic_cases.csv",index=False)
    cutoff = iso(utc(calendar.iloc[99].close_utc)+pd.Timedelta(minutes=cfg["decision_delay_minutes"]))
    model = fit(panel,cutoff,cfg)
    val_start = iso(utc(calendar.iloc[100].open_utc))
    val_end = iso(utc(calendar.iloc[129].close_utc)+pd.Timedelta(minutes=cfg["decision_delay_minutes"]))
    policy = calibrate(panel,model,val_start,val_end,1.,labels)
    policy.update(dataset_id=dataset_id,mode="synthetic",reviewed=True,
                  review_note="Synthetic software demonstration only; no real policy promotion",reviewed_at=iso(pd.Timestamp.now(tz="UTC")))
    runtime=out/"runtime"
    runtime.mkdir(parents=True)
    save_json(runtime/"model.json",model)
    save_json(runtime/"policy.json",policy)
    save_json(runtime/"config.json",cfg)
    save_json(runtime/"dataset.json",manifest)
    shutil.copyfile(bundle/"raw/sessions.csv",runtime/"sessions.csv")
    deployment={"dataset_id":dataset_id,"mode":"synthetic","bundle":str(bundle),"site":str(out/"site"),
                "scenario_file":str(bundle/"truth/fault_report.json"),"experiment_file":None,
                "prepared_at":iso(pd.Timestamp.now(tz="UTC")),"virtual_start":cutoff,
                "clock":"Accelerated virtual market time; each wake consumes next available cohort, not real market time"}
    save_json(runtime/"deployment.json",deployment)
    # Prime training observations. The first monitoring cycle commits them as
    # source history, but engine decisions start strictly after the fit cutoff.
    source = SyntheticSource(bundle,runtime,cfg)
    primed = source.deliver(cutoff)
    report={"dataset_id":dataset_id,"mode":"synthetic","runtime":str(runtime),"model_id":model["model_id"],
            "training_cutoff":cutoff,"training_rows":model["training_rows"],"primed":primed,
            "preparation_seconds":time.perf_counter()-started,"doctor":doctor(runtime)}
    save_json(out/"PREPARATION.json",report)
    return report


def synthetic_cycle(runtime, asof=None, *, source=None, publish=True):
    """Single combined scheduled/accelerated wake; no external services."""
    runtime=Path(runtime).resolve()
    if (runtime/"STOP").exists():
        return {"status":"stopped","runtime":str(runtime),"reason":"STOP sentinel present"}
    with source_lock(runtime/".cycle.lock"):
        deployment=_load(runtime/"deployment.json")
        if deployment.get("mode") != "synthetic":
            raise ValueError("Synthetic workflow cannot operate a real deployment")
        cfg=_load(runtime/"config.json"); model=_load(runtime/"model.json"); policy=_load(runtime/"policy.json")
        dataset=_load(runtime/"dataset.json")
        source=source or SyntheticSource(deployment["bundle"],runtime,cfg)
        if source.manifest["dataset_id"] != deployment["dataset_id"]:
            raise ValueError("Source/runtime dataset mismatch")
        chosen=asof or source.next_available_at()
        if chosen is None:
            checkpoint=source._checkpoint()
            chosen=checkpoint["virtual_asof"]
        if chosen is None:
            raise ValueError("No declared synthetic clock")
        started=time.perf_counter()
        delivery=source.deliver(chosen)
        context_store=Store(runtime/"monitor.db",dataset)
        try:
            with context_store.transaction():
                for context in source.available_context(chosen):
                    context_store.insert_context(context)
        finally:
            context_store.close()
        worker=run_once(runtime/"inbox",runtime/"monitor.db",model,policy,runtime/"sessions.csv",cfg,now=chosen,dataset_manifest=dataset)
        state_store=Store(runtime/"monitor.db")
        try:
            state_store.set("source_state",{"finite":True,"exhausted":source.next_available_at() is None,"pending_cohorts":delivery["pending_cohorts"],"virtual_asof":iso(chosen),"wall_recorded_at":iso(pd.Timestamp.now(tz="UTC"))})
        finally:
            state_store.close()
        report={"dataset_id":deployment["dataset_id"],"mode":"synthetic","delivery":delivery,"worker":worker,
                "source_exhausted":source.next_available_at() is None,
                "virtual_asof":iso(chosen),"wall_recorded_at":iso(pd.Timestamp.now(tz="UTC")),"runtime_seconds":time.perf_counter()-started}
        save_json(runtime/"cycle.json",report)
        if publish:
            report["publication"]=render_site(runtime/"monitor.db",deployment["site"],scenario_file=deployment["scenario_file"],experiment_file=deployment.get("experiment_file"))
        return report


def restore_drill(runtime, directory):
    """Online backup to a new path and catch-up with original source identity."""
    runtime=Path(runtime).resolve(); target=Path(directory).resolve()
    if target == runtime or runtime in target.parents or target in runtime.parents:
        raise ValueError("Restore drill uses a separate sibling directory")
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore target must be new or empty")
    target.mkdir(parents=True,exist_ok=True)
    store=Store(runtime/"monitor.db")
    store.backup(target/"monitor.db")
    store.close()
    baseline=logical_digest(runtime/"monitor.db")
    restored=logical_digest(target/"monitor.db")
    if baseline != restored:
        raise RuntimeError("Online backup restored different durable state")
    for name in ("model.json","policy.json","config.json","dataset.json","sessions.csv"):
        shutil.copyfile(runtime/name,target/name)
    # Redeliver recent archived inputs plus a duplicate file. No decisions or
    # outcomes may change; accepted-message/outbox state stays preserved.
    inbox=target/"inbox";inbox.mkdir()
    archived=sorted((runtime/"archive").rglob("*.csv"))
    if archived:
        shutil.copyfile(archived[-1],inbox/"redelivery.csv")
        shutil.copyfile(archived[-1],inbox/"duplicate.csv")
    cfg=_load(target/"config.json")
    cfg["notifications"]["enabled"]=False
    now=_load(runtime/"cycle.json")["virtual_asof"]
    report=run_once(inbox,target/"monitor.db",_load(target/"model.json"),_load(target/"policy.json"),target/"sessions.csv",cfg,now=now,dataset_manifest=_load(target/"dataset.json"))
    caught_up=logical_digest(target/"monitor.db")
    passed=baseline == caught_up and report["decisions"] == 0 and report["forecasts"] == 0 and report["events"] == 0 and all(v==0 for v in report["outcomes"].values())
    evidence={"dataset_id":_load(runtime/"dataset.json")["dataset_id"],"mode":"synthetic","backup_equal":baseline==restored,
              "duplicate_catchup_equal":baseline==caught_up,"passed":passed,"report":report,"logical_state":baseline,
              "note":"Logical online backup and duplicate catch-up; physical laptop loss/off-device restore not exercised"}
    save_json(target/"RESTORE_VERIFICATION.json",evidence)
    if not passed:
        raise RuntimeError("Restore/duplicate catch-up changed durable numerical state")
    return evidence



def recovery_checkpoint(runtime, target):
    """Snapshot DB plus source cursor/config for a later forward recovery drill."""
    runtime=Path(runtime).resolve();target=Path(target).resolve()
    if target==runtime or runtime in target.parents or target in runtime.parents:
        raise ValueError("Recovery checkpoint must use a separate sibling path")
    if target.exists() and any(target.iterdir()):
        raise ValueError("Recovery checkpoint must be new or empty")
    target.mkdir(parents=True,exist_ok=True)
    store=Store(runtime/"monitor.db")
    try:
        store.backup(target/"monitor.db")
    finally:
        store.close()
    for name in ("model.json","policy.json","config.json","dataset.json","sessions.csv","source_checkpoint.json","cycle.json"):
        shutil.copyfile(runtime/name,target/name)
    deployment=_load(runtime/"deployment.json")
    deployment["site"]=str(target.parent/"recovered-site")
    save_json(target/"deployment.json",deployment)
    return {"virtual_asof":_load(target/"cycle.json")["virtual_asof"],"state":logical_digest(target/"monitor.db")}


def resume_recovery_checkpoint(runtime, checkpoint, wake_times):
    """Reacquire subsequent canonical cohorts; compare future records and state."""
    runtime=Path(runtime).resolve();checkpoint=Path(checkpoint).resolve()
    deployment=_load(checkpoint/"deployment.json")
    source=SyntheticSource(deployment["bundle"],checkpoint,_load(checkpoint/"config.json"))
    reports=[]
    for now in wake_times:
        report=synthetic_cycle(checkpoint,now,source=source,publish=False)
        if report.get("status")=="stopped" or report["worker"]["errors"]:
            raise RuntimeError("Checkpoint recovery stopped or rejected source data")
        reports.append({"virtual_asof":report["virtual_asof"],"decisions":report["worker"]["decisions"],"forecasts":report["worker"]["forecasts"]})
    original=numerical_digest(runtime/"monitor.db")
    recovered=numerical_digest(checkpoint/"monitor.db")
    a=_load(runtime/"source_checkpoint.json");b=_load(checkpoint/"source_checkpoint.json")
    cursor_equal=all(a.get(k)==b.get(k) for k in ("manifest_id","dataset_id","mode","emitted","virtual_asof"))
    evidence={"dataset_id":deployment["dataset_id"],"mode":"synthetic","numerical_state_equal":original==recovered,
              "source_cursor_equal":cursor_equal,"subsequent_cycles":len(reports),"new_decisions":sum(r["decisions"] for r in reports),
              "new_forecasts":sum(r["forecasts"] for r in reports),"passed":original==recovered and cursor_equal and bool(reports),
              "original":original,"recovered":recovered,"note":"Execution run IDs and actual wall record times differ deliberately; stored target/input/issue clocks and numerical values are compared. No physical host-loss claim."}
    save_json(checkpoint/"FORWARD_RECOVERY_VERIFICATION.json",evidence)
    if not evidence["passed"]:
        raise RuntimeError("Forward recovery state differs from uninterrupted synthetic operation")
    return evidence


def synthetic_demo(directory, seed=731, sessions=160, with_experiment=True, source_bundle=None):
    """Raw-to-worker demonstration, restart/parity drill, website and comparisons."""
    prepared=prepare_synthetic(directory,seed,sessions,source_bundle=source_bundle)
    out=Path(directory).resolve();runtime=out/"runtime"
    cfg=_load(runtime/"config.json");source=SyntheticSource(out/"bundle",runtime,cfg)
    reports=[]
    calendar=pd.read_csv(runtime/"sessions.csv")
    scheduled=[x["interval_end"]+pd.Timedelta(minutes=cfg["decision_delay_minutes"]) for x in session_intervals(calendar)]
    release=pd.to_datetime(source.panel.get("source_ready_at",source.panel.decision_at),utc=True)
    decisions=pd.to_datetime(source.panel.decision_at,utc=True)
    ready=release[release>decisions].tolist()
    wake_times=sorted({utc(x) for x in scheduled+ready if utc(x)>utc(_load(runtime/"model.json")["training_cutoff"])})
    middle=max(0,len(wake_times)//2-1)
    checkpoint_record=None
    for index,now in enumerate(wake_times):
        report=synthetic_cycle(runtime,now,source=source,publish=False)
        if report.get("status") == "stopped":
            raise RuntimeError("Demo stopped by STOP sentinel")
        reports.append({"virtual_asof":report["virtual_asof"],"decisions":report["worker"]["decisions"],
                        "forecasts":report["worker"]["forecasts"],"events":report["worker"]["events"],
                        "health":report["worker"]["data_health"],"runtime_seconds":report["runtime_seconds"]})
        if report["worker"]["errors"]:
            raise RuntimeError("Synthetic source import rejected; inspect runtime/cycle.json")
        if index==middle:
            checkpoint_record=recovery_checkpoint(runtime,out/"forward-recovery/runtime")
    forward_recovery=resume_recovery_checkpoint(runtime,out/"forward-recovery/runtime",wake_times[middle+1:])
    repeated=synthetic_cycle(runtime,source=source,publish=False)
    panel=pd.read_csv(out/"bundle/panel.csv",float_precision="round_trip")
    model=_load(runtime/"model.json");policy=_load(runtime/"policy.json")
    expected_decisions,expected_events=replay(panel,model,policy["policy"])
    store=Store(runtime/"monitor.db")
    actual_decisions=[json.loads(r[0]) for r in store.db.execute("SELECT payload FROM decisions ORDER BY decision_at,instrument")]
    actual_events=[json.loads(r[0]) for r in store.db.execute("SELECT payload FROM events ORDER BY decision_at,event_key")]
    actual_forecasts=[json.loads(r[0]) for r in store.db.execute("SELECT payload FROM forecasts ORDER BY issued_at,instrument_id,forecast_id")]
    count=dict((r[0],r[1]) for r in store.db.execute("SELECT status,count(*) FROM latest_forecast_outcomes GROUP BY status"))
    store.close()
    # Core parity excludes only ordering of simultaneous event IDs. Fully future
    # forecast invariants are separately asserted from their persisted contracts.
    parity=actual_decisions == expected_decisions and sorted(actual_events,key=dumps)==sorted(expected_events,key=dumps)
    future=all(utc(f["input_watermark"]) <= utc(f["asof_at"]) <= utc(f["issued_at"]) <= utc(f["target_start"]) for f in actual_forecasts)
    # Recompute the frozen numerical forecast at its stored issue clock without
    # using target outcomes. Compare every forecast's identity/target/value.
    states={s:engine_state(model,s) for s in model["universe"]}
    issued_by_row={}
    for f in actual_forecasts:
        issued_by_row[(f["instrument_id"],f.get("origin_decision_at",f["asof_at"]))]=f["issued_at"]
    expected_forecasts=[]
    for row in enrich(panel,model["universe"]).to_dict("records"):
        if utc(row["decision_at"])<=utc(model["training_cutoff"]):
            continue
        symbol=row["instrument_id"]
        states[symbol],_,_=decide(row,model,policy["policy"],states[symbol])
        clock=issued_by_row.get((symbol,iso(row["decision_at"])))
        if clock:
            expected_forecasts.extend(build_forecasts(row,states[symbol],model,policy["policy"],calendar,
                dataset_id=model["dataset_id"],mode="synthetic",issued_at=clock,recorded_at=clock,run_id="parity-check"))
    projection=lambda f:{k:v for k,v in f.items() if k not in ("run_id","recorded_at")}
    forecast_parity=sorted((projection(f) for f in actual_forecasts),key=dumps)==sorted((projection(f) for f in expected_forecasts),key=dumps)
    duplicate=all(repeated["worker"][k]==0 for k in ("decisions","events","forecasts")) and all(v==0 for v in repeated["worker"]["outcomes"].values())
    verification={"dataset_id":prepared["dataset_id"],"mode":"synthetic","evidence_label":"Artificial software acceptance exercise only",
                  "core_replay_equal":parity,"forecast_numerical_replay_equal":forecast_parity,"future_forecast_ordering":future,"duplicate_idempotent":duplicate,
                  "outcome_states":count,"forecasts":len(actual_forecasts),"decisions":len(actual_decisions),
                  "cycles":reports,"forward_recovery":forward_recovery,"recovery_checkpoint":checkpoint_record,"unexecuted_checks":["physical lock/unlock","sleep/resume","reboot/login","Task Scheduler registration/overlap","external watchdog","off-device backup","real notification delivery","real predictive value"]}
    if with_experiment:
        from .experiments import freeze_manifest,run_experiment,save_packet,persist_packet,source_hash,packet_completion
        calendar=pd.read_csv(runtime/"sessions.csv");labels=pd.read_csv(out/"synthetic_cases.csv")
        bars=pd.read_csv(out/"bundle/raw/bars.csv")
        manifest=freeze_manifest(panel,calendar,labels,dataset_id=prepared["dataset_id"],seed=seed,config=cfg,raw_source_hash=source_hash(bars))
        save_json(out/"experiment/frozen_manifest.json",manifest)
        packet=run_experiment(panel,calendar,labels,manifest,config=cfg,bars=bars)
        save_packet(packet,out/"experiment")
        persist_packet(runtime/"monitor.db",packet)
        save_json(out/"experiment/packet.json",packet)
        deployment=_load(runtime/"deployment.json");deployment["experiment_file"]=str(out/"experiment/packet.json");save_json(runtime/"deployment.json",deployment)
        verification["experiment_packet_id"]=packet["packet_id"]
        verification["experiment_completion"]=packet_completion(packet)
        verification["experiment_execution_complete"]=verification["experiment_completion"]["complete"]
        verification["detector_performance_passed"]=packet["assessments"]["detector_performance_passed"]
    from .scenarios import evaluate_scenarios
    scenario_evaluation=evaluate_scenarios(out/"bundle",runtime/"monitor.db")
    save_json(out/"scenario_evaluation.json",scenario_evaluation)
    verification["scenario_evaluation"]=scenario_evaluation
    verification["software_assessments"]={"code_execution_completed":verification.get("experiment_execution_complete",True),
        "invariant_passed":parity and forecast_parity and future and duplicate and forward_recovery["passed"] and scenario_evaluation["all_measured_checks_passed"],
        "expected_scenario_response_passed":scenario_evaluation["scenario_response_passed"],
        "detector_performance_passed":verification.get("detector_performance_passed"),
        "detector_performance_failed":not verification.get("detector_performance_passed",True),
        "market_predictive_validity":"not established by synthetic data"}
    verification["restore"]=restore_drill(runtime,out/"restore-drill")
    deployment=_load(runtime/"deployment.json")
    deployment["scenario_file"]=str(out/"scenario_evaluation.json")
    save_json(runtime/"deployment.json",deployment)
    verification["publication"]=render_site(runtime/"monitor.db",deployment["site"],scenario_file=deployment["scenario_file"],experiment_file=deployment.get("experiment_file"))
    save_json(out/"VERIFICATION.json",verification)
    if not (parity and forecast_parity and future and duplicate and forward_recovery["passed"] and scenario_evaluation["all_measured_checks_passed"] and scenario_evaluation["scenario_response_passed"] and verification.get("experiment_execution_complete",True) and verification.get("detector_performance_passed",True)):
        raise RuntimeError("Synthetic integration gate failed; see VERIFICATION.json")
    return verification
