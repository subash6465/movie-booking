"""Transactional startup baselines, cross-provider identity and delivery outbox.

These v2 tables intentionally do not replay the old diagnostic outbox.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .core import UTC, now
from .identity import (
    explicit_theatre,
    performance_key,
    performance_key_values,
    preference_matches,
    theatre_name_key,
    validate_show,
)


class State:
    def __init__(self, path, *, test_mode=False):
        self.test_mode = test_mode
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(
                "SQLite integrity check failed; refusing to discard deduplication history"
            )
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS v2_scopes(
            watch TEXT, scope TEXT, initialized INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(watch,scope));
          CREATE TABLE IF NOT EXISTS v2_venues(
            city TEXT, provider TEXT, venue TEXT, name_key TEXT, canonical TEXT, name TEXT,
            PRIMARY KEY(city,provider,venue));
          CREATE TABLE IF NOT EXISTS v2_performances(
            key TEXT PRIMARY KEY, movie TEXT, city TEXT, theatre TEXT, theatre_name TEXT,
            starts_at TEXT, first_seen TEXT, first_available TEXT, baseline INTEGER NOT NULL,
            retired INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS v2_offers(
            watch TEXT, scope TEXT, provider TEXT, show_key TEXT, performance TEXT,
            observed_at TEXT, payload TEXT, valid INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(watch,provider,show_key));
          CREATE INDEX IF NOT EXISTS v2_offer_performance ON v2_offers(performance,valid,observed_at);
          CREATE TABLE IF NOT EXISTS v2_deliveries(
            id INTEGER PRIMARY KEY, performance TEXT, target TEXT, state TEXT NOT NULL,
            created_at TEXT, next_attempt TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            message_id TEXT, error TEXT, UNIQUE(performance,target));
          CREATE TABLE IF NOT EXISTS v2_observations(
            id INTEGER PRIMARY KEY, watch TEXT, scope TEXT, observed_at TEXT, status TEXT, payload TEXT);
          CREATE INDEX IF NOT EXISTS v2_observation_age ON v2_observations(observed_at);
          CREATE TABLE IF NOT EXISTS v2_scope_fingerprints(
            watch TEXT, scope TEXT, digest TEXT, last_logged TEXT, PRIMARY KEY(watch,scope));
          CREATE TABLE IF NOT EXISTS v2_scope_birth(
            watch TEXT, scope TEXT, suppress_initial INTEGER NOT NULL, PRIMARY KEY(watch,scope));
          CREATE TABLE IF NOT EXISTS v2_verified_snapshots(
            watch TEXT, scope TEXT, observed_at TEXT, performance_keys TEXT, PRIMARY KEY(watch,scope));
          CREATE TABLE IF NOT EXISTS v2_opening_evidence(
            performance TEXT PRIMARY KEY, observed_at TEXT, test_mode INTEGER NOT NULL, payload TEXT NOT NULL);
        """)
        # A crash after the POST could leave an accepted message unacknowledged.
        # Automatic resend would violate the no-duplicate requirement.
        with self.db:
            self.db.execute(
                "UPDATE v2_deliveries SET state='UNKNOWN',error='Process stopped during delivery; manual review needed' WHERE state='SENDING'"
            )

    def _theatre(self, show, aliases):
        alias = explicit_theatre(show, aliases)
        previous = self.db.execute(
            "SELECT * FROM v2_venues WHERE city=? AND provider=? AND venue=?",
            (show.city, show.provider, show.cinema_id),
        ).fetchone()
        name_key = theatre_name_key(show.cinema, show.city)
        if alias:
            canonical, name = alias
            if previous and previous["canonical"] != canonical:
                self._retarget_theatre(show.city, previous["canonical"], canonical, name)
        elif previous:
            return previous["canonical"], previous["name"]
        else:
            candidates = self.db.execute(
                "SELECT * FROM v2_venues WHERE city=? AND name_key=?", (show.city, name_key)
            ).fetchall()
            # Identical names for two different venue IDs from one provider are
            # ambiguous; never silently merge them with a fuzzy name guess.
            if any(
                r["provider"] == show.provider and r["venue"] != show.cinema_id for r in candidates
            ):
                raise ValueError("Ambiguous theatre identity; add a verified theatre_aliases entry")
            canonical = candidates[0]["canonical"] if candidates else "name:" + name_key
            name = candidates[0]["name"] if candidates else show.cinema
        self.db.execute(
            "INSERT INTO v2_venues VALUES(?,?,?,?,?,?) ON CONFLICT(city,provider,venue) DO UPDATE SET name_key=excluded.name_key,canonical=excluded.canonical,name=excluded.name",
            (show.city, show.provider, show.cinema_id, name_key, canonical, name),
        )
        return canonical, name

    def _retarget_theatre(self, city, old_theatre, theatre, name):
        for prior in self.db.execute(
            "SELECT * FROM v2_performances WHERE city=? AND theatre=? AND retired=0",
            (city, old_theatre),
        ).fetchall():
            key = performance_key_values(prior["movie"], city, theatre, prior["starts_at"])
            self.db.execute(
                """INSERT OR IGNORE INTO v2_performances
              (key,movie,city,theatre,theatre_name,starts_at,first_seen,first_available,baseline)
              VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    key,
                    prior["movie"],
                    city,
                    theatre,
                    name,
                    prior["starts_at"],
                    prior["first_seen"],
                    prior["first_available"],
                    prior["baseline"],
                ),
            )
            self._merge(prior["key"], key)
        self.db.execute(
            "UPDATE v2_venues SET canonical=?,name=? WHERE city=? AND canonical=?",
            (theatre, name, city, old_theatre),
        )

    def _merge(self, old, new):
        if old == new:
            return
        prior = self.db.execute("SELECT * FROM v2_performances WHERE key=?", (old,)).fetchone()
        if not prior:
            return
        self.db.execute(
            "UPDATE v2_performances SET baseline=MAX(baseline,?), first_seen=MIN(first_seen,?) WHERE key=?",
            (prior["baseline"], prior["first_seen"], new),
        )
        if prior["first_available"]:
            self.db.execute(
                "UPDATE v2_performances SET first_available=CASE WHEN first_available IS NULL OR first_available>? THEN ? ELSE first_available END WHERE key=?",
                (prior["first_available"], prior["first_available"], new),
            )
        for delivery in self.db.execute(
            "SELECT * FROM v2_deliveries WHERE performance=?", (old,)
        ).fetchall():
            existing = self.db.execute(
                "SELECT * FROM v2_deliveries WHERE performance=? AND target=?",
                (new, delivery["target"]),
            ).fetchone()
            if existing is None:
                self.db.execute(
                    "UPDATE v2_deliveries SET performance=? WHERE id=?", (new, delivery["id"])
                )
            elif (
                delivery["state"] in ("SENT", "SENDING", "UNKNOWN") and existing["state"] != "SENT"
            ):
                self.db.execute(
                    "UPDATE v2_deliveries SET state=?,message_id=?,error=? WHERE id=?",
                    (
                        "SENT" if delivery["state"] == "SENT" else "UNKNOWN",
                        delivery["message_id"],
                        delivery["error"],
                        existing["id"],
                    ),
                )
        self.db.execute("UPDATE v2_offers SET performance=? WHERE performance=?", (new, old))
        evidence = self.db.execute(
            "SELECT * FROM v2_opening_evidence WHERE performance=?", (old,)
        ).fetchone()
        if evidence:
            self.db.execute(
                "INSERT OR IGNORE INTO v2_opening_evidence VALUES(?,?,?,?)",
                (new, evidence["observed_at"], evidence["test_mode"], evidence["payload"]),
            )
        self.db.execute("UPDATE v2_performances SET retired=1 WHERE key=?", (old,))

    def ingest(self, watch_key, watch, observation, aliases=(), at=None, freshness=180):
        at = (at or now()).astimezone(UTC)
        scope = "|".join((observation.provider, observation.requested_date, observation.scope_id))
        good = observation.complete and observation.status in ("OK", "NO_SHOWS", "WAITING")
        issues = []
        validated = []
        try:
            stamp = datetime.fromisoformat(observation.observed_at)
            if stamp.tzinfo is None or not -30 <= (at - stamp).total_seconds() <= freshness:
                raise ValueError("Stale or future observation timestamp")
            stamp = stamp.astimezone(UTC)
            if observation.status in ("NO_SHOWS", "WAITING") and observation.shows:
                raise ValueError("Empty-snapshot status contains shows")
            if good:
                for show in observation.shows:
                    if show.provider != observation.provider:
                        raise ValueError("Observation contains another provider")
                    validate_show(show, watch, observation.requested_date)
                    if preference_matches(show, watch, aliases, at):
                        validated.append(show)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            good = False
            issues.append(str(exc))
        with self.db:
            prior = self.db.execute(
                "SELECT initialized FROM v2_scopes WHERE watch=? AND scope=?", (watch_key, scope)
            ).fetchone()
            baseline = not prior or not prior["initialized"]
            if (
                not prior
                and observation.provider == "bookmyshow"
                and observation.scope_id != "discovery"
            ):
                marker = self.db.execute(
                    "SELECT initialized FROM v2_scopes WHERE watch=? AND scope=?",
                    (watch_key, "|".join(("bookmyshow", observation.requested_date, "discovery"))),
                ).fetchone()
                if marker and marker["initialized"]:
                    baseline = False
            birth = self.db.execute(
                "SELECT suppress_initial FROM v2_scope_birth WHERE watch=? AND scope=?",
                (watch_key, scope),
            ).fetchone()
            if birth is None:
                self.db.execute(
                    "INSERT INTO v2_scope_birth VALUES(?,?,?)", (watch_key, scope, int(baseline))
                )
            elif not prior or not prior["initialized"]:
                baseline = bool(birth["suppress_initial"])
            self.db.execute(
                "UPDATE v2_offers SET valid=0 WHERE watch=? AND scope=?", (watch_key, scope)
            )
            if good:
                previous_snapshot = self.db.execute(
                    "SELECT * FROM v2_verified_snapshots WHERE watch=? AND scope=?",
                    (watch_key, scope),
                ).fetchone()
                current_keys = []
                for show in validated:
                    theatre, name = self._theatre(show, aliases)
                    key = performance_key(show, theatre)
                    current_keys.append(key)
                    existing_performance = self.db.execute(
                        "SELECT * FROM v2_performances WHERE key=?", (key,)
                    ).fetchone()
                    previous = self.db.execute(
                        "SELECT performance FROM v2_offers WHERE watch=? AND provider=? AND show_key=?",
                        (watch_key, show.provider, show.key),
                    ).fetchone()
                    self.db.execute(
                        """INSERT OR IGNORE INTO v2_performances
                      (key,movie,city,theatre,theatre_name,starts_at,first_seen,first_available,baseline)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            key,
                            show.movie,
                            show.city,
                            theatre,
                            name,
                            datetime.fromisoformat(show.starts_at).astimezone(UTC).isoformat(),
                            stamp.isoformat(),
                            stamp.isoformat()
                            if show.availability == "AVAILABLE" and not baseline
                            else None,
                            int(baseline),
                        ),
                    )
                    if previous:
                        self._merge(previous["performance"], key)
                    effective = self.db.execute(
                        "SELECT baseline FROM v2_performances WHERE key=?", (key,)
                    ).fetchone()
                    if (
                        show.availability == "AVAILABLE"
                        and not effective["baseline"]
                        and (
                            not existing_performance or not existing_performance["first_available"]
                        )
                    ):
                        evidence = {
                            "kind": "NEW_LISTING_AFTER_BASELINE"
                            if not existing_performance
                            else "FIRST_BOOKABILITY_OF_POST_BASELINE_LISTING",
                            "test_mode": self.test_mode,
                            "watch": watch_key,
                            "scope": scope,
                            "scope_previously_initialized": bool(prior and prior["initialized"]),
                            "new_cinema_after_discovery_baseline": not prior
                            and not baseline
                            and observation.provider == "bookmyshow",
                            "previous_snapshot": dict(previous_snapshot)
                            if previous_snapshot
                            else None,
                            "first_verified_at": stamp.isoformat(),
                            "show": show.to_dict(),
                            "validation": "Collector identity/date/stock checks and watch matching passed",
                            "independent_checkout_verified": False,
                        }
                        self.db.execute(
                            "INSERT OR IGNORE INTO v2_opening_evidence VALUES(?,?,?,?)",
                            (
                                key,
                                stamp.isoformat(),
                                int(self.test_mode),
                                json.dumps(evidence, ensure_ascii=False),
                            ),
                        )
                    if show.availability == "AVAILABLE":
                        self.db.execute(
                            "UPDATE v2_performances SET first_available=COALESCE(first_available,?) WHERE key=?",
                            (stamp.isoformat(), key),
                        )
                    self.db.execute(
                        """INSERT INTO v2_offers VALUES(?,?,?,?,?,?,?,1)
                      ON CONFLICT(watch,provider,show_key) DO UPDATE SET scope=excluded.scope,performance=excluded.performance,
                      observed_at=excluded.observed_at,payload=excluded.payload,valid=1""",
                        (
                            watch_key,
                            scope,
                            show.provider,
                            show.key,
                            key,
                            stamp.isoformat(),
                            json.dumps(show.to_dict(), ensure_ascii=False),
                        ),
                    )
                self.db.execute(
                    "DELETE FROM v2_offers WHERE watch=? AND scope=? AND valid=0",
                    (watch_key, scope),
                )
                self.db.execute(
                    "INSERT INTO v2_verified_snapshots VALUES(?,?,?,?) ON CONFLICT(watch,scope) DO UPDATE SET observed_at=excluded.observed_at,performance_keys=excluded.performance_keys",
                    (watch_key, scope, stamp.isoformat(), json.dumps(sorted(set(current_keys)))),
                )
            summary = observation.to_dict()
            summary.pop("shows")
            summary.update(
                show_count=len(observation.shows),
                accepted=len(validated) if good else 0,
                startup_baseline=bool(baseline and good),
                validation_errors=issues,
            )
            status = observation.status if not issues else "REJECTED"
            self.db.execute(
                "INSERT INTO v2_scopes VALUES(?,?,?,?,?) ON CONFLICT(watch,scope) DO UPDATE SET initialized=MAX(initialized,excluded.initialized),status=excluded.status,observed_at=excluded.observed_at",
                (watch_key, scope, int(good), status, at.isoformat()),
            )
            digest = hashlib.sha256(
                json.dumps(
                    [
                        status,
                        good,
                        [(s.key, s.availability, s.language, s.format) for s in validated],
                        observation.warnings,
                        observation.error,
                        issues,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            last = self.db.execute(
                "SELECT * FROM v2_scope_fingerprints WHERE watch=? AND scope=?", (watch_key, scope)
            ).fetchone()
            if (
                not last
                or last["digest"] != digest
                or (at - datetime.fromisoformat(last["last_logged"])).total_seconds() >= 3600
            ):
                self.db.execute(
                    "INSERT INTO v2_observations(watch,scope,observed_at,status,payload) VALUES(?,?,?,?,?)",
                    (
                        watch_key,
                        scope,
                        at.isoformat(),
                        status,
                        json.dumps(summary, ensure_ascii=False),
                    ),
                )
                self.db.execute(
                    "INSERT INTO v2_scope_fingerprints VALUES(?,?,?,?) ON CONFLICT(watch,scope) DO UPDATE SET digest=excluded.digest,last_logged=excluded.last_logged",
                    (watch_key, scope, digest, at.isoformat()),
                )
        return {
            "scope": scope,
            "status": status,
            "accepted": len(validated) if good else 0,
            "baseline": bool(baseline and good),
            "errors": issues,
            "complete": good,
        }

    def fresh_offers(self, key, active, freshness, at):
        at = at.astimezone(UTC)
        rows = self.db.execute(
            "SELECT * FROM v2_offers WHERE performance=? AND valid=1 AND observed_at>=? ORDER BY observed_at DESC, provider, show_key",
            (key, (at - timedelta(seconds=freshness)).isoformat()),
        ).fetchall()
        result, identities = {}, {}
        for row in rows:
            if row["watch"] not in active:
                continue
            show = json.loads(row["payload"])
            if (
                show["availability"] == "AVAILABLE"
                and datetime.fromisoformat(show["starts_at"]) > at
            ):
                provider = show["provider"]
                identities.setdefault(provider, set()).add(
                    (show["session_id"], show.get("screen", ""), show["language"], show["format"])
                )
                show["observed_at"] = row["observed_at"]
                result.setdefault(provider, show)
        for provider, variants in identities.items():
            if len(variants) > 1:
                # One alert may deliberately merge rooms/prints at the same time.
                # Never attach one arbitrary room's inventory to that performance.
                result[provider]["details"] = {}
        return list(result.values())

    def prepare(self, active, targets, freshness, aggregation, at=None):
        at = (at or now()).astimezone(UTC)
        targets = tuple(sorted(set(targets)))
        if not targets:
            return
        placeholders = ",".join("?" for _ in targets)
        with self.db:
            candidates = self.db.execute(
                f"""SELECT p.* FROM v2_performances p
                WHERE p.baseline=0 AND p.retired=0 AND p.first_available IS NOT NULL
                AND (SELECT COUNT(*) FROM v2_deliveries d
                     WHERE d.performance=p.key AND d.target IN ({placeholders})) < ?""",
                (*targets, len(targets)),
            ).fetchall()
            for perf in candidates:
                if (
                    datetime.fromisoformat(perf["starts_at"]) <= at
                    or (at - datetime.fromisoformat(perf["first_available"])).total_seconds()
                    < aggregation
                ):
                    continue
                if self.fresh_offers(perf["key"], active, freshness, at):
                    for target in targets:
                        self.db.execute(
                            "INSERT OR IGNORE INTO v2_deliveries(performance,target,state,created_at,next_attempt) VALUES(?,?,'PENDING',?,?)",
                            (perf["key"], target, at.isoformat(), at.isoformat()),
                        )

    def due(self, active, freshness, at=None):
        at = (at or now()).astimezone(UTC)
        result = []
        rows = self.db.execute(
            "SELECT d.*,p.movie,p.city,p.theatre_name,p.starts_at,p.first_available,p.baseline,p.retired FROM v2_deliveries d JOIN v2_performances p ON p.key=d.performance WHERE d.state='PENDING' AND d.next_attempt<=? ORDER BY d.id",
            (at.isoformat(),),
        ).fetchall()
        for row in rows:
            offers = self.fresh_offers(row["performance"], active, freshness, at)
            if offers and not row["baseline"] and not row["retired"]:
                result.append({**dict(row), "offers": offers})
        return result

    def claim(self, delivery_id):
        with self.db:
            return (
                self.db.execute(
                    "UPDATE v2_deliveries SET state='SENDING',attempts=attempts+1 WHERE id=? AND state='PENDING'",
                    (delivery_id,),
                ).rowcount
                == 1
            )

    def delivered(self, delivery_id, message_id):
        with self.db:
            self.db.execute(
                "UPDATE v2_deliveries SET state='SENT',message_id=?,error=NULL WHERE id=?",
                (message_id, delivery_id),
            )

    def delivery_failed(
        self, delivery_id, error, *, ambiguous=False, permanent=False, retry_after=60, at=None
    ):
        at = (at or now()).astimezone(UTC)
        state = "UNKNOWN" if ambiguous else "FAILED" if permanent else "PENDING"
        with self.db:
            self.db.execute(
                "UPDATE v2_deliveries SET state=?,error=?,next_attempt=? WHERE id=?",
                (state, error, (at + timedelta(seconds=retry_after)).isoformat(), delivery_id),
            )

    def status(self):
        return {
            "delivery_states": dict(
                self.db.execute(
                    "SELECT state,COUNT(*) FROM v2_deliveries GROUP BY state"
                ).fetchall()
            ),
            "performances": self.db.execute(
                "SELECT COUNT(*) FROM v2_performances WHERE retired=0"
            ).fetchone()[0],
            "baselined_performances": self.db.execute(
                "SELECT COUNT(*) FROM v2_performances WHERE baseline=1 AND retired=0"
            ).fetchone()[0],
            "name_based_theatres": [
                dict(r)
                for r in self.db.execute(
                    "SELECT city,provider,venue,name FROM v2_venues WHERE canonical LIKE 'name:%' ORDER BY city,provider,venue LIMIT 100"
                ).fetchall()
            ],
        }

    def invalidate_provider(self, watch, provider):
        with self.db:
            self.db.execute(
                "UPDATE v2_offers SET valid=0 WHERE watch=? AND provider=?", (watch, provider)
            )

    def prune(self, days, at=None):
        at = (at or now()).astimezone(UTC)
        with self.db:
            self.db.execute(
                "DELETE FROM v2_observations WHERE observed_at<?",
                ((at - timedelta(days=days)).isoformat(),),
            )
            self.db.execute(
                "UPDATE v2_deliveries SET state='EXPIRED' WHERE state='PENDING' AND performance IN (SELECT key FROM v2_performances WHERE starts_at<?)",
                (at.isoformat(),),
            )
            cutoff = (at - timedelta(days=90)).isoformat()
            self.db.execute(
                "DELETE FROM v2_offers WHERE performance IN (SELECT key FROM v2_performances WHERE starts_at<?)",
                (cutoff,),
            )
            self.db.execute(
                "DELETE FROM v2_deliveries WHERE performance IN (SELECT key FROM v2_performances WHERE starts_at<?)",
                (cutoff,),
            )
            self.db.execute(
                "DELETE FROM v2_opening_evidence WHERE performance IN (SELECT key FROM v2_performances WHERE starts_at<?)",
                (cutoff,),
            )
            self.db.execute("DELETE FROM v2_performances WHERE starts_at<?", (cutoff,))
            date_cutoff = (at - timedelta(days=90)).date().isoformat()
            self.db.execute(
                "DELETE FROM v2_scopes WHERE substr(scope,instr(scope,'|')+1,10)<?", (date_cutoff,)
            )
            for table in ("v2_scope_fingerprints", "v2_scope_birth", "v2_verified_snapshots"):
                self.db.execute(
                    f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM v2_scopes s WHERE s.watch={table}.watch AND s.scope={table}.scope)"
                )

    def close(self):
        self.db.close()
