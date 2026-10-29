# ghark

`ghark` lets a Claude Code session respond to review on the pull requests it
opens. When the reviewer comments, the session wakes, makes the changes,
pushes, and replies, without being told to. It runs as a local stdio MCP
server.

Each server process owns its subscriptions. No webhook endpoint, public host,
or shared service is required. The process polls GitHub while the session is
open. It stops when the session ends.

## Requirements

- Python 3.11 or later
- A GitHub personal access token in `GH_TOKEN` or `GITHUB_TOKEN`
- `uv` to run the server
- Claude Code for channel delivery; other clients can use `pending_events`

The token needs read access to the repositories and pull requests you watch.
Fine-grained tokens need **Pull requests: read-only**, **Contents: read-only**,
and read access to checks and commit statuses.

## Run

Install the project in editable mode:

```sh
uv sync
```

Add it to Claude Code as a local MCP server:

```sh
claude mcp add --scope user ghark -- uv run --directory /path/to/checkout ghark
```

To enable channel delivery in Claude Code, start the session with the channel
server enabled:

```sh
claude --dangerously-load-development-channels server:ghark
```

Custom channels are a Claude Code preview feature. Without the channel flag,
the tools still work and events remain available through `pending_events`.

For Codex, add a stdio server entry to `~/.codex/config.toml`:

```toml
[mcp_servers.ghark]
command = "uv"
args = ["run", "--directory", "/path/to/checkout", "ghark"]
```

Codex does not receive MCP channel pushes. Ask it to call `pending_events` when
you want to check for activity.

## Tools

- `subscribe` watches its `pr` argument. It accepts a GitHub PR URL or
  `OWNER/REPO#N`.
- `unsubscribe` takes the same `pr` argument and stops watching it.
- `subscriptions` lists this process's active PRs. When any exist, it also
  returns the Monitor command. It waits up to 30 seconds for startup discovery
  to finish.
- `pending_events` returns unread events and reports any events dropped at the
  queue limit.

The server reports commits, reviews, comments, check status changes, merge
state changes, and PR closure, merge, or reopen activity. It combines events
from a PR into a short notification. Merged and closed PRs leave the watch list.

## Review requests

An event is *actionable* when it is a review, issue comment, or review-thread
comment by a reviewer, other than a bare approval. The reviewers are the logins
in `GHARK_REVIEWERS` (comma-separated), or by default the token's owner.

Claude posts with the reviewer's token, so its replies carry the reviewer's
login. The server instructions tell it to end every comment with Claude Code's
signature, `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
Signed comments are never actionable, so Claude's replies do not wake it.

Actionable events wake the session through Claude Code's Monitor tool, which
works where channels are disabled by policy. Each server process appends a line
to `<log directory>/<pid>.wake` when a poll finds review requests. The
`subscribe` result gives the Monitor command that tails it. A monitor expires
after 30 minutes; the expiry also wakes the session, which calls
`pending_events` and starts the monitor again. The wake line is only a signal:
`pending_events` remains the durable read path.

## Restarted sessions

Subscriptions and unread events live in memory, and they end with the stdio
server process. A resumed or restarted Claude Code session starts a new
process. That process has no subscriptions, and the monitor that was tailing
the old wake file is gone.

On startup the server rediscovers the PRs that the session most plausibly owns.
Nothing is written to disk. In the background, it finds the git checkout of the
project directory and reads the GitHub repositories named by its remotes. It
then subscribes to every open PR by the token's owner in one of those
repositories whose head branch exists as a local branch. Branches in other
worktrees of the same repository count. Each PR gets the normal baseline, so
old activity is not replayed. The server instructions tell the model to call
`subscriptions` at the start of a session. If anything is listed, the model
starts the Monitor command that the result names.

The project directory is `CLAUDE_PROJECT_DIR`, then `PWD`, then the working
directory. Claude Code sets `CLAUDE_PROJECT_DIR` for stdio servers. The
working directory alone does not identify the project:
`uv run --directory /path/to/checkout` moves it to the ghark checkout.
Discovery is best effort. Failures are logged, and the server serves without
the missing subscriptions. Set `GHARK_DISCOVER=0` to turn it off.

Two sessions in the same repository both discover the same PRs. Each session
wakes on review of the other session's PRs. Set `GHARK_DISCOVER=0` for one of
the sessions, or `unsubscribe` PRs that the session does not own.

Channel delivery is best effort. The pending queue holds up to 1,000 events; `pending_events` reports
how many older events were dropped when that limit was reached.

## Development

```sh
uv run ghark
```

Check formatting and lint with:

```sh
uv run ruff format --check ghark
uv run ruff check ghark
```

Run the behavior and MCP stdio checks with:

```sh
uv run python -m unittest discover -s tests -v
```

Set `GHARK_POLL_SECONDS` to change the polling interval. The default is 30
seconds. Set `GHARK_BATCH_SECONDS` to change the event coalescing window. The
default is 5 seconds. Set `GHARK_DISCOVER=0` to disable startup discovery.

The server logs to stderr and to `~/.local/state/ghark/ghark.log` (or
`$XDG_STATE_HOME/ghark/ghark.log`). Set `GHARK_LOG_FILE` to change the path.
Each line carries the process ID. The log records why each process stopped:
the client closed the connection, a signal, or an uncaught exception. A log
that ends without one of these means the process was killed with SIGKILL.

See [DESIGN.md](DESIGN.md) for architecture and behavior details.
