from pathlib import Path

import pytest

from catchup import CatchupError
from catchup.config import Config, default_config_path, load_config, save_config


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


def test_reads_the_agent_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'agent_prompt = """\nCatch me up.\n"""\nagent_command = ["claude", "-p"]\nagent_every = "30m"\n'
    )
    config = load_config(path)
    assert (config.agent_prompt, config.agent_command, config.agent_every) == (
        "Catch me up.",
        ("claude", "-p"),
        "30m",
    )


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('agent_every = "1d"', "agent_every .* cannot read '1d' as an interval"),
        ("agent_command = []", "agent_command .* non-empty list of strings"),
        ("agent_prompt = 3", "agent_prompt .* must be a string"),
        ('todoist = "yes"', "todoist .* must be true or false"),
        ('todoist_command = "td"', "todoist_command .* non-empty list of strings"),
        ('todoist_filter = " "', "todoist_filter .* non-empty Todoist filter"),
    ],
)
def test_rejects_bad_agent_settings(tmp_path: Path, toml: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml)
    with pytest.raises(CatchupError, match=message):
        load_config(path)


def test_reads_the_todoist_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('todoist = true\ntodoist_command = ["/bin/td"]\ntodoist_filter = "today"\n')
    config = load_config(path)
    assert (config.todoist, config.todoist_command, config.todoist_filter) == (
        True,
        ("/bin/td",),
        "today",
    )
    assert load_config(tmp_path / "missing.toml").todoist is False


def test_save_keeps_comments_and_leaves_defaults_out(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# My opener.\nopen_command = ["/bin/opener"]\nagent_every = "4h"\n')
    config = Config(open_command=["/bin/opener", "--tab"], agent_prompt="Line one.\nLine two.")
    save_config(config, path)
    text = path.read_text()
    assert text.startswith("# My opener.\n")
    assert "agent_every" not in text and "open_label" not in text
    assert load_config(path) == config
