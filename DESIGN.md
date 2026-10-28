# Design

## Goal

Deliver pull request activity to one local coding session. Keep setup local and
small. Claude Code gets push messages through its channel extension. Other MCP
clients read the same event queue with `pending_events`.

## Process model

One stdio server process belongs to one client session. It holds that session's
subscriptions, GraphQL cursors, pending events, and delivery state in memory.
This avoids shared state and cross-session routing. Process exit releases all
state. A resumed session subscribes again.

The server polls GitHub at a configurable interval. It uses the GitHub GraphQL
and REST APIs with the token in `GH_TOKEN` or `GITHUB_TOKEN`. It does not accept
webhooks or open a network listener.

## State boundary

The GitHub adapter translates responses into frozen domain records. A snapshot
computes inferred changes in a distinct `SnapshotChange` type. `WatchState`
filters overlapping comments, creates activity events, and returns its next
immutable value. `ActivityEvent` and `ChannelMessage` own event formatting and
serialization. `SessionState` owns the mutable subscription map and unread
queue. Its verbs add or remove a watch, record a poll, and drain pending events.
The poller coordinates API reads and delivery; it does not own a second copy of
session state. State transitions contain no `await`; one event loop serializes
their mutations. A poll result is discarded if its watch was removed while the
API request was in flight.

`GitHubClient` owns the HTTP connection. `ClaudeChannel` owns the active MCP
transport. These resource lifecycles are mutable by nature and stay separate
from PR state. No memoization cache is needed. Seen review comment IDs last for
the life of the watch. They cover the REST cursor overlap and edits to reported
comments, so each comment is reported once. This is session correctness state,
not a performance cache.

## Data flow

1. `subscribe` parses a PR URL or `OWNER/REPO#N` and loads its current state.
2. The initial load sets the timeline cursor. Existing activity is not replayed.
3. A background task polls each watched PR and reads timeline items after its
   cursor. It reads review comments after a timestamp and checks current PR and
   latest commit state.
4. `PullRequestSnapshot` and `WatchState` convert new timeline items, review
   comments, and state changes to events without mutating either input.
5. Events enter the in-memory queue. A short batch window combines events for
   each PR into one message.
6. If the client supports Claude Code channels, the server sends
   `notifications/claude/channel`. `pending_events` returns unread events for
   clients without channel support and acts as a recovery path for Claude Code.

## Activity

The default event set is commits, submitted reviews, issue comments, review
thread comments, check rollup state changes, mergeability changes, and PR
close, merge, or reopen transitions. Each new review comment is one event.
Channel delivery combines events for the same PR into one message. Individual
check runs are summarized by the latest commit's rollup. Labels and review
requests are excluded from the first version.

The initial subscription establishes a baseline. New timeline entries advance
the per-PR cursor. State changes compare the current PR state and check rollup
with the previous snapshot. A merged or closed PR is reported and removed from
the active subscription set.

The first version has no GitHub write tools. It reports activity by every
author, including the session's user, so comments that contain instructions
remain visible. Activity created through other tools, such as the `gh` CLI,
can therefore return as a new event.

## Delivery

Channel messages are best effort. MCP has no channel acknowledgement, so the
server retains events for `pending_events`. A successful channel write does not
prove that the model saw or acted on the event. Clients should treat the tool
as the durable read path for the lifetime of the process.

Notifications say what changed and link to the PR. Server instructions tell
the model to notify the user and wait for direction before acting on activity.
The server does not perform actions on a PR.

## Limits and failure behavior

- GitHub rate limits and network failures delay a poll. The server keeps the
  cursor and retries on the next interval.
- GitHub timeline and REST comment pagination prevent fixed recent-item windows
  from silently dropping activity. The REST review-comment query uses its
  `since` timestamp, which matches edits as well as new comments. Edits to
  comments created before the subscription are filtered by creation time.
  Overlap and edits to reported comments are filtered by the seen IDs, which
  last for the life of the watch. Edits are not reported.
- Review-comment polling uses one REST request per watched PR per poll, plus
  extra pages when activity is high. Large watch lists can reach GitHub's
  authenticated API rate limit sooner.
- Check changes are detected from the latest commit's rollup. Older commit
  checks are not polled after the head changes.
- The unread queue holds up to 1,000 events. If it fills, the server drops the
  oldest events and reports the dropped count through `pending_events`.
- State is local to a running process. Restarting loses subscriptions and
  unread events.
- Claude Code channel support is experimental and requires its channel launch
  option. Unsupported clients ignore the extension and can call
  `pending_events`.

## Security

The token stays in the process environment and is sent only to GitHub over
HTTPS. Tool arguments are validated before API calls. PR URLs must use
`github.com`; enterprise hosts are not accepted in the first version. Logs do
not include the token or response headers.
