"""The persisted read cursor — what makes a restart resume without replay or duplicates.

The notifier's dedupe is the cursor (hive-native ops: the read path resumes from a
persisted point). It records the last spine `seq` observed and the log generation it was
taken under; on restart the service reads `(cursor, head]` and so never re-sends an event
it already delivered. The generation is stored beside it so a post-restore run (seq values
reused past the restore point, deployment spec §5) is detected as a mismatch rather than
silently replaying old history as fresh notifications.

The same file also carries the daily-heartbeat state (last send + between-heartbeat tally +
open blocks) under a `heartbeat` key. It is independent of the read cursor — a failed
heartbeat send never touches the cursor — but shares the file so there is no second volume.
The load is `.get`-based and additive: an old cursor-only file (no `heartbeat` key) loads
cleanly into a default heartbeat state.

State is written atomically (temp + rename) so a crash mid-write leaves the previous good
state intact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .heartbeat import HeartbeatState


@dataclass(frozen=True)
class CursorState:
    cursor: int | None
    generation: int | None


class CursorStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            # A corrupt/unreadable state file must not crash the service; re-baseline from
            # a clean snapshot on the next read rather than replay or die.
            return {}
        return data if isinstance(data, dict) else {}

    def load(self) -> CursorState:
        data = self._read()
        return CursorState(data.get("cursor"), data.get("generation"))

    def load_heartbeat(self) -> HeartbeatState:
        return HeartbeatState.from_dict(self._read().get("heartbeat"))

    def save(
        self,
        cursor: int | None,
        generation: int | None,
        heartbeat: HeartbeatState | None = None,
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        blob: dict = {"cursor": cursor, "generation": generation}
        if heartbeat is not None:
            blob["heartbeat"] = heartbeat.to_dict()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(blob))
        os.replace(tmp, self._path)  # atomic on POSIX — never a half-written state
