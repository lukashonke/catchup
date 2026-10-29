import os
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


async def test_runs_the_command_with_the_prompt_on_stdin_once_per_interval(tmp_path: Path) -> None:
    out = tmp_path / "prompt.txt"
    now = utc_now()
    environ = {**os.environ, "OUT": str(out)}
    slow = config('cat > "$OUT"; echo finished; sleep 0.3')
    assert agent.is_due(slow, now)

    process = await agent.start(slow, now, environ)
    assert process is not None
    assert agent.read_state().running
    assert await agent.start(slow, now, environ) is None
    assert not agent.is_due(slow, now + timedelta(hours=2))

    assert await process.wait() == 0
    assert out.read_text() == "Catch me up."
    assert agent.log_path().read_text() == "finished\n"
    state = agent.read_state()
    assert (state.running, state.started_at) == (False, now)
    assert not agent.is_due(slow, now + timedelta(minutes=59))
    assert agent.is_due(slow, now + timedelta(hours=1))


async def test_is_never_due_without_a_prompt() -> None:
    assert not agent.is_due(Config(), utc_now())
    with pytest.raises(CatchupError, match="no prompt"):
        await agent.start(Config(), utc_now())


async def test_a_missing_command_fails_clearly() -> None:
    broken = Config(agent_prompt="Catch me up.", agent_command=("/nonexistent/agent",))
    with pytest.raises(CatchupError, match="cannot run /nonexistent/agent"):
        await agent.start(broken, utc_now())
    assert not agent.read_state().running
