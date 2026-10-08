"""`omegahive ops` (`hive ops`): the operation service's CLI face.

Each operation goes to the running service over its socket, so the command line and the
web share one process, one set of locks and one receipt store. `serve` runs the service.
Exit status: 0 when the operation is done (or replayed done), 1 otherwise; the receipt or
the refusal is printed as JSON either way.
"""

from __future__ import annotations

import json
import os
from typing import Any

import typer

from .deployment import resolve
from .ops_service import OpsClient, OpsUnavailable, serve

ops_app = typer.Typer(help="Operate the hive through the operation service.",
                      no_args_is_help=True)

ID = typer.Option(..., "--id", help="operation id: a repeat with the same content replays it")
RUN = typer.Option(..., "--run", help="run id")
TASK = typer.Option(..., "--task", help="task id")


def _client() -> OpsClient:
    return OpsClient(resolve(os.environ)["ops_socket"])


def _operate(operation: str, operation_id: str, params: dict[str, Any]) -> None:
    try:
        receipt = _client().operate(operation, operation_id, params)
    except OpsUnavailable as exc:
        typer.echo(f"hive ops: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(receipt, indent=2))
    raise typer.Exit(code=0 if receipt.get("status") == "done" else 1)


@ops_app.command("serve")
def serve_cmd() -> None:
    """Run the operation service on the policy's ops_socket (hive-ops.service runs this)."""
    serve()


@ops_app.command("answer")
def answer_cmd(operation_id: str = ID, run: str = RUN, task: str = TASK,
               question_seq: int = typer.Option(..., "--question-seq"),
               text: str = typer.Option(..., "--text")) -> None:
    """Answer the task's latest question (one line), then nudge its worker."""
    _operate("answer", operation_id,
             {"run": run, "task": task, "question_seq": question_seq, "text": text})


@ops_app.command("close")
def close_cmd(operation_id: str = ID, run: str = RUN, task: str = TASK,
              result_ref: str = typer.Option(..., "--result-ref"),
              verdict: str = typer.Option(..., "--verdict"),
              reason: str = typer.Option("", "--reason")) -> None:
    """Close the task's latest result with a review verdict (scored)."""
    _operate("close", operation_id, {"run": run, "task": task, "result_ref": result_ref,
                                     "verdict": verdict, "reason": reason})


@ops_app.command("merge")
def merge_cmd(operation_id: str = ID, run: str = RUN, task: str = TASK,
              pr: int = typer.Option(..., "--pr"),
              head_sha: str = typer.Option(..., "--head")) -> None:
    """Squash-merge a PR the latest result report names, at the head you saw."""
    _operate("merge", operation_id, {"run": run, "task": task, "pr": pr, "head_sha": head_sha})


@ops_app.command("abandon")
def abandon_cmd(operation_id: str = ID, run: str = RUN, task: str = TASK,
                reason: str = typer.Option(..., "--reason")) -> None:
    """Abandon the task (cancelled); a dead worker's window is cleared first."""
    _operate("abandon", operation_id, {"run": run, "task": task, "reason": reason})


@ops_app.command("launch")
def launch_cmd(operation_id: str = ID,
               order_path: str = typer.Option(..., "--order", help="projects/<p>/orders/<f>.md"),
               route: str = typer.Option("", "--route",
                                         help="default: the order's Harness / model line")) -> None:
    """Launch a published order."""
    _operate("launch", operation_id, {"order_path": order_path, "route": route})


@ops_app.command("tail")
def tail_cmd(task: str = typer.Argument(...)) -> None:
    """Print the last lines of the task's worker output."""
    lines = _client().tail(task)
    if lines is None:
        typer.echo(f"hive ops: no window for '{task}'", err=True)
        raise typer.Exit(code=1)
    typer.echo("\n".join(lines))
