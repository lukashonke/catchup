from pathlib import Path

import pytest

from catchup import CatchupError
from catchup.config import Config, default_config_path, load_config


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "config.toml") == Config()


def test_reads_open_command(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('open_command = ["/bin/opener", "--new-tab"]\n')
    assert load_config(path).open_command == ["/bin/opener", "--new-tab"]


@pytest.mark.parametrize(
    "toml", ['open_command = "/bin/opener"', "open_command = []", "open_command = [1]"]
)
def test_rejects_open_command_that_is_not_a_list_of_strings(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml)
    with pytest.raises(CatchupError, match="must be a non-empty list of strings"):
        load_config(path)


def test_rejects_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("open_command = [")
    with pytest.raises(CatchupError, match="fix the file or remove it"):
        load_config(path)


def test_path_follows_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "catchup" / "config.toml"


def test_reads_open_label(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('open_label = "Open in Codex"\n')
    assert load_config(path).open_label == "Open in Codex"


def test_rejects_empty_open_label(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('open_label = " "\n')
    with pytest.raises(CatchupError, match="open_label .* must be a non-empty string"):
        load_config(path)
