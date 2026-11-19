import json
import sqlite3
from datetime import date

import pytest

from dashboard import resolutions as R


day = date(2030, 2, 3)
target = R.Target("event", "2030", 1)
req = R.Request(target, "panel", {"version": 1}, {})
first = R.Observation(target, "report-1", "2030-02-01", False, {"source": "February report"})
second = R.Observation(target, "report-2", "2030-02-02", True, {"source": "follow-up"})


class Reports:
    def __init__(self, observations):
        self.observations = observations
        self.checkpoints = []

    def refresh(self, requests, *, as_of, after):
        self.checkpoints.append(after)
        return R.RefreshBatch(self.observations, {"count": len(self.observations)})


def test_source_history_retries_and_corrections(tmp_path):
    db = tmp_path / "store"
    plugin = Reports([first])

    def update():
        R.refresh(db, [req], {"panel": plugin}, as_of=day)

    update()
    original_id, _ = R.history(db)
    plugin.observations = [second, first]
    update()
    head, h = R.history(db)
    assert head > original_id
    assert R.select(h[target], "first_published")["value"] is False
    latest = R.select(h[target], "latest")
    assert latest["value"] is True
    update()
    assert R.history(db)[0] == head
    assert plugin.checkpoints == [None, {"count": 1}, {"count": 2}]
    R.correct(db, *target[:2], req.rule, {"value": False, "vintage": second.published_at},
              previous=latest, reason="Transcription error")
    assert R.select(R.history(db)[1][target], "latest")["supersedes"] == latest
    assert R.select(R.history(db, through=head)[1][target], "latest") == latest
    assert len(R.history(db, through=original_id)[1][target]) == 1
    update()  # Seeing the uncorrected upstream evidence again preserves the correction.
    assert R.select(R.history(db)[1][target], "latest")["value"] is False


def test_invalid_batch_preserves_cursor_and_snapshot(tmp_path):
    db = tmp_path / "store"
    plugin = Reports([first])
    R.refresh(db, [req], {"panel": plugin}, as_of=day)
    id_before, _ = R.history(db)
    conflict = R.Observation(target, first.revision, first.published_at, True, first.evidence)
    plugin.observations = [second, conflict]
    with pytest.raises(ValueError, match="Conflicting evidence"):
        R.refresh(db, [req], {"panel": plugin}, as_of=day)
    assert R.history(db)[0] == id_before
    plugin.observations = []
    R.refresh(db, [req], {"panel": plugin}, as_of=day)
    assert plugin.checkpoints[-1] == {"count": 1}


def test_unknown_source_date_and_legacy_import(tmp_path):
    db = tmp_path / "store"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE outcomes(question,horizon,version,payload,recorded_at)")
        raw = {"value": False, "rule": req.rule, "vintage": first.published_at}
        conn.execute("INSERT INTO outcomes VALUES(?,?,?,?,?)", (*target, json.dumps(raw), "2020-02-02 00:00:00"))
    original, _ = R.history(db)
    plugin = Reports([R.Observation(target, "undated report", None, True, {})])
    R.refresh(db, [req], {"panel": plugin}, as_of=day)
    records = R.history(db)[1][target]
    assert R.select(records, "first_published") is None
    assert R.select(records, "first_observed")["revision_id"] == first.published_at
    assert len(R.history(db, through=original)[1][target]) == 1


def test_backfilled_first_release_and_ingestion_order(tmp_path):
    db = tmp_path / "store"
    with pytest.raises(ValueError, match="Unknown outcome snapshot"):
        R.history(db, through=1)
    R.refresh(db, [req], {"panel": Reports([second, first])}, as_of=day)
    records = R.history(db)[1][target]
    assert R.select(records, "first_observed")["revision_id"] == second.revision
    assert R.select(records, "first_published")["revision_id"] == first.revision
    assert R.select(records, "latest")["revision_id"] == second.revision
    assert [r["revision_id"] for r in R.snapshot(db)[target]["revisions"]] == [first.revision, second.revision]


def test_concurrent_refresh_preserves_winning_batch(tmp_path):
    db = tmp_path / "store"

    class Interleaved:
        def refresh(self, requests, *, as_of, after):
            assert after is None
            R.refresh(db, requests, {"panel": Reports([first])}, as_of=as_of)
            return R.RefreshBatch([second], {"stale": True})

    with pytest.raises(ValueError, match="Concurrent resolution refresh"):
        R.refresh(db, [req], {"panel": Interleaved()}, as_of=day)
    retry = Reports([second])
    R.refresh(db, [req], {"panel": retry}, as_of=day)
    assert retry.checkpoints == [{"count": 1}]
    assert [r["revision_id"] for r in R.history(db)[1][target]] == [first.revision, second.revision]


def test_invalid_second_resolver_rolls_back_all_observations_and_cursors(tmp_path):
    db = tmp_path / "store"
    other = R.Target("another", "2030", 1)
    requests = [req, R.Request(other, "other", req.rule, {})]
    reports = Reports([first])
    invalid = R.Observation(other, "", None, "open", {})
    with pytest.raises(ValueError, match="Empty source revision ID"):
        R.refresh(db, requests, {"panel": reports, "other": Reports([invalid])}, as_of=day)
    assert R.history(db) == (0, {})
    R.refresh(db, requests, {"panel": reports, "other": Reports([])}, as_of=day)
    assert reports.checkpoints == [None, None]


def test_forecast_eligibility_follows_first_decision_and_its_rule_version(tmp_path):
    from dashboard import resolutions as r
    db = tmp_path / "outcomes.sqlite"
    key = r.Target("rate", "2030", 1)
    assert r.forecast_snapshot(db) == (0, set())
    rule = {"version": 1}
    r.record(db, *key[:2], rule, {"value": 0, "vintage": "2031-01-01"})
    seq, closed = r.forecast_snapshot(db)
    assert closed == {key}
    r.record(db, *key[:2], rule, {"value": 1, "vintage": "2031-02-01"})
    assert r.resolved_targets(db) == closed
    assert r.forecast_snapshot(db, through=seq) == (seq, closed)
