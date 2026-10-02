"""Application configuration and path resolution."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from video_editor.analysis.pricing import GEMINI_MODEL
from video_editor.errors import ErrorCategory, VideoEditorError

# Worst-case reservation ceiling per source hour. This is a runtime spending
# limit; the release evaluation separately requires actual spend of at most
# USD 1.00 per source hour.
MAX_COST_PER_SOURCE_HOUR_USD = Decimal("3.00")


@dataclass(frozen=True)
class PathSettings:
    """Configured locations used by local workflows."""

    input_dir: Path
    workspace_dir: Path
    cache_dir: Path
    output_dir: Path
    state_dir: Path


@dataclass(frozen=True)
class GeminiSettings:
    """Validated Gemini execution settings without credential material."""

    enabled: bool = False
    model: str = GEMINI_MODEL
    broad_fps: Decimal = Decimal("0.5")
    candidate_min_fps: int = 2
    candidate_max_fps: int = 5
    max_cost_per_source_hour_usd: Decimal = MAX_COST_PER_SOURCE_HOUR_USD
    chunk_target_seconds: int = 720
    chunk_min_seconds: int = 600
    chunk_max_seconds: int = 900
    upload_poll_seconds: int = 5
    max_transient_attempts: int = 3
    max_schema_repair_attempts: int = 1


@dataclass(frozen=True)
class HighlightSettings:
    """Validated automatic highlight output settings."""

    max_short_count: int = 5
    long_max_seconds: int = 1800
    short_max_seconds: int = 180
    cross_short_overlap_ratio: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        """Enforce fixed cross-short overlap product policy."""
        if self.cross_short_overlap_ratio != Decimal("0.10"):
            raise VideoEditorError(
                ErrorCategory.CONFIGURATION,
                "highlights.cross_short_overlap_ratio must be 0.10",
            )


@dataclass(frozen=True)
class CropSettings:
    """Validated subject-tracking crop safety limits."""

    max_velocity_widths_per_second: Decimal = Decimal("0.25")
    max_acceleration_widths_per_second_squared: Decimal = Decimal("0.50")
    safe_margin_ratio: Decimal = Decimal("0.05")
    minimum_subject_retention_ratio: Decimal = Decimal("0.95")
    max_fallback_hold_seconds: Decimal = Decimal(2)


@dataclass(frozen=True)
class AppConfig:
    """Validated application settings."""

    paths: PathSettings
    storage_reserve_bytes: int
    cloud_enabled: bool = False
    render_concurrency: int = 1
    gemini: GeminiSettings = GeminiSettings()
    highlights: HighlightSettings = HighlightSettings()
    crop: CropSettings = CropSettings()


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, f"paths.{name} must be a non-empty string"
        )
    return Path(value).expanduser()


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise VideoEditorError(ErrorCategory.CONFIGURATION, f"{name} must be a decimal")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, f"{name} must be a decimal"
        ) from exc
    if not decimal.is_finite():
        raise VideoEditorError(ErrorCategory.CONFIGURATION, f"{name} must be finite")
    return decimal


def _int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, f"{name} must be an integer"
        )
    return value


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise VideoEditorError(ErrorCategory.CONFIGURATION, f"{name} must be boolean")
    return value


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise VideoEditorError(ErrorCategory.CONFIGURATION, f"[{name}] must be a table")
    return value


def _gemini_settings(raw: dict[str, Any]) -> GeminiSettings:
    values = _table(raw, "gemini")
    result = GeminiSettings(
        enabled=_bool(values.get("enabled", False), "gemini.enabled"),
        model=values.get("model", GeminiSettings.model),
        broad_fps=_decimal(
            values.get("broad_fps", GeminiSettings.broad_fps), "gemini.broad_fps"
        ),
        candidate_min_fps=_int(
            values.get("candidate_min_fps", GeminiSettings.candidate_min_fps),
            "gemini.candidate_min_fps",
        ),
        candidate_max_fps=_int(
            values.get("candidate_max_fps", GeminiSettings.candidate_max_fps),
            "gemini.candidate_max_fps",
        ),
        max_cost_per_source_hour_usd=_decimal(
            values.get(
                "max_cost_per_source_hour_usd",
                GeminiSettings.max_cost_per_source_hour_usd,
            ),
            "gemini.max_cost_per_source_hour_usd",
        ),
        chunk_target_seconds=_int(
            values.get("chunk_target_seconds", GeminiSettings.chunk_target_seconds),
            "gemini.chunk_target_seconds",
        ),
        chunk_min_seconds=_int(
            values.get("chunk_min_seconds", GeminiSettings.chunk_min_seconds),
            "gemini.chunk_min_seconds",
        ),
        chunk_max_seconds=_int(
            values.get("chunk_max_seconds", GeminiSettings.chunk_max_seconds),
            "gemini.chunk_max_seconds",
        ),
        upload_poll_seconds=_int(
            values.get("upload_poll_seconds", GeminiSettings.upload_poll_seconds),
            "gemini.upload_poll_seconds",
        ),
        max_transient_attempts=_int(
            values.get("max_transient_attempts", GeminiSettings.max_transient_attempts),
            "gemini.max_transient_attempts",
        ),
        max_schema_repair_attempts=_int(
            values.get(
                "max_schema_repair_attempts", GeminiSettings.max_schema_repair_attempts
            ),
            "gemini.max_schema_repair_attempts",
        ),
    )
    if result.model != GEMINI_MODEL:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, f"gemini.model must be {GEMINI_MODEL}"
        )
    if result.broad_fps != Decimal("0.5"):
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, "gemini.broad_fps must be 0.5"
        )
    if not 2 <= result.candidate_min_fps <= result.candidate_max_fps <= 5:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION,
            "gemini.candidate_min_fps and gemini.candidate_max_fps must be between 2 and 5",
        )
    if result.max_cost_per_source_hour_usd > MAX_COST_PER_SOURCE_HOUR_USD:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION,
            "gemini.max_cost_per_source_hour_usd must not exceed "
            f"{MAX_COST_PER_SOURCE_HOUR_USD}",
        )
    return result


def _highlight_settings(raw: dict[str, Any]) -> HighlightSettings:
    values = _table(raw, "highlights")
    result = HighlightSettings(
        max_short_count=_int(
            values.get("max_short_count", 5), "highlights.max_short_count"
        ),
        long_max_seconds=_int(
            values.get("long_max_seconds", 1800), "highlights.long_max_seconds"
        ),
        short_max_seconds=_int(
            values.get("short_max_seconds", 180), "highlights.short_max_seconds"
        ),
        cross_short_overlap_ratio=_decimal(
            values.get("cross_short_overlap_ratio", "0.10"),
            "highlights.cross_short_overlap_ratio",
        ),
    )
    if not 0 <= result.max_short_count <= 5:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION,
            "highlights.max_short_count must be between 0 and 5",
        )
    return result


def _crop_settings(raw: dict[str, Any]) -> CropSettings:
    values = _table(raw, "crop")
    result = CropSettings(
        max_velocity_widths_per_second=_decimal(
            values.get("max_velocity_widths_per_second", "0.25"),
            "crop.max_velocity_widths_per_second",
        ),
        max_acceleration_widths_per_second_squared=_decimal(
            values.get("max_acceleration_widths_per_second_squared", "0.50"),
            "crop.max_acceleration_widths_per_second_squared",
        ),
        safe_margin_ratio=_decimal(
            values.get("safe_margin_ratio", "0.05"), "crop.safe_margin_ratio"
        ),
        minimum_subject_retention_ratio=_decimal(
            values.get("minimum_subject_retention_ratio", "0.95"),
            "crop.minimum_subject_retention_ratio",
        ),
        max_fallback_hold_seconds=_decimal(
            values.get("max_fallback_hold_seconds", "2"),
            "crop.max_fallback_hold_seconds",
        ),
    )
    limits = (
        (
            "crop.max_velocity_widths_per_second",
            result.max_velocity_widths_per_second,
            Decimal("0.25"),
            "max",
        ),
        (
            "crop.max_acceleration_widths_per_second_squared",
            result.max_acceleration_widths_per_second_squared,
            Decimal("0.50"),
            "max",
        ),
        ("crop.safe_margin_ratio", result.safe_margin_ratio, Decimal("0.05"), "min"),
        (
            "crop.minimum_subject_retention_ratio",
            result.minimum_subject_retention_ratio,
            Decimal("0.95"),
            "min",
        ),
        (
            "crop.max_fallback_hold_seconds",
            result.max_fallback_hold_seconds,
            Decimal(2),
            "max",
        ),
    )
    for name, value, limit, comparison in limits:
        invalid = value > limit if comparison == "max" else value < limit
        if invalid:
            raise VideoEditorError(
                ErrorCategory.CONFIGURATION, f"{name} weakens required crop limit"
            )
    return result


def resolve_config(path: Path) -> AppConfig:
    """Read and validate TOML configuration without creating configured paths."""

    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, f"cannot read configuration: {exc}"
        ) from exc

    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, "missing [paths] configuration"
        )
    path_settings = PathSettings(
        input_dir=_path(paths.get("input_dir"), "input_dir"),
        workspace_dir=_path(paths.get("workspace_dir"), "workspace_dir"),
        cache_dir=_path(paths.get("cache_dir"), "cache_dir"),
        output_dir=_path(paths.get("output_dir"), "output_dir"),
        state_dir=_path(paths.get("state_dir"), "state_dir"),
    )

    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, "[settings] must be a table"
        )
    reserve = settings.get("storage_reserve_bytes", raw.get("storage_reserve_bytes", 0))
    cloud_enabled = settings.get("cloud_enabled", raw.get("cloud_enabled", False))
    concurrency = settings.get("render_concurrency", raw.get("render_concurrency", 1))
    if type(reserve) is not int or reserve < 0:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION,
            "storage_reserve_bytes must be a non-negative integer",
        )
    cloud_enabled = _bool(cloud_enabled, "cloud_enabled")
    if type(concurrency) is not int or concurrency < 1:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, "render_concurrency must be positive"
        )
    gemini = _gemini_settings(raw)
    if cloud_enabled and not gemini.enabled:
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION,
            "gemini.enabled must be true when cloud_enabled is true",
        )
    return AppConfig(
        path_settings,
        reserve,
        cloud_enabled,
        concurrency,
        gemini,
        _highlight_settings(raw),
        _crop_settings(raw),
    )


def load_gemini_api_key(env: Mapping[str, str]) -> str:
    """Load runtime-only Gemini credential without storing or exposing it in config."""

    key = env.get("GEMINI_API_KEY")
    if not key or not key.strip():
        raise VideoEditorError(
            ErrorCategory.CONFIGURATION, "GEMINI_API_KEY is required"
        )
    return key
