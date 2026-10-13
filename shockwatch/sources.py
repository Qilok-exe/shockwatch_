"""Immutable canonical-source delivery and explicit cumulative-volume conversion.

The synthetic adapter publishes one complete interval cohort with os.replace. A
persisted checkpoint advances only after publication; replaying a crashed publish
is harmless. Simulated availability and wall processing time are never conflated.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import pandas as pd

from .common import clean, digest, iso, utc
from .features import build_panel, read_tables

ADAPTER_VERSION = "synthetic-canonical-1"


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(clean(value), stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


@contextmanager
def source_lock(path):
    """OS-owned advisory lock, released automatically when a process dies."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    stream.seek(0, 2)
    if not stream.tell():
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class CumulativeVolume:
    """Difference only within contract/session, preserving ambiguous intervals.

    A first observation needs an explicit start-of-interval baseline. Without it,
    its interval volume is missing. A declared reset and any negative correction
    are missing, recorded with separate reasons, and establish a new anchor.
    Duplicate identities must retain the same first-release value and reset flag.
    """
    def __init__(self, state=None):
        self.state = state or {"anchors": {}, "observations": {}}

    def observe(self, contract_id, session, at, cumulative, baseline=None, reset=False):
        at = iso(at)
        value = None if cumulative is None else float(cumulative)
        if value is not None and (not math.isfinite(value) or value < 0 or value % 1):
            raise ValueError("Cumulative volume must be a nonnegative integer or explicit missing")
        if baseline is not None:
            baseline = float(baseline)
            if not math.isfinite(baseline) or baseline < 0 or baseline % 1:
                raise ValueError("Volume baseline must be a nonnegative integer")
        identity = digest([str(contract_id), str(session), at])
        payload = {"cumulative": value, "baseline": baseline, "reset": bool(reset)}
        old = self.state["observations"].get(identity)
        if old:
            if old["payload"] != payload:
                raise ValueError("Changed first-release cumulative observation")
            return old["result"].copy()
        key = digest([str(contract_id), str(session)])
        anchor = self.state["anchors"].get(key)
        if anchor and utc(at) <= utc(anchor["at"]):
            raise ValueError("Out-of-order cumulative volume requires isolated revision replay")
        previous = anchor["value"] if anchor and anchor["value"] is not None else baseline
        if value is None:
            result = {"interval_volume": None, "reason": "missing_cumulative_observation"}
        elif reset:
            result = {"interval_volume": None, "reason": "declared_cumulative_reset"}
        elif previous is None:
            result = {"interval_volume": None, "reason": "missing_preceding_counter" if anchor else "first_observation_without_baseline"}
        elif value < previous:
            result = {"interval_volume": None, "reason": "negative_cumulative_correction"}
        else:
            result = {"interval_volume": int(value - previous), "reason": "observed_difference"}
        self.state["anchors"][key] = {"at": at, "value": value}
        self.state["observations"][identity] = {"payload": payload, "result": result}
        return result.copy()


