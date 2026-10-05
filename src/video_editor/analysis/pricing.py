"""Exact offline pricing helpers for analysis provider requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from video_editor.errors import ErrorCategory, VideoEditorError

GEMINI_MODEL = "gemini-3.8-flash"
MILLION_TOKENS = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPricing:
    """Pinned per-million-token prices for one provider model."""

    model: str
    media_input_usd_per_million_tokens: Decimal
    text_input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal
    source_url: str
    effective_date: date
    audio_input_usd_per_million_tokens: Decimal | None = None
    expires_on: date | None = None

    def __post_init__(self) -> None:
        """Reject incomplete or unsafe provider pricing entries."""
        if not self.model:
            raise ValueError("model must not be empty")
        if not self.source_url:
            raise ValueError("source URL must not be empty")
        if self.expires_on is not None and self.expires_on <= self.effective_date:
            raise ValueError("pricing expiry must follow effective date")
        for price in (
            self.media_input_usd_per_million_tokens,
            self.text_input_usd_per_million_tokens,
            self.output_usd_per_million_tokens,
            self.audio_input_usd_per_million_tokens,
        ):
            if price is not None and (not price.is_finite() or price < 0):
                raise ValueError("prices must be finite and non-negative")


# Gemini Developer API Standard rates, not Batch, Flex, caching, or Vertex AI.
# Scheduled prices double on 2027-01-01; require a newly verified pin then.
PRODUCTION_PRICING: Mapping[str, tuple[ModelPricing, ...]] = {
    GEMINI_MODEL: (
        ModelPricing(
            model=GEMINI_MODEL,
            media_input_usd_per_million_tokens=Decimal("0.75"),
            audio_input_usd_per_million_tokens=Decimal("0.75"),
            text_input_usd_per_million_tokens=Decimal("0.75"),
            output_usd_per_million_tokens=Decimal("3.75"),
            source_url="https://ai.google.dev/gemini-api/docs/pricing",
            effective_date=date(2026, 10, 3),
            expires_on=date(2027, 1, 1),
        ),
    ),
}


def pricing_for_model(
    model: str,
    catalog: Mapping[str, Sequence[ModelPricing]] = PRODUCTION_PRICING,
    *,
    on_date: date | None = None,
) -> ModelPricing:
    """Return one unambiguous current pin or fail before provider dispatch."""
    today = on_date if on_date is not None else datetime.now(UTC).date()
    entries = tuple(catalog.get(model, ()))
    if (
        len(entries) == 1
        and entries[0].model == model
        and entries[0].effective_date <= today
        and (entries[0].expires_on is None or today < entries[0].expires_on)
    ):
        return entries[0]
    raise VideoEditorError(
        ErrorCategory.PROVIDER,
        f"pricing is unavailable or ambiguous for model: {model}",
        code="pricing_unknown",
    )


def maximum_request_cost(
    pricing: ModelPricing,
    media_tokens: int,
    prompt_tokens: int,
    output_tokens: int,
    *,
    audio_tokens: int = 0,
) -> Decimal:
    """Calculate exact request cost with audio billed separately from video."""
    tokens = (media_tokens, prompt_tokens, output_tokens, audio_tokens)
    if any(
        isinstance(token, bool) or not isinstance(token, int) or token < 0
        for token in tokens
    ):
        raise ValueError("token maxima must be non-negative integers")
    if audio_tokens and pricing.audio_input_usd_per_million_tokens is None:
        raise VideoEditorError(
            ErrorCategory.BUDGET,
            "audio input pricing is unavailable",
            code="pricing_unknown",
        )
    return (
        Decimal(media_tokens) * pricing.media_input_usd_per_million_tokens
        + Decimal(audio_tokens)
        * (pricing.audio_input_usd_per_million_tokens or Decimal(0))
        + Decimal(prompt_tokens) * pricing.text_input_usd_per_million_tokens
        + Decimal(output_tokens) * pricing.output_usd_per_million_tokens
    ) / MILLION_TOKENS
