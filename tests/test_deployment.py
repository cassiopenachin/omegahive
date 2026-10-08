"""The deployment policy: env over file over defaults, field by field; an invalid file is
loud and names its field; a missing file is the defaults (salvage plan D3, S3 A3/A6)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omegahive import deployment
from omegahive.deployment import DeploymentError, resolve

REPO = Path(__file__).resolve().parent.parent
PARSER = REPO / "src" / "omegahive" / "deployment.py"
EXAMPLE = REPO / "schemas" / "deployment.example.json"
HOME = "/home/someone"
ENV = {"HOME": HOME, "XDG_RUNTIME_DIR": "/run/user/1000"}


def _write(tmp_path: Path, policy: dict) -> Path:
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(policy))
    return path


def test_a_missing_file_is_the_defaults(tmp_path):
    values = resolve(ENV, tmp_path / "absent.json")
    assert values["code_root"] == f"{HOME}/src/SNET/omegahive"
    assert values["scripts_root"] == f"{HOME}/src/SNET/omegahive/scripts"
    assert values["operator_workspace"] == f"{HOME}/workspaces/hive"
    assert values["tmux_session"] == "hive"
    assert values["operator_actor"] == "operator"
    assert values["bounds"]["wip_review_max"] == 3
    assert values["bounds"]["max_concurrent"] is None
    assert values["authority"]["web_login"] is None


def test_env_wins_over_the_file_which_wins_over_the_defaults_field_by_field(tmp_path):
    path = _write(tmp_path, {"code_root": "/srv/omega", "work_root": "/srv/work",
                             "bounds": {"wip_review_max": 5, "max_concurrent": 2}})
    values = resolve({**ENV, "WORK_ROOT": "/env/work", "HIVE_WIP_REVIEW_MAX": "7"}, path)
    assert values["code_root"] == "/srv/omega"                # file over default
    assert values["scripts_root"] == "/srv/omega/scripts"      # derived from the resolved root
    assert values["work_root"] == "/env/work"                  # env over file
    assert values["canon_root"] == f"{HOME}/src/SNET"          # default where neither speaks
    assert values["bounds"]["wip_review_max"] == 7
    assert values["bounds"]["max_concurrent"] == 2


@pytest.mark.parametrize(
    ("policy", "field"),
    [
        ({"code_rot": "/x"}, "code_rot"),
        ({"code_root": "relative/path"}, "code_root"),
        ({"code_root": 3}, "code_root"),
        ({"tmux_session": "a:b"}, "tmux_session"),
        ({"operator_actor": "two words"}, "operator_actor"),
        ({"bounds": {"max_concurrent": 0}}, "bounds.max_concurrent"),
        ({"bounds": {"max_concurrent": True}}, "bounds.max_concurrent"),
        ({"bounds": {"wip_reviw_max": 3}}, "bounds.wip_reviw_max"),
        ({"authority": {"web_login": ""}}, "authority.web_login"),
        ({"authority": {"slack": {"channel": 5}}}, "authority.slack.channel"),
        ({"compose": "podman\ncompose"}, "compose"),
    ],
)
def test_an_invalid_file_names_the_field(tmp_path, policy, field):
    with pytest.raises(DeploymentError) as raised:
        resolve(ENV, _write(tmp_path, policy))
    assert raised.value.field == field
    assert field in str(raised.value)


def test_a_file_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "deployment.json"
    path.write_text("{not json")
    with pytest.raises(DeploymentError):
        resolve(ENV, path)


def test_the_example_is_a_valid_policy():
    values = resolve(ENV, EXAMPLE)
    assert values["authority"]["web_login"]
    assert values["bounds"]["max_concurrent"]


def test_the_parser_runs_on_the_bare_interpreter(tmp_path):
    """hive-common.sh calls it with the host's python3 and no virtualenv."""
    path = _write(tmp_path, {"work_root": "/srv/work"})
    proc = subprocess.run(["python3", "-I", str(PARSER), "--policy", str(path), "--shell"],
                          capture_output=True, text=True, env=ENV, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "WORK_ROOT\t/srv/work\n"   # only what the file says, for the shell


def test_the_parser_reports_an_invalid_file_and_fails(tmp_path):
    path = _write(tmp_path, {"bounds": {"max_concurrent": -1}})
    proc = subprocess.run(["python3", "-I", str(PARSER), "--policy", str(path)],
                          capture_output=True, text=True, env=ENV, check=False)
    assert proc.returncode != 0
    assert "bounds.max_concurrent" in proc.stderr


# --- the shell layer --------------------------------------------------------------------

SHELL_VARS = ("OMEGA_DIR", "CANON_ROOT", "WS_HUB", "OPS_WS", "WORK_ROOT", "HIVE_TMUX_SESSION",
              "HIVE_ROUTE_CATALOG", "HIVE_WIP_REVIEW_MAX")


def _shell(tmp_path: Path, policy: Path, **env: str) -> dict[str, str]:
    """Source a copy of hive-common.sh laid out like a checkout and print the layer."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "omegahive").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "scripts" / "hive-common.sh", root / "scripts" / "hive-common.sh")
    shutil.copy2(PARSER, root / "src" / "omegahive" / "deployment.py")
    script = (f'source "{root}/scripts/hive-common.sh"; '
              + "; ".join(f'printf "%s\\t%s\\n" {v} "${v}"' for v in SHELL_VARS))
    base = {"PATH": os.environ["PATH"], **ENV, "OMEGAHIVE_DEPLOYMENT": str(policy)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**base, **env}, check=False)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("\t", 1) for line in proc.stdout.splitlines())


def test_the_shell_defaults_agree_with_the_parsers(tmp_path):
    shell = _shell(tmp_path, tmp_path / "absent.json")
    values = resolve(ENV, tmp_path / "absent.json")
    for field, var in deployment.SHELL_FIELDS.items():
        if var in SHELL_VARS:
            expected = deployment.lookup(values, field)
            assert shell[var] == str(expected), var


def test_the_shell_layer_takes_the_file_under_the_environment(tmp_path):
    path = _write(tmp_path, {"work_root": "/srv/work", "operator_workspace": "/srv/ws",
                             "bounds": {"wip_review_max": 5}})
    shell = _shell(tmp_path, path, OPS_WS="/env/ws")
    assert shell["WORK_ROOT"] == "/srv/work"
    assert shell["OPS_WS"] == "/env/ws"
    assert shell["HIVE_WIP_REVIEW_MAX"] == "5"
    assert shell["HIVE_TMUX_SESSION"] == "hive"


def test_the_shell_refuses_an_invalid_file_naming_the_field(tmp_path):
    path = _write(tmp_path, {"work_rot": "/srv/work"})
    root = tmp_path / "checkout"
    _shell(tmp_path, tmp_path / "absent.json")   # lay the checkout out
    proc = subprocess.run(["bash", "-c", f'source "{root}/scripts/hive-common.sh"'],
                          capture_output=True, text=True, check=False,
                          env={"PATH": os.environ["PATH"], **ENV,
                               "OMEGAHIVE_DEPLOYMENT": str(path)})
    assert proc.returncode != 0
    assert "work_rot" in proc.stderr


# --- the `hive` entry point -------------------------------------------------------------

HIVE = REPO / "scripts" / "hive"


def _hive(*args: str, policy: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], **ENV, "OMEGAHIVE_DEPLOYMENT": str(policy)}
    return subprocess.run([str(HIVE), *args], capture_output=True, text=True,
                          env={**base, **env}, check=False)


def test_a_subcommand_runs_the_same_script_as_its_hyphenated_name(tmp_path):
    policy = _write(tmp_path, {"code_root": str(REPO)})
    via_hive = _hive("close", "--help", policy=policy)
    direct = subprocess.run([str(REPO / "scripts" / "hive-close"), "--help"],
                            capture_output=True, text=True, check=False,
                            env={"PATH": os.environ["PATH"], **ENV,
                                 "OMEGAHIVE_DEPLOYMENT": str(policy)})
    assert via_hive.returncode == direct.returncode == 2
    assert via_hive.stderr == direct.stderr
    assert "usage: hive-close" in via_hive.stderr


def test_the_scripts_come_from_the_policys_scripts_root(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "hive-hello").write_text('#!/bin/sh\necho "hello from elsewhere $*"\n')
    (other / "hive-hello").chmod(0o755)
    policy = _write(tmp_path, {"scripts_root": str(other)})
    proc = _hive("hello", "x", policy=policy)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "hello from elsewhere x\n"


def test_deployment_is_served_by_the_entry_point_itself(tmp_path):
    policy = _write(tmp_path, {"code_root": "/srv/omega"})
    proc = _hive("deployment", "--get", "scripts_root", policy=policy)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "/srv/omega/scripts\n"


@pytest.mark.parametrize("sub", ["no-such-command", "common.sh", "../hive-close", "Close"])
def test_a_name_that_is_not_an_operator_command_is_refused(tmp_path, sub):
    proc = _hive(sub, policy=tmp_path / "absent.json", HIVE_SCRIPTS_ROOT=str(REPO / "scripts"))
    assert proc.returncode != 0
    assert proc.stderr


# --- deploy checks 11b and 11c ----------------------------------------------------------

def _check_block(start: str, end: str, cwd: Path, **env: str) -> str:
    text = (REPO / "scripts" / "deploy_checks.sh").read_text()
    block = text[text.index(start):text.index(end)]
    script = 'ok() { echo "[PASS] $1"; }\nbad() { echo "[FAIL] $1"; }\n' + block
    base = {"PATH": os.environ["PATH"], **ENV}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          cwd=cwd, env={**base, **env}, check=False).stdout


def test_check_11c_passes_when_the_policy_names_this_checkout(tmp_path):
    policy = _write(tmp_path, {"code_root": str(REPO)})
    out = _check_block("# --- 11c.", "# --- 12.", REPO, OMEGAHIVE_DEPLOYMENT=str(policy))
    assert "[PASS] 11c." in out


def test_check_11c_fails_when_the_policy_names_another_checkout(tmp_path):
    policy = _write(tmp_path, {"code_root": "/srv/another-omegahive"})
    out = _check_block("# --- 11c.", "# --- 12.", REPO, OMEGAHIVE_DEPLOYMENT=str(policy))
    assert "[FAIL] 11c." in out
    assert "/srv/another-omegahive" in out


def test_check_11c_fails_on_an_invalid_policy(tmp_path):
    policy = _write(tmp_path, {"code_rot": str(REPO)})
    out = _check_block("# --- 11c.", "# --- 12.", REPO, OMEGAHIVE_DEPLOYMENT=str(policy))
    assert "[FAIL] 11c." in out


def test_check_11b_names_the_entry_point(tmp_path):
    links = tmp_path / "bin"
    links.mkdir()
    out = _check_block("# --- 11b.", "# --- 11c.", REPO, OMEGAHIVE_COMMAND_BIN=str(links))
    assert "[FAIL] 11b." in out
    missing = out.splitlines()[0].split(": ", 1)[1].split()
    assert "hive" in missing
