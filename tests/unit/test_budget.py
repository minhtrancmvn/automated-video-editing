"""Unit tests for exact analysis pricing and budget reservation controls."""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.analysis.budget import job_budget, reserve_all_uncached_broad_requests
from video_editor.analysis.models import (
    AnalysisBoundaryKind,
    AnalysisChunkData,
    AnalysisResultData,
    ProxyManifestData,
    RequestReservation,
)
from video_editor.analysis.pricing import (
    GEMINI_25_FLASH,
    ModelPricing,
    maximum_request_cost,
    pricing_for_model,
)
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore


def _pricing() -> ModelPricing:
    return ModelPricing(
        model=GEMINI_25_FLASH,
        media_input_usd_per_million_tokens=Decimal("0.30"),
        text_input_usd_per_million_tokens=Decimal("0.10"),
        output_usd_per_million_tokens=Decimal("2.50"),
        source_url="https://example.invalid/gemini-pricing",
        effective_date=date(2026, 9, 28),
    )


@pytest.fixture
def flash_pricing() -> ModelPricing:
    """Provide explicit verified-test pricing without a live lookup."""
    return _pricing()


def _reservation(
    job_id: str,
    request_id: str,
    cache_key: str,
    maximum_cost_usd: Decimal,
    *,
    mode: str = "broad",
) -> RequestReservation:
    return RequestReservation(
        request_id=request_id,
        job_id=job_id,
        cache_key=cache_key,
        mode=mode,
        maximum_cost_usd=maximum_cost_usd,
    )


def _create_job_with_budget(store: JobStore, limit: Decimal) -> str:
    job_id = store.create_job("{}", "{}")
    store.initialize_budget(job_id, limit)
    return job_id


def _proxy_manifest_data() -> ProxyManifestData:
    return ProxyManifestData(
        schema_version=1,
        job_id="job-1",
        source_id="source-1",
        chunk_id="chunk-1",
        source_fingerprint="source-fingerprint",
        source_identity="source-identity",
        source_path="/input/source.mp4",
        source_device=1,
        source_inode=2,
        source_size_bytes=100,
        artifact_path="/generated/proxy.mp4",
        generated_root="/generated",
        generated_root_device=1,
        generated_root_inode=3,
        mapping_version="v1",
        upstream_settings_hash="settings-v1",
        upstream_tool_version="ffmpeg-test",
        source_start=Decimal(0),
        source_end=Decimal(1),
        proxy_start=Decimal(0),
        proxy_end=Decimal(1),
        media_duration=Decimal(1),
        file_size_bytes=50,
        file_digest_sha256="digest",
        file_device=1,
        file_inode=4,
        video_codec="h264",
        video_width=1920,
        video_height=1080,
        video_fps=Decimal(30),
        audio_codec="aac",
        audio_channels=1,
        audio_bitrate_bps=128000,
        audio_probe_bitrate_bps=128000,
        implementation_version="v1",
    )


def _analysis_chunk_data() -> AnalysisChunkData:
    return AnalysisChunkData(
        schema_version=1,
        mapping_version="v1",
        proxy_start=Decimal(0),
        proxy_end=Decimal(1),
        boundary_kind=AnalysisBoundaryKind.SCENE,
        implementation_version="v1",
    )


def _analysis_result_data() -> AnalysisResultData:
    return AnalysisResultData(
        schema_version=1,
        provider="provider",
        model="model",
        request_id="request-1",
        prompt_version="v1",
        response_schema_version="v1",
        implementation_version="v1",
        token_count=3,
        request_token_count=2,
        output_token_count=1,
        normalized_record_ids=("record-1",),
    )


def test_maximum_request_cost_uses_exact_decimal_arithmetic(
    flash_pricing: ModelPricing,
) -> None:
    cost = maximum_request_cost(flash_pricing, 1_500_000, 250_000, 2_000_000)

    assert cost == Decimal("5.475")


@pytest.mark.parametrize("tokens", [(-1, 0, 0), (0, -1, 0), (0, 0, -1), (1.5, 0, 0)])
def test_maximum_request_cost_rejects_negative_or_non_integer_tokens(
    flash_pricing: ModelPricing,
    tokens: tuple[int, int, int],
) -> None:
    with pytest.raises(ValueError, match="token maxima"):
        maximum_request_cost(flash_pricing, *tokens)


@pytest.mark.parametrize(
    "price", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")]
)
def test_model_pricing_rejects_negative_or_non_finite_costs(price: Decimal) -> None:
    with pytest.raises(ValueError, match="prices"):
        ModelPricing(
            model=GEMINI_25_FLASH,
            media_input_usd_per_million_tokens=price,
            text_input_usd_per_million_tokens=Decimal(0),
            output_usd_per_million_tokens=Decimal(0),
            source_url="https://example.invalid/gemini-pricing",
            effective_date=date(2026, 9, 28),
        )


def test_pricing_for_model_uses_injected_pinned_catalog(
    flash_pricing: ModelPricing,
) -> None:
    assert (
        pricing_for_model(GEMINI_25_FLASH, {GEMINI_25_FLASH: (flash_pricing,)})
        == flash_pricing
    )


