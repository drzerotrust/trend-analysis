"""Persist snapshots and read legacy history using Peewee model queries."""

import json
from datetime import UTC, datetime

from peewee import SqliteDatabase, fn
from playhouse.reflection import generate_models

from models import snapshot_model

__all__ = ["save_snapshot", "load_snapshots"]


def save_snapshot(path, snapshot):
    # Keep the existing JSON format; Peewee handles the table and transaction.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    database_path = str(path)
    database = SqliteDatabase(database_path)
    Snapshot = snapshot_model(database)

    with database:
        database.create_tables([Snapshot])
        row = Snapshot.create(collected_at=snapshot["collected_at"], payload=payload)
        return row.id


def load_snapshots(path, since, until):
    """Reading a report never creates, migrates, or changes a database."""
    if not path.exists():
        return []

    # Read-only mode makes it impossible for report generation to alter history.
    database_path = path.resolve()
    database_uri = database_path.as_uri() + "?mode=ro"
    database = SqliteDatabase(database_uri, uri=True)
    Snapshot = snapshot_model(database)

    with database:
        tables = database.get_tables()
        snapshots = []
        if "snapshots" in tables:
            # Select the time window first, then decode each stored JSON snapshot.
            window_start = since.isoformat()
            window_end = until.isoformat()
            in_window = Snapshot.collected_at.between(window_start, window_end)
            query = Snapshot.select(Snapshot.payload)
            query = query.where(in_window)
            query = query.order_by(Snapshot.collected_at, Snapshot.id)
            for row in query:
                snapshot = json.loads(row.payload)
                snapshots.append(snapshot)

        if "collection_runs" in tables:
            legacy = _legacy_snapshots(database, since, until)
            snapshots.extend(legacy)

    snapshots.sort(key=_snapshot_order)
    return snapshots


def _snapshot_order(snapshot):
    return snapshot["collected_at"]


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    parsed = parsed.astimezone(UTC)
    return parsed.isoformat()


def _legacy_snapshots(database, since, until):
    """Compatibility only: preserve old tables and label their original semantics."""
    # Reflection keeps this read-only adapter compatible without duplicating the
    # old schema. Bare fields preserve original SQLite timestamp strings.
    models = generate_models(
        database,
        table_names=[
            "collection_runs",
            "observations",
            "source_health",
        ],
        literal_column_names=True,
        bare_fields=True,
    )
    Run = models["collection_runs"]

    # SQLite compares the old timestamp formats using their Julian dates.
    window_start = since.isoformat()
    window_end = until.isoformat()
    start_day = fn.julianday(window_start)
    end_day = fn.julianday(window_end)
    run_day = fn.julianday(Run.started_at)
    in_window = run_day.between(start_day, end_day)
    runs = Run.select(Run.id, Run.started_at)
    runs = runs.where(in_window)
    runs = runs.order_by(Run.id)
    runs = runs.dicts()

    result = []
    for run in runs:
        collected_at = _timestamp(run["started_at"])
        snapshot = {"collected_at": collected_at, "legacy": True}
        for table, key in (
            ("observations", "observations"),
            ("source_health", "health"),
        ):
            model = models[table]
            snapshot[key] = _legacy_records(model, run["id"])

        result.append(snapshot)

    return result


def _legacy_records(model, run_id):
    query = model.select()
    query = query.where(model.run_id == run_id)
    query = query.dicts()

    # Adapt old columns in memory; the original tables are never changed.
    records = []
    for record in query:
        del record["id"]
        del record["run_id"]
        if "raw_json" in record:
            raw_json = record.pop("raw_json")
            record["raw"] = json.loads(raw_json)

        for field in ("observed_at", "checked_at"):
            if record.get(field):
                record[field] = _timestamp(record[field])

        records.append(record)

    return records
