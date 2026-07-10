"""litellm-backed LLM client for the R1 vanilla coordinator (stage 2 V2b).

One interface over Anthropic + OpenRouter selected by the model string
(`"openrouter/…"`, `"anthropic/…"`). Usage and USD cost come from litellm's maintained
price map, so cost — a primary §7 outcome — needs no hand-rolled pricing table. The
provider key is read from the environment *by litellm* (`ANTHROPIC_API_KEY` /
`OPENROUTER_API_KEY`), never via the `OMEGAHIVE_`-prefixed `Settings`: the provider
credential stays out of the substrate's `OMEGAHIVE_` namespace and lives only in the R1
process (the credential-scope separation, deployment spec / stage2 §5.2).

Tests pass `mock_response=` so litellm returns a canned completion with no network or key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import litellm

# Keep litellm offline-safe and quiet inside the harness: no update pings, no banner.
litellm.telemetry = False
litellm.suppress_debug_info = True
# The Anthropic 4.8-family (Opus 4.8, Sonnet 5, Fable 5) removed the sampling knobs:
# temperature/top_p/top_k return 400. Anthropic exposes no seed or determinism control,
# and even temperature=0 was only ever near-deterministic. litellm's documented remedy is
# drop_params — an unsupported sampling param is dropped rather than raising
# UnsupportedParamsError — so these models run at provider default while OpenRouter cells
# still honor temperature=0. The frozen run-config's `sampling` pin records this asymmetry
# (V4: strong-cell stochasticity is absorbed by 20-seed aggregation + §7 boundary-replication).
litellm.drop_params = True

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    model: str
    usd: float


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: Usage


class LLMClient:
    """One-shot chat client. `complete(system, user)` returns the assistant text plus a
    `Usage` (tokens in/out, resolved model id, USD cost)."""

    def __init__(
        self,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        mock_response: str | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.mock_response = mock_response

    def complete(self, system: str, user: str) -> LLMResponse:
        resp = litellm.completion(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            mock_response=self.mock_response,
        )
        # a moderation block / truncated error can surface as choices=[]; degrade to an empty
        # turn (parses to no ops) rather than an IndexError that aborts the whole seed.
        choices = getattr(resp, "choices", None) or []
        text = (choices[0].message.content or "").strip() if choices else ""
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        model = getattr(resp, "model", None) or self.model
        try:
            usd = float(litellm.completion_cost(completion_response=resp))
        except Exception as exc:  # unpriced model / mock — cost must never crash a run
            log.warning("completion_cost unavailable for %s: %s", model, exc)
            usd = 0.0
        return LLMResponse(
            text=text,
            usage=Usage(tokens_in=tokens_in, tokens_out=tokens_out, model=model, usd=usd),
        )