def test_pricing_for_model_fails_closed_for_mismatched_catalog_entry() -> None:
    mismatched = ModelPricing(
        model="different-model",
        media_input_usd_per_million_tokens=Decimal("0.30"),
        text_input_usd_per_million_tokens=Decimal("0.10"),
        output_usd_per_million_tokens=Decimal("2.50"),
        source_url="https://example.invalid/gemini-pricing",
        effective_date=date(2026, 9, 28),
    )

    with pytest.raises(VideoEditorError) as raised:
        pricing_for_model(GEMINI_25_FLASH, {GEMINI_25_FLASH: (mismatched,)})

    assert raised.value.code == "pricing_unknown"


@pytest.mark.parametrize(
    "catalog",
    ({}, {GEMINI_25_FLASH: ()}, {GEMINI_25_FLASH: (_pricing(), _pricing())}),
)
def test_pricing_for_model_fails_closed_for_missing_or_ambiguous_prices(
    catalog: dict[str, tuple[ModelPricing, ...]],
) -> None:
    with pytest.raises(VideoEditorError) as raised:
        pricing_for_model(GEMINI_25_FLASH, catalog)

    assert raised.value.category == ErrorCategory.PROVIDER
    assert raised.value.code == "pricing_unknown"


def test_job_budget_uses_exact_decimal_arithmetic() -> None:
    assert job_budget(Decimal(5400), Decimal("1.25")) == Decimal("1.875")


