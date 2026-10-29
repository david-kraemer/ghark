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
- `subscriptions` lists this process's active PRs.
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

Subscriptions and unread events live in memory. They end with the stdio server
process. A restarted session must subscribe again. Channel delivery is best
effort. The pending queue holds up to 1,000 events; `pending_events` reports
how many older events were dropped when that limit was reached.

## Subscribe hook

The server instructions ask Claude to subscribe after it opens a PR, but a
model can forget. The `ghark-hook-subscribe` command is a Claude Code
`PostToolUse` hook that backs this up. After a successful Bash call that ran
`gh pr create`, it adds context telling Claude to call `subscribe` with the PR
URL from the command's stdout, and to start the Monitor the result names. If
stdout has no PR URL, the reminder asks Claude to subscribe to the PR it just
created. Failed runs and other commands produce no output.

Add it to `~/.claude/settings.json` or a project's `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "if": "Bash(gh pr create*)",
            "command": "uv run --directory /path/to/checkout ghark-hook-subscribe",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

The `if` filter keeps the hook from starting on other Bash calls. The hook
covers PRs created during a session; it does not find PRs opened before the
session started.

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
default is 5 seconds.

The server logs to stderr and to `~/.local/state/ghark/ghark.log` (or
`$XDG_STATE_HOME/ghark/ghark.log`). Set `GHARK_LOG_FILE` to change the path.
Each line carries the process ID. The log records why each process stopped:
the client closed the connection, a signal, or an uncaught exception. A log
that ends without one of these means the process was killed with SIGKILL.

See [DESIGN.md](DESIGN.md) for architecture and behavior details.
