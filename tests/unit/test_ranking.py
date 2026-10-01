from __future__ import annotations

import random
from decimal import Decimal

import pytest
from pydantic import ValidationError

from video_editor.analysis.models import PairwiseSimilarity, RankingCandidateEvidence
from video_editor.analysis.ranking import RankingSettings, rank_candidates

D = Decimal


def candidate_fixture(
    candidate_id: str = "candidate-a",
    **updates: object,
) -> RankingCandidateEvidence:
    payload: dict[str, object] = {
        "candidate_id": candidate_id,
        "source_id": "source-a",
        "source_start": D("10"),
        "source_end": D("20"),
        "category": "action",
        "event_id": f"event-{candidate_id}",
        "location_id": f"location-{candidate_id}",
        "confidence": D("0.8"),
        "action": D("0.8"),
        "scenic": D("0.4"),
        "human": D("0.5"),
        "story": D("0.6"),
        "technical": D("0.9"),
        "novelty": D("0.7"),
        "completeness": D("0.8"),
        "long_story": D("0.6"),
        "short": D("0.7"),
        "vertical": D("0.5"),
        "blur_exposure": D("0.1"),
        "shake_obstruction": D("0.2"),
        "incomplete": D("0.2"),
        "weak_boundary": D("0.1"),
        "repetition": D("0.1"),
        "overlap": D("0.1"),
        "evidence_ids": ("broad-1", "refinement-1", "technical-1"),
    }
    payload.update(updates)
    return RankingCandidateEvidence.model_validate(payload)


def unrestricted_settings(**updates: object) -> RankingSettings:
    payload: dict[str, object] = {
        "max_selected": 20,
        "selection_threshold": D("-1"),
        "max_category_share": D("1"),
        "max_source_share": D("1"),
        "max_event_share": D("1"),
        "max_location_share": D("1"),
    }
    payload.update(updates)
    return RankingSettings(**payload)


def test_score_exposes_every_dimension_and_penalty() -> None:
    ranked = rank_candidates([candidate_fixture()], unrestricted_settings())[0]

    assert set(ranked.score.positive) == {
        "action",
        "scenic",
        "human",
        "story",
        "technical",
        "novelty",
        "completeness",
        "long_story",
        "short",
        "vertical",
    }
    assert set(ranked.score.penalties) == {
        "blur_exposure",
        "shake_obstruction",
        "incomplete",
        "weak_boundary",
        "repetition",
        "overlap",
    }
    assert ranked.score.total == sum(
        ranked.score.positive.values(), start=D("0")
    ) - sum(ranked.score.penalties.values(), start=D("0"))


def test_settings_expose_versioned_decimal_weight_totals() -> None:
    settings = RankingSettings()

    assert settings.version == "ranking-v1"
    assert settings.positive_weight_total == D("1.00")
    assert settings.penalty_weight_total == D("0.50")
    assert sum(settings.positive_weights.values(), start=D("0")) == D("1.00")
    assert sum(settings.penalty_weights.values(), start=D("0")) == D("0.50")


def test_score_clamps_finite_inputs_but_preserves_raw_values_and_evidence() -> None:
    candidate = candidate_fixture(
        action=D("1.4"),
        scenic=D("-0.2"),
        blur_exposure=D("2"),
        evidence_ids=("broad-raw", "refined-raw", "local-raw"),
    )

    ranked = rank_candidates([candidate], unrestricted_settings())[0]

    assert ranked.score.raw["action"] == D("1.4")
    assert ranked.score.raw["scenic"] == D("-0.2")
    assert ranked.score.raw["blur_exposure"] == D("2")
    assert ranked.score.positive["action"] == D("0.12")
    assert ranked.score.positive["scenic"] == D("0")
    assert ranked.score.penalties["blur_exposure"] == D("0.10")
    assert ranked.evidence_ids == ("broad-raw", "refined-raw", "local-raw")


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_candidate_rejects_non_finite_normalized_input(value: Decimal) -> None:
    with pytest.raises(ValidationError):
        candidate_fixture(action=value)


