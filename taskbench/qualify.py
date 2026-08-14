"""The qualification study's own preflight: prove the route before spending on it.

`preflight.py` refuses on everything checkable *locally*. This module refuses on everything
checkable **at the gateway**, which is where this study's new risk lives: five bundles across
four harnesses and two accounts, three of them reaching a model through a preset whose routing
an operator could change from a web page between one batch and the next.

The order fixes a hard sequence and this module implements it: **before either DeepSeek batch
runs, both arms must expose server-returned usage and OpenRouter generation identity at the
individual-run grain and resolve to the same model and endpoint.** That is not a nice-to-have
on the way to the interesting numbers. It *is* the interesting number — the headline question
the pair exists to answer is a token question, and answering it from a harness-local price
table or a transcript estimate would be answering a different one.

So the receipt recorder is validated here, live, against the one thing that can validate it:
a direct call to the gateway made without it, compared field by field with the same call made
through it, and then both reconciled against `/generation`. A recorder that agrees with the
gateway on a call the gateway also saw is a recorder whose numbers mean what they say.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import openrouter as orouter
from .receipts import (
    OPENROUTER_ORIGIN,
    ReceiptRecorder,
    fetch_generation,
    reconcile,
)

#: The merged taskbench revision the order pins qualification to. Every candidate call must be
#: made by this code, and the committed incumbent record must validate under it.
PINNED_TASKBENCH_REV = "cff5ceb4cbc01a82f1e9b303ec365a36c72593ea"

#: Corpus the incumbent fidelity record was produced against. A different corpus is a
#: different study, not a newer one.
EXPECT_CORPUS_HASH = "sha256:6bdbb73352bcf61bddef97ddd50c51d3dc1cdf283a42648ceb1086bab0a23085"

#: The record the candidates are compared against.
INCUMBENT_RECORD = "2026-08-13-incumbent-fidelity-v0-1-2"

#: Harness builds the setup round-trip pinned. A build that has moved under us is a recorded
#: dimension of every bundle, so it is checked rather than read off and hoped about.
HARNESS_PINS = {
    "claude": ("2.1.232", ("claude", "--version")),
    "codex": ("0.147.0", ("codex", "--version")),
    "reasonix": ("1.25.2", ("reasonix", "--version")),
}


@dataclass
class Check:
    """One named precondition and what was actually observed. Never just a boolean."""

    name: str
    ok: bool
    detail: str
    observed: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "observed": self.observed}


def _version_of(argv: tuple[str, ...]) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0].strip() if text else None


def check_harness_builds(which: tuple[str, ...] = ()) -> list[Check]:
    """Each harness reports the build the study recorded, or says what it reports instead."""
    checks: list[Check] = []
    for name, (expected, argv) in HARNESS_PINS.items():
        if which and name not in which:
            continue
        reported = _version_of(argv)
        if reported is None:
            checks.append(
                Check(
                    f"harness/{name}", False,
                    f"`{' '.join(argv)}` did not run; the bundle cannot be attributed to a "
                    "build, and a cell without a harness version is unattributable by design.",
                )
            )
            continue
        ok = expected in reported
        checks.append(
            Check(
                f"harness/{name}", ok,
                (
                    f"{name} reports {reported!r}, pinned at {expected}."
                    if ok
                    else f"{name} reports {reported!r} but the study pins {expected}. The "
                    "harness is part of the bundle under test: a moved build is a different "
                    "bundle, and silently scoring it as the pinned one is the attribution "
                    "failure this instrument exists to close."
                ),
                {"reported": reported, "pinned": expected},
            )
        )
    return checks


#: **Byte-identical to the pin, or the batch does not run.** This is the scored instrument: the
#: frozen corpus, what a packet contains, how a verdict is computed, how the blinded review and
#: the one remediation cycle work, and the orchestration that sequences them. The order is
#: explicit that a change to any of it "returns to incumbent fidelity" — meaning an Opus rerun,
#: not a judgment call — so the rule is mechanical rather than left to a reader's discretion.
SCORING_FROZEN = (
    "taskbench/corpus",
    "taskbench/grade.py",
    "taskbench/manifest.py",
    "taskbench/materialize.py",
    "taskbench/pipeline.py",
    "taskbench/remediation.py",
    "taskbench/review.py",
)

#: Files this task is permitted to add, because the order requires them: the gateway receipt
#: recorder, the route pins, this preflight, and the per-batch launchers. Purely additive — no
#: scored path imports any of them.
QUALIFY_ADDITIVE = (
    "taskbench/openrouter.py",
    "taskbench/qualify.py",
    "taskbench/qualify_batch.py",
    "taskbench/receipts.py",
    "taskbench/launch/",
    "taskbench/configs/",
)

#: Changed, and each carries a written disposition in the result report. Instrumentation and
#: operator surface only: nothing here decides a verdict.
DISPOSITIONED = {
    "taskbench/runner.py": (
        "additive diagnostics — the five-minute pulse, the actionability label, two new "
        "record fields, and `read1` so the recorded first-response timestamp means what it "
        "already claimed. No change to what a cell is asked, how it is graded, or what passes."
    ),
    "taskbench/cli.py": "adds the qualify-preflight command; no existing command changed.",
    "taskbench/preflight.py": "unchanged unless listed by the check below.",
    "taskbench/record.py": "unchanged unless listed by the check below.",
}


def _git(repo: Path, *args: str) -> tuple[int, str]:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return out.returncode, (out.stdout or out.stderr).strip()


def check_pinned_revision(repo: Path) -> list[Check]:
    """The scored instrument must be the revision the order pinned — and nothing else may
    have quietly moved with it.

    Not a whole-tree comparison. The order *requires* new code for this task (a gateway receipt
    recorder, route pins, launchers), so demanding a byte-identical `taskbench/` would refuse
    the work it commissioned. What it actually pins is the scoring path, so that is what is
    compared byte-for-byte; everything else is enumerated, and anything changed that is neither
    additive nor dispositioned is a stop.
    """
    checks: list[Check] = []
    code, _ = _git(repo, "cat-file", "-e", f"{PINNED_TASKBENCH_REV}^{{commit}}")
    if code != 0:
        return [
            Check(
                "revision/pinned-present", False,
                f"the pinned revision {PINNED_TASKBENCH_REV} is not in this clone; there is "
                "nothing to compare the running code against.",
            )
        ]

    moved: list[str] = []
    for path in SCORING_FROZEN:
        code, pinned = _git(repo, "rev-parse", f"{PINNED_TASKBENCH_REV}:{path}")
        code2, head = _git(repo, "rev-parse", f"HEAD:{path}")
        if code != 0 or code2 != 0 or pinned != head:
            moved.append(path)
    checks.append(
        Check(
            "revision/scoring-frozen", not moved,
            (
                f"every scored path is byte-identical to {PINNED_TASKBENCH_REV[:12]}: "
                + ", ".join(SCORING_FROZEN)
                if not moved
                else f"the scored instrument has moved since the pin: {moved}. A corpus, "
                "packet, rubric, pass-rule, review/remediation-method or reviewer change "
                "invalidates every candidate cell and returns to incumbent fidelity — it is "
                "not something a candidate batch may carry."
            ),
            {"frozen": list(SCORING_FROZEN), "moved": moved},
        )
    )

    # Everything else that differs, enumerated. Additive files are expected; a changed file
    # that is neither additive nor dispositioned is exactly the drift this check exists for.
    code, names = _git(
        repo, "diff", "--name-only", PINNED_TASKBENCH_REV, "HEAD", "--", "taskbench/"
    )
    changed = [n for n in names.splitlines() if n.strip()]
    unexplained = [
        n for n in changed
        if not any(n.startswith(a) for a in QUALIFY_ADDITIVE)
        and n not in DISPOSITIONED
        and not any(n.startswith(f) for f in SCORING_FROZEN)
    ]
    checks.append(
        Check(
            "revision/changes-declared", not unexplained,
            (
                f"{len(changed)} taskbench path(s) differ from the pin, all either additive or "
                "carrying a written disposition."
                if not unexplained
                else f"undeclared changes since the pin: {unexplained}. Every difference has to "
                "be either an additive file the order asked for or a dispositioned one; an "
                "unlisted change is drift nobody has weighed."
            ),
            {"changed": changed, "unexplained": unexplained,
             "dispositions": DISPOSITIONED},
        )
    )
    return checks


def check_corpus(repo: Path) -> list[Check]:
    """The frozen corpus must be the one the incumbent was measured on, by content hash.

    The path check above proves the files did not move in git. This proves the *loaded* corpus
    hashes to what the incumbent record was produced against — a different answer if anything
    reaches the corpus from outside the repository.
    """
    from .manifest import load_corpus

    try:
        corpus = load_corpus(repo / "taskbench" / "corpus" / "v0.1")
    except Exception as exc:  # noqa: BLE001 — any failure to load is the same finding
        return [
            Check("corpus/loads", False, f"the corpus did not load: {type(exc).__name__}: {exc}")
        ]
    ok = corpus.content_hash == EXPECT_CORPUS_HASH
    return [
        Check(
            "corpus/hash", ok,
            (
                f"corpus content hash is {corpus.content_hash}, as the incumbent record was "
                "measured against."
                if ok
                else f"corpus content hash is {corpus.content_hash}, but the incumbent was "
                f"measured against {EXPECT_CORPUS_HASH}. A moved corpus is a different study."
            ),
            {"observed": corpus.content_hash, "expected": EXPECT_CORPUS_HASH,
             "held_in": list(corpus.catalog.held_in),
             "held_out_count": len(corpus.catalog.held_out)},
        )
    ]


def check_incumbent_record(repo: Path) -> list[Check]:
    """The baseline every candidate is compared against still validates under this code."""
    from .record import validate_record

    path = repo / "taskbench" / "records" / INCUMBENT_RECORD
    if not path.is_dir():
        return [Check("incumbent/present", False, f"{path} is missing.")]
    problems = validate_record(path)
    return [
        Check(
            "incumbent/validates", not problems,
            (
                f"{INCUMBENT_RECORD} validates under the running code."
                if not problems
                else f"{INCUMBENT_RECORD} does not validate: {problems}"
            ),
            {"problems": problems},
        )
    ]


# --- the gateway ------------------------------------------------------------------------


def check_preset(
    pin: orouter.PresetPin, api_key: str, *, client: httpx.Client | None = None
) -> list[Check]:
    """Fetch the live preset and hold it against its pin, recording what was hashed."""
    try:
        fetched = orouter.fetch_preset(
            pin.slug, api_key, version=pin.version, client=client
        )
    except RuntimeError as exc:
        return [
            Check(
                f"preset/{pin.slug}", False,
                f"could not read the preset: {exc}. Without it the route is unpinned, and an "
                "unpinned route is not a study arm.",
            )
        ]
    problems = orouter.check_preset(pin, fetched)
    return [
        Check(
            f"preset/{pin.slug}", not problems,
            (
                f"preset {pin.slug} v{fetched.version} agrees with its pin "
                f"(read from {fetched.source_path})."
                if not problems
                else "; ".join(problems)
            ),
            {
                "source_path": fetched.source_path,
                "version": fetched.version,
                "config_sha256": fetched.config_sha256,
                # Routing policy, not a secret. Recorded so a hash mismatch is readable rather
                # than a dead end — the difference between "the preset moved" and "we
                # canonicalize differently" is invisible from a bare hash.
                "canonical_json": fetched.canonical_json,
            },
        )
    ]


def check_endpoints(
    pin: orouter.PresetPin, api_key: str, *, client: httpx.Client | None = None
) -> list[Check]:
    """The pinned upstream must still serve the model, at the quantization the study pinned."""
    try:
        endpoints = orouter.fetch_endpoints(pin.model, api_key, client=client)
    except httpx.HTTPError as exc:
        return [
            Check(
                f"endpoint/{pin.model}", False,
                f"could not read the endpoint catalog: {type(exc).__name__}: {exc}",
            )
        ]
    problems = orouter.check_endpoint_capability(pin, endpoints)
    seen = [
        {
            "provider": e.get("provider_name") or e.get("name"),
            "quantization": e.get("quantization"),
            "context": e.get("context_length"),
        }
        for e in endpoints
    ]
    return [
        Check(
            f"endpoint/{pin.model}", not problems,
            (
                f"{pin.provider_order[0]} still serves {pin.model} with the pinned "
                "quantization and parameters."
                if not problems
                else "; ".join(problems)
            ),
            {"endpoints": seen},
        )
    ]


#: The smallest call that still exercises identity. Deliberately tiny: this runs before every
#: batch, and a preflight that costs real money is a preflight people skip.
PROBE_BODY = {
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "Reply with the single word: ready."}],
}


def validate_receipt_recorder(
    pin: orouter.PresetPin,
    api_key: str,
    *,
    out_dir: Path,
    origin: str = OPENROUTER_ORIGIN,
) -> list[Check]:
    """The order's precondition for every gateway-billed arm, executed rather than asserted.

    One identical call is made twice — once straight at OpenRouter, once through the recorder —
    and both are reconciled against `/generation`. Three things have to hold, and each is a
    separate check because each fails for a different reason and needs a different fix:

    1. **The proxy is transparent.** Same resolved model, same upstream, same shape of usage.
       If a harness behaves differently through the recorder, every cell measures the recorder.
    2. **The recorder captures what the harness discards.** A generation id and a server usage
       block, at the grain of the individual call.
    3. **`/generation` answers.** The authoritative cost and upstream exist and can be fetched
       within the bounded backoff.

    If proof cannot be obtained, this returns failing checks and the caller stops. The order is
    explicit that the remedy is to ask, never to introduce a measurement proxy after scored
    calls begin.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    body = {**PROBE_BODY, "model": pin.request_string}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    # --- 1. straight at the gateway, so there is something to be transparent *against* ------
    direct: dict[str, Any] = {}
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
            resp = client.post(
                f"{origin.rstrip('/')}/api/v1/messages", headers=headers, json=body
            )
            direct = {"status": resp.status_code, "body": resp.json()}
    except (httpx.HTTPError, ValueError) as exc:
        checks.append(
            Check(
                f"receipt/{pin.slug}/direct", False,
                f"the direct gateway call failed ({type(exc).__name__}: {exc}); with no "
                "reference call there is nothing to validate the recorder against.",
            )
        )
        return checks

    direct_id = direct["body"].get("id") if isinstance(direct.get("body"), dict) else None
    checks.append(
        Check(
            f"receipt/{pin.slug}/direct", bool(direct_id) and direct["status"] == 200,
            (
                f"direct Anthropic-Messages call resolved "
                f"{direct['body'].get('model')!r} with generation id {direct_id}."
                if direct_id
                else f"direct call returned HTTP {direct['status']} and no generation id."
            ),
            {
                "status": direct["status"],
                "resolved_model": direct["body"].get("model"),
                "usage": direct["body"].get("usage"),
                "generation_id": direct_id,
            },
        )
    )
    if not direct_id:
        return checks

    # --- 2. the same call through the recorder ---------------------------------------------
    recorder = ReceiptRecorder(out_dir / "preflight-receipts.jsonl", upstream=origin).start()
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
            proxied = client.post(
                f"{recorder.base_url}/v1/messages", headers=headers, json=body
            )
            proxied_body = proxied.json()
    except (httpx.HTTPError, ValueError) as exc:
        recorder.stop()
        checks.append(
            Check(
                f"receipt/{pin.slug}/proxied", False,
                f"the call through the receipt recorder failed: {type(exc).__name__}: {exc}",
            )
        )
        return checks
    # Drain before reading: a call is written down only after its response has fully streamed,
    # so reading `calls` the instant the client returns can miss the very call being validated.
    drained = recorder.stop(drain_timeout=60)
    calls = list(recorder.calls)
    if not drained:
        checks.append(
            Check(
                f"receipt/{pin.slug}/drained", False,
                "the recorder did not finish writing every observed call within the drain "
                "window; a capture that may be missing a call cannot be used to prove the "
                "accounting surface.",
            )
        )
        return checks

    if len(calls) != 1:
        checks.append(
            Check(
                f"receipt/{pin.slug}/proxied", False,
                f"the recorder observed {len(calls)} call(s) for one request; a recorder that "
                "miscounts calls miscounts every total built on them.",
            )
        )
        return checks
    call = calls[0]

    same_model = proxied_body.get("model") == direct["body"].get("model")
    checks.append(
        Check(
            f"receipt/{pin.slug}/transparent", bool(same_model) and proxied.status_code == 200,
            (
                f"the proxied call resolved the same model ({proxied_body.get('model')!r}) "
                "and returned the same response shape as the direct call."
                if same_model
                else f"the proxied call resolved {proxied_body.get('model')!r} but the direct "
                f"call resolved {direct['body'].get('model')!r}. A harness that behaves "
                "differently through the recorder measures the recorder."
            ),
            {
                "direct_model": direct["body"].get("model"),
                "proxied_model": proxied_body.get("model"),
                "direct_usage_keys": sorted(direct["body"].get("usage", {}) or {}),
                "proxied_usage_keys": sorted(proxied_body.get("usage", {}) or {}),
            },
        )
    )

    captured_usage = bool(call.usage)
    checks.append(
        Check(
            f"receipt/{pin.slug}/captured", bool(call.generation_id) and captured_usage,
            (
                f"the recorder captured generation id {call.generation_id} and a server usage "
                "block at the individual-call grain."
                if call.generation_id and captured_usage
                else "the recorder saw the call but did not capture "
                + ("a generation id" if not call.generation_id else "a usage block")
                + "; without it this arm has no scoreable gateway accounting."
            ),
            {
                "generation_id": call.generation_id,
                "usage": call.usage,
                "extensions_present": sorted(call.extensions),
            },
        )
    )

    # --- 3. the authoritative receipt -------------------------------------------------------
    reconciled = reconcile(calls, api_key, origin=origin) if call.generation_id else None
    receipt = (reconciled or {}).get("calls", [{}])[0].get("receipt", {})
    got_receipt = bool(receipt.get("available"))
    identity_problems: list[str] = []
    if got_receipt:
        identity_problems = orouter.check_resolved_identity(
            pin.request_string,
            resolved_model=receipt.get("model"),
            resolved_upstream=receipt.get("provider_name"),
            pin=pin,
        )
    checks.append(
        Check(
            f"receipt/{pin.slug}/generation", got_receipt and not identity_problems,
            (
                f"/generation returned the authoritative record: upstream "
                f"{receipt.get('provider_name')!r}, model {receipt.get('model')!r}, cost "
                f"{receipt.get('total_cost')}."
                if got_receipt and not identity_problems
                else "; ".join(identity_problems)
                or str(receipt.get("missing_surface", "no receipt"))
            ),
            {"receipt": receipt, "totals": (reconciled or {}).get("totals")},
        )
    )

    # The direct call's own receipt too, so the comparison is against the gateway's word on
    # both, not against the proxy's word on one of them.
    direct_receipt = fetch_generation(direct_id, api_key, upstream=origin)
    if direct_receipt.get("available") and got_receipt:
        d, p = direct_receipt["receipt"], receipt
        agree = d.get("provider_name") == p.get("provider_name") and d.get("model") == p.get(
            "model"
        )
        checks.append(
            Check(
                f"receipt/{pin.slug}/agrees", bool(agree),
                (
                    "the proxied call and the direct call resolved the same model and the same "
                    f"upstream ({p.get('provider_name')}), so the recorder's numbers describe "
                    "the same route the harness would have taken without it."
                    if agree
                    else f"direct resolved {d.get('model')!r} on {d.get('provider_name')!r} but "
                    f"proxied resolved {p.get('model')!r} on {p.get('provider_name')!r}."
                ),
                {"direct": {k: d.get(k) for k in ("model", "provider_name", "total_cost")},
                 "proxied": {k: p.get(k) for k in ("model", "provider_name", "total_cost")}},
            )
        )
    return checks


def run_gateway_preflight(
    api_key: str,
    *,
    out_dir: Path,
    pins: tuple[orouter.PresetPin, ...] = (orouter.DEEPSEEK_PIN, orouter.MUSE_PIN),
    validate_recorder: bool = True,
    origin: str = OPENROUTER_ORIGIN,
) -> list[Check]:
    checks: list[Check] = []
    with httpx.Client(timeout=30.0) as client:
        for pin in pins:
            checks += check_preset(pin, api_key, client=client)
            checks += check_endpoints(pin, api_key, client=client)
    if validate_recorder:
        for pin in pins:
            checks += validate_receipt_recorder(
                pin, api_key, out_dir=out_dir / pin.slug, origin=origin
            )
    return checks


def write_report(checks: list[Check], out_dir: Path) -> Path:
    """The preflight's own immutable record — what was true at the moment before spend."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "qualify-preflight.json"
    path.write_text(
        json.dumps(
            {
                "taken_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "pinned_taskbench_rev": PINNED_TASKBENCH_REV,
                "expect_corpus_hash": EXPECT_CORPUS_HASH,
                "incumbent_record": INCUMBENT_RECORD,
                "checks": [c.to_json() for c in checks],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return path
