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

    await open_item(item, config, {"PATH": "/bin", "HC_TERMINAL_ID": "t9"})
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


HERDR = {"HC_TERMINAL_ID": "t9"}
OK = (0, "{}", "")


async def test_herdr_opener_creates_a_tab_that_starts_claude_with_the_prompt(
    item: Item, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun((0, '{"pane": "p1", "terminal": "t12"}', ""))
    monkeypatch.setattr("catchup.herdr.run_command", fake)

    await open_item(item, Config(), HERDR)

    ((argv, _),) = fake.calls
    assert argv[:3] == ["herdr-app", "do", "tab.new"]
    assert json.loads(argv[3]) == {
        "name": "Answer Ken about the flaky…",
        "folder": str(Path.home()),
        "argv": ["claude"],
        "task": default_prompt(item),
    }


async def test_herdr_opener_reports_the_herdr_error(
    item: Item, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = '{"error": "no space is open"}'
    monkeypatch.setattr("catchup.herdr.run_command", FakeRun((1, error, "")))
    with pytest.raises(CatchupError, match="herdr-app tab.new failed: no space is open"):
        await open_item(item, Config(), HERDR)


async def test_without_config_or_herdr_it_explains_open_command(
    item: Item, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(CatchupError, match=f"Set open_command in {tmp_path}/catchup/config.toml"):
        await open_item(item, Config(), {})


def layout(name: str | None) -> tuple[int, str, str]:
    """The answer of `herdr-app ls` with the TUI's terminal t9 in tab tb2, named `name`."""
    tabs = [
        {"id": "tb1", "name": "other", "terminals": [{"terminal": "t1"}]},
        {"id": "tb2", "name": name, "terminals": [{"terminal": "t3"}, {"terminal": "t9"}]},
    ]
    return (0, json.dumps({"spaces": [{"id": "sp1", "tabs": tabs}]}), "")


def renames(fake: FakeRun) -> list[dict[str, str]]:
    return [json.loads(argv[3]) for argv, _ in fake.calls if argv[1:3] == ["do", "tab.rename"]]


async def test_tab_title_appends_the_counts_once_per_text_and_restores_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRun(layout("inbox"), OK, OK, OK)
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    title = TabTitle(HERDR)
    await title.show("3 · 1 new")
    await title.show("3 · 1 new")
    await title.show("3")
    await title.restore()
    assert fake.calls[0][0] == ["herdr-app", "ls"]
    assert renames(fake) == [
        {"id": "tb2", "name": "inbox 3 · 1 new"},
        {"id": "tb2", "name": "inbox 3"},
        {"id": "tb2", "name": "inbox"},
    ]


@pytest.mark.parametrize(
    ("start", "base", "restored"),
    [
        ("catchup 12 · 3 new", "catchup", "catchup"),
        ("inbox 7", "inbox", "inbox"),
        # An unnamed tab gets its number back.
        (None, "catchup", ""),
    ],
)
async def test_tab_title_drops_counts_left_by_a_crash_and_names_an_unnamed_tab(
    monkeypatch: pytest.MonkeyPatch, start: str | None, base: str, restored: str
) -> None:
    fake = FakeRun(layout(start), OK, OK)
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    title = TabTitle(HERDR)
    await title.show("4")
    await title.restore()
    assert [rename["name"] for rename in renames(fake)] == [f"{base} 4", restored]


async def test_tab_title_does_nothing_outside_herdr_or_until_a_tab_shows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRun()
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    title = TabTitle({"HERDR_ENV": "1", "HERDR_TAB_ID": "w1:t2"})
    await title.show("3")
    await title.restore()
    assert fake.calls == []

    # A terminal that no tab shows yet is looked for again on the next show.
    fake = FakeRun(layout("inbox"), layout("inbox"))
    monkeypatch.setattr("catchup.herdr.run_command", fake)
    title = TabTitle({"HC_TERMINAL_ID": "t40"})
    await title.show("3")
    await title.show("4")
    await title.restore()
    assert [argv for argv, _ in fake.calls] == [["herdr-app", "ls"], ["herdr-app", "ls"]]


async def test_herdr_opener_creates_a_space_for_the_new_space_action(
    item: Item, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun((0, '{"space": "sp9", "terminal": "t12"}', ""))
    monkeypatch.setattr("catchup.herdr.run_command", fake)

    await open_item(item, Config(), HERDR, space=True)

    ((argv, _),) = fake.calls
    assert argv[:3] == ["herdr-app", "do", "space.new"]
    args = json.loads(argv[3])
    assert args["name"] == args["tab"] == "Answer Ken about the flaky…"
    assert args["argv"] == ["claude"] and args["task"] == default_prompt(item)


async def test_the_new_space_action_runs_its_own_command(
    item: Item, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun((0, "", ""))
    monkeypatch.setattr("catchup.opener.run_command", fake)
    config = Config(open_command=["/opt/tab.sh"], open_space_command=["/opt/space.sh"])

    await open_item(item, config, {}, space=True)

    assert fake.calls[0][0] == ["/opt/space.sh"]
