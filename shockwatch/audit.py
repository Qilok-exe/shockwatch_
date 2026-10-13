"""Coverage against an explicit calendar, including wholly absent sessions."""
from .common import iso, utc
from .features import session_intervals, validate_panel

def coverage(panel, calendar, universe, start, end):
    f = validate_panel(panel)
    first, last = utc(start), utc(end)
    slots = [s for s in session_intervals(calendar) if first <= s["interval_end"] <= last]
    expected = {(symbol, iso(s["interval_end"])) for s in slots for symbol in universe}
    observed = {(r.instrument_id, r.interval_end): r for r in f.itertuples() if first <= utc(r.interval_end) <= last}
    missing, unexpected = expected-set(observed), set(observed)-expected
    healthy = sum(bool(observed[k].data_ok) for k in expected & set(observed))
    return {"expected_rows": len(expected), "observed_expected_rows": len(expected)-len(missing), "healthy_rows": healthy,
            "healthy_fraction": healthy/max(len(expected), 1), "missing_rows": len(missing), "unexpected_rows": len(unexpected),
            "missing_examples": sorted(missing)[:20], "unexpected_examples": sorted(unexpected)[:20]}
