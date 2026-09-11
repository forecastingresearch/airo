"""Question-version boundaries and dated incident onset windows.

The joint call changes as a whole even though only the incident definitions
changed. Old responses are retained, never inferred to answer the new rules.
"""
from datetime import date

CURRENT_INSTRUMENT = "airo-incidents-prospective-v1"


def instrument_rows(rows, version=CURRENT_INSTRUMENT):
    return [r for r in rows if r.get("instrument_version") == version]


def counting_windows(question, horizons, run_date, spec):
    """Inclusive UTC calendar-date onset windows for one incident question."""
    from .questions import resolves_on

    if question.get("category") != "incident":
        return None
    if isinstance(run_date, str):
        run_date = date.fromisoformat(run_date[:10])
    return {
        h: {"start": run_date.isoformat(),
            "end": resolves_on(h, run_date, spec),
            "timezone": "UTC", "harm_years": 3}
        for h in horizons if h in question["horizons"]
    }
