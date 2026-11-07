---
name: catchup
description: Check Slack and Gmail for what is waiting on the user's reply or tags them, and keep the local catchup inbox (the `catchup` CLI) in sync with it, closing what is finished. Writes only to the inbox, never to Slack or email. Built to run frequently from a schedule. Use when they say /catchup.
---

<!--
This is an example. Copy it to ~/.claude/skills/catchup/SKILL.md and adapt it
to your sources: the spots to change are marked with comments like this one.
Delete the comments when you are done. The inbox rules (when to add, update,
reopen or close an item, and how to fill its fields) live in the catchup-inbox
skill, so install that one too.
-->

# Catch-up (Slack + Gmail)

Goal: the local catchup inbox holds exactly what needs the user: messages waiting for their reply, and important context where they are tagged. The user reads it in the catchup TUI. Each run adds what is new, updates what changed, and closes everything that is finished. The inbox is the only output: never post on Slack or send email.

The catchup-inbox skill describes the `catchup` commands and the inbox rules in full; this skill applies them to these sources.

## Window

`$ARGUMENTS` is the start of the window. Accept dates, datetimes, or phrases ("since Friday", "past 3 days"). No argument = 17:00 local time on the previous working day (Friday if today is Monday), so yesterday's evening messages are included. Convert to a Unix ts `T` and use it everywhere: Slack `oldest: T`, Gmail `after:T` (epoch seconds; a `YYYY/MM/DD` literal means midnight PST and drops the time). Slack *search* filters only take dates, so there use `after:D` where `D` is the day before `T` in `YYYY-MM-DD`, and drop results with `ts < T`.

## Identify the user

Call the logged-in Slack user ID `ME`. The `slack_search_public_and_private` tool description states it ("Current logged in user's user_id is …"); if it does not, look yourself up with `slack_search_users` by the Gmail account's address. Never hardcode it and never guess it: without `ME`, stop and say why.

## Read the inbox first

Run `catchup list --state all --kind all --since 14d --json`. It returns every item touched in the last 14 days with its state (`open`, `snoozed`, `done`, `ignored`; FYIs have none) and its last event. Run `catchup show ID --json` when you need an item's full history, for example to see that the user snoozed it twice or closed it themselves (actor `user`). Each item's `notes` are the user's own context and to-dos, such as "waiting on Dana" or "close when the MR merges". Act on them and never repeat them back as news. `opened_at` means the user started working on the item, not that it is handled.

## Gather: Slack

Use the Slack MCP tools with `response_format: detailed` (concise search results omit the channel ID and ts that the thread reads need), sorted by timestamp, paging until the window is covered. Keep `limit` fixed while paging; changing it invalidates the cursor.

1. Direct mentions: `slack_search_public_and_private` with `keywords: ["<@ME>"]`, `filters: "after:D -from:<@ME>"`.
2. DMs and threads the user is part of: `filters: "after:D with:<@ME>"`, then `"after:D is:dm"`.
3. `slack_read_thread` on every candidate and on every open or snoozed item's conversation to see the latest replies. Reads show reaction counts but not who reacted, so call `slack_get_reactions` on candidates that have reactions.

<!-- Add channels you want read in full, e.g. "4. Read #team-announcements with slack_read_channel." -->

## Gather: Gmail

1. `search_threads` with `in:inbox after:T -in:draft`, excluding senders that only send notifications: calendar replies, newsletters, and your tools' automated mail. Page until covered.
   <!-- List your noise senders here, e.g. "excluding: calendar-notification, newsletter, jira, ci". -->
2. `get_thread` (`PLAIN_TEXT`) on threads where the user is in To or CC or named in the body, and a human wrote the last message.

## Gather: code review

<!-- Keep the forge you use, or delete this section if you do not review code. -->

1. Review requests in Gmail, same cutoff: on GitLab `from:gitlab after:T ("added as a reviewer" OR "added as an assignee" OR "mentioned you")`, on GitHub `from:notifications@github.com after:T ("requested your review" OR "assigned you" OR "mentioned you")`.
2. If `glab` or `gh` is available, drop merge requests that are merged, closed, approved by the user, or that the user commented on after the request.