@pytest.mark.parametrize("value", [Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_job_budget_rejects_negative_or_non_finite_duration(value: Decimal) -> None:
    with pytest.raises(ValueError, match="duration and cap"):
        job_budget(value)


@pytest.mark.parametrize("value", [Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_job_budget_rejects_negative_or_non_finite_cap(value: Decimal) -> None:
    with pytest.raises(ValueError, match="duration and cap"):
        job_budget(Decimal(1), value)


def test_budget_limit_floors_and_request_maximum_ceils_to_microusd(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.0000009"))
        store.reserve_request(
            _reservation(job_id, "request-1", "cache-1", Decimal("0.1000001"))
        )

        assert store.budget_state(job_id).limit_usd == Decimal(1)
        assert store.budget_state(job_id).reserved_usd == Decimal("0.100001")


def test_batch_reserves_all_uncached_broad_requests_atomically(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        requests = (
            _reservation(job_id, "request-1", "cache-1", Decimal("0.40")),
            _reservation(job_id, "request-2", "cache-2", Decimal("0.40")),
            _reservation(job_id, "request-3", "cache-3", Decimal("0.40")),
        )

        with pytest.raises(VideoEditorError, match="exceeds job budget"):
            reserve_all_uncached_broad_requests(store, job_id, requests)

        assert store.budget_state(job_id).reserved_usd == Decimal(0)


def test_batch_skips_validated_cache_and_reserves_remaining_requests(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        store.save_proxy_manifest(
            job_id, "manifest-1", "digest", _proxy_manifest_data()
        )
        store.save_analysis_chunk(
            job_id,
            "chunk-1",
            "manifest-1",
            source_id="source-1",
            source_start=Decimal(0),
            source_end=Decimal(1),
            data=_analysis_chunk_data(),
        )
        store.save_analysis_result(
            "result-1",
            job_id,
            "cache-1",
            "chunk-1",
            "broad",
            _analysis_result_data(),
            validated=True,
        )

        reserved = reserve_all_uncached_broad_requests(
            store,
            job_id,
            (
                _reservation(job_id, "request-1", "cache-1", Decimal("0.40")),
                _reservation(job_id, "request-2", "cache-2", Decimal("0.40")),
            ),
        )

        assert reserved == ("request-2",)
        assert store.budget_state(job_id).reserved_usd == Decimal("0.40")


def test_two_open_connections_cannot_overreserve(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    with JobStore(database_path) as setup_store:
        job_id = _create_job_with_budget(setup_store, Decimal("1.00"))

    ready_events = (threading.Event(), threading.Event())
    first_store_opened = threading.Event()
    start_event = threading.Event()
    outcomes: list[str] = []

    def reserve(index: int, request_id: str, cache_key: str) -> None:
        if index == 1:
            assert first_store_opened.wait(timeout=5)
        with JobStore(database_path) as store:
            if index == 0:
                first_store_opened.set()
            ready_events[index].set()
            assert start_event.wait(timeout=5)
            try:
                store.reserve_request(
                    _reservation(job_id, request_id, cache_key, Decimal("0.60"))
                )
            except VideoEditorError as error:
                outcomes.append(error.code or "unknown")
            else:
                outcomes.append("reserved")

    threads = (
        threading.Thread(target=reserve, args=(0, "request-1", "cache-1")),
        threading.Thread(target=reserve, args=(1, "request-2", "cache-2")),
    )
    for thread in threads:
        thread.start()
    for ready_event in ready_events:
        assert ready_event.wait(timeout=5)
    start_event.set()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["budget_exhausted", "reserved"]
    with JobStore(database_path) as store:
        assert store.budget_state(job_id).reserved_usd == Decimal("0.60")


def test_reserved_request_can_be_reused_only_with_exact_immutable_fields(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        reservation = _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))

        assert store.reserve_request(reservation) == "request-1"
        assert store.reserve_request(reservation) == "request-1"
        assert store.budget_state(job_id).reserved_usd == Decimal("0.25")

        with pytest.raises(ValueError, match="does not match request fields"):
            store.reserve_request(
                _reservation(job_id, "request-1", "cache-1", Decimal("0.30"))
            )


def test_batch_rejects_duplicate_request_or_cache_identity(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))

        with pytest.raises(ValueError, match="duplicate request_id"):
            store.reserve_request_batch(
                job_id,
                (
                    _reservation(job_id, "request-1", "cache-1", Decimal("0.25")),
                    _reservation(job_id, "request-1", "cache-2", Decimal("0.25")),
                ),
            )
        with pytest.raises(ValueError, match="duplicate job_id and cache_key"):
            store.reserve_request_batch(
                job_id,
                (
                    _reservation(job_id, "request-1", "cache-1", Decimal("0.25")),
                    _reservation(job_id, "request-2", "cache-1", Decimal("0.25")),
                ),
            )

        assert store.budget_state(job_id).reserved_usd == Decimal(0)


@pytest.mark.parametrize("status", ["dispatched", "billing_unknown", "completed"])
def test_nonreserved_request_is_not_dispatchable(tmp_path: Path, status: str) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        reservation = _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
        assert store.reserve_request(reservation) == "request-1"

        match status:
            case "dispatched":
                store.mark_request_dispatched("request-1")
            case "billing_unknown":
                store.mark_request_billing_unknown("request-1")
            case "completed":
                store.settle_request("request-1", Decimal("0.125000"))
            case _:
                raise AssertionError(f"unexpected status: {status}")

        with pytest.raises(ValueError, match="not dispatchable"):
            store.reserve_request(reservation)


def test_unknown_billing_keeps_reservation_after_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    with JobStore(database_path) as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        request_id = store.reserve_request(
            _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
        )
        assert request_id == "request-1"
        store.mark_request_dispatched(request_id)
        store.mark_request_billing_unknown(request_id)

    with JobStore(database_path) as store:
        assert store.budget_state(job_id).reserved_usd == Decimal("0.25")
        with pytest.raises(ValueError, match="not dispatchable"):
            store.reserve_request(
                _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
            )


def test_confirmed_nonbillable_releases_reservation(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        request_id = store.reserve_request(
            _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
        )
        assert request_id == "request-1"

        store.release_confirmed_nonbillable(request_id)

        state = store.budget_state(job_id)
    assert state.spent_usd == Decimal(0)
    assert state.reserved_usd == Decimal(0)


def test_settlement_replaces_reservation_with_exact_actual_cost(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        request_id = store.reserve_request(
            _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
        )
        assert request_id == "request-1"

        store.settle_request(request_id, Decimal("0.125001"))

        state = store.budget_state(job_id)
    assert state.spent_usd == Decimal("0.125001")
    assert state.reserved_usd == Decimal(0)


def test_settlement_rejects_sub_microusd_actual_cost(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        request_id = store.reserve_request(
            _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
        )
        assert request_id == "request-1"

        with pytest.raises(ValueError, match="representable in whole microUSD"):
            store.settle_request(request_id, Decimal("0.1250001"))

        assert store.budget_state(job_id).reserved_usd == Decimal("0.25")


def test_reconciliation_requires_provider_confirmed_outcome(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        request_id = store.reserve_request(
            _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
        )
        assert request_id == "request-1"
        store.mark_request_billing_unknown(request_id)

        with pytest.raises(ValueError, match="exactly one confirmed billing outcome"):
            store.reconcile_unknown_request(request_id)
        with pytest.raises(ValueError, match="exactly one confirmed billing outcome"):
            store.reconcile_unknown_request(
                request_id,
                provider_confirmed_actual_cost_usd=Decimal("0.10"),
                confirmed_nonbillable=True,
            )

        assert store.budget_state(job_id).reserved_usd == Decimal("0.25")


def test_reconciliation_settles_or_releases_only_unknown_billing(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = _create_job_with_budget(store, Decimal("1.00"))
        first = store.reserve_request(
            _reservation(job_id, "request-1", "cache-1", Decimal("0.25"))
        )
        second = store.reserve_request(
            _reservation(job_id, "request-2", "cache-2", Decimal("0.25"))
        )
        assert first == "request-1"
        assert second == "request-2"
        store.mark_request_billing_unknown(first)
        store.mark_request_billing_unknown(second)

        store.reconcile_unknown_request(
            first, provider_confirmed_actual_cost_usd=Decimal("0.10")
        )
        store.reconcile_unknown_request(second, confirmed_nonbillable=True)

        state = store.budget_state(job_id)
    assert state.spent_usd == Decimal("0.10")
    assert state.reserved_usd == Decimal(0)
