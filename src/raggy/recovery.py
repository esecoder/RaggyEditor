"""
recovery.py — snapshots of unsaved work, so a crash does not cost the user a file.

===============================================================================
WHAT THIS IS FOR
===============================================================================
Until now the only copy of an unsaved document lived in the editor's memory. A
crash, a force-quit, a flat battery or a stray `kill` lost everything since the
last manual save. TextEdit autosaves; a text editor that can lose your work is
not one people will trust with it.

While a document has unsaved changes, the app writes a snapshot here every few
seconds. On the next launch those snapshots are offered back. A clean save or a
clean close deletes the relevant one, so nothing is offered twice.

    ~/.config/RaggyEditor/recovery/       (or RAGGY_RECOVERY_DIR)

⚠️ THE SNAPSHOT CONTAINS THE DOCUMENT. It is the user's text in plain form, so it
is written with mode 0600 into a directory with mode 0700, and it is deleted as
soon as it is not needed. It is deliberately NOT inside the folder someone might
sync or share.

⚠️ A snapshot is only worth offering if it is NEWER than the file on disk. If the
user saved and then the app died, restoring would silently roll them backwards.
`pending()` enforces that rule; it is the difference between a safety net and a
trap.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from dataclasses import dataclass, field

from raggy.textfile import DEFAULT_FORMAT, TextFormat

APP_DIR_NAME = "RaggyEditor"
RECOVERY_DIR_ENV = "RAGGY_RECOVERY_DIR"
SUFFIX = ".snapshot.json"
UNTITLED = "Untitled"


def recovery_dir() -> pathlib.Path:
    """Where snapshots live. RAGGY_RECOVERY_DIR overrides it (tests use this)."""
    override = os.environ.get(RECOVERY_DIR_ENV)
    if override:
        return pathlib.Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return pathlib.Path(base) / APP_DIR_NAME / "recovery"


def slot_id(path: str | None, fallback: str = "") -> str:
    """A stable filename-safe id for a document.

    ⚠️ Keyed on the document PATH, not on the text: the point is that the same
    document gets the same slot across runs, so a snapshot from the previous
    session is recognised. An unsaved document uses a per-session fallback so two
    untitled windows cannot overwrite each other.
    """
    key = os.path.abspath(path) if path else f"<untitled:{fallback}>"
    return hashlib.blake2b(key.encode("utf-8"), digest_size=12).hexdigest()


@dataclass
class Snapshot:
    slot: str
    text: str
    path: str | None = None
    encoding: str = DEFAULT_FORMAT.encoding
    newline: str = DEFAULT_FORMAT.newline
    bom: bytes = b""
    taken_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------- format
    def text_format(self) -> TextFormat:
        return TextFormat(self.encoding, self.newline, self.bom)

    @property
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else UNTITLED

    def is_stale_for(self, disk_path: str) -> bool:
        """True when the file on disk is newer than this snapshot.

        Then the snapshot is not worth offering — the user has saved since.
        """
        try:
            return os.path.getmtime(disk_path) > self.taken_at
        except OSError:
            return False                      # file is gone: definitely offer it

    # --------------------------------------------------------- serialising
    def to_json(self) -> str:
        return json.dumps({
            "slot": self.slot,
            "text": self.text,
            "path": self.path,
            "encoding": self.encoding,
            "newline": self.newline,
            "bom": self.bom.hex(),
            "taken_at": self.taken_at,
            "version": 1,
        })

    @classmethod
    def from_json(cls, raw: str) -> "Snapshot":
        data = json.loads(raw)
        return cls(
            slot=str(data.get("slot", "")),
            text=str(data.get("text", "")),
            path=data.get("path") or None,
            encoding=str(data.get("encoding") or DEFAULT_FORMAT.encoding),
            newline=str(data.get("newline") or DEFAULT_FORMAT.newline),
            bom=bytes.fromhex(data.get("bom") or ""),
            taken_at=float(data.get("taken_at") or 0.0),
        )


# =============================================================================
# FILE OPERATIONS
# =============================================================================
def _path_for(slot: str) -> pathlib.Path:
    return recovery_dir() / f"{slot}{SUFFIX}"


def save(snapshot: Snapshot) -> pathlib.Path:
    """Write a snapshot with 0600 permissions. Atomic: never a torn file."""
    d = recovery_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    target = _path_for(snapshot.slot)
    tmp = target.with_suffix(target.suffix + ".part")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(snapshot.to_json())
    os.replace(tmp, target)
    return target


def load(slot: str) -> Snapshot | None:
    try:
        return Snapshot.from_json(_path_for(slot).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear(slot: str) -> None:
    """Forget a document's snapshot. Called on a clean save or close."""
    for suffix in ("", ".part"):
        try:
            _path_for(slot).with_suffix(_path_for(slot).suffix + suffix).unlink()
        except OSError:
            pass


def clear_all() -> None:
    for snap in list_all():
        clear(snap.slot)


def list_all() -> list[Snapshot]:
    """Every readable snapshot, newest first. Unreadable ones are ignored."""
    out = []
    d = recovery_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob(f"*{SUFFIX}")):
        try:
            out.append(Snapshot.from_json(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda s: s.taken_at, reverse=True)


def pending() -> list[Snapshot]:
    """Snapshots worth offering on startup.

    ⚠️ Excludes any whose document has been saved since (see
    `Snapshot.is_stale_for`), and any that are empty — offering to restore an
    empty document is noise, not a rescue.

    ⚠️ An UNTITLED document (no path) is always offered. It has never been saved,
    so there is nothing to compare against and no file to lose it to — and it is
    the case where recovery matters most, because the text exists nowhere else.
    Writing the test as `if snap.path and ...` silently dropped exactly those.
    """
    out = []
    for snap in list_all():
        if not snap.text.strip():
            continue
        if snap.path is None or not snap.is_stale_for(snap.path):
            out.append(snap)
    return out
