"""Synthetic runner tests; operation must never invoke refinement or rendering."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from video_editor.analysis import evaluation

D = Decimal


def test_evaluation_runner_requires_authorization_before_provider_factory():
    calls = []
    with pytest.raises(ValueError, match="approval"):
        evaluation.run_evaluation(
            plan=None,
            approval=None,
            provider_factory=lambda: calls.append("provider"),
            store=object(),
            paths=object(),
        )
    assert calls == []


def test_untyped_evaluation_scope_refused_before_provider_factory():
    calls = []
    with pytest.raises(ValueError, match="approval"):
        evaluation.run_evaluation(
            plan=SimpleNamespace(inputs=(1, 2)),
            approval=SimpleNamespace(),
            provider_factory=lambda: calls.append("provider"),
            store=object(),
            paths=object(),
        )
    assert calls == []


def test_partial_evaluation_cannot_become_completed_selection():
    with pytest.raises(ValueError, match="complete"):
        evaluation.admit_completed_bundle({"complete": False, "decisions": []})
