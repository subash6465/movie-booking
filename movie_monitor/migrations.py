"""Explicit, transactional state migrations; never run against an active monitor."""

import copy
import sqlite3

from .config import generation


def migrate_removed_provider(
    db: sqlite3.Connection, previous: dict, current: dict, removed: str
) -> dict:
    """Preserve retained scopes when only one provider is removed from a watch.

    The caller must stop the monitor, hold its database lock and wrap this call
    in a transaction. Performance keys, baseline flags, delivery receipts and
    removed-provider history are not changed. Conflicting destination history
    requires review instead of silently overwriting it.
    """
    expected = copy.deepcopy(previous)
    if removed not in expected.get("providers", {}):
        raise ValueError("The previous watch does not contain the removed provider")
    del expected["providers"][removed]
    if expected != current or not current["providers"]:
        raise ValueError("Migration requires exactly a provider removal with unchanged preferences")
    before, after = generation(previous), generation(current)
    providers = tuple(sorted(current["providers"]))
    placeholders = ",".join("?" for _ in providers)
    tables = (
        "v2_scopes",
        "v2_scope_fingerprints",
        "v2_scope_birth",
        "v2_verified_snapshots",
        "v2_observations",
        "v2_offers",
    )
    if (
        "bookmyshow" in providers
        and db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='v2_bms_presence'"
        ).fetchone()
    ):
        tables += ("v2_bms_presence",)
    plans = []
    for table in tables:
        field = "provider" if table == "v2_offers" else "substr(scope,1,instr(scope,'|')-1)"
        predicate = (
            "watch=?" if table == "v2_bms_presence" else f"watch=? AND {field} IN ({placeholders})"
        )
        suffix = () if table == "v2_bms_presence" else providers
        source = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {predicate}", (before, *suffix)
        ).fetchone()[0]
        destination = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {predicate}", (after, *suffix)
        ).fetchone()[0]
        if source and destination:
            raise RuntimeError("Destination watch history already exists; manual review required")
        plans.append((table, predicate, source, suffix))
    for table, predicate, source, suffix in plans:
        if source:
            db.execute(f"UPDATE {table} SET watch=? WHERE {predicate}", (after, before, *suffix))
    return {
        "previous_generation": before,
        "generation": after,
        "rows_moved": {t: n for t, _, n, _ in plans},
    }