## Classify

A conversation is handled when the user replied after the last message to them, left an emoji reaction on it (their user ID is among the reactors), wrote the last message in the email thread, or the merge request is merged, closed or approved by them. A reply that only promises something ("will look EOB") does not count as handled.

Sort every candidate into one of two kinds, or drop it:

- **waiting:** someone asks the user a question, requests a review, decision, or approval, or is otherwise blocked on them.
- **fyi:** the user is tagged, in To/CC, or named, no action is asked, but the content matters to their work: a decision, a deadline, an incident, a change to something they own. Drop greetings, thanks, acknowledgements, and chatter.

Drop bots and automation, except the code review requests above: CI and deploy notifications, calendar invites, reminders, newsletters, vendor cold outreach, join/leave notices.

<!-- Name your bots here, and any other automated mail you want kept. -->

## Close what is finished

Go through every open and snoozed waiting item, however old its conversation, and check its current state at the source: read the thread or email to its end, and check each merge request or ticket where it lives. An item is finished when:

- the user handled it, by the rules in Classify;
- the merge request is merged or closed, or the user approved it when the ask was a review;
- the ticket is done or canceled, or no longer assigned to the user;
- someone else answered the question or did the work, or the asker withdrew it;
- a condition in the user's notes is met.

Close each finished item with `catchup done ID --note "<what finished it, e.g. !42 merged>"`. Leave an item open when you cannot tell; never close one because it is old.

## Sync the inbox

Decide per candidate whether it belongs to an existing item: the same conversation, or the same piece of work (one merge request, one ticket). You decide grouping: one item may cover several merge requests or people when the user would handle them in one go, and a conversation with two separate asks may become two items.

- **No matching item:** `catchup add`. Skip handled conversations.
- **Matches an open or snoozed item:** if there is news, `catchup update ID` with the new title or body and a `--note` saying what changed. If someone other than the user posted in a snoozed item's conversation, also `catchup reopen ID --note "<who posted>"`.
- **Matches a done item:** if someone other than the user posted after the item's last event and it waits on the user again, `catchup reopen ID --note "<who posted>"` and update the text. Otherwise leave it.
- **Matches an ignored item:** leave it, whatever happened.
- **FYI:** add each new FYI once. Never close, update or repeat an FYI.

Fields for `add` and `update`:

- `--title`: one line, who and what, e.g. `Dana: review the retry limit change !42`.
- `--body`: one to three short sentences with the detail the user needs to act. Pass long text with `--body -` on stdin.
- `--priority urgent` when someone is blocked today, a deadline is within two days, or it is about a production incident or money; `low` for open-ended questions with no deadline; otherwise leave the default.
- `--waiting-on "<name>"` when the next move is someone else's: the user asked them or handed them the work. Give it back with `--waiting-on ""` when they answer. Check it on every open item each run.
- `--link "label=url"` for every place the user would open: the Slack thread or DM, the email thread, each merge request, the ticket. Use short labels (`DM`, `#channel`, `!42`, `ABC-123`). `update --link` replaces the whole list, so pass all of them.
- `--prompt` when one coding-agent task fits the item. The agent runs in the user's home directory, so include full URLs:
  - a merge request waiting for review: `Review <MR url> and list the problems you find. Do not post anything.`
  - an approved merge request waiting for them to merge: `Merge <MR url> once its pipeline is green.`
  - a ticket to work on: `Read <ticket url> and plan the work.`
  - a question to answer: `Read <thread url> and draft a reply as a Slack draft. Do not send it.`

<!-- If you have your own agent commands or skills, put them in these prompts instead. -->

## Finish

Print a short summary in the terminal: one line per item this run added, reopened or closed (id, title, and for a closed item what finished it), then the count of open waiting items. Print `Nothing new.` when the run changed nothing.

The `catchup` commands are the only writes this skill makes: no Slack messages, drafts or reactions, no email, no labels, no edits anywhere else.
