"""Application configuration and path resolution."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_editor.errors import ErrorCategory, VideoEditorError


@dataclass(frozen=True)
class PathSettings:
    """Configured locations used by local workflows."""

    input_dir: Path
    workspace_dir: Path
    cache_dir: Path
    output_dir: Path
    state_dir: Path


@dataclass(frozen=True)
class AppConfig:
    """Validated application settings."""

    paths: PathSettings
    storage_reserve_bytes: int
    cloud_enabled: bool = False
    render_concurrency: int = 1


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise VideoEditorError(ErrorCategory.CONFIGURATION, f"paths.{name} must be a non-empty string")
    return Path(value).expanduser()


def resolve_config(path: Path) -> AppConfig:
    """Read TOML configuration without creating or redirecting configured paths."""

    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VideoEditorError(ErrorCategory.CONFIGURATION, f"cannot read configuration: {exc}") from exc

    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise VideoEditorError(ErrorCategory.CONFIGURATION, "missing [paths] configuration")
    path_settings = PathSettings(
        input_dir=_path(paths.get("input_dir"), "input_dir"),
        workspace_dir=_path(paths.get("workspace_dir"), "workspace_dir"),
        cache_dir=_path(paths.get("cache_dir"), "cache_dir"),
        output_dir=_path(paths.get("output_dir"), "output_dir"),
        state_dir=_path(paths.get("state_dir"), "state_dir"),
    )

    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise VideoEditorError(ErrorCategory.CONFIGURATION, "[settings] must be a table")
    reserve = settings.get("storage_reserve_bytes", raw.get("storage_reserve_bytes", 0))
    cloud_enabled = settings.get("cloud_enabled", raw.get("cloud_enabled", False))
    concurrency = settings.get("render_concurrency", raw.get("render_concurrency", 1))
    if not isinstance(reserve, int) or reserve < 0:
        raise VideoEditorError(ErrorCategory.CONFIGURATION, "storage_reserve_bytes must be a non-negative integer")
    if not isinstance(cloud_enabled, bool):
        raise VideoEditorError(ErrorCategory.CONFIGURATION, "cloud_enabled must be boolean")
    if cloud_enabled:
        raise VideoEditorError(ErrorCategory.CONFIGURATION, "cloud_enabled is not supported in Phase 1")
    if not isinstance(concurrency, int) or concurrency < 1:
        raise VideoEditorError(ErrorCategory.CONFIGURATION, "render_concurrency must be positive")
    return AppConfig(path_settings, reserve, cloud_enabled, concurrency)
