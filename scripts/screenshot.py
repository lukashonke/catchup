"""Render docs/screenshot.svg from a temporary database of fictional items.

Run with `uv run python scripts/screenshot.py`.
"""

import asyncio
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from catchup.config import Config
from catchup.links import Link
from catchup.store import Store, utc_now

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "screenshot.svg"
GITLAB = "https://gitlab.com/acme/shop/-/merge_requests/"
SLACK = "https://acme.slack.com/archives/C024BE91L/p17300"

# (kind, title, priority, age, links, body)
ITEMS = [
    (
        "waiting",
        "Priya: can we ship the checkout fix before Friday's release?",
        "urgent",
        timedelta(hours=3),
        [Link("#checkout thread", SLACK + "11")],
        (
            "Priya asks whether the **double-charge fix** can go into Friday's release."
            " QA needs the build by Thursday noon."
        ),
    ),
    (
        "waiting",
        "Review: rate limiter for the public API",
        "urgent",
        timedelta(days=1, hours=2),
        [
            Link("!412 Rate limiter", GITLAB + "412"),
            Link("Design doc", "https://docs.google.com/d/1"),
        ],
        "Tomás asked you to review before he merges. The design doc has the limits per plan.",
    ),
    (
        "waiting",
        "Marta: which dashboard should the on-call runbook link to?",
        "normal",
        timedelta(days=8),
        [Link("#oncall thread", SLACK + "22")],
        "She proposed two Grafana dashboards and waits for your pick.",
    ),
    (
        "waiting",
        "Approve the Q4 cloud budget forecast",
        "normal",
        timedelta(days=4, hours=5),
        [Link("Forecast from finance", "https://mail.google.com/mail/u/0/#inbox/1")],
        None,
    ),
    (
        "waiting",
        "Answer Ken about the flaky payment tests",
        "normal",
        timedelta(days=2),
        [Link("SHOP-881 Flaky payment tests", "https://linear.app/acme/issue/SHOP-881")],
        "Ken wants to know if quarantining the three tests is fine until the mock server lands.",
    ),
    (
        "waiting",
        "Merge the three approved dependency MRs",
        "normal",
        timedelta(hours=20),
        [
            Link("!398 Bump httpx", GITLAB + "398"),
            Link("!401 Bump pydantic", GITLAB + "401"),
            Link("!405 Bump ruff", GITLAB + "405"),
        ],
        "All three are approved and green.",
    ),
    (
        "waiting",
        "Give feedback on the search relevance write-up",
        "low",
        timedelta(days=5),
        [Link("Search relevance notes", "https://acme.notion.site/search-relevance")],
        None,
    ),
    (
        "waiting",
        "Reply to the conference talk invitation",
        "low",
        timedelta(days=11),
        [Link("Invitation", "https://mail.google.com/mail/u/0/#inbox/2")],
        None,
    ),
    (
        "fyi",
        "Release 4.12 went out to all users",
        "normal",
        timedelta(hours=1),
        [Link("#releases", SLACK + "33")],
        None,
    ),
    (
        "fyi",
        "Ana took over the billing service on-call from Monday",
        "normal",
        timedelta(hours=6),
        [Link("#team-billing", SLACK + "44")],
        None,
    ),
    (
        "fyi",
        "The staging database moves to the new cluster on Thursday",
        "normal",
        timedelta(days=1),
        [Link("INFRA-77 Staging DB move", "https://linear.app/acme/issue/INFRA-77")],
        None,
    ),
]


def seed(path: Path) -> int:
    """Write the fictional items and return the id of the one to select."""
    now = utc_now()
    for kind, title, priority, age, links, body in sorted(ITEMS, key=lambda i: -i[3]):
        with Store(path, clock=lambda age=age: now - age) as store:
            store.add(kind, title, priority=priority, links=links, body=body)
    with Store(path, clock=lambda: now - timedelta(hours=4)) as store:
        # The user has seen everything but the items of the last few hours, which stay new.
        for item in store.list(kind="all"):
            if item.created_at < now - timedelta(hours=4):
                store.mark_seen(item.id)
        rate_limiter = next(i for i in store.list() if i.title.startswith("Review: rate"))
    with Store(path, clock=lambda: now - timedelta(hours=2)) as store:
        store.mark_opened(rate_limiter.id)
    with Store(path, clock=lambda: now - timedelta(minutes=40)) as store:
        store.update(rate_limiter.id, note="Tomás pushed the requested changes")
    with Store(path, clock=lambda: now - timedelta(minutes=25)) as store:
        store.update(rate_limiter.id, note="Merge only after the load test", actor="user")
    return rate_limiter.id


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "catchup.db"
        selected = seed(path)
        os.environ["CATCHUP_DB"] = str(path)
        from catchup.tui.app import CatchupApp
        from catchup.tui.widgets import ItemList

        app = CatchupApp(Config())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            item_list = app.query_one(ItemList)
            item_list.highlighted = item_list.get_option_index(str(selected))
            await pilot.pause(0.5)
            app.save_screenshot(OUTPUT.name, str(OUTPUT.parent))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
