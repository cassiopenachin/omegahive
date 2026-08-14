"""Model identity and route pinning at the gateway.

The order's whole DeepSeek comparison rests on one claim: both arms ran *the same model on the
same silicon*. The same OpenRouter slug is served by providers at different quantizations —
DeepInfra is FP4, GMICloud is FP8 — so a slug alone does not pin an experiment. What pins it is
an operator-owned preset whose only routing rule is a single provider endpoint with fallback
disabled, plus a check that the endpoint still advertises the quantization and the parameters
the study needs.

This module is the refusal layer for that. It fetches the live preset, canonicalizes it, hashes
it, and compares it against a pin — and it checks the pin *semantically as well*, field by
field, because a hash tells you something moved but not what, and "something moved" is not a
diagnosis anyone can act on at three in the morning.

**The canonical bytes are recorded, not just their hash.** A preset config is routing policy,
not a secret, and a bare hash mismatch is undiagnosable: it cannot distinguish a preset that
genuinely changed from a canonicalization that differs from whoever computed the pinned value.
Writing down what was hashed makes that distinction readable instead of a guess.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from .receipts import OPENROUTER_ORIGIN

#: Exact request strings. The order allowlists these and refuses everything else — a moving
#: first-party alias would let the study measure a model nobody chose.
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash-0731"
DEEPSEEK_CANONICAL = "deepseek/deepseek-v4-flash-20260731"
MUSE_MODEL = "meta/muse-spark-1.2"
MUSE_CANONICAL = "meta/muse-spark-1.2-20260805"

#: Prohibited outright by the order, and worth naming rather than merely omitting: a
#: contributor SKU trains on the traffic, which is the reason it is refused and not a detail.
PROHIBITED_MODELS = frozenset({"meta/muse-spark-1.2-contributor"})

ALLOWED_MODELS = frozenset({DEEPSEEK_MODEL, MUSE_MODEL})

#: Canonical ids a resolved response is allowed to report for each requested string. A
#: response naming anything else is a different model and stops the cell.
CANONICAL_FOR = {
    DEEPSEEK_MODEL: {DEEPSEEK_MODEL, DEEPSEEK_CANONICAL},
    MUSE_MODEL: {MUSE_MODEL, MUSE_CANONICAL},
}


@dataclass(frozen=True)
class PresetPin:
    """What an operator-owned preset must still say, checked before every batch and cell."""

    slug: str
    version: int
    model: str
    provider_order: tuple[str, ...]
    allow_fallbacks: bool
    config_sha256: str
    #: Quantization the pinned endpoint must still advertise, when the study depends on it.
    quantization: str | None = None

    @property
    def request_string(self) -> str:
        """Model plus preset, as a harness must send it."""
        return f"{self.model}@preset/{self.slug}"


#: The two pins the order fixes. Version and hash are re-fetched and re-verified at launch;
#: they are written here so that drift is a refusal rather than a shrug.
DEEPSEEK_PIN = PresetPin(
    slug="omegahive-deepseek-v4-flash-0731",
    version=3,
    model=DEEPSEEK_MODEL,
    provider_order=("gmicloud/fp8",),
    allow_fallbacks=False,
    config_sha256="d4baa44ba0d4f552a56d3c40ff3fb5948108e0a6c307689ca66ab1a1f8b7ede5",
    quantization="fp8",
)

MUSE_PIN = PresetPin(
    slug="omegahive-muse-spark-1-2",
    version=1,
    model=MUSE_MODEL,
    provider_order=("meta",),
    allow_fallbacks=False,
    config_sha256="d83f9918465db2dcf400a2944157e73d2deb25868ee08f2773e0b015aad195b1",
)


# --- fetching -------------------------------------------------------------------------------

#: Path shapes tried in order. OpenRouter's preset API has moved between doc revisions, and a
#: launcher that hard-codes one shape fails opaquely when it moves again. Which one answered is
#: recorded, so the record says what surface the pin was read from.
PRESET_PATHS = (
    "/api/v1/presets/{slug}",
    "/api/v1/presets/{slug}/versions/{version}",
    "/api/v1/presets",
)


@dataclass
class FetchedPreset:
    """One live preset as the gateway returned it, plus how it was obtained."""

    slug: str
    source_path: str
    document: dict[str, Any]
    canonical_json: str
    config_sha256: str
    version: int | None = None
    problems: list[str] = field(default_factory=list)


def _config_subtree(document: dict[str, Any]) -> dict[str, Any]:
    """The routing config inside whatever envelope the API wraps it in.

    Tried in order rather than assumed: the envelope carries mutable bookkeeping (timestamps,
    ids, counters) that must never enter the hash, or every fetch would produce a new value and
    the pin would refuse constantly for no reason.
    """
    for key in ("config", "designated_version", "version_config", "data"):
        inner = document.get(key)
        if isinstance(inner, dict):
            # A designated-version envelope nests the config one level further down.
            deeper = inner.get("config")
            return deeper if isinstance(deeper, dict) else inner
    return document


#: Excluded from the hash. Two kinds, and both belong out of it:
#:
#: *Bookkeeping* (`created_at`, `usage_count`, …) changes on its own, and a pin that refuses
#: daily for no reason is a pin everyone learns to override.
#:
#: *Identity* (`slug`, `version`) is checked explicitly and by name a few lines below, so
#: hashing it too would only make the hash redundant with a better check — while making it
#: sensitive to whether the API happened to return the config wrapped or flat. The hash's job
#: is the residue: a system prompt, a temperature, a tool list, anything the field-by-field
#: checks do not name.
_VOLATILE = frozenset(
    {
        "created_at", "updated_at", "id", "owner_id", "user_id", "workspace_id",
        "usage_count", "last_used_at", "is_default", "name", "description",
        "slug", "version", "designated_version", "preset_id",
    }
)


def canonicalize(config: dict[str, Any]) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace, volatile fields dropped.

    Spelled out rather than left to `json.dumps` defaults because the value this produces is
    pinned in the order — the rule has to be readable by whoever has to reproduce it.
    """
    pruned = {k: v for k, v in config.items() if k not in _VOLATILE}
    return json.dumps(pruned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fetch_preset(
    slug: str,
    api_key: str,
    *,
    version: int | None = None,
    origin: str = OPENROUTER_ORIGIN,
    client: httpx.Client | None = None,
) -> FetchedPreset:
    """Read the live preset. Raises only if no documented surface answered at all."""
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    headers = {"Authorization": f"Bearer {api_key}"}
    attempts: list[str] = []
    try:
        for shape in PRESET_PATHS:
            if "{version}" in shape and version is None:
                continue
            path = shape.format(slug=slug, version=version)
            try:
                resp = http.get(origin.rstrip("/") + path, headers=headers)
            except httpx.HTTPError as exc:
                attempts.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if resp.status_code != 200:
                attempts.append(f"{path}: HTTP {resp.status_code}")
                continue
            doc = resp.json()
            if isinstance(doc, dict) and isinstance(doc.get("data"), (dict, list)):
                doc = doc["data"]
            if isinstance(doc, list):
                # The list endpoint: find ours rather than trusting the ordering.
                match = next(
                    (p for p in doc if isinstance(p, dict) and p.get("slug") == slug), None
                )
                if match is None:
                    attempts.append(f"{path}: {slug} not in the preset list")
                    continue
                doc = match
            if not isinstance(doc, dict):
                attempts.append(f"{path}: response was not an object")
                continue
            config = _config_subtree(doc)
            canonical = canonicalize(config)
            found_version = doc.get("version")
            if not isinstance(found_version, int):
                inner = doc.get("designated_version")
                if isinstance(inner, dict) and isinstance(inner.get("version"), int):
                    found_version = inner["version"]
                else:
                    found_version = None
            return FetchedPreset(
                slug=slug,
                source_path=path,
                document=doc,
                canonical_json=canonical,
                config_sha256=sha256_of(canonical),
                version=found_version,
            )
        raise RuntimeError(
            f"no OpenRouter preset surface returned {slug}. Tried: " + "; ".join(attempts)
        )
    finally:
        if owned:
            http.close()


# --- checking -------------------------------------------------------------------------------


def _provider_block(config: dict[str, Any]) -> dict[str, Any]:
    provider = config.get("provider")
    return provider if isinstance(provider, dict) else {}


def check_preset(pin: PresetPin, fetched: FetchedPreset) -> list[str]:
    """Every way the live preset disagrees with its pin, all at once.

    All at once, not first-wins: an operator fixing one disagreement and re-running to discover
    the next is the slow way to find out the preset was rebuilt.

    The semantic checks come first and are the real gate. The hash is a tripwire for anything
    the semantic checks do not name — a system prompt, a temperature, a tool list — and it is
    reported with the canonical bytes so a mismatch can be read rather than guessed at.
    """
    problems: list[str] = []
    config = _config_subtree(fetched.document)
    provider = _provider_block(config)

    model = config.get("model")
    if model in PROHIBITED_MODELS:
        problems.append(
            f"preset {pin.slug} names {model!r}, which the order prohibits outright."
        )
    elif model != pin.model:
        problems.append(
            f"preset {pin.slug} model is {model!r}, pinned as {pin.model!r}. A different model "
            "is a different experiment."
        )

    order = provider.get("order")
    order_tuple = tuple(order) if isinstance(order, list) else None
    if order_tuple != pin.provider_order:
        problems.append(
            f"preset {pin.slug} provider.order is {order!r}, pinned as "
            f"{list(pin.provider_order)!r}. The upstream is the experiment's silicon, not a "
            "preference."
        )

    # Absent means OpenRouter's default, which is fallback ENABLED. Treating a missing key as
    # the safe value is how an unpinned upstream gets into a matched pair unnoticed.
    fallbacks = provider.get("allow_fallbacks", True)
    if bool(fallbacks) != pin.allow_fallbacks:
        problems.append(
            f"preset {pin.slug} allow_fallbacks is {fallbacks!r}, pinned as "
            f"{pin.allow_fallbacks!r}. With fallback on, a busy upstream silently becomes a "
            "different one mid-batch."
        )

    if fetched.version is not None and fetched.version != pin.version:
        problems.append(
            f"preset {pin.slug} is at version {fetched.version}, pinned at version "
            f"{pin.version}. A new version is a new configuration even if it looks the same."
        )

    if fetched.config_sha256 != pin.config_sha256:
        problems.append(
            f"preset {pin.slug} canonical config hash is {fetched.config_sha256}, pinned as "
            f"{pin.config_sha256}. What was hashed is recorded beside this so the difference "
            "can be read: if every semantic check above agrees, suspect the canonicalization "
            "rule before suspecting the preset, and settle it with the operator rather than "
            "re-pinning to whatever today's fetch returned."
        )
    return problems


def check_requested_model(requested: str) -> list[str]:
    """The exact string a harness is about to send, before it sends it."""
    bare = requested.split("@preset/")[0]
    if bare in PROHIBITED_MODELS:
        return [f"{bare!r} is prohibited by the order."]
    if bare not in ALLOWED_MODELS:
        return [
            f"{bare!r} is not on the order's exact-model allowlist "
            f"({sorted(ALLOWED_MODELS)}). Moving aliases and substitutions refuse: if a named "
            "model is no longer served, that is a question for the operator, not a swap."
        ]
    return []


def check_resolved_identity(
    requested: str,
    *,
    resolved_model: str | None,
    resolved_upstream: str | None,
    pin: PresetPin,
) -> list[str]:
    """What the gateway said it actually ran, against what the study asked for.

    This is the check that the whole matched-pair claim reduces to, and it runs against the
    `/generation` receipt rather than the harness's own report — a harness that resolves the
    wrong model has no reason to know it did.
    """
    problems: list[str] = []
    bare = requested.split("@preset/")[0]
    allowed = CANONICAL_FOR.get(bare, {bare})
    if resolved_model is None:
        problems.append(
            f"no resolved model id for a call requesting {requested!r}: the gateway receipt is "
            "missing, so the cell's identity is unproven."
        )
    elif resolved_model not in allowed:
        problems.append(
            f"requested {requested!r} but the gateway resolved {resolved_model!r}, which is "
            f"not among {sorted(allowed)}."
        )

    expected_upstream = pin.provider_order[0] if pin.provider_order else None
    if resolved_upstream is None:
        problems.append(
            "no resolved upstream for this call: without it, fallback to another provider "
            "cannot be ruled out, which is the DeepSeek pair's central claim."
        )
    elif expected_upstream and not _upstream_matches(resolved_upstream, expected_upstream):
        problems.append(
            f"the gateway resolved upstream {resolved_upstream!r}, but the preset pins "
            f"{expected_upstream!r}. This is the fallback the pin exists to prevent."
        )
    return problems


def _upstream_matches(resolved: str, pinned: str) -> bool:
    """`provider_name` is a display name ("GMICloud"); a preset order entry is a slug with a
    quantization suffix ("gmicloud/fp8"). Compare on the vendor part, case-folded."""
    return resolved.strip().lower().replace(" ", "") == pinned.split("/")[0].strip().lower()


# --- endpoint capability --------------------------------------------------------------------


def fetch_endpoints(
    model: str,
    api_key: str,
    *,
    origin: str = OPENROUTER_ORIGIN,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Every provider endpoint currently serving a model, with its advertised capabilities."""
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(
            f"{origin.rstrip('/')}/api/v1/models/{model}/endpoints",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        doc = resp.json()
        data = doc.get("data") if isinstance(doc, dict) else None
        endpoints = data.get("endpoints") if isinstance(data, dict) else None
        return [e for e in (endpoints or []) if isinstance(e, dict)]
    finally:
        if owned:
            http.close()


def check_endpoint_capability(
    pin: PresetPin,
    endpoints: list[dict[str, Any]],
    *,
    require_parameters: tuple[str, ...] = ("tools", "reasoning"),
) -> list[str]:
    """The pinned upstream must still be served, and still advertise what the study needs.

    An endpoint that quietly stops offering FP8, tool calling or a reasoning control has not
    disappeared — it has become a different measurement wearing the same name.
    """
    if not pin.provider_order:
        return ["the pin names no provider order; there is nothing to check against."]
    wanted = pin.provider_order[0]
    vendor = wanted.split("/")[0]
    matches = [
        e for e in endpoints
        if _upstream_matches(str(e.get("provider_name") or e.get("name") or ""), vendor)
    ]
    if not matches:
        seen = sorted({str(e.get("provider_name") or e.get("name")) for e in endpoints})
        return [
            f"the pinned upstream {wanted!r} is not currently serving {pin.model}. Available: "
            f"{seen}. This is a stop, not permission to load-balance onto another provider."
        ]

    problems: list[str] = []
    if pin.quantization:
        quants = {str(e.get("quantization") or "").lower() for e in matches}
        if pin.quantization.lower() not in quants:
            problems.append(
                f"the {wanted!r} endpoint advertises quantization {sorted(quants)}, pinned as "
                f"{pin.quantization!r}. The same slug at a different quantization is different "
                "silicon, which is precisely what this pin exists to hold fixed."
            )
    supported: set[str] = set()
    for endpoint in matches:
        params = endpoint.get("supported_parameters")
        if isinstance(params, list):
            supported.update(str(p) for p in params)
    if not supported:
        # An absent surface is not an agreement. Reading "no `supported_parameters` field" as
        # "everything is supported" would let a refusal layer report the route as proved on the
        # strength of a field the API stopped sending — the exact shape of drift `PRESET_PATHS`
        # already anticipates one endpoint away from here.
        problems.append(
            f"the {wanted!r} endpoint advertises no supported_parameters at all, so "
            f"{list(require_parameters)} cannot be confirmed. An absent surface is unproven, "
            "not agreed."
        )
    for needed in require_parameters:
        if supported and needed not in supported:
            problems.append(
                f"the {wanted!r} endpoint does not advertise {needed!r} "
                f"(advertises {sorted(supported)}). The study's cells need it."
            )
    return problems
