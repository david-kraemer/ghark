"""Checks for the Claude Code hook commands."""

import io
import json
import unittest
from typing import Any
from unittest import mock

import ghark.hooks

PR_URL = "https://github.com/owner/repo/pull/12"


class SubscribeContextTests(unittest.TestCase):
    def test_successful_create_names_url_and_tool(self) -> None:
        context = ghark.hooks.subscribe_context(_event())

        assert context is not None
        self.assertIn(f'"{PR_URL}"', context)
        self.assertIn("mcp__ghark__subscribe", context)
        self.assertIn("Monitor", context)

    def test_uses_last_url_on_stdout(self) -> None:
        stdout = f"See https://github.com/owner/repo/pull/3 for context\n{PR_URL}\n"
        context = ghark.hooks.subscribe_context(_event(stdout=stdout))

        assert context is not None
        self.assertIn(f'"{PR_URL}"', context)
        self.assertNotIn("/pull/3", context)

    def test_create_in_compound_command(self) -> None:
        command = 'git push -u origin HEAD && gh pr create --fill --title "x"'
        context = ghark.hooks.subscribe_context(_event(command=command))

        assert context is not None
        self.assertIn(PR_URL, context)

    def test_missing_url_gives_generic_reminder(self) -> None:
        context = ghark.hooks.subscribe_context(_event(stdout=""))

        assert context is not None
        self.assertIn("mcp__ghark__subscribe", context)
        self.assertNotIn("github.com", context)

    def test_ignores_other_commands(self) -> None:
        for command in ("gh pr view 12", "gh pr list", "git status", "gh issue create"):
            with self.subTest(command=command):
                self.assertIsNone(
                    ghark.hooks.subscribe_context(_event(command=command))
                )

    def test_ignores_other_tools(self) -> None:
        event = _event() | {"tool_name": "Read"}

        self.assertIsNone(ghark.hooks.subscribe_context(event))

    def test_ignores_interrupted_run(self) -> None:
        event = _event(interrupted=True)

        self.assertIsNone(ghark.hooks.subscribe_context(event))

    def test_ignores_malformed_fields(self) -> None:
        for event in (
            {},
            {"tool_name": "Bash", "tool_input": "gh pr create"},
            {"tool_name": "Bash", "tool_input": {"command": "gh pr create"}},
            _event() | {"tool_response": "https://github.com/o/r/pull/1"},
        ):
            with self.subTest(event=event):
                self.assertIsNone(ghark.hooks.subscribe_context(event))


class SubscribeHookMainTests(unittest.TestCase):
    def test_prints_post_tool_use_context(self) -> None:
        output = _run_main(json.dumps(_event()))

        payload = json.loads(output)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PostToolUse")
        self.assertIn(PR_URL, specific["additionalContext"])

    def test_prints_nothing_for_other_commands(self) -> None:
        self.assertEqual(_run_main(json.dumps(_event(command="ls"))), "")

    def test_prints_nothing_for_invalid_json(self) -> None:
        self.assertEqual(_run_main("not json"), "")
        self.assertEqual(_run_main("[]"), "")


def _event(
    *,
    command: str = "gh pr create --fill",
    stdout: str = f"{PR_URL}\n",
    interrupted: bool = False,
) -> dict[str, Any]:
    return {
        "session_id": "session",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "Open a PR"},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": interrupted,
            "isImage": False,
        },
    }


def _run_main(stdin: str) -> str:
    stdout = io.StringIO()
    with (
        mock.patch("sys.stdin", io.StringIO(stdin)),
        mock.patch("sys.stdout", stdout),
    ):
        ghark.hooks.subscribe_hook_main()
    return stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
