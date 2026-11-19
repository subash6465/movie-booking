import copy
import unittest

from movie_monitor.config import generation
from movie_monitor.delivery import deliver
from movie_monitor.migrations import migrate_removed_provider
from movie_monitor.state import State
from tests.support import ConfirmedReadiness
from tests.test_watches import AT, Sender, obs, raw_app, show, watch


class ProviderRemovalMigrationTests(unittest.TestCase):
    def test_retained_bookmyshow_presence_moves_with_its_watch(self):
        current = watch()
        previous = copy.deepcopy(current)
        previous["providers"]["ticketnew"] = {"content_id": "176735"}
        state = State(":memory:")
        try:
            state.ingest(
                generation(previous), previous, obs("bookmyshow", [show("bookmyshow")]), (), AT
            )
            with state.db:
                moved = migrate_removed_provider(state.db, previous, current, "ticketnew")
            self.assertEqual(moved["rows_moved"]["v2_bms_presence"], 1)
            self.assertEqual(
                state.db.execute("SELECT watch FROM v2_bms_presence").fetchone()[0],
                generation(current),
            )
        finally:
            state.close()

    def test_removal_preserves_silent_baselines_receipts_and_new_opening_detection(self):
        current = watch()
        previous = copy.deepcopy(current)
        previous["providers"]["ticketnew"] = {"content_id": "176735", "slug": "avengers-doomsday"}
        old_key, new_key = generation(previous), generation(current)
        state = State(":memory:")
        sender = Sender()
        try:
            state.ingest(old_key, previous, obs(shows=[show()]), (), AT)
            state.ingest(
                old_key, previous, obs(shows=[show(), show(sid="2", clock="19:00")]), (), AT
            )
            self.assertEqual(
                deliver(
                    state,
                    sender,
                    {old_key},
                    ["-1"],
                    raw_app(),
                    readiness=ConfirmedReadiness(),
                    at=AT,
                )["sent"],
                1,
            )
            before = state.status()
            with state.db:
                migrate_removed_provider(state.db, previous, current, "ticketnew")
            self.assertEqual(state.status(), before)
            self.assertEqual(
                deliver(
                    state,
                    sender,
                    {new_key},
                    ["-1"],
                    raw_app(),
                    readiness=ConfirmedReadiness(),
                    at=AT,
                )["sent"],
                0,
            )
            result = state.ingest(
                new_key,
                current,
                obs(shows=[show(), show(sid="2", clock="19:00"), show(sid="3", clock="20:00")]),
                (),
                AT,
            )
            self.assertFalse(result["baseline"])
            self.assertEqual(
                deliver(
                    state,
                    sender,
                    {new_key},
                    ["-1"],
                    raw_app(),
                    readiness=ConfirmedReadiness(),
                    at=AT,
                )["sent"],
                1,
            )
        finally:
            state.close()

    def test_removal_cannot_silently_change_dates_or_overwrite_existing_history(self):
        current = watch()
        previous = copy.deepcopy(current)
        previous["providers"]["ticketnew"] = {"content_id": "176735"}
        state = State(":memory:")
        try:
            changed = {**current, "languages": ["Tamil"]}
            with state.db, self.assertRaises(ValueError):
                migrate_removed_provider(state.db, previous, changed, "ticketnew")
            for cfg in (previous, current):
                state.ingest(generation(cfg), cfg, obs(), (), AT)
            with state.db, self.assertRaises(RuntimeError):
                migrate_removed_provider(state.db, previous, current, "ticketnew")
        finally:
            state.close()
