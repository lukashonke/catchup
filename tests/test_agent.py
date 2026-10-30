import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from catchup import CatchupError, agent
from catchup.config import Config
from catchup.store import utc_now


@pytest.fixture(autouse=True)
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "catchup.db"
    monkeypatch.setenv("CATCHUP_DB", str(path))
    return path


def config(command: str) -> Config:
    return Config(agent_prompt="Catch me up.", agent_command=("sh", "-c", command))


def test_runs_the_command_with_the_prompt_on_stdin_once_per_interval(tmp_path: Path) -> None:
    out = tmp_path / "prompt.txt"
    now = utc_now()
    environ = {**os.environ, "OUT": str(out)}
    slow = config('cat > "$OUT"; echo finished; sleep 0.3')
    assert agent.is_due(slow, now)

    process = agent.start(slow, now, environ)
    assert process is not None
    assert agent.read_state().running
    assert agent.start(slow, now, environ) is None
    assert not agent.is_due(slow, now + timedelta(hours=2))

    assert process.wait() == 0
    assert out.read_text() == "Catch me up."
    log = agent.log_path().read_text().splitlines()
    assert log[0].endswith("""started: sh -c 'cat > "$OUT"; echo finished; sleep 0.3'""")
    assert log[1:] == ["finished", log[2]]
    assert log[2].endswith("exited with code 0")
    state = agent.read_state()
    assert (state.running, state.started_at, state.exit_code) == (False, now, 0)
    assert not state.failed
    assert not agent.is_due(slow, now + timedelta(minutes=59))
    assert agent.is_due(slow, now + timedelta(hours=1))


def test_is_never_due_without_a_prompt() -> None:
    assert not agent.is_due(Config(), utc_now())
    with pytest.raises(CatchupError, match="no prompt"):
        agent.start(Config(), utc_now())


def test_a_missing_command_fails_clearly() -> None:
    broken = Config(agent_prompt="Catch me up.", agent_command=("/nonexistent/agent",))
    with pytest.raises(CatchupError, match="cannot run /nonexistent/agent"):
        agent.start(broken, utc_now())
    assert not agent.read_state().running


def test_a_failed_run_is_recorded_and_the_log_keeps_earlier_runs() -> None:
    now = utc_now()
    first = agent.start(config("echo first"), now)
    assert first is not None and first.wait() == 0
    failing = agent.start(config("echo broken; exit 3"), now)
    assert failing is not None and failing.wait() == 3

    state = agent.read_state()
    assert (state.exit_code, state.failed) == (3, True)
    log = agent.log_path().read_text()
    assert "first" in log and "broken" in log and log.endswith("exited with code 3\n")


def test_a_run_past_the_time_limit_is_stopped(capfd: pytest.CaptureFixture[str]) -> None:
    # As start() records it, so the supervisor recognizes the run as its own.
    agent.state_dir().mkdir(parents=True, exist_ok=True)
    state_file = agent.state_dir() / "agent.json"
    state_file.write_text(json.dumps({"pid": os.getpid(), "started_at": utc_now().isoformat()}))

    began = time.monotonic()
    code = agent.supervise(["sh", "-c", "sleep 30"], limit=timedelta(seconds=0.3))

    assert code == agent.TIMED_OUT
    assert time.monotonic() - began < 5
    assert "ran too long" in capfd.readouterr().out
    assert agent.read_state().failed
