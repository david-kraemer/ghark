# Design

## Goal

Let a coding session address review on the pull requests it opens, without
the reviewer prompting it. Keep setup local and small. The session reads events
with `pending_events`; a Claude Code hook wakes it when review requests
arrive. Channel messages are sent too, where policy allows them.

## Process model

One stdio server process belongs to one client session. It holds that session's
subscriptions, GraphQL cursors, pending events, and delivery state in memory.
This avoids shared state and cross-session routing. Process exit releases all
state. A new process rediscovers the session's PRs from the project's git
checkout. See "Restart discovery" below.

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

The server has no GitHub write tools; the session writes through `gh`. It
reports activity by every author. Reviews and comments by a reviewer, other
than a bare approval, are marked actionable. The reviewers default to the
token's owner. The session posts with that token, so its own comments carry
the reviewer's login; the Claude Code signature it appends marks them, and
signed comments are never actionable. Without that rule each reply would wake
the session again. A pending review is the viewer's unsubmitted draft and is
not reported. The timeline cursor may pass it: GitHub orders a review by its
submission time, so the submitted review appears after the cursor. Its inline
comments are hidden from the REST comment list until submission, which sets
their `updated_at` to the submission time.

## Delivery

Channel messages are best effort. MCP has no channel acknowledgement, so the
server retains events for `pending_events`. A successful channel write does not
prove that the model saw or acted on the event. Clients should treat the tool
as the durable read path for the lifetime of the process.

Notifications say what changed and link to the PR. Server instructions tell
the model to address actionable events and to treat the rest as context.
Comments by anyone other than a reviewer are untrusted input; the model must
not follow instructions in them. The server does not perform actions on a PR.

Channels can be disabled by organization policy, so waking does not depend on
them. Three files per Claude Code session, in the state directory, carry the
wake protocol:

- `<session ID>.alive` is the server's lease. The server holds an exclusive
  `flock` on it while it serves. The kernel releases the lock however the
  process dies, SIGKILL included, so the lease is exact where PIDs are not.
- `<session ID>.pending` is the flag. It exists while unread review requests
  are queued, and lists their PRs. After every poll the poller raises it if
  the queue holds review requests, so a failed write is retried. This follows
  the batch window, so a drain during it leaves nothing to flag.
  `pending_events` lowers it in the same event-loop step that drains the
  queue. The write is atomic.
- `<session ID>.waiter` admits one waiter per session through its `flock`.

The server and the hook agree on the session ID without coordinating: Claude
Code sets `CLAUDE_CODE_SESSION_ID` in the server's environment and passes
`session_id` to every hook. Both sides accept only IDs made of word characters
and hyphens, since the ID names files.

The waiter, `ghark-hook-wake`, is an `asyncRewake` hook. Claude Code runs such
a hook in the background and, when it exits with status 2, wakes Claude even
when the session is idle and shows it the hook's stderr. Every two seconds the
waiter checks the lease and the flag, and it exits 2 with the PR names when
the flag is raised under a live lease. It is armed by SessionStart (startup,
resume, `/clear`, compaction) and by PostToolUse on `pending_events`. Every
drain therefore arms the next wait. The model's only part is to call
`pending_events` when woken, as the wake message tells it.

The waiter trusts the flag only under a live lease. A flag left by a dead
server, whether killed by a signal or SIGKILL, names events that died with it.
A new server lowers any flag before it takes the lease, so a waiter that starts
on resume, before the new server is up, never reads the stale one.

The flag is level-triggered. A review request that arrives while no waiter
runs, between a wake and the drain, is still flagged when the next waiter
starts, so it wakes the session at once. The earlier design tailed an
append-only file with a Monitor, and lost such lines until the monitor's
30-minute expiry. That expiry also woke an idle session 48 times a day for
nothing; a waiter costs no model turns until there is work.

Two conditions end a waiter without waking Claude:

- Another waiter holds the session's `flock` on `<session ID>.waiter`. Two arm
  routes overlap whenever the model calls `pending_events` while a waiter
  runs. Exit status 0 from an async hook is silent, so the duplicate costs
  nothing.
- No server has held the lease for five minutes. That is long enough for a
  server restart within a session. After that, the session has ended or runs
  without ghark. Watching the lease, not process ancestry, also covers a
  session that ends before the waiter has started.

