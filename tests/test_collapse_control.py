"""Reserved collapse cells are parsed, never evaluated."""
from __future__ import annotations

import pytest

from py_agent.collapse_control import parse_collapse


def test_literal_call_and_comments():
    assert parse_collapse('# checkpoint\ncollapse("u1", "m2", "kept\\nexact")') == (
        "u1", "m2", "kept\nexact",
    )


@pytest.mark.parametrize("source", [
    "print('collapse( is just text')",
    "!ls",
    "%pwd",
    "say('done', final=True)",
])
def test_ordinary_cells_only_allowed_outside_forced_mode(source):
    assert parse_collapse(source) is None
    with pytest.raises(ValueError, match="standalone"):
        parse_collapse(source, forced=True)


@pytest.mark.parametrize("source", [
    "collapse('u1', 'm1', str(secret))",
    "collapse('u1', 'm1', f'{secret}')",
    "collapse('u1', 'm1', 'summary'); print('side effect')",
    "x = collapse('u1', 'm1', 'summary')",
    "collapse(*args)",
    "collapse(start_id='u1', end_id='m1', summary='x')",
    "collapse('u1', 'm1', 7)",
    "collapse('u1', 'm1', 'unterminated)",
    "if True:\n    collapse('u1', 'm1', 'x')",
    "collapse('u1', 'm1', 'x')\ncollapse('u2', 'm2', 'y')",
])
def test_nonliteral_or_mixed_cells_are_rejected(source):
    with pytest.raises(ValueError, match="three literal strings"):
        parse_collapse(source)