def test_candidate_rejects_opaque_provider_total() -> None:
    with pytest.raises(ValidationError, match="provider_total"):
        candidate_fixture(provider_total=D("0.9"))


def test_technical_hard_failure_disables_both_formats_with_reason_codes() -> None:
    ranked = rank_candidates(
        [
            candidate_fixture(
                technical_failure_codes=("unusable_exposure", "corrupt_frames")
            )
        ],
        unrestricted_settings(),
    )[0]

    assert ranked.eligible_long is False
    assert ranked.eligible_short is False
    assert ranked.selected is False
    assert ranked.reason_codes == ("unusable_exposure", "corrupt_frames")
    assert [rejection.reason_code for rejection in ranked.rejections] == [
        "unusable_exposure",
        "corrupt_frames",
    ]


def test_equal_scores_use_stable_source_time_candidate_order() -> None:
    candidates = [
        candidate_fixture(
            candidate_id,
            source_id=source_id,
            source_start=start,
            source_end=start + D("0.5"),
        )
        for candidate_id, source_id, start in (
            ("candidate-c", "source-b", D("2")),
            ("candidate-b", "source-a", D("2")),
            ("candidate-a", "source-a", D("2")),
            ("candidate-d", "source-a", D("1")),
        )
    ]
    first_input = candidates.copy()
    second_input = candidates.copy()
    random.Random(1).shuffle(first_input)
    random.Random(2).shuffle(second_input)

    first = rank_candidates(first_input, unrestricted_settings())
    second = rank_candidates(second_input, unrestricted_settings())

    assert [candidate.candidate_id for candidate in first] == [
        "candidate-d",
        "candidate-c",
        "candidate-b",
        "candidate-a",
    ]
    assert [candidate.candidate_id for candidate in first] == [
        candidate.candidate_id for candidate in second
    ]


@pytest.mark.parametrize(
    ("left_updates", "right_updates"),
    [
        (
            {"source_id": "source-a", "source_start": D("0"), "source_end": D("10")},
            {"source_id": "source-a", "source_start": D("2"), "source_end": D("8")},
        ),
        (
            {
                "similarities": (
                    PairwiseSimilarity(
                        other_candidate_id="candidate-b",
                        visual=D("0.95"),
                        evidence_ids=("visual-1",),
                    ),
                )
            },
            {},
        ),
        (
            {
                "similarities": (
                    PairwiseSimilarity(
                        other_candidate_id="candidate-b",
                        semantic=D("0.95"),
                        evidence_ids=("semantic-1",),
                    ),
                )
            },
            {},
        ),
        ({"exact_event_id": "event-shared"}, {"exact_event_id": "event-shared"}),
    ],
    ids=("temporal", "visual", "semantic", "event"),
)
def test_dedup_edges_reject_lower_ranked_candidate_with_winner_reference(
    left_updates: dict[str, object],
    right_updates: dict[str, object],
) -> None:
    left = candidate_fixture("candidate-a", action=D("0.9"), **left_updates)
    right = candidate_fixture("candidate-b", action=D("0.3"), **right_updates)

    ranked = rank_candidates([right, left], unrestricted_settings())
    by_id = {candidate.candidate_id: candidate for candidate in ranked}

    assert by_id["candidate-a"].dedup_group_id == by_id["candidate-b"].dedup_group_id
    assert by_id["candidate-a"].selected is True
    assert by_id["candidate-b"].selected is False
    assert by_id["candidate-b"].rejections[0].reason_code == "duplicate"
    assert by_id["candidate-b"].rejections[0].winner_candidate_id == "candidate-a"