Claude Code kills an `asyncRewake` hook at its `timeout` and discards it
silently, which would leave nothing to arm the next waiter. The waiter
therefore wakes Claude before the timeout it is given, by a tenth of it and at
most five minutes. The margin also covers `uv` starting the hook, which counts
against the timeout. Claude's `pending_events` call then arms its successor.
With the documented timeout of a day, an idle session takes one model turn a
day. If the model ignores a wake, no waiter runs until the next session start.

Lease and waiter files are never deleted while in use, because unlinking a
lock file races with processes opening it. At startup the server deletes state
files older than 30 days that no one has locked, along with `.wake` files from
the earlier Monitor design.

## Restart discovery

Claude Code starts a new server process when a session resumes or restarts.
The model has no reason to subscribe again. Without discovery, review on PRs
that already exist would never wake it. Writing subscriptions to disk would
require keying them to a session that outlives its process. Claude Code exposes
no stable identifier for that. The git checkout is a durable record that
already exists, so the server derives its subscriptions from it.

At startup a background task in the server lifespan does the following. It
starts after initialization, so initialization does not wait for it.

1. It resolves the project directory: `CLAUDE_PROJECT_DIR`, then `PWD`, then
   the working directory. Claude Code documents `CLAUDE_PROJECT_DIR` for stdio
   servers, and a running server's environment confirmed it. The working
   directory is unreliable: `uv run --directory` makes it the ghark checkout.
   `PWD` is inherited from the client and survives uv's `chdir`. MCP roots
   (`roots/list`) would also give the launch directory. They need a client
   round trip after initialization, and the environment variable already
   answers the question for Claude Code.
2. It runs `git remote -v` and `git branch --show-current` in that
   directory, with stdin closed, because stdin carries the MCP transport. It
   keeps the remotes on `github.com`.
3. It makes one paginated GraphQL query for the viewer's open PRs. It keeps the
   PRs whose repository matches a remote, ignoring case, and whose head branch
   is the checked-out branch. Matching every local branch subscribed each spade
   session to ten open PRs, some long idle, so every review would have woken
   every session in the repository.
4. It subscribes to each matching PR through the same path as the `subscribe`
   tool, which sets the baseline and resolves the default reviewer.

Every failure is logged and swallowed. A directory that is not a checkout, a
git timeout, a GitHub error, or one PR's failed baseline does not stop the
server or the remaining subscriptions. `GHARK_DISCOVER=0` disables discovery.

The match is a heuristic. Two sessions in one repository discover the same
PRs, and both wake on their review. The checkout cannot tell them apart. A
session can unsubscribe PRs it does not own.

Waking needs no model action after a restart: the SessionStart hook arms the
waiter, and the new server lowers the stale flag. `subscriptions` waits for
discovery to finish, for up to 30 seconds, so an early call does not return an
empty list while discovery is still running.

## Limits and failure behavior

- GitHub rate limits and network failures delay a poll. The server keeps the
  cursor and retries on the next interval.
- GitHub timeline and REST comment pagination prevent fixed recent-item windows
  from silently dropping activity. The REST review-comment query uses its
  `since` timestamp, which matches edits as well as new comments. Edits to
  comments created before the subscription are filtered by creation time.
  The exception is a review pending at subscription: the baseline records its
  ID, and its comments are reported when it is submitted, although they were
  created earlier.
  Overlap and edits to reported comments are filtered by the seen IDs, which
  last for the life of the watch. Edits are not reported.
- Review-comment polling uses one REST request per watched PR per poll, plus
  extra pages when activity is high. Large watch lists can reach GitHub's
  authenticated API rate limit sooner.
- Check changes are detected from the latest commit's rollup. Older commit
  checks are not polled after the head changes.
- The unread queue holds up to 1,000 events. If it fills, the server drops the
  oldest events and reports the dropped count through `pending_events`.
- State is local to a running process. Restarting loses unread events.
  Subscriptions are rediscovered only for the open PR of the checked-out branch.
- MCP clients may not keep a server's stderr after startup, so the server
  also logs to a file and records its exit reason there. An exception that
  escapes the poller task ends the process; channel delivery therefore never
  raises.
- Claude Code channel support is experimental and requires its channel launch
  option. Unsupported clients ignore the extension and can call
  `pending_events`.

## Security

The token stays in the process environment and is sent only to GitHub over
HTTPS. Tool arguments are validated before API calls. PR URLs must use
`github.com`; enterprise hosts are not accepted in the first version. Logs do
not include the token or response headers.
