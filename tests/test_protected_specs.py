"""The specification files must stay byte-for-byte unchanged (track addenda, section 1).

One exception, requested by the owner on 2026-11-08 (ADR-0024): README.md starts with an
orientation section (status, roadmap, next steps) that may be updated. The original project
brief follows a marker line and must remain byte-identical; its recorded hash covers exactly the
bytes after the marker.

Owner decision 2026-11-09 (ADR-0037): the specification files moved to docs/specs/ unchanged,
and the brief's seven links to them were repointed there (the only bytes that changed).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

BRIEF_MARKER = (
    b"<!-- ORIGINAL PROJECT BRIEF (protected): everything below this line is byte-identical to "
    b"the 13 October 2026 brief except link targets to the moved specification files (ADR-0037); "
    b"tests/test_protected_specs.py checks it -->\n"
)


def _protected_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.name != "README.md":
        return data
    _, marker, brief = data.partition(BRIEF_MARKER)
    assert marker, "README.md lost the marker that precedes the original project brief"
    return brief


def test_protected_specification_files_are_unchanged(repo_root: Path) -> None:
    manifest = (repo_root / "docs" / "protected_spec_hashes.sha256").read_text(encoding="utf-8")
    entries = [line.split() for line in manifest.splitlines() if line.strip()]
    assert len(entries) == 10
    for expected, name in entries:
        actual = hashlib.sha256(_protected_bytes(repo_root / name)).hexdigest()
        assert actual == expected, f"{name} was modified"


def test_protected_files_are_exempt_from_eol_conversion(repo_root: Path) -> None:
    attributes = (repo_root / ".gitattributes").read_text(encoding="utf-8")
    manifest = (repo_root / "docs" / "protected_spec_hashes.sha256").read_text(encoding="utf-8")
    for line in manifest.splitlines():
        name = line.split()[1]
        assert f"{name} -text" in attributes
