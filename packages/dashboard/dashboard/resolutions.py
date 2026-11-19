"""A dashboard's resolver contract and append-only outcome history (one DB per dashboard)."""
from collections.abc import Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import NamedTuple, Protocol


class Target(NamedTuple):
    question: str
    horizon: str
    version: int


@dataclass(frozen=True)
class Request:
    target: Target
    resolver_id: str
    rule: dict
    params: dict


@dataclass(frozen=True)
class Observation:
    target: Target
    revision: str
    published_at: str | None
    value: float | int | str | bool
    evidence: dict


@dataclass(frozen=True)
class RefreshBatch:
    observations: Sequence[Observation]
    next_checkpoint: dict


class Resolver(Protocol):
    def refresh(self, requests: Sequence[Request], *, as_of: date,
                after: dict | None) -> RefreshBatch: ...


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


def _write(db, target, revision, published, payload, stamp=None):
    db.execute("""INSERT INTO observations(question,horizon,version,revision,published,payload,observed)
                  VALUES (?,?,?,?,?,?,coalesce(?,strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
               (*target, revision, published, _json(payload), stamp))


def _initialize(db):
    if _has(db, "observations"):
        return
    db.execute("""CREATE TABLE observations (
        seq INTEGER PRIMARY KEY, question TEXT NOT NULL, horizon TEXT NOT NULL, version INTEGER NOT NULL,
        revision TEXT NOT NULL, published TEXT, payload TEXT NOT NULL, observed TEXT NOT NULL)""")
    db.execute("CREATE INDEX by_target ON observations(question,horizon,version,revision,seq)")
    db.execute("CREATE TABLE checkpoints (resolver TEXT PRIMARY KEY, generation INTEGER, cursor TEXT)")
    if _has(db, "outcomes"):
        for q, h, v, raw, stamp in db.execute("SELECT * FROM outcomes ORDER BY recorded_at,question,horizon"):
            payload = json.loads(raw)
            original = payload
            while "supersedes" in original:
                original = original["supersedes"]
            _write(db, Target(q, h, v), original["vintage"], payload.get("vintage"), payload, stamp)


def _latest(db, target, revision):
    return db.execute("""SELECT payload,observed FROM observations WHERE
        question=? AND horizon=? AND version=? AND revision=? ORDER BY seq DESC LIMIT 1""",
                      (*target, revision)).fetchone()


def _unpack(row, revision):
    raw, stamp = row
    return {**json.loads(raw), "revision_id": revision, "recorded_at": stamp}


def _publication_time(text):
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        if len(text) != 10:
            raise ValueError("Publication timestamps require a timezone")
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_value(value, published):
    if published is not None:
        _publication_time(published)
    if type(value) not in (int, float, bool, str):
        raise ValueError("Expected a numeric, boolean or categorical outcome")


def _append(db, request, obs):
    _validate_value(obs.value, obs.published_at)
    if not isinstance(obs.revision, str) or not obs.revision:
        raise ValueError("Empty source revision ID")
    known = db.execute("SELECT payload FROM observations WHERE question=? AND horizon=? AND version=? LIMIT 1",
                       request.target).fetchone()
    if known and json.loads(known[0])["rule"] != request.rule:
        raise ValueError(f"Rule changed without a version bump for {request.target}")
    reserved = {"rule", "value", "vintage", "recorded_at", "revision_id", "supersedes", "revisions", "correction_reason"}
    if reserved.intersection(obs.evidence):
        raise ValueError("Evidence contains reserved outcome fields")
    payload = dict(obs.evidence, rule=request.rule, value=obs.value, vintage=obs.published_at)
    prior = _latest(db, request.target, obs.revision)
    if prior:
        stored = json.loads(prior[0])
        while "supersedes" in stored:
            stored = stored["supersedes"]
        original = {k: v for k, v in stored.items() if k not in ("recorded_at", "revision_id")}
        if _json(original) != _json(payload):
            raise ValueError(f"Conflicting evidence for {request.target} revision {obs.revision}")
    else:
        _write(db, request.target, obs.revision, obs.published_at, payload)


def refresh(path, requests: Sequence[Request], resolvers: Mapping[str, Resolver], *, as_of: date):
    """Fetch outside the write transaction. Commit all results and cursors together."""
    groups = {}
    for req in requests:
        if req.rule["version"] != req.target.version:
            raise ValueError("Target and rule versions differ")
        groups.setdefault(req.resolver_id, []).append(req)
    if len({r.target for r in requests}) != len(requests):
        raise ValueError("Duplicate resolution target")
    if not groups:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            _initialize(db)
        staged = []
        for name, group in groups.items():
            row = db.execute("SELECT generation,cursor FROM checkpoints WHERE resolver=?", (name,)).fetchone()
            generation, cursor = row if row else (0, "null")
            batch = resolvers[name].refresh(group, as_of=as_of, after=json.loads(cursor))
            staged.append((name, generation, {r.target: r for r in group}, batch))
        with db:
            db.execute("BEGIN IMMEDIATE")
            for name, generation, targets, batch in staged:
                row = db.execute("SELECT generation FROM checkpoints WHERE resolver=?", (name,)).fetchone()
                if generation != (row[0] if row else 0):
                    raise ValueError("Concurrent resolution refresh; retry with the new checkpoint")
                for obs in batch.observations:
                    _append(db, targets[obs.target], obs)
                db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)",
                           (name, generation + 1, _json(batch.next_checkpoint)))


@contextmanager
def _read_snapshot(path, through):
    if not path or not Path(path).exists():
        if through not in (None, 0):
            raise ValueError("Unknown outcome snapshot")
        yield None, 0
        return
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN")
        if not _has(db, "observations"):
            # Seed sequence IDs on first access so historical views use stable IDs.
            db.rollback()
            db.execute("BEGIN IMMEDIATE")
            _initialize(db)
        head, = db.execute("SELECT coalesce(max(seq),0) FROM observations").fetchone()
        cutoff = head if through is None else through
        if not 0 <= cutoff <= head:
            raise ValueError("Unknown outcome snapshot")
        yield db, cutoff


def history(path, *, through=None):
    """Return (snapshot ID, histories). Each target retains first-ingestion order."""
    with _read_snapshot(path, through) as (db, cutoff):
        if db is None:
            return cutoff, {}
        targets = {}
        for q, h, v, rev, raw, stamp in db.execute("""SELECT question,horizon,version,revision,payload,observed
                FROM observations WHERE seq<=? ORDER BY seq""", (cutoff,)):
            targets.setdefault(Target(q, h, v), {})[rev] = _unpack((raw, stamp), rev)
        return cutoff, {target: list(records.values()) for target, records in targets.items()}


def forecast_snapshot(path=None, *, through=None):
    """Accepted outcomes in this dashboard's ledger, keyed by question, horizon and rule."""
    path = path or os.environ.get("DASHBOARD_RESOLUTIONS_DB")
    with _read_snapshot(path, through) as (db, cutoff):
        if db is None:
            return cutoff, set()
        targets = db.execute("SELECT DISTINCT question,horizon,version FROM observations WHERE seq<=?", (cutoff,))
        return cutoff, {Target(*row) for row in targets}


def resolved_targets(path=None):
    return forecast_snapshot(path)[1]


# Scheduled runners use a distinct exit code so an empty selection is a successful
# skip, while a runner that accidentally produces no artifact still fails validation.
NO_OPEN_TARGETS = 42


def no_open_targets():
    print("No open forecast targets.")
    return NO_OPEN_TARGETS if os.environ.get("DASHBOARD_SCHEDULED_FORECAST") else 0


def _release_order(records):
    if all(record["vintage"] is not None for record in records):
        return sorted(records, key=lambda r: _publication_time(r["vintage"]))
    return records


def select(records, view):
    """Select a view of one target's history, preserving arrival order for ties."""
    if view == "first_observed":
        return records[0] if records else None
    if view == "first_published":
        if records and all(record["vintage"] is not None for record in records):
            return min(records, key=lambda r: _publication_time(r["vintage"]))
        return None
    if view == "latest":
        return _release_order(records)[-1] if records else None
    raise ValueError(f"Unknown outcome view {view}")


def project(histories):
    """Compatibility view for existing chart/export consumers. Keep the first outcome at the top."""
    result = {}
    for target, records in histories.items():
        first = select(records, "first_published") or select(records, "first_observed")
        result[target] = dict(first)
        if len(records) > 1:
            result[target]["revisions"] = _release_order(records)
    return result


def snapshot(path):
    return project(history(path)[1])


def record(path, question, horizon, rule, outcome, *, revision=None):
    """Record one reviewed source release. A later vintage is a separate observation."""
    target = Target(question, horizon, rule["version"])
    obs = Observation(target, revision or outcome["vintage"], outcome["vintage"], outcome["value"],
                      {k: v for k, v in outcome.items() if k not in ("vintage", "value")})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _initialize(db)
        _append(db, Request(target, "reviewed", rule, {}), obs)


def correct(path, question, horizon, rule, outcome, *, previous, reason):
    if not reason.strip() or not previous:
        raise ValueError("A correction requires the previous decision and a reason")
    target = Target(question, horizon, rule["version"])
    revision = previous.get("revision_id", previous["vintage"])
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _initialize(db)
        row = _latest(db, target, revision)
        expected = dict(previous)
        expected.pop("revisions", None)
        expected.setdefault("revision_id", revision)
        if not row or _unpack(row, revision) != expected or expected["rule"] != rule:
            raise ValueError("The prior resolution or its rule changed")
        _validate_value(outcome["value"], outcome["vintage"])
        payload = dict(outcome, rule=rule, supersedes=expected, correction_reason=reason)
        _write(db, target, revision, outcome.get("vintage"), payload)
