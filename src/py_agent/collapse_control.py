"""Parse reserved context-control cells without executing model-supplied code."""

from __future__ import annotations

import ast
import re

_FORMAT = "Use exactly one standalone collapse(start_id, end_id, summary) call with three literal strings."


def parse_collapse(source: str, *, forced: bool = False) -> tuple[str, str, str] | None:
    """Return literal arguments, None for ordinary code, or a bounded diagnostic.

    The bare name collapse is reserved, even inside mixed cells. No expression,
    argument, IPython transformation, or user-defined helper is evaluated here.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        if forced or re.search(r"\bcollapse\s*\(", source):
            raise ValueError(_FORMAT) from None
        return None
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "collapse"
    ]
    if not calls:
        if forced:
            raise ValueError("FORCED COLLAPSE MODE: " + _FORMAT)
        return None
    if (
        len(tree.body) != 1
        or not isinstance(tree.body[0], ast.Expr)
        or len(calls) != 1
        or tree.body[0].value is not calls[0]
    ):
        raise ValueError(_FORMAT)
    call = calls[0]
    if call.keywords or len(call.args) != 3:
        raise ValueError(_FORMAT)
    values: list[str] = []
    for arg in call.args:
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            raise ValueError(_FORMAT)
        values.append(arg.value)
    return values[0], values[1], values[2]
