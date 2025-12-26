from __future__ import annotations

from wallwatch.app.telegram_bot import ParsedCommand, parse_command


def test_parse_command_simple() -> None:
    command = parse_command("/add SBER")
    assert command == ParsedCommand(name="add", args=["SBER"])


def test_parse_command_bot_mention() -> None:
    command = parse_command("/start@wallwatchbot")
    assert command == ParsedCommand(name="start", args=[])


def test_parse_command_non_command() -> None:
    assert parse_command("hello") is None
