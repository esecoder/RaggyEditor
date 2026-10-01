"""recovery: snapshots of unsaved work, and the rules for offering them back.

⚠️ The dangerous property is not "does it save" — it is "does it ever restore
something STALE". A snapshot older than the file on disk means the user saved and
then the app died; offering it would silently roll their document backwards. That
is a data-loss bug wearing a safety net's clothes, so it is tested explicitly.
"""

import os
import pathlib
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy import recovery  # noqa: E402


class RecoveryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {"RAGGY_RECOVERY_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _snap(self, text="half-written paragraph", **kw):
        return recovery.Snapshot(slot=kw.pop("slot", "slot-a"), text=text, **kw)


class TestSlotIdentity(RecoveryTest):

    def test_the_same_path_always_maps_to_the_same_slot(self):
        a = recovery.slot_id("/tmp/notes.txt")
        self.assertEqual(a, recovery.slot_id("/tmp/notes.txt"))

    def test_different_paths_get_different_slots(self):
        self.assertNotEqual(recovery.slot_id("/tmp/a.txt"),
                            recovery.slot_id("/tmp/b.txt"))

    def test_unsaved_documents_do_not_collide(self):
        # Two untitled windows must not overwrite each other's snapshot.
        self.assertNotEqual(recovery.slot_id(None, "session1"),
                            recovery.slot_id(None, "session2"))

    def test_slot_is_a_safe_filename(self):
        slot = recovery.slot_id("/tmp/a file with spaces/and:colons.txt")
        self.assertTrue(slot.isalnum(), slot)


class TestSaveLoadClear(RecoveryTest):

    def test_save_then_load_round_trips_everything(self):
        snap = self._snap(path="/tmp/notes.txt", encoding="latin-1",
                          newline="\r\n", bom=b"")
        recovery.save(snap)
        back = recovery.load(snap.slot)
        self.assertEqual(back.text, snap.text)
        self.assertEqual(back.path, "/tmp/notes.txt")
        self.assertEqual(back.text_format().newline, "\r\n")
        self.assertEqual(back.text_format().encoding, "latin-1")

    def test_the_snapshot_file_is_not_world_readable(self):
        # ⚠️ It contains the user's document in plain text.
        recovery.save(self._snap())
        p = pathlib.Path(tempfile.mkdtemp())      # placeholder, real check below
        files = list(pathlib.Path(self.tmp.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)

    def test_clear_removes_it(self):
        snap = self._snap()
        recovery.save(snap)
        recovery.clear(snap.slot)
        self.assertIsNone(recovery.load(snap.slot))

    def test_clear_on_a_missing_slot_is_harmless(self):
        recovery.clear("never-existed")           # must not raise

    def test_a_corrupt_snapshot_is_ignored_rather_than_crashing(self):
        pathlib.Path(self.tmp.name, "broken" + recovery.SUFFIX).write_text(
            "{not json", encoding="utf-8")
        self.assertEqual(recovery.list_all(), [])

    def test_saving_twice_leaves_no_partial_file(self):
        snap = self._snap()
        recovery.save(snap)
        recovery.save(self._snap(text="second version"))
        parts = list(pathlib.Path(self.tmp.name).glob("*.part"))
        self.assertEqual(parts, [])
        self.assertEqual(recovery.load(snap.slot).text, "second version")


class TestPending(RecoveryTest):
    """What is worth offering on the next launch — and what is not."""

    def test_a_snapshot_newer_than_the_file_is_offered(self):
        doc = pathlib.Path(self.tmp.name, "doc.txt")
        doc.write_text("saved version", encoding="utf-8")
        os.utime(doc, (time.time() - 100, time.time() - 100))
        recovery.save(self._snap(text="newer unsaved work", path=str(doc)))
        self.assertEqual(len(recovery.pending()), 1)

    def test_a_stale_snapshot_is_NOT_offered(self):
        # ⚠️ The important one: the user saved AFTER this snapshot was taken, so
        # restoring it would throw away their work.
        doc = pathlib.Path(self.tmp.name, "doc.txt")
        doc.write_text("saved version", encoding="utf-8")
        snap = self._snap(text="stale", path=str(doc))
        snap.taken_at = time.time() - 100
        recovery.save(snap)
        os.utime(doc, (time.time(), time.time()))
        self.assertEqual(recovery.pending(), [])

    def test_an_untitled_document_is_ALWAYS_offered(self):
        # ⚠️ The case recovery exists for: text that was never saved anywhere,
        # so there is no file to compare it against and nothing else holds a copy.
        # Writing the check as `if snap.path and ...` dropped exactly this.
        recovery.save(self._snap(text="never saved anywhere", path=None))
        pending = recovery.pending()
        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0].path)

    def test_a_snapshot_for_a_deleted_file_is_offered(self):
        recovery.save(self._snap(path=str(pathlib.Path(self.tmp.name, "gone.txt"))))
        self.assertEqual(len(recovery.pending()), 1)

    def test_an_empty_snapshot_is_not_offered(self):
        # Offering to restore an empty document is noise, not a rescue.
        recovery.save(self._snap(text="   \n  "))
        self.assertEqual(recovery.pending(), [])

    def test_pending_is_newest_first(self):
        a = self._snap(slot="a", path=str(pathlib.Path(self.tmp.name, "a.txt")))
        b = self._snap(slot="b", path=str(pathlib.Path(self.tmp.name, "b.txt")))
        a.taken_at = time.time() - 50
        b.taken_at = time.time()
        recovery.save(a)
        recovery.save(b)
        self.assertEqual([s.slot for s in recovery.pending()], ["b", "a"])

    def test_no_recovery_directory_is_not_an_error(self):
        with mock.patch.dict(os.environ,
                             {"RAGGY_RECOVERY_DIR": self.tmp.name + "/nope/deep"}):
            self.assertEqual(recovery.pending(), [])

    def test_clear_all_empties_everything(self):
        recovery.save(self._snap(slot="a", path="/tmp/a.txt"))
        recovery.save(self._snap(slot="b", path="/tmp/b.txt"))
        recovery.clear_all()
        self.assertEqual(recovery.list_all(), [])


if __name__ == "__main__":
    unittest.main()
