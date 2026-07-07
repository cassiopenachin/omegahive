"""C2 coordinator-qualification battery (docs/omegahive_c2_battery_spec.md).

Slice 1 — the pure, deterministic grading core: scenario schema, three scenarios
(S1/S3/S8) with fixtures and an op catalog, and a metrics module computed over a
captured-artifacts *bundle*. No LLM, no containers; the bundle is filled by the
slice-2 in-container capture and consumed here as canned fixtures in tests.
"""
