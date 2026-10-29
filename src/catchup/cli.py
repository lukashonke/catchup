"""The `catchup` command line, through which an agent writes and reads items."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import get_args

from catchup import CatchupError
from catchup.links import Link, parse_link
from catchup.store import Event, Item, Kind, Priority, State, Store, default_db_path, utc_now
from catchup.when import format_age, parse_since, parse_until

_MARKERS = {"urgent": "!", "low": "↓"}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the exit code; without a subcommand, start the TUI."""
    args = _parser().parse_args(argv)
    if args.command is None:
        from catchup.tui import run  # Lazy, so agent commands never pay for importing Textual.

        return run()
    try:
        _read_stdin_values(args)
        with Store() as store:
            _COMMANDS[args.command](store, args)
    except CatchupError as e:
        print(f"catchup: error: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(
            f"catchup: error: the database at {default_db_path()} failed: {e};"
            " try again, and if it keeps failing check that file",
            file=sys.stderr,
        )
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print machine-readable JSON")
    writer = argparse.ArgumentParser(add_help=False, parents=[common])
    writer.add_argument("--note", help="a note to record with the event")
    writer.add_argument(
        "--by", choices=("agent", "user"), default="agent", help="who makes the change"
    )
    item_id = argparse.ArgumentParser(add_help=False)
    item_id.add_argument("id", type=int, help="the item id")

    parser = argparse.ArgumentParser(
        prog="catchup",
        description="A local inbox of things waiting on you. Without a command, starts the TUI.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    add = sub.add_parser("add", parents=[writer], help="create an item")
    add.add_argument("--kind", choices=get_args(Kind), required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--priority", choices=get_args(Priority), default="normal")
    _content_options(add)

    update = sub.add_parser("update", parents=[item_id, writer], help="change an item")
    update.add_argument("--title")
    update.add_argument("--priority", choices=get_args(Priority))
    _content_options(update, links_help="replaces the whole link list")

    ls = sub.add_parser("list", parents=[common], help="list items (default: open waiting items)")
    ls.add_argument("--state", choices=(*get_args(State), "all"), default="open")
    ls.add_argument("--kind", choices=(*get_args(Kind), "all"), default="waiting")
    ls.add_argument("--since", help="only items updated since 1d, 3d, 1w or YYYY-MM-DD")

    sub.add_parser("show", parents=[item_id, common], help="show an item with its history")
    sub.add_parser("done", parents=[item_id, writer], help="mark a waiting item done")
    sub.add_parser("ignore", parents=[item_id, writer], help="mute a waiting item")
    sub.add_parser("reopen", parents=[item_id, writer], help="make a waiting item open again")
    snooze = sub.add_parser("snooze", parents=[item_id, writer], help="hide a waiting item")
    snooze.add_argument(
        "--until", required=True, help="3h, 1d, 3d, 1w, mon, tomorrow or YYYY-MM-DD"
    )
    return parser


def _content_options(
    parser: argparse.ArgumentParser, links_help: str = "the first link is the main one"
) -> None:
    parser.add_argument("--body", help="short Markdown text, or - to read it from stdin")
    parser.add_argument("--prompt", help="text for the coding agent, or - to read it from stdin")
    parser.add_argument(
        "--link",
        action="append",
        type=_link_arg,
        metavar="[TYPE:]LABEL=URL",
        help=f"repeatable; {links_help}",
    )


def _link_arg(spec: str) -> Link:
    try:
        return parse_link(spec)
    except CatchupError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _read_stdin_values(args: argparse.Namespace) -> None:
    from_stdin = [name for name in ("body", "prompt") if getattr(args, name, None) == "-"]
    if len(from_stdin) > 1:
        raise CatchupError("only one of --body and --prompt can be - (read from stdin)")
    for name in from_stdin:
        setattr(args, name, sys.stdin.read().rstrip("\n"))


def _add(store: Store, args: argparse.Namespace) -> None:
    item = store.add(
        args.kind,
        args.title,
        body=args.body,
        priority=args.priority,
        links=args.link or (),
        prompt=args.prompt,
        note=args.note,
        actor=args.by,
    )
    _print_change(args, item, "Created #{}")


def _update(store: Store, args: argparse.Namespace) -> None:
    item = store.update(
        args.id,
        title=args.title,
        body=args.body,
        priority=args.priority,
        links=args.link,
        prompt=args.prompt,
        note=args.note,
        actor=args.by,
    )
    _print_change(args, item, "Updated #{}")


def _list(store: Store, args: argparse.Namespace) -> None:
    since = parse_since(args.since, utc_now()) if args.since else None
    items = store.list(state=args.state, kind=args.kind, since=since)
    if args.json:
        notes = store.user_notes()
        _print_json(
            [
                {
                    **asdict(item),
                    "notes": [{"at": n.at, "note": n.note} for n in notes.get(item.id, [])],
                }
                for item in items
            ]
        )
    elif not items:
        print("No items match.")
    else:
        now = utc_now()
        for item in items:
            print(_line(item, now))


def _show(store: Store, args: argparse.Namespace) -> None:
    item = store.get(args.id)
    events = store.history(args.id)
    if args.json:
        _print_json({**asdict(item), "events": [asdict(event) for event in events]})
    else:
        print(_details(item, events))


def _done(store: Store, args: argparse.Namespace) -> None:
    _print_change(args, store.done(args.id, note=args.note, actor=args.by), "Marked #{} done")


def _ignore(store: Store, args: argparse.Namespace) -> None:
    _print_change(args, store.ignore(args.id, note=args.note, actor=args.by), "Ignored #{}")


def _reopen(store: Store, args: argparse.Namespace) -> None:
    _print_change(args, store.reopen(args.id, note=args.note, actor=args.by), "Reopened #{}")


def _snooze(store: Store, args: argparse.Namespace) -> None:
    until = parse_until(args.until, utc_now())
    item = store.snooze(args.id, until, note=args.note, actor=args.by)
    _print_change(args, item, "Snoozed #{} until " + _local(until))


_COMMANDS = {
    "add": _add,
    "update": _update,
    "list": _list,
    "show": _show,
    "done": _done,
    "ignore": _ignore,
    "reopen": _reopen,
    "snooze": _snooze,
}


def _print_change(args: argparse.Namespace, item: Item, summary: str) -> None:
    """Print the changed item: as JSON, or as `summary` (with `{}` for the id) and the title."""
    if args.json:
        _print_json(asdict(item))
    else:
        print(f"{summary.format(item.id)}: {item.title}")


def _print_json(data: object) -> None:
    print(json.dumps(data, default=_json_value, ensure_ascii=False, indent=2))


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _line(item: Item, now: datetime) -> str:
    marker = _MARKERS.get(item.priority, " ") if item.kind == "waiting" else " "
    kind_state = f"waiting/{item.state}" if item.kind == "waiting" else "fyi"
    link_type = f"  [{item.links[0].type}]" if item.links else ""
    age = format_age(now - item.created_at)
    return f"{'#' + str(item.id):>5} {marker} {kind_state:<15} {age:>4}  {item.title}{link_type}"


def _details(item: Item, events: list[Event]) -> str:
    facts = [item.kind]
    if item.kind == "waiting":
        facts += [str(item.state), item.priority]
    lines = [
        f"#{item.id}  {item.title}",
        " · ".join(facts),
        f"First seen {_local(item.created_at)}, updated {_local(item.updated_at)}",
    ]
    if item.snoozed_until:
        lines.append(f"Snoozed until {_local(item.snoozed_until)}")
    if item.body:
        lines += ["", item.body]
    if item.links:
        lines += ["", "Links"]
        type_width = max(len(str(link.type)) for link in item.links)
        label_width = max(len(link.label) for link in item.links)
        lines += [
            f"  {link.type:<{type_width}}  {link.label:<{label_width}}  {link.url}"
            for link in item.links
        ]
    if item.prompt:
        lines += ["", "Prompt", *(f"  {line}" for line in item.prompt.splitlines())]
    lines += ["", "History"]
    for event in events:
        note = f"  {event.note}" if event.note else ""
        lines.append(f"  {_local(event.at)}  {event.actor:<5}  {event.action:<8}{note}".rstrip())
    return "\n".join(lines)


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")
