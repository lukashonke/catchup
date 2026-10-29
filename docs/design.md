# Design

Catchup has two halves that share one SQLite database:

- The **CLI** is how an agent writes items. The agent decides what is an item, what to group, and when to reuse an existing item. Catchup only stores and shows what the agent wrote.
- The **TUI** is how you read items and act on them. It runs in a terminal tab and reloads when the database changes.

The words used here are defined in [GLOSSARY.md](../GLOSSARY.md).

## Key decisions

- **One writer interface.** All writes go through the `catchup` package: the CLI for agents, and the same store module for the TUI. Agents never edit the database or any file directly. Several writers can run at the same time (a scheduled agent run and the TUI), so the store uses SQLite in WAL mode with a busy timeout.
- **The agent owns judgment, catchup owns state.** Catchup does not deduplicate, group or close items on its own. It records every change as an event, so the agent can read what you did ("you snoozed this twice") on its next run.
- **Snooze expiry is computed on read.** A snoozed item whose date has passed counts as open. No background job exists.
- **FYIs have no state.** They are a feed. Old FYIs are not deleted; they move down as new ones arrive.
- **Opening an item runs a configurable command.** Item data reaches the command only through environment variables, never through a shell string. The prompt text comes from messages written by other people, and pasting it into a shell string would let a message run commands on your machine.

## Data

An item has:

- `id`: an integer that the store assigns.
- `kind`: `waiting` or `fyi`.
- `title`: one line.
- `body`: optional short Markdown text.
- `priority`: `urgent`, `normal` or `low`, default `normal`. FYIs ignore it.
- `links`: an ordered list of `{label, url, type}`. `type` is optional. If it is missing, catchup infers it from the URL (see below). The first link is the main one.
- `prompt`: optional text for the coding agent.
- `state`: `open`, `snoozed`, `done` or `ignored` for waiting items, empty for FYIs.
- `snoozed_until`: a timestamp, set only while snoozed.
- `created_at`, `updated_at`: timestamps that the store sets.

An event has `item_id`, `at`, `actor` (`agent` or `user`), `action` (`created`, `updated`, `done`, `snoozed`, `reopened`, `ignored`, `note`) and an optional `note`.

All timestamps are stored as ISO 8601 in UTC and shown in local time.

### Link type inference

The first rule that matches wins:

1. `slack.com` → Slack
2. `merge_requests` or `/pull/` in the path → MR
3. `mail.google.com` or `outlook.` → Email
4. `linear.app`, `atlassian.net/browse`, or `/issues/` in the path → Ticket
5. `claude.ai/artifact` or `claude.ai/code/artifact` → Artifact
6. `docs.google.com`, `notion.so`, `notion.site` → Doc
7. anything else → Web

## CLI contract

`catchup` with no arguments starts the TUI. Every subcommand accepts `--json` and then prints machine-readable output to stdout. Errors go to stderr with a non-zero exit code.

```
catchup add --kind waiting|fyi --title TEXT [--body TEXT|-] [--priority urgent|normal|low]
            [--link "[type:]label=url"]... [--prompt TEXT|-] [--note TEXT]
catchup update ID [--title TEXT] [--body TEXT|-] [--priority P] [--link ...]... [--prompt TEXT|-] [--note TEXT]
catchup list [--state open|snoozed|done|ignored|all] [--kind waiting|fyi|all] [--since WHEN]
catchup show ID
catchup done ID [--note TEXT]
catchup ignore ID [--note TEXT]
catchup reopen ID [--note TEXT]
catchup snooze ID --until 1d|3d|1w|mon|YYYY-MM-DD [--note TEXT]
```

- `-` as the value of `--body` or `--prompt` reads the value from stdin, so an agent can pass long text without shell quoting.
- `--link` on `update` replaces the whole link list.
- `--link` splits at the first `=`. A prefix before the first `:` is a type only if it names a known link type, case-insensitive. So `doc:Spec=https://…` has type Doc, while `Re: hello=https://…` is a label.
- `list` without flags returns open waiting items. FYIs have no state, so they match only `--state open` and `--state all`. `--since` takes the same forms as `--until` in the past direction (`1d`, `3d`, `1w`, a date) and filters by `updated_at`. Each listed item includes its last event.
- `show` returns the item with its full event history.
- `done`, `ignore`, `reopen` and `snooze` reject FYIs with a clear error.
- The CLI records events with actor `agent` unless `--by user` is given. The TUI records actor `user`.

## Files and configuration

- Database: `$CATCHUP_DB`, else `$XDG_DATA_HOME/catchup/catchup.db`, else `~/.local/share/catchup/catchup.db`.
- Config: `$XDG_CONFIG_HOME/catchup/config.toml`, else `~/.config/catchup/config.toml`. It is optional.

```toml
# The command that "Open in Claude" runs, as a list of arguments.
# catchup passes the item in CATCHUP_ID, CATCHUP_TITLE, CATCHUP_PROMPT and CATCHUP_LINKS (one URL per line).
open_command = ["/path/to/my-opener.sh"]
```

If `open_command` is not set and the TUI runs inside herdr (`HERDR_ENV=1`), catchup uses its bundled herdr opener. That opener creates a herdr tab in the current workspace, labeled with the item title, in the home directory, and starts Claude in it with the prompt. Outside herdr with no config, the button tells you how to set `open_command`.

If an item has no prompt, catchup sends a default one: the title, the body, the link URLs, and "Help me handle this."

## TUI

The TUI is a two-pane view:

- **Header**: the name, the number of open waiting items and urgent items, and the time of the agent's last event.
- **State tabs**: Open, Snoozed, Done, Ignored, each with a count.
- **List**, on the left. On the Open tab it shows a "Waiting on you" section first, sorted by priority and then oldest first. Below it is the FYI feed, newest first, in dimmer text. Each row shows a priority marker (`!` urgent, `↓` low), the title, the type of the main link, and the age. The age turns yellow after 3 days and red after 7.
- **Details**, on the right: the title, the state, when it was first seen, the body, the links as clickable chips with their type, the buttons, the prompt that "Open in Claude" will send, and the history.
- **Buttons** for a waiting item: Open in Claude, Done, Snooze (1d, 3d, until Monday), Ignore. For done and ignored items: Open in Claude and Reopen. For an FYI: Open in Claude only.

In a narrow terminal the details pane moves below the list. Arrow keys and the mouse select items, and Enter is the same as "Open in Claude". The first version has no other shortcuts.
