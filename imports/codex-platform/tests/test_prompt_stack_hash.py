"""Acceptance suite for the prompt-stack identity helper.

The case names here are the shared contract: the sibling engine repository runs
the same scenarios against its own declared file set, so a behaviour that
diverges between engines shows up as a named case that only one side has.

What these assert is mostly about *not lying*. A digest that covers part of a
prompt stack is indistinguishable from one that covers all of it, and a digest
that moves for a reason nobody intended reads downstream as a real prompt
change. So the cases below pin the abstention paths — an unreadable file, an
empty set — at least as hard as they pin the happy path, and pin the hash input
definition itself against an independent implementation of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".codex/skills/critique/scripts" / "prompt-stack-hash.js"

HASH_INPUT_VERSION = 1

# Pinned here rather than read from the script. The list *is* the definition,
# so a change to it must fail a test and be taken deliberately as a hash-input
# redefinition rather than land as an implementation detail.
PROMPT_STACK_FILES = [
    ".codex/REVIEW_WORKFLOW.md",
    ".codex/references/local-review-ledger.md",
    ".codex/references/roles/code-reviewer.md",
    ".codex/references/roles/comment-analyzer.md",
    ".codex/references/roles/pr-test-analyzer.md",
    ".codex/references/roles/security-reviewer.md",
    ".codex/references/roles/silent-failure-hunter.md",
    ".codex/references/roles/type-design-analyzer.md",
    ".codex/skills/critique/SKILL.md",
    ".codex/skills/deepcritique/SKILL.md",
    ".codex/skills/pr-critique/SKILL.md",
    ".codex/skills/refactorpass/SKILL.md",
    ".codex/skills/reviewit/SKILL.md",
]

REPO_INSTRUCTION_FILES = ["AGENTS.md", "CLAUDE.md"]


def run(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        ["node", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    # A telemetry defect must never fail a review that found real defects, so a
    # non-zero exit is itself a defect regardless of what went wrong inside.
    assert result.returncode == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def write(root: Path, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def populate(root: Path, files: list[str] | None = None) -> None:
    """A repository whose declared prompt stack is entirely present."""
    for relative in files if files is not None else PROMPT_STACK_FILES:
        write(root, relative, f"# {relative}\n".encode())
    for relative in REPO_INSTRUCTION_FILES:
        write(root, relative, f"# {relative}\n".encode())


def expected_digest(domain: str, root: Path, files: list[str]) -> str | None:
    """An independent implementation of the documented hash input, version 1."""
    outer = hashlib.sha256()
    outer.update(f"loom-review-prompt-hash/v{HASH_INPUT_VERSION}/{domain}\n".encode())
    present = 0
    for relative in sorted(files):
        path = root / relative
        if path.exists():
            data = path.read_bytes()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            digest = hashlib.sha256(data).hexdigest()
            present += 1
        else:
            digest = "-"
        outer.update(f"{relative}\0{digest}\n".encode())
    return outer.hexdigest() if present else None


def test_the_documented_hash_input_definition_is_what_is_computed(
    tmp_path: Path,
) -> None:
    """The definition in the workflow doc is the contract, not the code."""
    populate(tmp_path)
    payload = run("--repo-root", str(tmp_path))
    assert payload["hashInputVersion"] == HASH_INPUT_VERSION
    assert payload["promptStackSha256"] == expected_digest(
        "prompt-stack", tmp_path, PROMPT_STACK_FILES
    )
    assert payload["repoInstructionsSha256"] == expected_digest(
        "repo-instructions", tmp_path, REPO_INSTRUCTION_FILES
    )
    assert payload["error"] is None


def test_the_two_digests_are_never_collapsed(tmp_path: Path) -> None:
    """Separate fields, separate domains: a stack is never an instruction set."""
    populate(tmp_path)
    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] != payload["repoInstructionsSha256"]

    # The same single file in each set must not produce the same digest, or the
    # domain separation is decorative.
    left = tmp_path / "left"
    right = tmp_path / "right"
    write(left, PROMPT_STACK_FILES[0], b"same\n")
    write(right, REPO_INSTRUCTION_FILES[0], b"same\n")
    assert (
        run("--repo-root", str(left))["promptStackSha256"]
        != run("--repo-root", str(right))["repoInstructionsSha256"]
    )


def test_the_hash_is_stable_under_file_order_variation(tmp_path: Path) -> None:
    """Two engines hashing in different orders would mint two identities."""
    forward = tmp_path / "forward"
    reverse = tmp_path / "reverse"
    for relative in PROMPT_STACK_FILES:
        write(forward, relative, b"body\n")
    for relative in reversed(PROMPT_STACK_FILES):
        write(reverse, relative, b"body\n")
    assert (
        run("--repo-root", str(forward))["promptStackSha256"]
        == run("--repo-root", str(reverse))["promptStackSha256"]
    )


def test_the_hash_is_stable_under_line_ending_variation(tmp_path: Path) -> None:
    """A CRLF checkout runs the same prompt generation as an LF one."""
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    cr = tmp_path / "cr"
    for relative in PROMPT_STACK_FILES:
        write(lf, relative, b"one\ntwo\n")
        write(crlf, relative, b"one\r\ntwo\r\n")
        write(cr, relative, b"one\rtwo\r")
    digest = run("--repo-root", str(lf))["promptStackSha256"]
    assert digest is not None
    assert run("--repo-root", str(crlf))["promptStackSha256"] == digest
    assert run("--repo-root", str(cr))["promptStackSha256"] == digest


def test_the_hash_is_stable_under_a_utf8_bom(tmp_path: Path) -> None:
    """A BOM is an encoding artifact, not an edit to the prompt."""
    plain = tmp_path / "plain"
    bom = tmp_path / "bom"
    for relative in PROMPT_STACK_FILES:
        write(plain, relative, b"body\n")
        write(bom, relative, b"\xef\xbb\xbfbody\n")
    assert (
        run("--repo-root", str(bom))["promptStackSha256"]
        == run("--repo-root", str(plain))["promptStackSha256"]
    )


def test_the_hash_changes_when_a_stack_file_changes(tmp_path: Path) -> None:
    """Detecting drift is the whole point; every declared file must count."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))["promptStackSha256"]
    seen = {baseline}
    for relative in PROMPT_STACK_FILES:
        original = (tmp_path / relative).read_bytes()
        write(tmp_path, relative, original + b"drift\n")
        digest = run("--repo-root", str(tmp_path))["promptStackSha256"]
        assert digest not in seen, relative
        seen.add(digest)
        write(tmp_path, relative, original)
    assert run("--repo-root", str(tmp_path))["promptStackSha256"] == baseline