def build_source_panel(raw_directory, cfg, dataset_id, mode="synthetic"):
    """Use the real builder; add first-release provenance and outcome evidence."""
    frame = build_panel(raw_directory, cfg)
    tables = read_tables(raw_directory)
    bars = tables["bars"].sort_values(["instrument_id", "bar_end"]).copy()
    # A fixed split basis supports signed returns across a split without double
    # adjustment. These are frozen completed-interval observations. The original
    # options scorer ignores them; the stock-only comparator may use their
    # observed return, and later outcome evaluation may use the same evidence.
    bars["basis_factor"] = bars.groupby("instrument_id").previous_close_factor.cumprod()
    bars["basis_open"] = bars.open / bars.basis_factor
    bars["basis_close"] = bars.close / bars.basis_factor
    bar_groups = {key: group for key, group in bars.groupby("instrument_id")}
    source_groups = {}
    for name in ("bars", "options", "batches"):
        endcol = "bar_end" if name == "bars" else "interval_end"
        for (symbol, end), group in tables[name].groupby(["instrument_id", endcol]):
            if name == "bars":
                continue
            key = (symbol, iso(end))
            source_groups[key] = max(source_groups.get(key, utc(end)), group.available_at.max())
    input_groups = {}
    for name in ("options", "batches"):
        input_groups[name] = {(symbol, iso(end)): group for (symbol, end), group in tables[name].groupby(["instrument_id", "interval_end"])}
    oi_times = sorted(set(iso(x) for x in tables["oi"].loc[tables["oi"].available_at >= tables["oi"].published_at, "available_at"]))
    starts, ends, watermarks, ready_times = [], [], [], []
    for row in frame.to_dict("records"):
        known = bar_groups.get(row["instrument_id"], bars.iloc[:0])
        known = known[(known.bar_start >= utc(row["interval_start"])) &
                      (known.bar_end <= utc(row["interval_end"])) &
                      (known.available_at <= utc(row["decision_at"]))]
        valid = len(known) == int(row["minutes"] / 5) and pd.notna(row["rv"])
        starts.append(float(known.basis_open.iloc[0]) if valid else None)
        ends.append(float(known.basis_close.iloc[-1]) if valid else None)
        key = (row["instrument_id"], row["interval_end"])
        available = source_groups.get(key, utc(row["interval_end"]))
        if len(known):
            available = max(available, known.available_at.max())
        ready_times.append(iso(available))
        consumed = [utc(row["interval_end"])]
        if len(known):
            consumed.append(known.available_at.max())
        for name in ("options", "batches"):
            selected = input_groups[name].get(key, tables[name].iloc[:0])
            selected = selected[selected.available_at <= utc(row["decision_at"])]
            if len(selected):
                consumed.append(selected.available_at.max())
        # OI release watermarks describe validated archived releases; the
        # builder separately restricts evidence to the matched active chain.
        import bisect
        pos = bisect.bisect_right(oi_times, iso(row["decision_at"]))
        if pos:
            consumed.append(utc(oi_times[pos - 1]))
        watermarks.append(iso(max(consumed)))
    frame["dataset_id"] = dataset_id
    frame["mode"] = mode
    frame["source_version"] = ADAPTER_VERSION
    frame["underlying_start_price"] = starts
    frame["underlying_end_price"] = ends
    frame["available_at"] = watermarks
    frame["input_watermark"] = watermarks
    frame["source_ready_at"] = ready_times
    return frame


