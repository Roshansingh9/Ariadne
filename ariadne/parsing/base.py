"""Shared tree-sitter helpers."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from tree_sitter import Language, Node, Parser

from ..model import ParsedFile, Symbol, truncate_source

_WS = re.compile(r"\s+")


@lru_cache(maxsize=None)
def get_parser(language: str) -> Parser:
    if language == "python":
        import tree_sitter_python as m
        return Parser(Language(m.language()))
    if language == "javascript":
        import tree_sitter_javascript as m
        return Parser(Language(m.language()))
    if language == "typescript":
        import tree_sitter_typescript as m
        return Parser(Language(m.language_typescript()))
    if language == "tsx":
        import tree_sitter_typescript as m
        return Parser(Language(m.language_tsx()))
    raise ValueError(f"unsupported language: {language}")


def text(node: Optional[Node]) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def one_line(s: str, limit: int = 300) -> str:
    s = _WS.sub(" ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


class SymbolBuilder:
    """Allocates deterministic, unique uids within one file."""

    def __init__(self, parsed: ParsedFile, source_lines: list[str]):
        self.parsed = parsed
        self.lines = source_lines
        self._used: set[str] = set()

    def make(self, *, label: str, kind: str, name: str, qualname: str, node: Node,
             signature: str, docstring: str = "", parent: Optional[Symbol] = None,
             class_sym: Optional[Symbol] = None) -> Symbol:
        line_start = node.start_point.row + 1
        line_end = node.end_point.row + 1
        uid = f"{self.parsed.path}::{qualname}"
        if uid in self._used:
            uid = f"{uid}#L{line_start}"
        self._used.add(uid)
        source = truncate_source("\n".join(self.lines[line_start - 1: line_end]))
        sym = Symbol(
            uid=uid, label=label, kind=kind, name=name, qualname=qualname,
            path=self.parsed.path, language=self.parsed.language,
            line_start=line_start, line_end=line_end,
            signature=one_line(signature), docstring=docstring[:600], source=source,
            parent_uid=parent.uid if parent else None,
            class_uid=class_sym.uid if class_sym else None,
        )
        self.parsed.symbols.append(sym)
        return sym
