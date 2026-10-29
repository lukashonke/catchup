import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from catchup import CatchupError
from catchup.config import Config
from catchup.herdr import TabTitle
from catchup.links import Link
from catchup.opener import default_prompt, open_item, run_command
from catchup.store import Item, Store


@pytest.fixture
def item(tmp_path: Path) -> Item:
    with Store(tmp_path / "catchup.db") as store:
        return store.add(
            "waiting",
            "Answer Ken about the flaky payment tests",
            body="Ken asks if quarantining them is fine.",
            links=[
                Link("SHOP-881", "https://linear.app/acme/issue/SHOP-881"),
                Link("Thread", "https://acme.slack.com/archives/C1/p2"),
            ],
        )


class FakeRun:
    """Stands in for run_command: records each call and answers from a list of results."""

    def __init__(self, *results: tuple[int, str, str]) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.results = list(results)

    async def __call__(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(env)))
        code, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(list(argv), code, out, err)


def test_default_prompt(item: Item) -> None:
    assert default_prompt(item) == (
        "Answer Ken about the flaky payment tests\n\n"
        "Ken asks if quarantining them is fine.\n\n"
        "https://linear.app/acme/issue/SHOP-881\n"
        "https://acme.slack.com/archives/C1/p2\n\n"
        "Help me handle this."
    )


async def test_open_command_gets_the_item_in_env(
    item: Item, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun((0, "", ""), (3, "", "no terminal found\n"))
    monkeypatch.setattr("catchup.opener.run_command", fake)
    config = Config(open_command=["/opt/open.sh", "--new"])

    await open_item(item, config, {"PATH": "/bin", "HERDR_ENV": "1"})
    argv, env = fake.calls[0]
    assert argv == ["/opt/open.sh", "--new"]
    assert env["PATH"] == "/bin"
    assert env["CATCHUP_ID"] == str(item.id)
    assert env["CATCHUP_TITLE"] == item.title
    assert env["CATCHUP_PROMPT"] == default_prompt(item)
    assert env["CATCHUP_LINKS"] == (
        "https://linear.app/acme/issue/SHOP-881\nhttps://acme.slack.com/archives/C1/p2"
    )

    with pytest.raises(CatchupError, match="exited with code 3: no terminal found"):
        await open_item(item, config, {})


async def test_run_command_reports_a_missing_executable() -> None:
    with pytest.raises(CatchupError, match="cannot run /nonexistent/opener"):
        await run_command(["/nonexistent/opener"], {})


async def test_herdr_opener_creates_a_tab_and_starts_claude_with_the_prompt(
    item: Item, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(
        (0, '{"result": {"tab": {"tab_id": "w1:t9"}, "root_pane": {"pane_id": "w1:p7"}}}', ""),
        (0, '{"result": {}}', ""),
        (0, '{"result": {}}', ""),
    )
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    environ = {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1", "HERDR_BIN_PATH": "/opt/herdr"}

    await open_item(item, Config(), environ)

    (create, _), (start, _), (prompt, _) = fake.calls
    assert create == [
        "/opt/herdr", "tab", "create", "--workspace", "w1", "--cwd", str(Path.home()),
        "--label", "Answer Ken about the flaky…", "--focus",
    ]  # fmt: skip
    name = start[3]
    assert name.startswith(f"catchup-{item.id}-")
    assert start == ["/opt/herdr", "agent", "start", name, "--kind", "claude", "--pane", "w1:p7"]
    assert prompt == ["/opt/herdr", "agent", "prompt", name, default_prompt(item)]


async def test_herdr_opener_reports_the_herdr_error(
    item: Item, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = '{"error": {"code": "workspace_not_found", "message": "workspace w1 not found"}}'
    monkeypatch.setattr("catchup.herdr.run_command", FakeRun((1, error, "")))
    with pytest.raises(CatchupError, match="herdr tab create failed: workspace w1 not found"):
        await open_item(item, Config(), {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"})


async def test_without_config_or_herdr_it_explains_open_command(
    item: Item, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(CatchupError, match=f"Set open_command in {tmp_path}/catchup/config.toml"):
        await open_item(item, Config(), {})


HERDR = {"HERDR_ENV": "1", "HERDR_TAB_ID": "w1:t2", "HERDR_BIN_PATH": "/opt/herdr"}
OK = (0, '{"result": {}}', "")


def tab_info(label: str) -> tuple[int, str, str]:
    return (0, json.dumps({"result": {"tab": {"label": label}}}), "")


async def test_tab_title_renames_the_tab_once_per_text_and_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRun(tab_info("inbox"), OK, OK, OK)
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    title = TabTitle(HERDR)
    await title.show("catchup 3 · 1 new")
    await title.show("catchup 3 · 1 new")
    await title.show("catchup 3")
    await title.restore()
    assert [argv[1:] for argv, _ in fake.calls] == [
        ["tab", "get", "w1:t2"],
        ["tab", "rename", "w1:t2", "catchup 3 · 1 new"],
        ["tab", "rename", "w1:t2", "catchup 3"],
        ["tab", "rename", "w1:t2", "inbox"],
    ]


async def test_tab_title_restores_catchup_after_a_label_left_by_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRun(tab_info("catchup 12 · 3 new"), OK, OK)
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    title = TabTitle(HERDR)
    await title.show("catchup 4")
    await title.restore()
    assert fake.calls[-1][0][1:] == ["tab", "rename", "w1:t2", "catchup"]


async def test_tab_title_does_nothing_outside_herdr(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun()
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    title = TabTitle({"HERDR_TAB_ID": "w1:t2"})
    await title.show("catchup 3")
    await title.restore()
    assert fake.calls == []
