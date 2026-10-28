# ghark

`ghark` watches GitHub pull requests and reports new activity to one coding
session. It runs as a local stdio MCP server. Claude Code can receive live
channel messages. Any MCP client can ask for pending events.

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

Subscriptions and unread events live in memory. They end with the stdio server
process. A restarted session must subscribe again. Channel delivery is best
effort. The pending queue holds up to 1,000 events; `pending_events` reports
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
default is 5 seconds.

See [DESIGN.md](DESIGN.md) for architecture and behavior details.