class SyntheticSource:
    """Replaceable source adapter backed by a hashed, immutable raw bundle.

    The panel cache was built from canonical raw inputs with the actual feature
    builder at generation time. Its hash, configuration and raw content hashes
    are verified before delivery. No truth files are read by this adapter.
    """
    def __init__(self, bundle, runtime, cfg):
        self.bundle = Path(bundle)
        self.runtime = Path(runtime)
        self.cfg = cfg
        self.manifest = json.loads((self.bundle / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest.get("mode") != "synthetic":
            raise ValueError("Synthetic source requires synthetic dataset identity")
        if digest(self.manifest["identity"]) != self.manifest.get("manifest_id"):
            raise ValueError("Changed source manifest identity")
        for relative, expected in self.manifest["identity"]["content_hashes"].items():
            candidate = (self.bundle / relative).resolve()
            if self.bundle.resolve() not in candidate.parents:
                raise ValueError("Source manifest path escapes bundle")
            if file_hash(candidate) != expected:
                raise ValueError("Changed first-release source content: " + relative)
        if digest(self.manifest["identity"]["builder_config"]) != digest({k: cfg[k] for k in self.manifest["identity"]["builder_config"]}):
            raise ValueError("Builder configuration differs from immutable source cache")
        self.panel = pd.read_csv(self.bundle / "panel.csv", float_precision="round_trip")
        if not self.panel.dataset_id.eq(self.manifest["dataset_id"]).all() or not self.panel["mode"].eq("synthetic").all():
            raise ValueError("Panel identity differs from source manifest")
        self._cohorts = list(self.panel.groupby("decision_at", sort=True))
        self.context = pd.read_csv(self.bundle / "source_archive/public_context.csv")
        self.inbox = self.runtime / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.runtime / "source_checkpoint.json"

    def _checkpoint(self):
        if self.checkpoint_path.exists():
            result = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if result.get("manifest_id") != self.manifest["manifest_id"]:
                raise ValueError("Checkpoint belongs to another immutable source")
            return result
        return {"manifest_id": self.manifest["manifest_id"], "dataset_id": self.manifest["dataset_id"],
                "mode": "synthetic", "emitted": {}, "virtual_asof": None, "wall_processed_at": None}

    def deliver(self, asof, max_cohorts=None):
        """Emit newly available cohorts atomically; bounded for scheduled wakes."""
        asof = utc(asof)
        if max_cohorts is not None and (not isinstance(max_cohorts, int) or max_cohorts < 1):
            raise ValueError("max_cohorts must be a positive integer")
        with source_lock(self.runtime / ".source.lock"):
            checkpoint = self._checkpoint()
            if checkpoint["virtual_asof"] and asof < utc(checkpoint["virtual_asof"]):
                raise ValueError("Virtual source clock cannot run backwards")
            report = {"dataset_id": self.manifest["dataset_id"], "mode": "synthetic",
                      "simulated_asof": iso(asof), "wall_processed_at": iso(pd.Timestamp.now(tz="UTC")),
                      "emitted_cohorts": 0, "emitted_rows": 0, "partial_cohorts": [], "files": []}
            for at, group in self._cohorts:
                if utc(at) > asof:
                    break
                interval_end = str(group.interval_end.iloc[0])
                available = max(utc(value) for value in group.source_ready_at)
                if available > asof:
                    # Preserve chronological publication: no later cohort may
                    # outrun an incomplete arrival and advance worker watermark.
                    break
                if interval_end in checkpoint["emitted"]:
                    continue
                if max_cohorts is not None and report["emitted_cohorts"] >= max_cohorts:
                    break
                if set(group.instrument_id) != set(self.manifest["identity"]["universe"]):
                    report["partial_cohorts"].append(interval_end)
                filename = "cohort_" + utc(interval_end).strftime("%Y%m%dT%H%M%SZ") + ".csv"
                destination = self.inbox / filename
                tmp = self.inbox / (filename + ".tmp")
                with tmp.open("w", encoding="utf-8", newline="") as stream:
                    group.to_csv(stream, index=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                content_hash = file_hash(tmp)
                if destination.exists() and file_hash(destination) != content_hash:
                    tmp.unlink()
                    raise ValueError("Changed first-release published cohort")
                os.replace(tmp, destination)
                checkpoint["emitted"][interval_end] = content_hash
                report["emitted_cohorts"] += 1
                report["emitted_rows"] += len(group)
                report["files"].append(filename)
                checkpoint["virtual_asof"] = iso(asof)
                checkpoint["wall_processed_at"] = report["wall_processed_at"]
                atomic_json(self.checkpoint_path, checkpoint)
            checkpoint["virtual_asof"] = iso(asof)
            checkpoint["wall_processed_at"] = report["wall_processed_at"]
            atomic_json(self.checkpoint_path, checkpoint)
            report["delivered_total"] = len(checkpoint["emitted"])
            report["pending_cohorts"] = self.panel.decision_at.nunique() - len(checkpoint["emitted"])
            return report

    def next_available_at(self):
        checkpoint = self._checkpoint()
        for at, group in self._cohorts:
            if str(group.interval_end.iloc[0]) not in checkpoint["emitted"]:
                return iso(max(utc(at), max(utc(value) for value in group.source_ready_at)))
        return None

    def advance(self, minutes=60, max_cohorts=None):
        """Deterministic accelerated virtual-clock wake; no wall-clock sleeping."""
        if not math.isfinite(float(minutes)) or minutes <= 0:
            raise ValueError("Virtual step must be positive")
        checkpoint = self._checkpoint()
        if checkpoint["virtual_asof"]:
            asof = utc(checkpoint["virtual_asof"]) + pd.Timedelta(minutes=minutes)
        else:
            next_at = self.next_available_at()
            if next_at is None:
                return self.deliver(pd.Timestamp.now(tz="UTC"), max_cohorts=max_cohorts)
            asof = utc(next_at)
        return self.deliver(asof, max_cohorts=max_cohorts)

    def available_context(self, asof):
        """Timestamped public information only; never evaluation truth or answers."""
        if self.context.empty:
            return []
        known = self.context[pd.to_datetime(self.context.available_at, utc=True) <= utc(asof)]
        return [clean({**row, "dataset_id": self.manifest["dataset_id"], "mode": "synthetic"}) for row in known.to_dict("records")]
