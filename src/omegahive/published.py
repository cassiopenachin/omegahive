"""Orders as the workspace hub holds them: what is published, for which run, on which route.

An order is published when it is committed on the hub's `main` at
`projects/<project>/orders/<file>.md`; its task id is the file name without a leading date
and `.md`, and its run is the project's committed `RUN_ID`. Committing an order is not
approving it (salvage plan D10): this lists what could be launched, and a human launches.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ORDER_PATH = re.compile(r"projects/([A-Za-z0-9._-]+)/orders/([A-Za-z0-9._-]+)\.md")
_HARNESS_LINE = re.compile(r"\*\*Harness / model:\*\*\s*([A-Za-z0-9._-]+)")
_GIT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}


@dataclass(frozen=True)
class Order:
    task: str
    path: str
    project: str


def task_of(stem: str) -> str:
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)


def git_read(hub: Path | str, *args: str) -> str | None:
    done = subprocess.run(["/usr/bin/git", "-C", str(hub), *args],  # noqa: S603 - fixed git
                          capture_output=True, text=True, env=_GIT_ENV, timeout=30, check=False)
    return done.stdout if done.returncode == 0 else None


def hub_file(hub: Path | str, path: str) -> str | None:
    return git_read(hub, "show", f"main:{path}")


def orders(hub: Path | str) -> dict[str, Order]:
    """Every order on the hub's main, by task id."""
    listing = git_read(hub, "ls-tree", "-r", "--name-only", "main", "projects") or ""
    found = {}
    for path in listing.splitlines():
        match = ORDER_PATH.fullmatch(path)
        if match:
            order = Order(task_of(match.group(2)), path, match.group(1))
            found[order.task] = order
    return found


def project_run(hub: Path | str, project: str) -> str | None:
    conf = hub_file(hub, f"projects/{project}/project.conf") or ""
    match = re.search(r"^RUN_ID=[\"']?([A-Za-z0-9._-]+)", conf, re.MULTILINE)
    return match.group(1) if match else None


def order_route(text: str) -> str | None:
    """The route the order names: the first word of its `Harness / model` line."""
    match = _HARNESS_LINE.search(text)
    return match.group(1) if match else None


def not_launched(hub: Path | str, run: str,
                 tasks_on_board: set[str]) -> list[dict[str, str | None]]:
    """The run's published orders that have no task on its board, oldest first."""
    runs: dict[str, str | None] = {}
    rows: list[dict[str, str | None]] = []
    for order in sorted(orders(hub).values(), key=lambda o: o.path):
        if order.project not in runs:
            runs[order.project] = project_run(hub, order.project)
        if runs[order.project] != run or order.task in tasks_on_board:
            continue
        text = hub_file(hub, order.path) or ""
        title = next((line.lstrip("# ").removeprefix("Order:").strip()
                      for line in text.splitlines() if line.startswith("# ")), order.task)
        rows.append({"task": order.task, "path": order.path, "title": title,
                     "route": order_route(text)})
    return rows
