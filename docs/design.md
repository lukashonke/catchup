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
- `seen_at`: when the user last saw the item in the TUI. It is not an event and does not change `updated_at`.
- `opened_at`: when the user last opened the item in a coding agent. It is read from the events, not stored.
- `new`: whether the agent created, reopened or updated the item after `seen_at`. It is read from the events, not stored.

An event has `item_id`, `at`, `actor` (`agent` or `user`), `action` (`created`, `updated`, `done`, `snoozed`, `reopened`, `ignored`, `note`, `opened`) and an optional `note`.

All timestamps are stored as ISO 8601 in UTC and shown in local time.

The schema version is in `PRAGMA user_version`, and the store migrates an older database when it opens it. Version 2 added `seen_at` and set it to the time of the migration, so items that existed before do not count as new.

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
catchup snooze ID --until 3h|1d|3d|1w|mon|tomorrow|YYYY-MM-DD [--note TEXT]
```

- `-` as the value of `--body` or `--prompt` reads the value from stdin, so an agent can pass long text without shell quoting.
- `--link` on `update` replaces the whole link list.
- `--link` splits at the first `=`. A prefix before the first `:` is a type only if it names a known link type, case-insensitive. So `doc:Spec=https://…` has type Doc, while `Re: hello=https://…` is a label.
- `list` without flags returns open waiting items. FYIs have no state, so they match only `--state open` and `--state all`. `--since` takes `1d`, `3d`, `1w` or a date, counted back from now, and filters by `updated_at`. Each listed item includes its last event, and in JSON a `notes` array with the user's notes (`at`, `note`), so the agent sees them without `show`.
- `show` returns the item with its full event history.
- `snooze --until` counts `3h`, `1d` and `1w` from now. `mon`, `tomorrow` and a date mean the start of that day in local time.
- `done`, `ignore`, `reopen` and `snooze` reject FYIs with a clear error.
- The CLI records events with actor `agent` unless `--by user` is given. The TUI records actor `user`.

## Files and configuration

- Database: `$CATCHUP_DB`, else `$XDG_DATA_HOME/catchup/catchup.db`, else `~/.local/share/catchup/catchup.db`.
- Config: `$XDG_CONFIG_HOME/catchup/config.toml`, else `~/.config/catchup/config.toml`. It is optional.

```toml
# The command that "Open in Claude" runs, as a list of arguments.
# catchup passes the item in CATCHUP_ID, CATCHUP_TITLE, CATCHUP_PROMPT and CATCHUP_LINKS (one URL per line).
open_command = ["/path/to/my-opener.sh"]
# The name of that action on the button, in the footer and in notifications.
open_label = "Open in Codex"
# The agent run; it is off without a prompt. The command gets the prompt on stdin.
agent_prompt = "Use the catchup-inbox skill to sync my inbox with Slack and email."
agent_command = ["claude", "-p", "--permission-mode", "auto"]
agent_every = "1h"
```

The settings screen in the TUI edits the same file. It writes only keys that differ from the default and keeps comments and unknown keys.

### Agent runs

The TUI starts the agent run, so a user needs no separate scheduler. A run is a background process in its own session, so it outlives the TUI. `agent.json` next to the database holds the process id and start time of the last run, and `agent.lock` serializes reading and writing it, so of several open TUIs only one starts each run. A run is due when the interval has passed since the last start and the last process is gone.

If `open_command` is not set and the TUI runs inside herdr (`HERDR_ENV=1`), catchup uses its bundled herdr opener. That opener creates a herdr tab in the current workspace, labeled with the item title, in the home directory, and starts Claude in it with the prompt. Outside herdr with no config, the button tells you how to set `open_command`.

If an item has no prompt, catchup sends a default one: the title, the body, the link URLs, and "Help me handle this."

## TUI

The TUI is a two-pane view:

- **Header**: the name, the number of open waiting items, urgent items and new items, and the time of the agent's last event. With an agent run configured, it also shows when the run started while it runs, and else when the next run is due.
- **State tabs**: Open, Snoozed, Done, Ignored, each with a count.
- **Filter bar**, above the list while a filter is set.
- **List**, on the left. On the Open tab it shows a "Waiting on you" section first, sorted by priority and then oldest first. Below it is the FYI feed, newest first, in dimmer text. Each row shows a dot for a new item or an arrow for an opened one, a priority marker (`!` urgent, `↓` low), the title, the type of the main link, and the age. The age turns yellow after 3 days and red after 7.
- **Details**, on the right: the title, the state, when it was first seen and last opened, the body, the links as clickable chips with their type, the buttons, the prompt that "Open in Claude" will send, and the history, including your notes.
- **Buttons** for a waiting item: Open in Claude, Done, Snooze (1d, 3d, until Monday), Ignore. For done and ignored items: Open in Claude and Reopen. For an FYI: Open in Claude only.
- **Footer**: the most used keys, and `?` for the list of all keys.

In a narrow terminal the details pane moves below the list, and the footer drops the hints that do not fit.

Inside herdr (`HERDR_ENV=1` and `HERDR_TAB_ID` set), the TUI also shows the counts after the label of its own tab, such as `catchup 12 · 3 new`, so you see them from the other tabs. It reads the label once at start, renames the tab with `herdr tab rename` in the background only when the text changes, and puts the label back when it quits. herdr's pane title (`pane report-metadata --title`) is not drawn on the tab bar, so the tab label is the only place the counts show. A label left behind by a TUI that crashed loses its trailing counts when the next TUI starts, and an empty label becomes `catchup`. The counts belong to the whole tab, so in a tab split with other panes they label those panes too. If herdr fails, the TUI says so once and leaves the tab alone.

An item is new when the agent created, reopened or updated it after you last saw it. An item counts as seen when it stays selected for a second, so moving the selection past a row does not count. Seeing an item is not an event and does not change `updated_at`, so it never makes the item look changed to the agent.

When "Open in Claude" succeeds, the TUI records an `opened` event by you. The details pane shows how long ago you last opened the item, and its row shows an arrow while it is not new.

The mouse and the up and down keys select items. The keys:

- Left and right: the previous and the next state tab.
- Enter: Open in Claude.
- Ctrl+Enter: open the first link in the browser. The terminal must send Ctrl+Enter as its own key (the kitty keyboard protocol); otherwise it arrives as Enter.
- Space: Done. Backspace: Ignore. z: the snooze picker. These act only on open and snoozed waiting items.
- u: undo the latest state change made in this TUI session (done, ignore, snooze, reopen, from keys or buttons). It restores the previous state and snooze date as your change, with the note "undo". The undo history is in memory and ends with the session.
- n: write a note on the selected item, waiting item or FYI. Ctrl+S saves it as a `note` event by you; Esc or an empty note cancels.
- /: filter the rows of the current tab by title, body and link labels, ignoring case. The section counts show the filtered rows. Enter returns to the list and keeps the filter; Esc clears it.
- c: catch up now, which starts the agent run. s: the settings screen.
- ?: the list of all keys. q: quit.

The inbox keys do nothing while a dialog is open or while you type in the filter bar. Ctrl+C always quits.

The snooze picker offers 3 hours, tomorrow, in 3 days, next Monday and in a week, on the keys 1 to 5, each with the moment it resolves to. Tab moves to a field that takes any value `snooze --until` accepts. Esc cancels.