@pytest.mark.parametrize(
    ("left_updates", "right_updates"),
    [
        (
            {"source_id": "source-a", "source_start": D("0"), "source_end": D("5")},
            {"source_id": "source-a", "source_start": D("5"), "source_end": D("10")},
        ),
        (
            {"source_id": "source-a", "source_start": D("0"), "source_end": D("5")},
            {"source_id": "source-a", "source_start": D("6"), "source_end": D("10")},
        ),
        (
            {"source_id": "source-a", "source_start": D("0"), "source_end": D("5")},
            {"source_id": "source-b", "source_start": D("0"), "source_end": D("5")},
        ),
    ],
    ids=("boundary-touching", "disjoint", "different-source"),
)
def test_zero_temporal_threshold_requires_positive_overlap(
    left_updates: dict[str, object],
    right_updates: dict[str, object],
) -> None:
    ranked = rank_candidates(
        [
            candidate_fixture("candidate-a", **left_updates),
            candidate_fixture("candidate-b", **right_updates),
        ],
        unrestricted_settings(temporal_overlap_threshold=D("0")),
    )

    assert len({candidate.dedup_group_id for candidate in ranked}) == 2
    assert all(candidate.selected for candidate in ranked)


def test_connected_component_id_is_stable_and_transitive() -> None:
    first = candidate_fixture(
        "candidate-a",
        similarities=(
            PairwiseSimilarity(
                other_candidate_id="candidate-b",
                semantic=D("0.95"),
                evidence_ids=("semantic-ab",),
            ),
        ),
    )
    middle = candidate_fixture(
        "candidate-b",
        similarities=(
            PairwiseSimilarity(
                other_candidate_id="candidate-c",
                visual=D("0.95"),
                evidence_ids=("visual-bc",),
            ),
        ),
    )
    last = candidate_fixture("candidate-c")

    forward = rank_candidates([first, middle, last], unrestricted_settings())
    reverse = rank_candidates([last, middle, first], unrestricted_settings())

    assert len({candidate.dedup_group_id for candidate in forward}) == 1
    assert {
        candidate.candidate_id: candidate.dedup_group_id for candidate in forward
    } == {candidate.candidate_id: candidate.dedup_group_id for candidate in reverse}


@pytest.mark.parametrize(
    ("field", "setting", "reason_code"),
    [
        ("category", "max_category_share", "category_quota"),
        ("source_id", "max_source_share", "source_quota"),
        ("event_id", "max_event_share", "event_quota"),
        ("location_id", "max_location_share", "location_quota"),
    ],
)
def test_diversity_quotas_record_machine_readable_reason_and_winner(
    field: str,
    setting: str,
    reason_code: str,
) -> None:
    common = "shared"
    first_updates = {
        field: common,
        "action": D("1"),
        "source_start": D("0"),
        "source_end": D("5"),
    }
    second_updates = {
        field: common,
        "action": D("0.9"),
        "source_start": D("10"),
        "source_end": D("15"),
    }
    alternative_updates = {
        field: "alternative",
        "action": D("0.8"),
        "source_start": D("20"),
        "source_end": D("25"),
    }
    settings = unrestricted_settings(max_selected=4, **{setting: D("0.25")})

    ranked = rank_candidates(
        [
            candidate_fixture("candidate-a", **first_updates),
            candidate_fixture("candidate-b", **second_updates),
            candidate_fixture("candidate-c", **alternative_updates),
        ],
        settings,
    )
    by_id = {candidate.candidate_id: candidate for candidate in ranked}

    assert by_id["candidate-a"].selected is True
    assert by_id["candidate-b"].selected is False
    assert by_id["candidate-b"].rejections[0].reason_code == reason_code
    assert by_id["candidate-b"].rejections[0].winner_candidate_id == "candidate-a"
    assert by_id["candidate-c"].selected is True


@pytest.mark.parametrize(
    ("field", "setting", "reason_code"),
    [
        ("category", "max_category_share", "category_quota"),
        ("source_id", "max_source_share", "source_quota"),
        ("event_id", "max_event_share", "event_quota"),
        ("location_id", "max_location_share", "location_quota"),
    ],
)
def test_zero_share_has_zero_capacity(
    field: str,
    setting: str,
    reason_code: str,
) -> None:
    ranked = rank_candidates(
        [candidate_fixture("candidate-a", **{field: "value"})],
        unrestricted_settings(max_selected=4, **{setting: D("0")}),
    )

    assert ranked[0].selected is False
    assert ranked[0].reason_codes == (reason_code,)
    assert ranked[0].rejections[0].winner_candidate_id is None


