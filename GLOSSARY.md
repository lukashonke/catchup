# Catchup

Catchup is a local inbox that an AI agent fills with the things waiting on you, and that you work through in a terminal.

## Language

**Agent**:
The program, usually a scheduled AI agent run, that reads your sources and writes items into catchup.
_Avoid_: Bot, worker, catchup run

**Item**:
One entry in catchup. It is either a waiting item or an FYI.
_Avoid_: Task, card, notification

**Waiting item**:
An item where someone waits on you: a question, a review, a decision. It has a state.
_Avoid_: Todo, action item

**FYI**:
An item that needs no action but matters to your work. It has no state and stays in the feed as history.
_Avoid_: Info item, notice

**Feed**:
The list of FYIs, newest first.
_Avoid_: Timeline, log

**State**:
Where a waiting item is in its life: open, snoozed, done or ignored.
_Avoid_: Status

**Open**:
The state of a waiting item that still waits on you.

**Snoozed**:
The state of a waiting item that is hidden until a date. After that date it counts as open again. The agent reopens it early if someone else posts something new in the conversation.

**Done**:
The state of a waiting item that you or the agent closed. The agent reopens it if someone else posts in the conversation again.

**Ignored**:
The state of a waiting item that stays muted, even when new messages arrive.

**Priority**:
How urgent a waiting item is: urgent, normal or low. The agent sets it.

**Link**:
A labeled pointer from an item to where the conversation or the work lives, such as a Slack thread or a merge request.
_Avoid_: URL, reference

**Link type**:
The kind of place a link points to: MR, Slack, Email, Ticket, Doc, Artifact or Web.

**Prompt**:
The text that catchup sends to the coding agent when you open an item in it.

**Event**:
One entry in an item's history: who did what to the item, and when.
_Avoid_: Log entry, audit record

**Actor**:
Who made an event: you or the agent.
