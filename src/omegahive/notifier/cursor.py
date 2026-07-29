"""The persisted read cursors — what makes a restart resume without replay or duplicates.

The notifier's dedupe is the cursor (hive-native ops: the read path resumes from a
persisted point). One notifier watches every active run, so the state is a **set** of
cursors, one per run: the last spine `seq` observed on that run and the log generation it
was taken under. On restart each run is read as `(cursor, head]` and so never re-sends an
event it already delivered. The generation is stored beside each cursor because the port
reports GENERATION_MISMATCH only to a client presenting the generation it last saw — read
with `generation=None` and a restore-rewound log answers "no change" forever (the same
obligation the portfolio UI stream carries).

The per-run set, rather than one spine-wide cursor, is what lets a restore on one run
re-baseline that run alone while every other run keeps streaming.

The same file also carries the portfolio heartbeat state (one send schedule + a per-run
tally) under a `heartbeat` key. It is independent of the read cursors — a failed heartbeat
send never touches them — but shares the file so there is no second volume.

**Cutover from the single-run notifier.** A legacy file (`{"cursor": …, "generation": …}`)
carries a cursor for whichever run that instance followed. It is deliberately **not**
adopted: every run re-arms at its current head on first sight, so the cutover is silent.
The history the old instance missed is the board's story, never a burst of pages at 2am.

State is written atomically (temp + rename) so a crash mid-write leaves the previous good
state intact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .heartbeat import PortfolioHeartbeat


@dataclass(frozen=True)
class RunCursor:
    """One run's read position: the last observed seq and the generation it was taken under."""

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

    def load(self) -> dict[str, RunCursor]:
        """The per-run cursor set. A legacy single-run file yields an empty set — every run
        re-arms at head rather than replaying one run's backlog into the portfolio."""
        raw = self._read().get("runs")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, RunCursor] = {}
        for run_id, row in raw.items():
            if isinstance(row, dict):
                out[str(run_id)] = RunCursor(row.get("cursor"), row.get("generation"))
        return out

    def load_heartbeat(self) -> PortfolioHeartbeat:
        return PortfolioHeartbeat.from_dict(self._read().get("heartbeat"))

    def save(
        self,
        cursors: dict[str, RunCursor],
        heartbeat: PortfolioHeartbeat | None = None,
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        blob: dict = {
            "runs": {
                run_id: {"cursor": rc.cursor, "generation": rc.generation}
                for run_id, rc in cursors.items()
            }
        }
        if heartbeat is not None:
            blob["heartbeat"] = heartbeat.to_dict()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(blob))
        os.replace(tmp, self._path)  # atomic on POSIX — never a half-written state
