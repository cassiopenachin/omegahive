"""The deployment policy: one file that states what this deployment is (salvage plan D3).

`~/.config/omegahive/deployment.json` (or `$OMEGAHIVE_DEPLOYMENT`) is owned by the operator
and written by nothing in ordinary operation. It holds deployment facts, the authority the
operation service checks, and bounds. Run identity and code repository are not here: they
are committed per project in `projects/<name>/project.conf`.

Every value resolves the same way, field by field: the environment wins, then the file,
then the Beastie default. A missing file means the defaults; an invalid file is an error
that names the field. The human, the operator scripts and the operation service all read
the same answer: `hive deployment` prints it, `hive-common.sh` sources the file's values
through `--shell`, and the service imports `resolve`.

Standard library only, on purpose: `hive-common.sh` runs this with the host's `python3 -I`,
outside any virtual environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_PATH = "~/.config/omegahive/deployment.json"
POLICY_ENV = "OMEGAHIVE_DEPLOYMENT"

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")
_SAFE_ACTOR = re.compile(r"[A-Za-z0-9._@-]+")

# Field -> the environment variable that overrides it. The shell layer sources exactly the
# fields named here that it already reads (hive-common.sh's deployment layer).
SHELL_FIELDS: dict[str, str] = {
    "code_root": "OMEGA_DIR",
    "canon_root": "CANON_ROOT",
    "workspace_hub": "WS_HUB",
    "operator_workspace": "OPS_WS",
    "work_root": "WORK_ROOT",
    "tmux_session": "HIVE_TMUX_SESSION",
    "route_catalog": "HIVE_ROUTE_CATALOG",
    "compose": "OMEGAHIVE_COMPOSE",
    "bounds.wip_review_max": "HIVE_WIP_REVIEW_MAX",
}
ENV_FIELDS: dict[str, str] = {
    **SHELL_FIELDS,
    "scripts_root": "HIVE_SCRIPTS_ROOT",
    "env_file": "HIVE_ENV_FILE",
    "ops_socket": "HIVE_OPS_SOCKET",
    "receipts_dir": "HIVE_RECEIPTS_DIR",
    "operator_actor": "HIVE_OPERATOR_ACTOR",
    "authority.web_login": "HIVE_WEB_LOGIN",
    "bounds.max_concurrent": "HIVE_MAX_CONCURRENT",
    "bounds.operation_timeout_s": "HIVE_OPERATION_TIMEOUT_S",
    "bounds.observer_quiet_min": "HIVE_OBSERVER_QUIET_MIN",
}


class DeploymentError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"field '{field}': {message}")
        self.field = field


def _path(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\n" in value or "\t" in value:
        raise DeploymentError(field, f"must be an absolute path, got {value!r}")
    return value


def _optional_path(field: str, value: object) -> str | None:
    return None if value is None else _path(field, value)


def _text(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\t" in value:
        raise DeploymentError(field, f"must be a non-empty single-line string, got {value!r}")
    return value


def _optional_text(field: str, value: object) -> str | None:
    return None if value is None else _text(field, value)


def _name(field: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise DeploymentError(field, f"must match [A-Za-z0-9._-]+, got {value!r}")
    return value


def _actor(field: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ACTOR.fullmatch(value):
        raise DeploymentError(field, f"must match [A-Za-z0-9._@-]+, got {value!r}")
    return value


def _positive(field: str, value: object) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)   # from the environment
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DeploymentError(field, f"must be a positive integer, got {value!r}")
    return value


def _optional_positive(field: str, value: object) -> int | None:
    return None if value is None else _positive(field, value)


Check = Callable[[str, object], Any]

_SCHEMA: dict[str, Check] = {
    "code_root": _path,
    "scripts_root": _path,
    "canon_root": _path,
    "workspace_hub": _path,
    "operator_workspace": _path,
    "work_root": _path,
    "tmux_session": _name,
    "route_catalog": _path,
    "compose": _optional_text,
    "env_file": _optional_path,
    "ops_socket": _path,
    "receipts_dir": _path,
    "operator_actor": _actor,
    "authority.web_login": _optional_text,
    "authority.slack.team": _optional_text,
    "authority.slack.channel": _optional_text,
    "authority.slack.user": _optional_text,
    "bounds.max_concurrent": _optional_positive,
    "bounds.wip_review_max": _positive,
    "bounds.operation_timeout_s": _positive,
    "bounds.observer_quiet_min": _positive,
}


def _defaults(env: Mapping[str, str]) -> dict[str, object]:
    home = env.get("HOME") or str(Path.home())
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return {
        "code_root": f"{home}/src/SNET/omegahive",
        "scripts_root": None,   # derived: <code_root>/scripts
        "canon_root": f"{home}/src/SNET",
        "workspace_hub": f"{home}/repos/hive-workspace.git",
        "operator_workspace": f"{home}/workspaces/hive",
        "work_root": f"{home}/work",
        "tmux_session": "hive",
        "route_catalog": f"{home}/.config/omegahive/routes.json",
        "compose": None,        # probed by hive-common.sh's resolve_compose
        "env_file": None,
        "ops_socket": f"{runtime}/omegahive/ops.sock",
        "receipts_dir": f"{home}/.local/state/omegahive/receipts",
        "operator_actor": "operator",
        "authority.web_login": None,
        "authority.slack.team": None,
        "authority.slack.channel": None,
        "authority.slack.user": None,
        "bounds.max_concurrent": None,   # no cap
        "bounds.wip_review_max": 3,
        "bounds.operation_timeout_s": 900,
        "bounds.observer_quiet_min": 30,
    }


def _flatten(data: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


def policy_path(env: Mapping[str, str]) -> Path:
    return Path(os.path.expanduser(env.get(POLICY_ENV) or DEFAULT_PATH))


def load(path: Path) -> dict[str, object]:
    """The file's own fields, validated and flattened. A missing file has none."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise DeploymentError("(file)", f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentError("(file)", f"{path} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DeploymentError("(file)", f"{path} must hold a JSON object")
    flat = _flatten(data)
    for field, value in flat.items():
        check = _SCHEMA.get(field)
        if check is None:
            raise DeploymentError(field, "unknown field")
        flat[field] = check(field, value)
    return flat


