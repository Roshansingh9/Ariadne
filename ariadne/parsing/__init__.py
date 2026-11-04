"""Language dispatch for source parsing."""

from __future__ import annotations

from ..ingest.filters import language_for
from ..model import ParsedFile
from .javascript import parse_javascript
from .python import parse_python


def parse_file(path: str, data: bytes) -> ParsedFile:
    lang = language_for(path)
    if lang == "python":
        return parse_python(path, data)
    if lang in ("javascript", "typescript", "tsx"):
        return parse_javascript(path, data, lang)
    raise ValueError(f"unsupported file: {path}")


__all__ = ["parse_file"]
