from pathlib import Path

import pytest

from video_editor.config import resolve_config
from video_editor.errors import VideoEditorError


def test_config_expands_paths_without_redirecting_missing_volume(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[paths]\ninput_dir="/Volumes/Missing/footage"\n'
        'workspace_dir="/Volumes/Missing/work"\ncache_dir="/Volumes/Missing/cache"\n'
        'output_dir="/Volumes/Missing/out"\n'
        'state_dir="~/Library/Application Support/video-editor"\n'
    )
    config = resolve_config(config_file)
    assert config.paths.input_dir == Path("/Volumes/Missing/footage")
    assert config.paths.state_dir == Path("~/Library/Application Support/video-editor").expanduser()


@pytest.mark.parametrize("setting", ["storage_reserve_bytes", "render_concurrency"])
def test_boolean_numeric_settings_are_rejected(tmp_path: Path, setting: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[paths]\ninput_dir="/tmp/in"\nworkspace_dir="/tmp/work"\n'
        'cache_dir="/tmp/cache"\noutput_dir="/tmp/out"\nstate_dir="/tmp/state"\n'
        f'\n[settings]\n{setting}=true\n'
    )
    with pytest.raises(VideoEditorError, match=setting):
        resolve_config(config_file)


def test_cloud_enabled_true_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[paths]\ninput_dir="/tmp/in"\nworkspace_dir="/tmp/work"\n'
        'cache_dir="/tmp/cache"\noutput_dir="/tmp/out"\nstate_dir="/tmp/state"\n'
        '\n[settings]\ncloud_enabled=true\n'
    )
    with pytest.raises(VideoEditorError, match="cloud_enabled"):
        resolve_config(config_file)