def _nest(flat: Mapping[str, object]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for field, value in flat.items():
        *parents, leaf = field.split(".")
        node = nested
        for parent in parents:
            node = node.setdefault(parent, {})
        node[leaf] = value
    return nested


def resolve(env: Mapping[str, str], path: Path | None = None) -> dict[str, Any]:
    """Env over file over defaults, field by field, as a nested mapping."""
    from_file = load(path if path is not None else policy_path(env))
    values = _defaults(env)
    values.update(from_file)
    for field, var in ENV_FIELDS.items():
        if env.get(var):
            values[field] = _SCHEMA[field](field, env[var])
    if values["scripts_root"] is None:
        values["scripts_root"] = f"{values['code_root']}/scripts"
    return _nest(values)


def lookup(values: Mapping[str, Any], field: str) -> Any:
    node: Any = values
    for part in field.split("."):
        node = node[part]
    return node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hive deployment", description=__doc__.split("\n")[0])
    parser.add_argument("--policy", type=Path, help=f"policy file (default ${POLICY_ENV}, "
                        f"then {DEFAULT_PATH})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shell", action="store_true",
                      help="print the file's values for hive-common.sh, NAME<TAB>VALUE")
    mode.add_argument("--get", metavar="FIELD", help="print one resolved field")
    mode.add_argument("--json", action="store_true", help="print every resolved field as JSON")
    args = parser.parse_args(argv)
    env = os.environ
    path = args.policy or policy_path(env)
    try:
        if args.shell:
            from_file = load(path)
            for field, var in SHELL_FIELDS.items():
                if field in from_file and from_file[field] is not None:
                    print(f"{var}\t{from_file[field]}")
            return 0
        values = resolve(env, path)
        if args.get:
            value = lookup(values, args.get)
            print("" if value is None else value)
        elif args.json:
            print(json.dumps(values, indent=2, sort_keys=True))
        else:
            source = path if path.exists() else f"{path} (absent: defaults)"
            print(f"policy: {source}")
            for field in _SCHEMA:
                print(f"{field} = {lookup(values, field)}")
    except DeploymentError as exc:
        print(f"hive deployment: invalid policy {path}: {exc}", file=sys.stderr)
        return 2
    except KeyError:
        print(f"hive deployment: no field {args.get!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
