"""Execute baseline/new gates and save logs. A nonzero exit leaves gates incomplete."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import os
import threading
import urllib.request
import urllib.error
import functools
from http.server import ThreadingHTTPServer
from html.parser import HTMLParser


def verify_site(site):
    """Check completed local snapshots and fully consume HTTP bodies."""
    site=Path(site).resolve(strict=True)
    from shockwatch.web import ReadOnlyHandler
    publication=json.loads((site/"current.json").read_text(encoding="utf-8"))
    snapshot=site/publication["snapshot_id"]
    class Links(HTMLParser):
        def __init__(self):
            super().__init__();self.links=[]
        def handle_starttag(self,tag,attrs):
            if tag in ("a","link","script"):
                self.links += [v for k,v in attrs if k in ("href","src") and v]
    page_count=0
    for file in snapshot.glob("*.html"):
        parser=Links();parser.feed(file.read_text(encoding="utf-8"))
        for link in parser.links:
            if "://" not in link and not link.startswith("#"):
                target=(file.parent/link.split("#")[0]).resolve()
                target.relative_to(site.resolve())
                if not target.is_file():
                    raise RuntimeError(f"Broken site link: {file.name} -> {link}")
        page_count+=1
    handler=functools.partial(ReadOnlyHandler,directory=str(site))
    server=ThreadingHTTPServer(("127.0.0.1",0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        base=f"http://127.0.0.1:{server.server_port}"
        for name in ("index","scans","instruments","episodes","forecasts","experiments","scenarios","health","methods"):
            with urllib.request.urlopen(base+"/"+publication["snapshot_id"]+"/"+name+".html",timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("Page not served: "+name)
                while response.read(65536):
                    pass
        try:
            urllib.request.urlopen(urllib.request.Request(base+"/",data=b"mutation",method="POST"),timeout=5)
            raise RuntimeError("Mutation route unexpectedly accepted")
        except urllib.error.HTTPError as exc:
            if exc.code != 405:
                raise
        try:
            urllib.request.urlopen(base+"/monitor.db",timeout=5)
            raise RuntimeError("Private file route unexpectedly accepted")
        except urllib.error.HTTPError as exc:
            if exc.code not in (403,404):
                raise
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
    return {"name":"read-only-site-http-and-links","status":"passed","pages_checked":page_count,
            "snapshot_id":publication["snapshot_id"],"note":"HTTP bodies fully read; links bounded; POST405 and private file403/404"}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--out")
    p.add_argument("--site-only",help="Verify an existing rendered snapshot without rerunning numerical jobs")
    p.add_argument("--source-bundle",help="Reuse a hashed source fixture after independent coverage preflight")
    args=p.parse_args()
    project=Path(__file__).resolve().parents[1]
    workspace=project.parents[1]
    output=Path(args.out).resolve() if args.out else workspace/"implementation_evidence"/("python-validation-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    if output.exists() and any(output.iterdir()):
        raise ValueError("Validation output must be new or empty")
    output.mkdir(parents=True,exist_ok=True)
    evidence={"started_at":datetime.now(timezone.utc).isoformat(),"python":sys.version,"executable":sys.executable,
              "platform":platform.platform(),"gates":[],"status":"running","evidence_label":"Synthetic software verification only"}
    numerical_limits = {name: "1" for name in
                        ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                         "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS")}
    os.environ.update(numerical_limits)
    evidence["scope"]="site_only" if args.site_only else "full_pipeline"
    evidence["resource_policy"] = {"numerical_threads": 1, "sequential_jobs": True,
                                   "priority": "below_normal" if os.name == "nt" else "normal"}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetPriorityClass.restype = wintypes.BOOL
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000):
            raise OSError("Could not set below-normal verification priority")
    def save():
        (output/"VALIDATION.json").write_text(json.dumps(evidence,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    save()
    env=dict(os.environ);env["PYTHONDONTWRITEBYTECODE"]="1"
    env.update(numerical_limits)
    # Never let local notification environment configuration affect this exercise.
    for name in list(env):
        if name.startswith("SHOCKWATCH_"):
            env.pop(name)
    def run(name,arguments,cwd=project):
        command=[sys.executable]+arguments
        started=datetime.now(timezone.utc)
        print(f"Starting {name}; log: {output/(name+'.log')}", flush=True)
        with (output/(name+".log")).open("w",encoding="utf-8") as log:
            completed=subprocess.run(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0)
        gate={"name":name,"command":command,"cwd":str(cwd),"exit_code":completed.returncode,
              "elapsed_seconds":(datetime.now(timezone.utc)-started).total_seconds(),"status":"passed" if completed.returncode==0 else "failed"}
        evidence["gates"].append(gate);save()
        print(f"{name}: {gate['status']} ({gate['elapsed_seconds']:.1f}s)", flush=True)
        if completed.returncode:
            raise RuntimeError(f"{name} failed; inspect {output/(name+'.log')}")
    if args.site_only:
        try:
            evidence["gates"].append(verify_site(args.site_only))
            evidence["status"]="passed"
        except Exception as exc:
            evidence["status"]="failed"
            evidence["failure"]={"type":type(exc).__name__,"message":str(exc)}
            raise
        finally:
            evidence["finished_at"]=datetime.now(timezone.utc).isoformat();save()
        print(json.dumps({"status":evidence["status"],"evidence":str(output/"VALIDATION.json")},indent=2))
        return
    try:
        run("syntax",["-m","compileall","-q","shockwatch","scripts"])
        run("dependencies",["-c","import numpy,pandas,tzdata; print('numpy',numpy.__version__); print('pandas',pandas.__version__); print('tzdata',tzdata.__version__)"])
        original=workspace/"implementation_evidence/starter_original"
        if not (original/"shockwatch/cli.py").is_file():
            raise ValueError("Original baseline snapshot missing")
        run("baseline-tests",["-m","unittest","discover","-s","tests","-v"],original)
        run("baseline-demo",["-m","shockwatch","demo","--out",str(output/"baseline-demo")],original)
        run("new-tests",["-m","unittest","discover","-s","tests","-v"])
        demo_arguments=["-m","shockwatch","synthetic-demo","--out",str(output/"synthetic-demo")]
        if args.source_bundle:
            demo_arguments += ["--source-bundle",str(Path(args.source_bundle).resolve())]
        run("synthetic-demo",demo_arguments)
        runtime=output/"synthetic-demo/runtime"
        run("doctor",["-m","shockwatch","doctor","--runtime",str(runtime)])
        run("status",["-m","shockwatch","status","--db",str(runtime/"monitor.db")])
        sys.path.insert(0,str(project))
        evidence["gates"].append(verify_site(output/"synthetic-demo/site"))
        evidence["status"]="passed"
        evidence["unexecuted"]=["physical lock/unlock, suspend/resume and reboot/login","Windows scheduler registration/overlap","independent external watchdog","off-device restore","real data/feed predictive value","outbound service acceptance"]
    except Exception as exc:
        evidence["status"]="failed";evidence["failure"]={"type":type(exc).__name__,"message":str(exc)}
        save()
        raise
    finally:
        evidence["finished_at"]=datetime.now(timezone.utc).isoformat();save()
    print(json.dumps({"status":evidence["status"],"evidence":str(output/"VALIDATION.json")},indent=2))

if __name__ == "__main__":
    main()
