"""The persisted read cursor — what makes a restart resume without replay or duplicates.

The notifier's dedupe is the cursor (hive-native ops: the read path resumes from a
persisted point). It records the last spine `seq` observed and the log generation it was
taken under; on restart the service reads `(cursor, head]` and so never re-sends an event
it already delivered. The generation is stored beside it so a post-restore run (seq values
reused past the restore point, deployment spec §5) is detected as a mismatch rather than
silently replaying old history as fresh notifications.

State lives on the service's own volume (compose), written atomically (temp + rename) so a
crash mid-write leaves the previous good cursor intact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CursorState:
    cursor: int | None
    generation: int | None


class CursorStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def load(self) -> CursorState:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return CursorState(None, None)
        except (ValueError, OSError):
            # A corrupt/unreadable state file must not crash the service; re-baseline from
            # a clean snapshot on the next read rather than replay or die.
            return CursorState(None, None)
        return CursorState(data.get("cursor"), data.get("generation"))

    def save(self, cursor: int | None, generation: int | None) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"cursor": cursor, "generation": generation}))
        os.replace(tmp, self._path)  # atomic on POSIX — never a half-written cursor
