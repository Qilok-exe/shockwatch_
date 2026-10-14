"""Single bounded paper-Yahoo wake; never commits, pushes or places orders."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from shockwatch.paper_yahoo import run_cycle
from shockwatch.paper_publication import export_paper_site, ARCHIVE


def _now(): return datetime.now(timezone.utc).isoformat()


def _log(logs,record):
    logs.mkdir(parents=True,exist_ok=True)
    path=logs/"scheduled-task.jsonl"
    if path.exists() and path.stat().st_size>1024*1024:
        backup=logs/"scheduled-task.jsonl.1"
        if backup.exists(): backup.unlink()
        path.replace(backup)
    wire=json.dumps(record,sort_keys=True,ensure_ascii=False)+"\n"
    with path.open("a",encoding="utf-8") as handle: handle.write(wire)
    (logs/"scheduled-task-latest.json").write_text(wire,encoding="utf-8")


def _remove_private_site(path,runtime):
    target=path.resolve()
    if target.parent!=runtime.resolve() or not path.name.startswith("site."):
        raise ValueError("Refusing to remove path outside paper runtime site staging")
    if path.exists(): shutil.rmtree(path)


def _regenerate_site(runtime):
    site=runtime/"site"; previous=runtime/"site.previous"
    if not site.exists() and previous.exists(): previous.rename(site)
    staging=runtime/("site.staging-"+uuid.uuid4().hex[:10])
    try:
        export_paper_site(runtime/"paper.db",staging,PROJECT/"docs"/ARCHIVE)
        if previous.exists(): _remove_private_site(previous,runtime)
        if site.exists(): site.rename(previous)
        try:
            staging.rename(site)
        except Exception:
            if previous.exists() and not site.exists(): previous.rename(site)
            raise
        if previous.exists(): _remove_private_site(previous,runtime)
    finally:
        if staging.exists(): _remove_private_site(staging,runtime)


def run_task(runtime):
    runtime=Path(runtime).resolve()
    logs=runtime/"logs"
    record={"started_at":_now(),"status":"running","new_decisions":0,"requests":0,"site_regenerated":False}
    if (runtime/"STOP").exists():
        record.update(status="stopped",finished_at=_now()); _log(logs,record); return 0
    lock=runtime/"paper.lock"
    lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            if os.name=="nt":
                import msvcrt
                handle.seek(0); handle.write(b"0"); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            record.update(status="overlap_skipped",finished_at=_now()); _log(logs,record); return 0
        try:
            identity=json.loads((runtime/"dataset.json").read_text(encoding="utf-8-sig"))
            if identity.get("mode")!="paper" or identity.get("source")!="yahoo":
                raise ValueError("Runtime identity is not paper-Yahoo")
            report=run_cycle(runtime/"paper.db",identity["dataset_id"])
            record.update(status=report["status"],dataset_id=identity["dataset_id"],
                          new_decisions=report.get("new_decisions",0),requests=len(report["requests"]),
                          error=report.get("error"))
            if report["status"]=="source_failed":
                (runtime/"STOP").write_text("Yahoo source failed; inspect private logs before resuming.\n",encoding="utf-8")
                record["source_paused"]=True
            elif report["status"]=="ok" and report.get("new_decisions",0)>0:
                _regenerate_site(runtime)
                record["site_regenerated"]=True
        except Exception as exc:
            record.update(status="failed",error={"type":type(exc).__name__,"message":str(exc)[:500]})
        finally:
            if os.name=="nt":
                import msvcrt
                handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
    record["finished_at"]=_now()
    _log(logs,record)
    return 0 if record["status"] in ("ok","stopped","overlap_skipped") else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime",required=True)
    args=parser.parse_args()
    return run_task(args.runtime)


if __name__=="__main__": raise SystemExit(main())
