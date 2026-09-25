"""`review.passed` / `review.failed` may say what kind of review they record, so an
operator's acceptance at close is never mistaken for an independent review."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omegahive.events.types import ReviewFailed, ReviewPassed


def test_review_kind_is_optional_so_existing_events_still_validate():
    assert ReviewPassed(ref_result="r").review_kind is None
    assert ReviewFailed(ref_result="r").review_kind is None


@pytest.mark.parametrize("kind", ["independent", "operator_acceptance"])
def test_review_kind_accepts_the_two_kinds(kind):
    assert ReviewPassed(ref_result="r", review_kind=kind).review_kind == kind
    assert ReviewFailed(ref_result="r", review_kind=kind).review_kind == kind


def test_review_kind_refuses_anything_else():
    with pytest.raises(ValidationError):
        ReviewPassed(ref_result="r", review_kind="vibes")
