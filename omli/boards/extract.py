"""Turn a dropped file into text by deterministic code, before any model sees it.

Only the file types listed in the pack (``dump.file_types`` in ``mindmap.yaml``) are read,
and only up to ``dump.max_bytes``; the text is capped at ``dump.max_chars``. PDF, Word and
Excel files need the optional ``files`` extra (``pip install "omli[files]"``: pypdf,
python-docx, openpyxl); without it the refusal names the library.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import PurePath
from typing import Callable, Dict

from omli.boards.vocab import section

_LIBRARIES = {"pdf": ("pypdf", "pypdf"), "docx": ("docx", "python-docx"),
              "xlsx": ("openpyxl", "openpyxl")}


class ExtractError(ValueError):
    """The file cannot be turned into text here."""


class UnsupportedType(ExtractError):
    pass


class TooLarge(ExtractError):
    pass


class MissingLibrary(ExtractError):
    pass


@dataclass(frozen=True)
class Extracted:
    text: str
    method: str
    truncated: bool


def limits() -> Dict[str, int]:
    dump = section("dump")
    return {"max_bytes": int(dump["max_bytes"]), "max_chars": int(dump["max_chars"]),
            "max_rows": int(dump["max_rows"])}


def file_types() -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in section("dump")["file_types"].items()}


def _plain(data: bytes, rows: int) -> str:
    return data.decode("utf-8", errors="replace")


def _csv(data: bytes, rows: int) -> str:
    reader = csv.reader(io.StringIO(data.decode("utf-8", errors="replace")))
    out = []
    for i, row in enumerate(reader):
        if i >= rows:
            break
        out.append(", ".join(row))
    return "\n".join(out)


def _json(data: bytes, rows: int) -> str:
    return json.dumps(json.loads(data.decode("utf-8", errors="replace")), indent=1)


def _import(method: str):
    module, lib = _LIBRARIES[method]
    try:
        return __import__(module)
    except ImportError:
        raise MissingLibrary(f"reading a .{method} file needs {lib}, which is not installed; "
                             f'install it with: pip install "omli[files]"') from None


def _pdf(data: bytes, rows: int) -> str:
    reader = _import("pdf").PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _docx(data: bytes, rows: int) -> str:
    doc = _import("docx").Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text)


def _xlsx(data: bytes, rows: int) -> str:
    book = _import("xlsx").load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    try:
        for sheet in book.worksheets:
            out.append(f"# {sheet.title}")
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= rows:
                    break
                out.append(", ".join("" if c is None else str(c) for c in row))
    finally:
        book.close()
    return "\n".join(out)


_READERS: Dict[str, Callable[[bytes, int], str]] = {
    "text": _plain, "csv": _csv, "json": _json, "pdf": _pdf, "docx": _docx, "xlsx": _xlsx}


def extract(data: bytes, filename: str) -> Extracted:
    """The file's text, capped. Raises UnsupportedType, TooLarge, MissingLibrary, or
    ExtractError for a file that does not parse as its type."""
    ext = PurePath(filename or "").suffix.lstrip(".").lower()
    method = file_types().get(ext)
    if method is None or method not in _READERS:
        raise UnsupportedType(f"files of type '.{ext}' are not read here" if ext
                              else "a file without a type is not read here")
    lim = limits()
    if len(data) > lim["max_bytes"]:
        raise TooLarge(f"the file is over the {lim['max_bytes']} byte limit")
    try:
        text = _READERS[method](data, lim["max_rows"]).strip()
    except MissingLibrary:
        raise
    except Exception as exc:  # a corrupt file of an allowed type
        raise ExtractError(f"the .{ext} file could not be read ({type(exc).__name__})") from None
    return Extracted(text[:lim["max_chars"]], method, len(text) > lim["max_chars"])