def test_trailing_whitespace_is_a_real_edit(tmp_path: Path) -> None:
    """Only line endings and a BOM are normalised, nothing else."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))["promptStackSha256"]
    write(tmp_path, PROMPT_STACK_FILES[0], b"# body  \n")
    assert run("--repo-root", str(tmp_path))["promptStackSha256"] != baseline


def test_an_absent_file_is_recorded_not_skipped(tmp_path: Path) -> None:
    """A consumer that never received a file has a different stack."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))
    assert baseline["promptStack"]["present"] == len(PROMPT_STACK_FILES)

    (tmp_path / PROMPT_STACK_FILES[-1]).unlink()
    reduced = run("--repo-root", str(tmp_path))
    assert reduced["promptStack"]["present"] == len(PROMPT_STACK_FILES) - 1
    assert reduced["promptStack"]["declared"] == len(PROMPT_STACK_FILES)
    assert reduced["promptStackSha256"] != baseline["promptStackSha256"]
    assert reduced["promptStackSha256"] is not None


def test_a_stack_with_nothing_present_yields_null(tmp_path: Path) -> None:
    """"Everything absent" is not a prompt generation to compare against."""
    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["repoInstructionsSha256"] is None
    assert payload["promptStack"]["present"] == 0
    assert payload["error"] is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_an_unreadable_file_yields_null_not_a_partial_hash(tmp_path: Path) -> None:
    """A digest over part of the stack is a different stack's digest."""
    populate(tmp_path)
    blocked = tmp_path / PROMPT_STACK_FILES[0]
    blocked.chmod(0o000)
    try:
        payload = run("--repo-root", str(tmp_path))
    finally:
        blocked.chmod(0o600)
    assert payload["promptStackSha256"] is None
    assert payload["error"] == "the prompt stack could not be read"
    # The other set is independent and must survive.
    assert payload["repoInstructionsSha256"] is not None


def test_a_repo_instruction_change_leaves_the_stack_digest_alone(
    tmp_path: Path,
) -> None:
    """Collapsing the two would make every repository its own generation."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))
    write(tmp_path, REPO_INSTRUCTION_FILES[0], b"# local rule\n")
    changed = run("--repo-root", str(tmp_path))
    assert changed["promptStackSha256"] == baseline["promptStackSha256"]
    assert changed["repoInstructionsSha256"] != baseline["repoInstructionsSha256"]


def test_both_instruction_names_are_declared(tmp_path: Path) -> None:
    """The same repository state must join across engines."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))["repoInstructionsSha256"]
    for relative in REPO_INSTRUCTION_FILES:
        write(tmp_path, relative, b"# changed\n")
        assert run("--repo-root", str(tmp_path))["repoInstructionsSha256"] != baseline
        write(tmp_path, relative, f"# {relative}\n".encode())


def test_an_unknown_argument_reports_and_exits_zero(tmp_path: Path) -> None:
    """Never fail the pass; report the problem in the payload."""
    payload = run("--nope", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["repoInstructionsSha256"] is None
    assert payload["error"] == "unknown argument --nope"


def test_a_missing_repo_root_reports_no_digest(tmp_path: Path) -> None:
    """An absent root is an absent stack, not an error and not a guess."""
    payload = run("--repo-root", str(tmp_path / "gone"))
    assert payload["promptStackSha256"] is None
    assert payload["error"] is None
