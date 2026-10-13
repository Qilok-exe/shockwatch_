"""Run one synthetic scheduler wake and retain bounded operational evidence.

This wrapper never fits or promotes a model. The child uses the existing cycle.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

LOG_BYTES = 1024 * 1024
LOG_BACKUPS = 5
CAPTURE_CHARS = 65536
THREAD_VARIABLES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value):
    value = value or ""
    if len(value) <= CAPTURE_CHARS:
        return value
    return value[:CAPTURE_CHARS] + f"\n[truncated; original characters={len(value)}]"


def _write_record(logs, record):
    """Rotate by bytes, append evidence, and atomically replace latest status."""
    logs.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > LOG_BYTES:
        # Full child output can be unexpectedly large. Keep its status and
        # bounded captures rather than making an unbounded operational log.
        record = dict(record)
        record["report"] = {"truncated": True, "worker_status": record.get("worker_status")}
        record["worker"] = {"status": record.get("worker_status"), "truncated": True}
        record["stdout"] = record.get("stdout", "")[:8192]
        record["stderr"] = record.get("stderr", "")[:8192]
        record["log_record_truncated"] = True
        payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > LOG_BYTES:
        raise ValueError("Operational record exceeds log size limit")
    log = logs / "scheduled-task.jsonl"
    if log.exists() and log.stat().st_size + len(payload) > LOG_BYTES:
        oldest = logs / f"scheduled-task.jsonl.{LOG_BACKUPS}"
        if oldest.exists():
            oldest.unlink()
        for number in range(LOG_BACKUPS - 1, 0, -1):
            previous = logs / f"scheduled-task.jsonl.{number}"
            if previous.exists():
                previous.replace(logs / f"scheduled-task.jsonl.{number + 1}")
        log.replace(logs / "scheduled-task.jsonl.1")
    with log.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=logs, prefix=".scheduled-task-", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(logs / "scheduled-task-latest.json")
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def run_task(python, runtime, task_name=None):
    project = Path(__file__).resolve().parents[1]
    executable = str(Path(python).resolve())
    runtime = Path(runtime).resolve()
    command = [executable, "-m", "shockwatch", "synthetic-cycle", "--runtime", str(runtime)]
    record = {
        "started_at": _now(), "finished_at": None, "executable": executable,
        "arguments": command[1:], "cwd": str(project), "runtime": str(runtime),
        "child_exit": None, "exit_code": 1, "status": "failed",
        "worker_status": None, "worker": None, "report": None, "error": None,
        "stdout": "", "stderr": "", "thread_limit": 1,
        "task_name": task_name, "task_action": None,
    }
    env = dict(os.environ)
    env.update({name: "1" for name in THREAD_VARIABLES})
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0
    if os.name == "nt":
        flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    record["windows_creation_flags"] = flags
    exit_code = 1
    try:
        child = subprocess.run(command, cwd=project, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", creationflags=flags)
        record["child_exit"] = child.returncode
        record["stdout"] = _bounded_text(child.stdout)
        record["stderr"] = _bounded_text(child.stderr)
        try:
            report = json.loads(child.stdout)
            if not isinstance(report, dict):
                raise ValueError("Cycle output must be a JSON object")
            record["report"] = report
        except (json.JSONDecodeError, ValueError) as exc:
            report = None
            record["error"] = {"type": type(exc).__name__, "message": _bounded_text(str(exc))}
        worker = report.get("worker") if isinstance(report, dict) else None
        if isinstance(worker, dict):
            record["worker"] = worker
            record["worker_status"] = worker.get("status")
        if child.returncode != 0:
            exit_code = child.returncode
            record["status"] = "child_failed"
            if record["error"] is None:
                record["error"] = {"type": "ChildProcessError", "message": "Synthetic cycle exited unsuccessfully"}
        elif report is None:
            record["status"] = "malformed_report"
        elif report.get("status") == "stopped":
            exit_code = 0
            record["status"] = "stopped"
        else:
            if not isinstance(worker, dict) or not isinstance(worker.get("status"), str) or not worker["status"]:
                record["status"] = "malformed_report"
                record["error"] = {"type": "ValueError", "message": "Cycle report lacks worker status"}
            else:
                exit_code = 0 if worker["status"] == "ok" else 2
                record["status"] = "passed" if exit_code == 0 else "degraded"
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": _bounded_text(str(exc))}
    if record["status"] == "passed" and task_name and record["report"].get("source_exhausted") is True:
        action = {"status": "failed", "exit_code": None, "error": None}
        record["task_action"] = action
        try:
            if os.name != "nt":
                raise RuntimeError("Automatic task disable requires Windows")
            scheduler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
            disable_command = [str(scheduler), "/Change", "/TN", task_name, "/Disable"]
            action["command"] = disable_command
            disabled = subprocess.run(disable_command, cwd=project, env=env, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", creationflags=flags)
            action.update({"exit_code": disabled.returncode, "stdout": _bounded_text(disabled.stdout),
                           "stderr": _bounded_text(disabled.stderr)})
            if disabled.returncode != 0:
                exit_code = disabled.returncode
                record["status"] = "task_disable_failed"
                action["error"] = {"type": "TaskDisableError", "message": "Task Scheduler did not accept disable"}
                record["error"] = action["error"]
            else:
                action["status"] = "disabled"
                record["status"] = "source_exhausted_task_disabled"
        except Exception as exc:
            exit_code = 1
            record["status"] = "task_disable_failed"
            action["error"] = {"type": type(exc).__name__, "message": _bounded_text(str(exc))}
            record["error"] = action["error"]
    record["finished_at"] = _now()
    record["exit_code"] = exit_code
    try:
        _write_record(runtime / "logs", record)
    except Exception as exc:
        # pythonw.exe has no console streams. A logging failure must still
        # produce an unsuccessful scheduler result without raising on stderr.
        if sys.stderr is not None:
            print(f"Operational logging failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Explicit child python.exe path")
    parser.add_argument("--runtime", required=True, help="Prepared synthetic runtime directory")
    parser.add_argument("--task-name", help="Disable this registered task after successful source exhaustion")
    args = parser.parse_args()
    return run_task(args.python, args.runtime, args.task_name)


if __name__ == "__main__":
    sys.exit(main())
