import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from catchup import CatchupError, context
from catchup.config import Config
from catchup.store import Item, Store, utc_now


@pytest.fixture(autouse=True)
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "catchup.db"
    monkeypatch.setenv("CATCHUP_DB", str(path))
    return path


@pytest.fixture
def item(db: Path) -> Item:
    with Store(db) as store:
        return store.add("waiting", "Answer Ken", body="Ken asks about the retry limit.")


def config(command: str) -> Config:
    return Config(context_prompt="Catch me up.", context_command=("sh", "-c", command))


def wait_for(key: str, state: str) -> context.Context:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = context.read_all().get(key)
        if found and found.state == state:
            return found
        time.sleep(0.05)
    raise AssertionError(f"no {state} context for {key}: {context.read_all()}")


def test_a_fetch_stores_what_the_command_prints_for_the_prompt(item: Item) -> None:
    assert context.fetch(str(item.id), item, [], config("echo '# Summary'; cat"))
    assert context.read_all()[str(item.id)].state == "fetching"
    assert not context.fetch(str(item.id), item, [], config("true"))

    ready = wait_for(str(item.id), "ready")
    assert ready.text.startswith("# Summary\nCatch me up.\n\n## The item\n\nTitle: Answer Ken")
    assert not ready.stale(item)
    assert "1 ready" in context.log_path().read_text()


def test_a_failed_fetch_keeps_the_reason_and_the_output(item: Item) -> None:
    context.fetch(str(item.id), item, [], config("echo 'no Slack access' >&2; exit 2"))
    failed = wait_for(str(item.id), "failed")
    assert failed.text.startswith("It exited with code 2.")
    assert "no Slack access" in context.log_path().read_text()


def test_the_context_goes_stale_when_the_agent_changes_the_item(db: Path, item: Item) -> None:
    context.fetch(str(item.id), item, [], config("echo done"))
    ready = wait_for(str(item.id), "ready")
    with Store(db, clock=lambda: utc_now() + timedelta(minutes=1)) as store:
        changed = store.update(item.id, title="Answer Ken today")
    assert ready.stale(changed)


def test_at_most_three_fetches_run_at_once(db: Path) -> None:
    with Store(db) as store:
        items = [store.add("waiting", f"Item {n}") for n in range(4)]
    for item in items[:3]:
        context.fetch(str(item.id), item, [], config("sleep 5"))
    with pytest.raises(CatchupError, match="3 contexts are being fetched already"):
        context.fetch(str(items[3].id), items[3], [], config("sleep 5"))


def test_a_fetch_whose_supervisor_is_gone_reads_as_failed(item: Item) -> None:
    context.contexts_dir().mkdir(parents=True)
    record = {"state": "fetching", "pid": 999_999, "started_at": utc_now().isoformat()}
    (context.contexts_dir() / f"{item.id}.json").write_text(json.dumps(record))
    assert context.read_all()[str(item.id)].state == "failed"


def test_a_fetch_past_the_time_limit_is_stopped(item: Item) -> None:
    context.contexts_dir().mkdir(parents=True)
    record = {"state": "fetching", "pid": os.getpid(), "started_at": utc_now().isoformat()}
    (context.contexts_dir() / f"{item.id}.json").write_text(json.dumps(record))

    began = time.monotonic()
    context.supervise(str(item.id), ["sh", "-c", "sleep 30"], limit=timedelta(seconds=0.3))

    assert time.monotonic() - began < 5
    failed = context.read_all()[str(item.id)]
    assert (failed.state, failed.text.split(".")[0]) == ("failed", "It took longer than 0 minutes")


def stream(path: Path, *events: dict[str, object]) -> Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def test_a_stream_shows_its_steps_while_fetching_then_keeps_only_the_answer(
    item: Item, tmp_path: Path
) -> None:
    tool = {"type": "tool_use", "name": "mcp__claude_ai_Slack__slack_search_public"}
    bash = {"type": "tool_use", "name": "Bash"}
    steps = stream(
        tmp_path / "steps.jsonl",
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "Searching   Slack.\n"},
                    {**tool, "input": {"channel": "C1", "query": "retry limit"}},
                    {**bash, "input": {"description": "View", "command": "td task view id:1"}},
                ]
            },
        },
    )
    result = {"type": "result", "is_error": False, "result": "## Ken\n\nWaits on you."}
    answer = stream(tmp_path / "answer.jsonl", result)
    context.fetch(str(item.id), item, [], config(f"cat {steps}; sleep 2; cat {answer}"))

    deadline = time.monotonic() + 10
    while not (fetching := context.read_all()[str(item.id)]).steps:
        assert time.monotonic() < deadline, "no steps"
        time.sleep(0.05)
    assert fetching.state == "fetching"
    assert fetching.steps == (
        context.Step("text", "Searching Slack."),
        context.Step("tool", "Slack · slack_search_public  retry limit"),
        context.Step("tool", "Bash  td task view id:1"),
    )
    ready = wait_for(str(item.id), "ready")
    assert (ready.text, ready.steps) == ("## Ken\n\nWaits on you.", ())
    assert "tool: Slack · slack_search_public  retry limit" in context.log_path().read_text()


def test_a_stream_that_reports_an_error_fails(item: Item, tmp_path: Path) -> None:
    result = {"type": "result", "is_error": True, "result": "Credit balance is too low"}
    answer = stream(tmp_path / "answer.jsonl", result)
    context.fetch(str(item.id), item, [], config(f"cat {answer}"))
    failed = wait_for(str(item.id), "failed")
    assert failed.text.startswith("It reported an error.")
    assert failed.text.endswith("Credit balance is too low")
