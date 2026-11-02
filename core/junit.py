"""Read pytest's JUnit XML: every failed test, with the message that explains it.

Text output is a poor carrier for failure details. A verbose run of a large suite
puts the ``short test summary info`` block - the one line per failure that names
the assertion - at the very end, and any length cap applied to the head of the log
throws exactly that block away. ``--junitxml`` is written into the isolated
working directory instead, and holds one element per failure with the node id,
the exception line and the traceback. Parsing it means the report can list every
failed test regardless of how long the console output was.

This module is pure reading: it never runs pytest, never raises on malformed
input, and returns an empty list when the file is missing, so a run without XML
behaves exactly like a run before it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A traceback can be megabytes of pytest markup; the report needs the tail, where
# the exception and its message live.
MAX_MESSAGE_CHARS = 1_000
MAX_TRACEBACK_CHARS = 2_000
FAILURE_TAGS = ("failure", "error")
# The file is written by the project under test, so it is untrusted input.
# pytest's own JUnit output never carries a DTD, so refusing one is both a hard
# guarantee against entity-external and billion-laughs payloads and no loss of
# legitimate data. The size cap bounds the parse cost of a hostile large file.
MAX_XML_BYTES = 8_000_000
_UNTRUSTED_XML_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


@dataclass
class TestFailure:
    """One failed or errored test, as pytest recorded it."""

    node: str
    kind: str
    message: str = ""
    traceback: str = ""
    suite: str = ""
    duration_seconds: float = 0.0
    environment: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "node": self.node,
            "kind": self.kind,
            "message": _clip_tail(self.message, MAX_MESSAGE_CHARS),
            "traceback": _clip_tail(self.traceback, MAX_TRACEBACK_CHARS),
            "suite": self.suite,
            "duration_seconds": self.duration_seconds,
        }
        data.update(self.environment)
        return data


def _clip_tail(text: str, limit: int) -> str:
    """Keep the *end* of a long value: the assertion sits at the bottom."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    return f"...[dropped {removed} leading chars]\n" + text[-limit:]


def _node_id(case: ET.Element, suite_name: str) -> str:
    """Rebuild a runnable node id (``path::name``) from one ``<testcase>``.

    pytest gives the file as ``file`` when it can; otherwise the dotted
    ``classname`` is turned back into a path. A classname that ends in a segment
    no file matches is a unittest class, so it stays in the id as a component.
    """
    name = (case.get("name") or "").strip() or "unknown"
    explicit = (case.get("file") or "").strip()
    if explicit:
        return f"{explicit}::{name}"
    classname = (case.get("classname") or suite_name or "").strip()
    if not classname:
        return name
    parts = [part for part in classname.split(".") if part]
    as_file = "/".join(parts) + ".py"
    return f"{as_file}::{name}"


def parse_failures(xml_path: Path) -> list[TestFailure]:
    """Every failure and error in a JUnit XML file; empty when unreadable.

    Never raises: a missing, oversized, DTD-bearing or malformed file simply
    yields no failures, which leaves the caller on its text-output path.
    """
    try:
        if not xml_path.is_file():
            return []
        if xml_path.stat().st_size > MAX_XML_BYTES:
            return []
        raw = xml_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if _UNTRUSTED_XML_RE.search(raw):
        return []
    try:
        root = ET.fromstring(raw.lstrip())
    except ET.ParseError:
        return []

    failures: list[TestFailure] = []
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        suite_name = (suite.get("name") or "").strip()
        for case in suite.findall("testcase"):
            for tag in FAILURE_TAGS:
                detail = case.find(tag)
                if detail is None:
                    continue
                try:
                    duration = float(case.get("time") or 0.0)
                except ValueError:
                    duration = 0.0
                failures.append(
                    TestFailure(
                        node=_node_id(case, suite_name),
                        kind="error" if tag == "error" else "failed",
                        message=(detail.get("message") or "").strip(),
                        traceback=(detail.text or "").strip(),
                        suite=suite_name,
                        duration_seconds=duration,
                    )
                )
    return failures
