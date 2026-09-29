"""Peewee models for the existing SQLite schema."""

from peewee import AutoField, Model, TextField

__all__ = ["snapshot_model"]


def snapshot_model(database):
    """Bind a fresh model per connection so concurrent calls cannot change it."""

    class Snapshot(Model):
        """One complete collection in the existing on-disk format."""

        id = AutoField()
        collected_at = TextField()
        payload = TextField()

        class Meta:
            table_name = "snapshots"

    # Each connection gets its own model, so concurrent calls cannot rebind it.
    Snapshot.bind(database)
    Snapshot.add_index(Snapshot.collected_at, name="snapshots_time")
    return Snapshot
