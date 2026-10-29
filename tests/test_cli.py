import io
import json
from pathlib import Path
from typing import Any

import pytest

from catchup.cli import main


@pytest.fixture(autouse=True)
def temp_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CATCHUP_DB", str(tmp_path / "catchup.db"))


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    assert main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_add_and_show_json(capsys: pytest.CaptureFixture[str]) -> None:
    added = run_json(
        capsys,
        "add",
        "--kind=waiting",
        "--title=Review pricing MR",
        "--priority=urgent",
        "--link=Pricing=https://gitlab.com/g/p/-/merge_requests/1",
        "--link=doc:Spec=https://x.test/spec",
        "--note=from #pricing",
    )
    assert added["links"] == [
        {"label": "Pricing", "url": "https://gitlab.com/g/p/-/merge_requests/1", "type": "MR"},
        {"label": "Spec", "url": "https://x.test/spec", "type": "Doc"},
    ]
    assert added["created_at"].endswith("Z")

    shown = run_json(capsys, "show", str(added["id"]))
    assert {k: shown[k] for k in ("id", "kind", "state", "priority")} == {
        "id": added["id"],
        "kind": "waiting",
        "state": "open",
        "priority": "urgent",
    }
    assert [(e["action"], e["actor"], e["note"]) for e in shown["events"]] == [
        ("created", "agent", "from #pricing")
    ]


def test_body_from_stdin(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("Line with 'quotes' and $vars\nsecond\n"))
    added = run_json(capsys, "add", "--kind", "fyi", "--title", "t", "--body", "-")
    assert added["body"] == "Line with 'quotes' and $vars\nsecond"
    assert added["state"] is None


def test_body_and_prompt_cannot_both_read_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--kind=waiting", "--title=t", "--body=-", "--prompt=-"]) == 1
    assert "only one of --body and --prompt" in capsys.readouterr().err


def test_state_commands_and_list_json(capsys: pytest.CaptureFixture[str]) -> None:
    first = run_json(capsys, "add", "--kind=waiting", "--title=one")["id"]
    second = run_json(capsys, "add", "--kind=waiting", "--title=two")["id"]
    assert run_json(capsys, "done", str(first), "--by=user", "--note=handled")["state"] == "done"
    snoozed = run_json(capsys, "snooze", str(second), "--until=1d")
    assert snoozed["state"] == "snoozed" and snoozed["snoozed_until"]

    assert run_json(capsys, "list") == []
    done = run_json(capsys, "list", "--state=done")
    assert [i["id"] for i in done] == [first]
    assert done[0]["last_event"] == {
        "id": 3,
        "item_id": first,
        "at": done[0]["last_event"]["at"],
        "actor": "user",
        "action": "done",
        "note": "handled",
    }

    assert run_json(capsys, "reopen", str(first))["state"] == "open"
    assert [i["id"] for i in run_json(capsys, "list", "--since=1d")] == [first]


def test_fyi_rejects_state_commands(capsys: pytest.CaptureFixture[str]) -> None:
    fyi = run_json(capsys, "add", "--kind=fyi", "--title=deployed")["id"]
    assert main(["done", str(fyi)]) == 1
    assert "is an FYI" in capsys.readouterr().err


def test_update_replaces_links(capsys: pytest.CaptureFixture[str]) -> None:
    item = run_json(capsys, "add", "--kind=waiting", "--title=t", "--link=a=https://a.test")["id"]
    updated = run_json(capsys, "update", str(item), "--link=Thread=https://x.slack.com/p1")
    assert updated["links"] == [
        {"label": "Thread", "url": "https://x.slack.com/p1", "type": "Slack"}
    ]


def test_human_list_line(capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "add",
            "--kind=waiting",
            "--title=Review",
            "--priority=urgent",
            "--link=MR=https://github.com/o/r/pull/1",
        ]
    )
    main(["add", "--kind=fyi", "--title=Deployed"])
    capsys.readouterr()
    assert main(["list", "--kind=all"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "   #1 ! waiting/open     now  Review  [MR]",
        "   #2   fyi              now  Deployed",
    ]


def test_errors_go_to_stderr_with_next_step(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "7"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "item 7 does not exist; run `catchup list --state all --kind all`" in captured.err

    assert main(["snooze", "7", "--until=someday"]) == 1
    assert "use 3h, 1d, 3d, 1w, mon, tomorrow or YYYY-MM-DD" in capsys.readouterr().err


def test_malformed_link_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["add", "--kind=waiting", "--title=t", "--link=no-label"])
    assert exit_info.value.code == 2
    assert "[type:]label=url" in capsys.readouterr().err


def test_no_command_starts_the_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    started = []
    monkeypatch.setattr("catchup.tui.run", lambda: started.append(True) or 0)
    assert main([]) == 0
    assert started == [True]