@pytest.mark.parametrize(
    ("field", "setting", "reason_code"),
    [
        ("category", "max_category_share", "category_quota"),
        ("source_id", "max_source_share", "source_quota"),
        ("event_id", "max_event_share", "event_quota"),
        ("location_id", "max_location_share", "location_quota"),
    ],
)
def test_positive_fractional_share_has_minimum_one_fixed_slot(
    field: str,
    setting: str,
    reason_code: str,
) -> None:
    ranked = rank_candidates(
        [
            candidate_fixture(
                "candidate-a",
                source_start=D("0"),
                source_end=D("5"),
                **{field: "shared"},
            ),
            candidate_fixture(
                "candidate-b",
                source_start=D("10"),
                source_end=D("15"),
                **{field: "shared"},
            ),
        ],
        unrestricted_settings(max_selected=3, **{setting: D("0.1")}),
    )

    assert ranked[0].selected is True
    assert ranked[1].selected is False
    assert ranked[1].reason_codes == (reason_code,)
    assert ranked[1].rejections[0].winner_candidate_id == "candidate-a"


@pytest.mark.parametrize(
    ("field", "setting"),
    [
        ("category", "max_category_share"),
        ("source_id", "max_source_share"),
        ("event_id", "max_event_share"),
        ("location_id", "max_location_share"),
    ],
)
def test_fixed_quota_capacity_uses_target_slots_with_fewer_candidates(
    field: str,
    setting: str,
) -> None:
    ranked = rank_candidates(
        [
            candidate_fixture(
                "candidate-a",
                source_start=D("0"),
                source_end=D("5"),
                **{field: "shared"},
            ),
            candidate_fixture(
                "candidate-b",
                source_start=D("10"),
                source_end=D("15"),
                **{field: "shared"},
            ),
        ],
        unrestricted_settings(max_selected=10, **{setting: D("0.2")}),
    )

    assert all(candidate.selected for candidate in ranked)


@pytest.mark.parametrize(
    ("field", "setting"),
    [
        ("category", "max_category_share"),
        ("source_id", "max_source_share"),
        ("event_id", "max_event_share"),
        ("location_id", "max_location_share"),
    ],
)
def test_fixed_quota_allows_distinct_values(
    field: str,
    setting: str,
) -> None:
    ranked = rank_candidates(
        [
            candidate_fixture(
                "candidate-a",
                source_start=D("0"),
                source_end=D("5"),
                **{field: "first"},
            ),
            candidate_fixture(
                "candidate-b",
                source_start=D("10"),
                source_end=D("15"),
                **{field: "second"},
            ),
        ],
        unrestricted_settings(max_selected=2, **{setting: D("0.5")}),
    )

    assert all(candidate.selected for candidate in ranked)


def test_category_cannot_dominate_when_alternatives_clear_threshold() -> None:
    candidates = [
        candidate_fixture(
            f"action-{index}",
            category="action",
            action=D("1") - D(index) / D("10"),
            source_start=D(index * 10),
            source_end=D(index * 10 + 5),
        )
        for index in range(3)
    ] + [
        candidate_fixture(
            f"scenic-{index}",
            category="scenic",
            action=D("0.6") - D(index) / D("10"),
            source_start=D(30 + index * 10),
            source_end=D(35 + index * 10),
        )
        for index in range(2)
    ]

    ranked = rank_candidates(
        candidates,
        unrestricted_settings(max_selected=4, max_category_share=D("0.5")),
    )
    selected = [candidate for candidate in ranked if candidate.selected]

    assert len(selected) == 4
    assert sum(candidate.category == "action" for candidate in selected) == 2
    assert sum(candidate.category == "scenic" for candidate in selected) == 2
    rejected = next(
        candidate for candidate in ranked if candidate.candidate_id == "action-2"
    )
    assert rejected.reason_codes == ("category_quota",)
