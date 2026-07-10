"""Real-provider capture backend — drives a REAL model through the OmegaClaw fork
container and captures the artifacts the battery grades (slice-3, v0a half).

It is `smoke_fork` grown up: same podman boot + cribbed `Autotests/mock` **comm** controller
+ `host.containers.internal` networking, but the LLM is a real provider (OpenRouter/Anthropic)
instead of the Test mock. Per (scenario × model × rep) it boots the **hive** image (superset →
emits `[LLM_USAGE]` token lines), injects the scenario's turns over the mock channel, lets the
real model cycle, then captures container stdout (`[LLM_RAW]` replies + `[LLM_USAGE]` tokens)
and `memory/history.metta`, and folds them into a graded `Bundle`.

Provider keys come from `~/.config/omegahive/secrets/harness.env` (values are quote-stripped
and passed as `-e VAR` so they never appear in argv/logs); in-container they terminate in the
nginx key-isolation proxy, never reaching the agent process.

Parse-trace fidelity (v0a): the fork emits *bare* command lines (`query user goals`), so a
line "parses pre-repair" only if it is already a balanced parenthesised s-expr; `parses_post`
/ `dispatched` use the fork's own `helper.LLM_COMMANDS` table (imported host-side — pure
stdlib, identical to the image since the image is `COPY .` of this source). `repair_dependency`
is therefore the real signal: how much `balance_parentheses` carries a model that emits bare
lines. Board-op events are not captured here (v0a has no board); `event_log_slice` is empty.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from .bundle import (
    Bundle,
    BundleMeta,
    HistoryEntry,
    ParseLine,
    Telemetry,
    TurnCapture,
    TurnTelemetry,
)
from .capture import CaptureResult
from .loader import LoadedScenario

DEFAULT_IMAGE = "localhost/omegaclaw-hive:0.1"
DEFAULT_FORK_REPO = os.environ.get("OMEGACLAW_FORK_REPO", "/home/cassio/src/SNET/OmegaClaw-Core")
DEFAULT_SECRETS = os.path.expanduser("~/.config/omegahive/secrets/harness.env")
HISTORY_PATH = "/PeTTa/repos/OmegaClaw-Core/memory/history.metta"
TEST_HOST = "host.containers.internal"

# battery model id -> (fork provider flag, key env var). Model string is fixed per provider
# in the fork registry (lib_llm_ext.py): OpenRouter→z-ai/glm-5.2, MiniMaxM3→minimax/minimax-m3,
# Anthropic→claude-opus-4-8. The local provider (Ollama-local) needs no key — it reaches a host
# llama-server via the repointed nginx `/ollama-local/` upstream (see `_patched_nginx_template`).
MODEL_PROVIDER: dict[str, tuple[str, str]] = {
    "glm-5.2": ("OpenRouter", "OPENROUTER_API_KEY"),
    "minimax-m3": ("MiniMaxM3", "OPENROUTER_API_KEY"),
    "claude-opus-4-8": ("Anthropic", "ANTHROPIC_API_KEY"),
    "qwen3.6-local": ("Ollama-local", ""),
}

_RAW_RE = re.compile(r"\[LLM_RAW\] ts=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).* raw=(.*)$")
_USAGE_RE = re.compile(r"\[LLM_USAGE\].* tokens_in=(-?\d+) tokens_out=(-?\d+)")


def _load_key(secrets_file: str, var: str) -> str:
    with open(secrets_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and line.split("=", 1)[0] == var:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise KeyError(f"{var} not found in {secrets_file}")


def _import_comm(fork_repo: str):
    mock_dir = os.path.join(fork_repo, "Autotests", "mock")
    if mock_dir not in sys.path:
        sys.path.insert(0, mock_dir)
    import comm as comm_mod  # noqa: PLC0415
    return comm_mod


def _import_helper(fork_repo: str):
    src_dir = os.path.join(fork_repo, "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    import helper as helper_mod  # noqa: PLC0415  — pure-stdlib parser, identical to the image
    return helper_mod


def _patched_nginx_template(fork_repo: str, upstream: str) -> str:
    """Copy the fork's nginx template, repointing the `/ollama-local/` upstream at `upstream`
    (host:port of a llama-server), and return a world-readable temp path to bind-mount over
    `/opt/nginx/nginx.conf.template` (nginx.sh envsubsts it at boot, running as www-data)."""
    with open(os.path.join(fork_repo, "proxy", "nginx.conf.template")) as f:
        text = f.read()
    host = upstream.split(":", 1)[0]
    text = text.replace(
        "proxy_pass http://localhost:11434/v1/;", f"proxy_pass http://{upstream}/v1/;"
    )
    text = text.replace("proxy_set_header Host localhost;", f"proxy_set_header Host {host};", 1)
    fd, path = tempfile.mkstemp(prefix="qual-nginx-", suffix=".template")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o644)  # nginx.sh runs as www-data; the mounted file must be world-readable
    return path


class ForkContainerCaptureBackend:
    """Boots the fork image with a real provider + mock channel and captures a graded bundle."""

    def __init__(
        self,
        image_ref: str = DEFAULT_IMAGE,
        *,
        fork_repo: str = DEFAULT_FORK_REPO,
        secrets_file: str = DEFAULT_SECRETS,
        per_turn_wait: float = 8.0,
        settle: float = 6.0,
        ready_timeout: float = 150.0,
        local_upstream: str = "host.containers.internal:8080",
    ) -> None:
        self.image_ref = image_ref
        self.image_id = ""
        self.fork_repo = fork_repo
        self.secrets_file = secrets_file
        self.per_turn_wait = per_turn_wait
        self.settle = settle
        self.ready_timeout = ready_timeout
        self.local_upstream = local_upstream  # host llama-server the /ollama-local/ proxy hits

    def _podman(self, *args: str, timeout: int = 120, env: dict | None = None):
        return subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=timeout, env=env
        )

    def capture(self, loaded: LoadedScenario, model: str, rep: int) -> CaptureResult:
        if model not in MODEL_PROVIDER:
            raise ValueError(f"unknown model {model!r} (known: {sorted(MODEL_PROVIDER)})")
        provider, key_var = MODEL_PROVIDER[model]
        env = dict(os.environ)
        if key_var:
            env[key_var] = _load_key(self.secrets_file, key_var)

        comm_mod = _import_comm(self.fork_repo)
        helper = _import_helper(self.fork_repo)
        container = f"qual-{model}-{rep}-{os.getpid()}".replace(".", "-")
        server = comm_mod.CommMockServer(("0.0.0.0", comm_mod.COMM_MOCK_PORT))

        run_args = [
            "run", "-d", "--name", container,
            "--add-host", f"{TEST_HOST}:host-gateway",
            "-e", f"TEST_SERVER_IP={TEST_HOST}",
        ]
        if key_var:
            run_args += ["-e", key_var]  # value forwarded from env
        nginx_override = ""
        if provider == "Ollama-local":
            # repoint the in-container /ollama-local/ nginx upstream at the host llama-server
            nginx_override = _patched_nginx_template(self.fork_repo, self.local_upstream)
            run_args += ["-v", f"{nginx_override}:/opt/nginx/nginx.conf.template:ro,Z"]
        run_args += [
            self.image_ref,
            "commchannel=test", f"provider={provider}", "embeddingprovider=Local",
            "securityPolicyPath=/PeTTa/repos/OmegaClaw-Core/profile/policy.yaml",
            f"TEST_SERVER_IP={TEST_HOST}",
        ]

        logs, history_text = "", ""
        try:
            boot = self._podman(*run_args, env=env)
            if boot.returncode != 0:
                raise RuntimeError(f"podman run failed: {boot.stderr.strip()}")
            self.image_id = self._podman(
                "image", "inspect", "--format", "{{.Id}}", self.image_ref
            ).stdout.strip()

            if not self._wait_ready(container):
                raise RuntimeError(
                    f"{container} did not reach ready in {self.ready_timeout}s:\n"
                    + self._podman("logs", container).stderr[-1500:]
                )

            for turn in loaded.scenario.turns:
                if turn.inject is not None:
                    server.send_message(turn.inject)
                    time.sleep(self.per_turn_wait)
            time.sleep(self.settle)

            got = self._podman("logs", container)
            logs = got.stdout + got.stderr
            history_text = self._podman("exec", container, "cat", HISTORY_PATH).stdout
        finally:
            self._podman("rm", "-f", container)
            server.stop(5)
            if nginx_override:
                with contextlib.suppress(OSError):
                    os.unlink(nginx_override)

        cycles = _parse_cycles(logs)
        bundle = _build_bundle(loaded, model, rep, cycles, history_text, helper)
        raw_llm = "\n".join(f"[LLM_RAW] {c['raw']!r}" for c in cycles)
        return CaptureResult(
            bundle=bundle,
            raw_llm=raw_llm,
            event_log_slice=[],
            image_ref=self.image_ref,
            image_id=self.image_id,
        )

    def _wait_ready(self, container: str) -> bool:
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            got = self._podman("logs", container)
            if "CHARS_SENT" in (got.stdout + got.stderr):
                return True
            running = self._podman("inspect", "-f", "{{.State.Running}}", container).stdout.strip()
            if running != "true":
                return False
            time.sleep(2)
        return False


def _parse_cycles(logs: str) -> list[dict]:
    """One cycle per [LLM_RAW]; its [LLM_USAGE] is the next usage line."""
    cycles: list[dict] = []
    pending: dict | None = None
    for line in logs.splitlines():
        m = _RAW_RE.search(line)
        if m:
            if pending is not None:
                cycles.append(pending)
            try:
                raw = ast.literal_eval(m.group(2))
            except (ValueError, SyntaxError):
                raw = m.group(2)
            pending = {"ts": m.group(1), "raw": raw, "tokens_in": 0, "tokens_out": 0}
            continue
        u = _USAGE_RE.search(line)
        if u and pending is not None:
            pending["tokens_in"] = max(0, int(u.group(1)))
            pending["tokens_out"] = max(0, int(u.group(2)))
    if pending is not None:
        cycles.append(pending)
    return cycles


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()


def _line_to_parseline(raw_line: str, heads: set[str]) -> ParseLine:
    text = raw_line.strip()
    head = ""
    if text.startswith("("):
        inner = text[1:].strip()
        head = inner.split()[0] if inner else ""
        args: list[str] = []
    elif text:
        parts = text.split()
        head, args = parts[0], parts[1:]
    else:
        args = []

    balanced_parens = text.count("(") == text.count(")")
    parses_pre = text.startswith("(") and balanced_parens
    recognized = head in heads
    return ParseLine(
        raw=raw_line,
        parses_pre_repair=parses_pre,
        parses_post_repair=recognized,  # balance_parentheses yields a command iff head is known
        emitted_head=head,
        emitted_args=args,
        dispatched_op=recognized,
        results_echo="" if recognized else raw_line,  # unknown heads self-evaluate (fact g)
    )


def _build_bundle(
    loaded: LoadedScenario, model: str, rep: int, cycles: list[dict], history_text: str, helper
) -> Bundle:
    heads = set(getattr(helper, "LLM_COMMANDS", set())) or loaded.catalog.heads
    turns: list[TurnCapture] = []
    history: list[HistoryEntry] = []
    per_turn: list[TurnTelemetry] = []

    for i, cyc in enumerate(cycles, start=1):
        lines = [
            _line_to_parseline(ln, heads)
            for ln in str(cyc["raw"]).splitlines()
            if ln.strip()
        ]
        turns.append(TurnCapture(index=i, lines=lines))
        if any(ln.emitted_head == "pin" for ln in lines):
            history.append(HistoryEntry(turn=i, kind="pin_set", text="pinned"))
        wall_ms = 0
        if i > 1:
            wall_ms = max(0, int((_epoch(cyc["ts"]) - _epoch(cycles[i - 2]["ts"])) * 1000))
        per_turn.append(
            TurnTelemetry(
                turn=i, tokens=cyc["tokens_in"] + cyc["tokens_out"], usd=0.0, wall_ms=wall_ms
            )
        )

    return Bundle(
        meta=BundleMeta(
            scenario_id=loaded.scenario.id, model=model, rep=rep, turns_played=len(cycles)
        ),
        turns=turns,
        history=history,
        telemetry=Telemetry(per_turn=per_turn),
    )
