"""Small, terminal-safe Markdown presentation for agent say() messages."""
from __future__ import annotations

import re

from prompt_toolkit.formatted_text import FormattedText


def _inline(text: str, default_style: str = "") -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    pattern = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^]\n]+\]\([^)\n]+\))")
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position:
            fragments.append((default_style, text[position:match.start()]))
        token = match.group()
        if token.startswith("`"):
            fragments.append(("class:md-code", token[1:-1]))
        elif token.startswith(("**", "__")):
            fragments.append(("class:md-bold", token[2:-2]))
        elif token.startswith(("*", "_")):
            fragments.append(("class:md-italic", token[1:-1]))
        else:
            label, target = token[1:].split("](", 1)
            fragments.extend([(default_style, label), ("class:md-code", f" ({target[:-1]})")])
        position = match.end()
    fragments.append((default_style, text[position:]))
    return fragments


def _table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells, current, escaped, code = [], [], False, False
    for char in stripped:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
            current.append(char)
        elif char == "`":
            code = not code
            current.append(char)
        elif char == "|" and not code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells if len(cells) > 1 else None


def _table_fragments(rows: list[list[str]]) -> list[tuple[str, str]]:
    rendered = [[_inline(cell) for cell in row] for row in rows]
    widths = [max(sum(len(text) for _, text in row[column]) for row in rendered) for column in range(len(rows[0]))]
    fragments: list[tuple[str, str]] = []
    for row_number, row in enumerate(rows):
        if row_number:
            fragments.append(("", "\n"))
        for column, cell in enumerate(row):
            if column:
                fragments.append(("", "  "))
            style = "class:md-heading" if row_number == 0 else ""
            parts = _inline(cell, style)
            fragments.extend(parts)
            visible = sum(len(text) for _, text in parts)
            fragments.append((style, " " * (widths[column] - visible)))
    return fragments


def markdown_fragments(safe: str) -> FormattedText:
    """Render sanitized Markdown to text fragments without executing HTML/escapes."""
    safe = safe.strip("\n")
    fragments: list[tuple[str, str]] = []
    fenced = False
    lines = safe.split("\n")
    line_number = 0
    while line_number < len(lines):
        line = lines[line_number]
        if fragments:
            fragments.append(("", "\n"))
        if re.match(r"^\s*```", line):
            fenced = not fenced
            line_number += 1
            continue
        if fenced:
            fragments.append(("class:md-code", line))
            line_number += 1
            continue
        header = _table_row(line)
        if header and line_number + 1 < len(lines):
            separator = _table_row(lines[line_number + 1])
            if (separator and len(separator) == len(header)
                    and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator)):
                rows = [header]
                line_number += 2
                while line_number < len(lines):
                    row = _table_row(lines[line_number])
                    if row is None or len(row) != len(header):
                        break
                    rows.append(row)
                    line_number += 1
                fragments.extend(_table_fragments(rows))
                continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        quote = re.match(r"^\s{0,3}>\s?(.*)$", line)
        bullet = re.match(r"^(\s*)[-+*]\s+(.*)$", line)
        if heading:
            fragments.extend(_inline(heading.group(1), "class:md-heading"))
        elif quote:
            fragments.append(("class:md-quote", "│ "))
            fragments.extend(_inline(quote.group(1), "class:md-quote"))
        elif bullet:
            fragments.append(("", f"{bullet.group(1)}• "))
            fragments.extend(_inline(bullet.group(2)))
        else:
            fragments.extend(_inline(line))
        line_number += 1
    return FormattedText(fragments)
