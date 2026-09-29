"""Exact offline pricing helpers for analysis provider requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from video_editor.errors import ErrorCategory, VideoEditorError

GEMINI_25_FLASH = "gemini-2.5-flash"
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

    def __post_init__(self) -> None:
        """Reject incomplete or unsafe provider pricing entries."""
        if not self.model:
            raise ValueError("model must not be empty")
        if not self.source_url:
            raise ValueError("source URL must not be empty")
        for price in (
            self.media_input_usd_per_million_tokens,
            self.text_input_usd_per_million_tokens,
            self.output_usd_per_million_tokens,
        ):
            if not price.is_finite() or price < 0:
                raise ValueError("prices must be finite and non-negative")


# No price values are encoded until an official price is independently verified and pinned.
# Keep the required production model key so missing/ambiguous pricing fails closed.
PRODUCTION_PRICING: Mapping[str, tuple[ModelPricing, ...]] = {
    GEMINI_25_FLASH: (),
}


def pricing_for_model(
    model: str,
    catalog: Mapping[str, Sequence[ModelPricing]] = PRODUCTION_PRICING,
) -> ModelPricing:
    """Return one unambiguous pinned price or fail before provider dispatch."""
    entries = tuple(catalog.get(model, ()))
    if len(entries) == 1 and entries[0].model == model:
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
) -> Decimal:
    """Calculate exact maximum request cost from non-negative token maxima."""
    tokens = (media_tokens, prompt_tokens, output_tokens)
    if any(
        isinstance(token, bool) or not isinstance(token, int) or token < 0
        for token in tokens
    ):
        raise ValueError("token maxima must be non-negative integers")
    return (
        Decimal(media_tokens) * pricing.media_input_usd_per_million_tokens
        + Decimal(prompt_tokens) * pricing.text_input_usd_per_million_tokens
        + Decimal(output_tokens) * pricing.output_usd_per_million_tokens
    ) / MILLION_TOKENS
