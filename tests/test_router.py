"""Reserved prefixes must not swallow path-prefixed diagnostic pastes."""
from __future__ import annotations

import pytest

from py_agent.builtin_services import DefaultRouter
from py_agent.contracts import Origin, UserAction


def route(text):
    return DefaultRouter().route(UserAction(Origin("session", "request", "terminal", 0), text))


@pytest.mark.parametrize("text", [
    "/py_agent/production_services.py:74: contract\n"
    "Request failed: Codex request failed (SSLError); no source accepted",
    "/home/user/project.py has an error",
    "/README.md\nPlease inspect this file",
    "//server/share/log.txt",
])
def test_path_prefixed_input_is_preserved_as_english(text):
    action = route(text)
    assert action.kind == "ask"
    assert action.source == text


@pytest.mark.parametrize("text", [
    "/status", "/unknown", "/plugin-command argument", "/plugin_command argument",
    '/config set example.value "first\nsecond"',
])
def test_command_identifiers_keep_existing_routing(text):
    action = route(text)
    assert action.kind == "command"
    assert action.source == text[1:]


def test_quoted_slash_still_forces_english_for_ambiguous_root_path():
    action = route("\\/tmp please inspect")
    assert action.kind == "ask"
    assert action.source == "/tmp please inspect"


@pytest.mark.parametrize("text", ["/", "/ \n"])
def test_empty_commands_still_fail(text):
    with pytest.raises(ValueError, match="Empty command"):
        route(text)
