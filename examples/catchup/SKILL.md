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

Follow the catchup-inbox skill for the inbox commands and rules. This skill says which sources to read and how to judge what you find there.

## Window

`$ARGUMENTS` is the start of the window. Accept dates, datetimes, or phrases ("since Friday", "past 3 days"). No argument = 17:00 local time on the previous working day (Friday if today is Monday), so yesterday's evening messages are included. Convert to a Unix ts `T` and use it everywhere: Slack `oldest: T`, Gmail `after:T` (epoch seconds; a `YYYY/MM/DD` literal means midnight PST and drops the time). Slack *search* filters only take dates, so there use `after:D` where `D` is the day before `T` in `YYYY-MM-DD`, and drop results with `ts < T`.

## Identify the user

Call the logged-in Slack user ID `ME`. The `slack_search_public_and_private` tool description states it ("Current logged in user's user_id is …"); if it does not, look yourself up with `slack_search_users` by the Gmail account's address. Never hardcode it and never guess it: without `ME`, stop and say why.

## Read the inbox first

Run `catchup list --state all --kind all --since 14d --json`. It returns every item touched in the last 14 days with its state and its last event. Run `catchup show ID --json` when you need an item's full history, for example to see that the user snoozed it twice or closed it themselves (actor `user`). Each item's `notes` are the user's own context and to-dos, such as "waiting on Dana" or "close when the MR merges". Act on them and never repeat them back as news.

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

<!--
## Gather: code review

Delete this section if you do not review code. Otherwise keep the forge you use.

1. Review requests, same cutoff: on GitLab `from:gitlab after:T ("added as a reviewer" OR "mentioned you")`, on GitHub `from:notifications@github.com after:T ("requested your review" OR "mentioned you")`.
2. Check each one with `glab` or `gh`. Drop merge requests that are merged, closed, approved by the user, or that the user commented on after the request.
-->

## Classify

A conversation is handled when the user replied after the last message to them, left an emoji reaction on it (their user ID is among the reactors), or wrote the last message in the email thread. A reply that only promises something ("will look EOB") does not count as handled.

Sort every candidate into one of two kinds, or drop it:

- **waiting:** someone asks the user a question, requests a review, decision, or approval, or is otherwise blocked on them.
- **fyi:** the user is tagged, in To/CC, or named, no action is asked, but the content matters to their work: a decision, a deadline, an incident, a change to something they own. Drop greetings, thanks, acknowledgements, and chatter.

Drop bots and automation: CI and deploy notifications, calendar invites, reminders, newsletters, vendor cold outreach, join/leave notices.

<!-- Name your bots here, and any automated mail you want kept, such as review requests. -->

## Close what is finished

Go through every open and snoozed waiting item, however old its conversation, and check its current state at the source: read the thread or email to its end, and check each merge request or ticket where it lives. An item is finished when:

- the user handled it, by the rules in Classify;
- the merge request is merged or closed, or the user approved it when the ask was a review;
- the ticket is done or canceled, or no longer assigned to the user;
- someone else answered the question or did the work, or the asker withdrew it;
- a condition in the user's notes is met.

Close each finished item with `catchup done ID --note "<what finished it, e.g. !42 merged>"`. Leave an item open when you cannot tell; never close one because it is old.

## Sync the inbox

Match each candidate to an existing item, then add, update, reopen or skip it by the catchup-inbox rules. Group by the piece of work, not by the message: one item may cover several merge requests or people when the user would handle them in one go, and a conversation with two separate asks may become two items.

- `--title`: one line, who and what, e.g. `Dana: review the retry limit change !42`.
- `--link`: every place the user would open: the Slack thread or DM, the email thread, each merge request, the ticket. Use short labels (`DM`, `#channel`, `!42`, `ABC-123`).
- `--priority urgent` when someone is blocked today, a deadline is within two days, or it is about a production incident or money; `low` for open-ended questions with no deadline.
- `--prompt` when one coding-agent task fits the item. The agent runs in the user's home directory, so include full URLs:
  - a merge request waiting for review: `Review <MR url> and list the problems you find. Do not post anything.`
  - a question to answer: `Read <thread url> and draft a reply as a Slack draft. Do not send it.`

<!-- If you have your own agent commands or skills, put them in these prompts instead. -->

## Finish

Print a short summary in the terminal: one line per item this run added, reopened or closed (id, title, and for a closed item what finished it), then the count of open waiting items. Print `Nothing new.` when the run changed nothing.

The `catchup` commands are the only writes this skill makes: no Slack messages, drafts or reactions, no email, no labels, no edits anywhere else.
