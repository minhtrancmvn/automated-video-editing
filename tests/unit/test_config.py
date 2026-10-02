import os
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.config import HighlightSettings, load_gemini_api_key, resolve_config
from video_editor.errors import ErrorCategory, VideoEditorError


def write_config(
    tmp_path: Path,
    *,
    cloud_enabled: bool = False,
    gemini_enabled: bool = False,
    gemini: str = "",
    highlights: str = "",
    crop: str = "",
) -> Path:
    """Write valid Phase 2 configuration with optional table overrides."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[paths]\ninput_dir="/tmp/in"\nworkspace_dir="/tmp/work"\n'
        'cache_dir="/tmp/cache"\noutput_dir="/tmp/out"\nstate_dir="/tmp/state"\n'
        f"\n[settings]\ncloud_enabled={str(cloud_enabled).lower()}\n"
        f"\n[gemini]\nenabled={str(gemini_enabled).lower()}\n{gemini}"
        f"\n[highlights]\n{highlights}"
        f"\n[crop]\n{crop}"
    )
    return config_file


def test_config_expands_paths_without_redirecting_missing_volume(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[paths]\ninput_dir="/Volumes/Missing/footage"\n'
        'workspace_dir="/Volumes/Missing/work"\ncache_dir="/Volumes/Missing/cache"\n'
        'output_dir="/Volumes/Missing/out"\n'
        'state_dir="~/Library/Application Support/video-editor"\n'
    )
    config = resolve_config(config_file)
    assert config.paths.input_dir == Path("/Volumes/Missing/footage")
    assert (
        config.paths.state_dir
        == Path("~/Library/Application Support/video-editor").expanduser()
    )


@pytest.mark.parametrize("setting", ["storage_reserve_bytes", "render_concurrency"])
def test_boolean_numeric_settings_are_rejected(tmp_path: Path, setting: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[paths]\ninput_dir="/tmp/in"\nworkspace_dir="/tmp/work"\n'
        'cache_dir="/tmp/cache"\noutput_dir="/tmp/out"\nstate_dir="/tmp/state"\n'
        f"\n[settings]\n{setting}=true\n"
    )
    with pytest.raises(VideoEditorError, match=setting):
        resolve_config(config_file)


def test_phase2_config_keeps_api_key_out_of_app_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    config = resolve_config(
        write_config(tmp_path, cloud_enabled=True, gemini_enabled=True)
    )
    assert config.gemini.model == "gemini-3.8-flash"
    assert config.gemini.max_cost_per_source_hour_usd == Decimal("3.00")
    assert "secret-value" not in repr(config)
    assert load_gemini_api_key(os.environ) == "secret-value"


def test_phase2_requires_key_only_when_cloud_execution_starts(tmp_path: Path) -> None:
    resolve_config(write_config(tmp_path, cloud_enabled=True, gemini_enabled=True))
    with pytest.raises(VideoEditorError, match="GEMINI_API_KEY") as caught:
        load_gemini_api_key({})
    assert caught.value.category == ErrorCategory.CONFIGURATION
    assert "secret" not in str(caught.value).lower()


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_phase2_rejects_blank_gemini_api_key(value: str) -> None:
    with pytest.raises(VideoEditorError, match="GEMINI_API_KEY") as caught:
        load_gemini_api_key({"GEMINI_API_KEY": value})
    assert caught.value.category == ErrorCategory.CONFIGURATION


def test_cloud_mode_requires_enabled_gemini(tmp_path: Path) -> None:
    with pytest.raises(VideoEditorError, match="gemini.enabled"):
        resolve_config(write_config(tmp_path, cloud_enabled=True))


@pytest.mark.parametrize("cap", ["0.50", "1.00", "2.50", "3.00"])
def test_cost_cap_accepts_values_up_to_three_dollars(tmp_path: Path, cap: str) -> None:
    config = resolve_config(
        write_config(
            tmp_path,
            cloud_enabled=True,
            gemini_enabled=True,
            gemini=f'max_cost_per_source_hour_usd="{cap}"\n',
        )
    )

    assert config.gemini.max_cost_per_source_hour_usd == Decimal(cap)


@pytest.mark.parametrize(
    ("table", "setting", "value"),
    [
        ("gemini", "model", '"gemini-2.0-flash"'),
        ("gemini", "broad_fps", '"1"'),
        ("gemini", "candidate_min_fps", "1"),
        ("gemini", "candidate_max_fps", "6"),
        ("gemini", "max_cost_per_source_hour_usd", '"3.01"'),
        ("highlights", "max_short_count", "6"),
        ("crop", "max_velocity_widths_per_second", '"0.26"'),
        ("crop", "max_acceleration_widths_per_second_squared", '"0.51"'),
        ("crop", "safe_margin_ratio", '"0.04"'),
        ("crop", "minimum_subject_retention_ratio", '"0.94"'),
        ("crop", "max_fallback_hold_seconds", '"2.01"'),
    ],
)
def test_phase2_rejects_weakened_limits(
    tmp_path: Path, table: str, setting: str, value: str
) -> None:
    config = {"gemini": "", "highlights": "", "crop": ""}
    config[table] = f"{setting}={value}\n"
    with pytest.raises(VideoEditorError, match=setting):
        resolve_config(
            write_config(
                tmp_path,
                gemini=config["gemini"],
                highlights=config["highlights"],
                crop=config["crop"],
            )
        )


@pytest.mark.parametrize(
    ("table", "setting"),
    [
        ("gemini", "broad_fps"),
        ("gemini", "max_cost_per_source_hour_usd"),
        ("highlights", "cross_short_overlap_ratio"),
        ("crop", "max_velocity_widths_per_second"),
        ("crop", "max_acceleration_widths_per_second_squared"),
        ("crop", "safe_margin_ratio"),
        ("crop", "minimum_subject_retention_ratio"),
        ("crop", "max_fallback_hold_seconds"),
    ],
)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_phase2_rejects_non_finite_decimal_settings(
    tmp_path: Path, table: str, setting: str, value: str
) -> None:
    config = {"gemini": "", "highlights": "", "crop": ""}
    config[table] = f'{setting}="{value}"\n'
    with pytest.raises(VideoEditorError, match=setting) as caught:
        resolve_config(
            write_config(
                tmp_path,
                gemini=config["gemini"],
                highlights=config["highlights"],
                crop=config["crop"],
            )
        )
    assert caught.value.category == ErrorCategory.CONFIGURATION


@pytest.mark.parametrize("setting", ["candidate_min_fps", "candidate_max_fps"])
def test_phase2_accepts_candidate_fps_in_allowed_range(
    tmp_path: Path, setting: str
) -> None:
    config = resolve_config(write_config(tmp_path, gemini=f"{setting}=3\n"))
    assert getattr(config.gemini, setting) == 3


@pytest.mark.parametrize("count", [0, 5])
def test_phase2_accepts_short_count_boundary(tmp_path: Path, count: int) -> None:
    config = resolve_config(
        write_config(tmp_path, highlights=f"max_short_count={count}\n")
    )
    assert config.highlights.max_short_count == count


@pytest.mark.parametrize("ratio", [Decimal(0), Decimal("0.20")])
def test_highlight_settings_rejects_non_policy_overlap_ratio(ratio: Decimal) -> None:
    with pytest.raises(VideoEditorError, match="cross_short_overlap_ratio"):
        HighlightSettings(cross_short_overlap_ratio=ratio)


def test_highlight_settings_accepts_fixed_overlap_ratio() -> None:
    settings = HighlightSettings(cross_short_overlap_ratio=Decimal("0.10"))

    assert settings.cross_short_overlap_ratio == Decimal("0.10")
