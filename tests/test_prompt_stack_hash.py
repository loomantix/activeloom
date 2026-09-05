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
SCRIPT = REPO_ROOT / ".claude/skills/critique/scripts" / "prompt-stack-hash.js"

HASH_INPUT_VERSION = 2

HARNESS_ROOT = ".claude"
MANIFEST_PATH = f"{HARNESS_ROOT}/prompt-stack.json"
MANIFEST_VERSION = 1
STACK_VERSION = "4.5.6"

# The stack these fixtures declare. Under hash input version 2 the membership is
# no longer pinned in this file, because it is no longer pinned in the script
# either: the list is a build output of the repository that owns the prompts,
# shipped as `prompt-stack.json` and read from there. What stays pinned is the
# *definition* — how a declared set becomes a digest — which is what
# `expected_digest` below reimplements independently.
PROMPT_STACK_FILES = [
    ".claude/MODEL_NOTES.md",
    ".claude/REVIEW_WORKFLOW.md",
    ".claude/references/local-review-ledger.md",
    ".claude/skills/critique/SKILL.md",
    ".claude/skills/deepcritique/SKILL.md",
    ".claude/skills/refactorpass/SKILL.md",
    ".claude/skills/reviewit/SKILL.md",
]

REPO_INSTRUCTION_FILES = ["AGENTS.md", "CLAUDE.md"]


def run(*args: str) -> dict[str, Any]:
    return run_script(SCRIPT, *args)


def run_script(script: Path, *args: str) -> dict[str, Any]:
    """Run one engine's copy of the helper.

    Named separately because which copy runs is a property under test for the
    shipped manifests: each engine's script derives its own `HARNESS_ROOT`.
    """
    result = subprocess.run(
        ["node", str(script), *args],
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


def write_manifest(repo: Path, **overrides: Any) -> None:
    """Ship a stack declaration, as the upstream renderer would.

    The parameter is `repo`, not `root`: `root` is a manifest field a caller
    needs to override to build a manifest belonging to another harness.
    """
    payload: dict[str, Any] = {
        "manifestVersion": MANIFEST_VERSION,
        "promptStackVersion": STACK_VERSION,
        "engine": "claude",
        "root": HARNESS_ROOT,
        "files": list(PROMPT_STACK_FILES),
    }
    payload.update(overrides)
    write(repo, MANIFEST_PATH, f"{json.dumps(payload, indent=2)}\n".encode())


def populate(root: Path, files: list[str] | None = None) -> None:
    """A repository whose declared prompt stack is entirely present."""
    declared = files if files is not None else PROMPT_STACK_FILES
    for relative in declared:
        write(root, relative, f"# {relative}\n".encode())
    for relative in REPO_INSTRUCTION_FILES:
        write(root, relative, f"# {relative}\n".encode())
    write_manifest(root, files=list(declared))


def expected_digest(domain: str, root: Path, files: list[str]) -> str | None:
    """An independent implementation of the documented hash input, version 2."""
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
    write_manifest(left, files=[PROMPT_STACK_FILES[0]])
    write(right, REPO_INSTRUCTION_FILES[0], b"same\n")
    write_manifest(right, files=[PROMPT_STACK_FILES[0]])
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
    write_manifest(forward, files=list(PROMPT_STACK_FILES))
    for relative in reversed(PROMPT_STACK_FILES):
        write(reverse, relative, b"body\n")
    # Declared in the opposite order as well: the manifest is a file, editable
    # anywhere it lands, so its order must not be part of the hash input.
    write_manifest(reverse, files=list(reversed(PROMPT_STACK_FILES)))
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
    for tree in (lf, crlf, cr):
        write_manifest(tree)
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
    for tree in (plain, bom):
        write_manifest(tree)
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
    """ "Everything absent" is not a prompt generation to compare against.

    The stack half states its reason; the instructions half does not. That
    asymmetry is the point: a manifest that parsed asserts these files *are* the
    stack, so nothing arriving contradicts it, while a repository carrying
    neither instruction file is simply a repository without one.
    """
    write_manifest(tmp_path)
    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["repoInstructionsSha256"] is None
    assert payload["promptStack"]["present"] == 0
    assert payload["error"] == (
        "no declared prompt-stack file is present under .claude"
    )


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
    """An absent root has no declaration to read, and says so."""
    payload = run("--repo-root", str(tmp_path / "gone"))
    assert payload["promptStackSha256"] is None
    assert payload["promptStackVersion"] is None
    assert payload["error"] == "no prompt-stack.json under .claude"


# --------------------------------------------------------------------------
# The stack declaration
# --------------------------------------------------------------------------


def test_the_declared_version_is_reported_beside_the_digest(tmp_path: Path) -> None:
    """A digest identifies a generation; only the version orders two of them."""
    populate(tmp_path)
    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackVersion"] == STACK_VERSION
    assert payload["manifestVersion"] == MANIFEST_VERSION
    assert payload["harnessRoot"] == HARNESS_ROOT


def test_the_version_is_not_mixed_into_the_digest(tmp_path: Path) -> None:
    """A bump that changed no prompt must not look like a prompt change."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))["promptStackSha256"]
    write_manifest(tmp_path, promptStackVersion="99.0.0")
    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] == baseline
    assert payload["promptStackVersion"] == "99.0.0"


def test_a_prompt_edit_moves_the_digest_without_a_version_bump(
    tmp_path: Path,
) -> None:
    """The converse: the digest does not depend on anyone remembering to bump."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))["promptStackSha256"]
    write(tmp_path, PROMPT_STACK_FILES[0], b"# edited\n")
    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] != baseline
    assert payload["promptStackVersion"] == STACK_VERSION


def test_a_changed_declaration_changes_the_identity(tmp_path: Path) -> None:
    """Membership is part of the stack: covering less is a different stack."""
    populate(tmp_path)
    baseline = run("--repo-root", str(tmp_path))
    write_manifest(tmp_path, files=PROMPT_STACK_FILES[:-1])
    reduced = run("--repo-root", str(tmp_path))
    assert reduced["promptStack"]["declared"] == len(PROMPT_STACK_FILES) - 1
    assert reduced["promptStackSha256"] != baseline["promptStackSha256"]


@pytest.mark.parametrize(
    ("overrides", "raw", "expected"),
    [
        pytest.param(None, "{ not json", "not valid JSON", id="malformed-json"),
        pytest.param(None, "[]\n", "not an object", id="array"),
        pytest.param(None, "null\n", "not an object", id="null"),
        pytest.param(
            {"manifestVersion": 2},
            None,
            "unsupported manifestVersion",
            id="newer-schema",
        ),
        pytest.param(
            {"manifestVersion": None},
            None,
            "unsupported manifestVersion",
            id="absent-schema",
        ),
        pytest.param({"root": ".codex"}, None, "declares root", id="another-harness"),
        pytest.param(
            {"promptStackVersion": "1.0"}, None, "MAJOR.MINOR.PATCH", id="short-version"
        ),
        pytest.param(
            {"promptStackVersion": None}, None, "MAJOR.MINOR.PATCH", id="absent-version"
        ),
        pytest.param({"files": []}, None, "declares no prompt files", id="empty-set"),
        pytest.param(
            {"files": "x"}, None, "declares no prompt files", id="files-not-a-list"
        ),
        pytest.param(
            {"files": ["../outside.md"]}, None, "unusable path", id="escaping"
        ),
        pytest.param({"files": ["/etc/passwd"]}, None, "unusable path", id="absolute"),
        pytest.param(
            {"files": [".claude/../../x.md"]}, None, "unusable path", id="traversal"
        ),
        pytest.param(
            {"files": [".codex/SKILL.md"]}, None, "unusable path", id="other-root"
        ),
        pytest.param(
            {"files": [".claude\\x.md"]}, None, "unusable path", id="backslash"
        ),
        pytest.param({"files": [3]}, None, "unusable path", id="not-a-string"),
        # The renderer refuses to emit this, but a manifest is an ordinary file
        # by the time it is read here. Hashing it would fold
        # `promptStackVersion` into the digest, so a version-only bump would
        # move a digest that is documented never to move for one.
        pytest.param(
            {"files": [".claude/prompt-stack.json"]},
            None,
            "unusable path",
            id="self-reference",
        ),
        pytest.param(
            {"files": [".claude/a.md", ".claude/a.md"]},
            None,
            "duplicate path",
            id="duplicate",
        ),
    ],
)
def test_an_unusable_declaration_abstains(
    tmp_path: Path,
    overrides: dict[str, Any] | None,
    raw: str | None,
    expected: str,
) -> None:
    """Every rejection abstains rather than falling back to some other list.

    A fallback would produce a digest anyway, which is the one outcome worse
    than no digest: it looks measured.
    """
    populate(tmp_path)
    if raw is not None:
        write(tmp_path, MANIFEST_PATH, raw.encode())
    else:
        assert overrides is not None
        write_manifest(tmp_path, **overrides)

    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["promptStackVersion"] is None
    assert expected in payload["error"]
    # The two sets are independent, so a broken declaration must not cost the
    # record its repo-instructions digest as well.
    assert payload["repoInstructionsSha256"] is not None


def test_an_absent_declaration_is_told_apart_from_a_corrupt_one(
    tmp_path: Path,
) -> None:
    """A consumer whose sync did not deliver the manifest is a different bug."""
    populate(tmp_path)
    (tmp_path / MANIFEST_PATH).unlink()
    absent = run("--repo-root", str(tmp_path))
    write(tmp_path, MANIFEST_PATH, b"{ not json")
    corrupt = run("--repo-root", str(tmp_path))
    assert absent["error"] == "no prompt-stack.json under .claude"
    assert corrupt["error"] != absent["error"]


def test_a_symlinked_manifest_abstains(tmp_path: Path) -> None:
    populate(tmp_path)
    manifest = tmp_path / MANIFEST_PATH
    external = tmp_path / "external-manifest.json"
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(external)

    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["promptStackVersion"] is None
    assert payload["error"] == "prompt-stack.json could not be read"


def test_a_symlinked_declared_prompt_abstains(tmp_path: Path) -> None:
    populate(tmp_path)
    prompt = tmp_path / PROMPT_STACK_FILES[0]
    external = tmp_path / "external-prompt.md"
    external.write_text("# machine-local\n", encoding="utf-8")
    prompt.unlink()
    prompt.symlink_to(external)

    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["promptStackVersion"] == STACK_VERSION
    assert payload["error"] == "the prompt stack could not be read"


def test_an_oversized_declared_prompt_abstains(tmp_path: Path) -> None:
    populate(tmp_path)
    write(tmp_path, PROMPT_STACK_FILES[0], b"x" * (1024 * 1024 + 1))

    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["promptStackVersion"] == STACK_VERSION
    assert payload["error"] == "the prompt stack could not be read"


@pytest.mark.parametrize("root", [".claude", ".codex"])
def test_the_shipped_declaration_is_well_formed(root: str) -> None:
    """Every shipped manifest must satisfy the reader shipped beside it.

    Parametrized over both roots because the two manifests are not two copies of
    one file the way the two scripts are. They are separately generated
    artifacts with their own `engine`, `root`, and `files`, so the byte-equality
    assertion below covers the script and says nothing about either of them. Each
    engine's own copy is run here, since `HARNESS_ROOT` comes from the script's
    location and is the whole point of the distinction.
    """
    script = REPO_ROOT / root / "skills/critique/scripts" / "prompt-stack-hash.js"
    payload = run_script(script, "--repo-root", str(REPO_ROOT))
    assert payload["error"] is None
    assert payload["harnessRoot"] == root
    assert payload["promptStackSha256"] is not None
    assert payload["promptStackVersion"] == (
        (REPO_ROOT / "PROMPT_STACK_VERSION").read_text(encoding="utf-8").strip()
    )
    declared = json.loads(
        (REPO_ROOT / root / "prompt-stack.json").read_text(encoding="utf-8")
    )
    assert declared["root"] == root
    assert payload["promptStack"]["declared"] == len(declared["files"])
    assert payload["promptStack"]["present"] == len(declared["files"])


def test_a_symlinked_directory_component_abstains(tmp_path: Path) -> None:
    """The per-component walk, which `O_NOFOLLOW` cannot stand in for.

    `O_NOFOLLOW` constrains only the final component, so the two symlink cases
    above pass with the component loop deleted. This one does not: it links a
    *directory* component at an otherwise valid target, which is the case the
    loop exists for and the shape the round-1 finding reported.
    """
    populate(tmp_path)
    # `.claude/references` holds a declared prompt. Move the real directory
    # outside the harness root and leave a link where it was: every declared
    # file still resolves and still has the right bytes, so only the component
    # check can tell the difference.
    inside = tmp_path / ".claude/references"
    outside = tmp_path / "external-references"
    inside.rename(outside)
    inside.symlink_to(outside)

    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["promptStackVersion"] == STACK_VERSION
    assert payload["error"] == "the prompt stack could not be read"


def test_a_declared_stack_that_arrived_empty_says_so(tmp_path: Path) -> None:
    """A null digest must never be a null reason.

    A manifest that parsed is a positive assertion that these files are the
    stack, so `declared > 0, present == 0` contradicts it. Reporting no reason
    would leave a record that abstains without saying why — the same failure as
    a digest that looks measured, which is what the abstention exists to avoid.
    A partial sync reaches this: the manifest and the prompts it names are
    separate sync targets.
    """
    populate(tmp_path)
    for relative in PROMPT_STACK_FILES:
        (tmp_path / relative).unlink()

    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["promptStackVersion"] == STACK_VERSION
    assert payload["promptStack"] == {
        "declared": len(PROMPT_STACK_FILES),
        "present": 0,
    }
    assert payload["error"] == (
        "no declared prompt-stack file is present under .claude"
    )


def test_a_wholly_absent_instruction_set_is_not_an_error(tmp_path: Path) -> None:
    """The counterpart: a repository with no instruction file has no fault.

    Pinned so the reason added for the stack half is not copied to this one. A
    repository carrying neither `AGENTS.md` nor `CLAUDE.md` is a normal
    repository, not a broken sync.
    """
    populate(tmp_path)
    for relative in REPO_INSTRUCTION_FILES:
        path = tmp_path / relative
        if path.exists():
            path.unlink()

    payload = run("--repo-root", str(tmp_path))
    assert payload["repoInstructionsSha256"] is None
    assert payload["promptStackSha256"] is not None
    assert payload["error"] is None


def test_both_abstention_reasons_are_reported(tmp_path: Path) -> None:
    """A manifest problem must not hide a repo-instructions read failure.

    The two digests are documented as independent, and they are. The reason
    channel is one scalar, so a short-circuit that reports only the first cause
    leaves a null `repoInstructionsSha256` with a stated reason that belongs to
    the other half — a null that looks diagnosed. The counters cannot close the
    gap either: `present: 0` is what a wholly absent set and a set that bailed
    on a read error both report.
    """
    populate(tmp_path)
    (tmp_path / MANIFEST_PATH).unlink()
    # A directory where a declared file is expected reads as EISDIR, which is a
    # failure rather than an absence, and needs no non-root user to arrange.
    (tmp_path / "CLAUDE.md").unlink()
    (tmp_path / "CLAUDE.md").mkdir()

    payload = run("--repo-root", str(tmp_path))
    assert payload["promptStackSha256"] is None
    assert payload["repoInstructionsSha256"] is None
    assert "no prompt-stack.json under .claude" in payload["error"]
    assert "the repo instructions could not be read" in payload["error"]


def test_a_single_abstention_reason_reads_unchanged(tmp_path: Path) -> None:
    """Reporting both causes must not reword the one-cause case."""
    populate(tmp_path)
    (tmp_path / MANIFEST_PATH).unlink()
    payload = run("--repo-root", str(tmp_path))
    assert payload["error"] == "no prompt-stack.json under .claude"


def test_the_two_engine_copies_of_this_helper_are_identical() -> None:
    """One implementation, two install locations — enforced, not conventional.

    Every case in this file runs the `.claude` copy. That proves nothing about
    the `.codex` copy unless the two are the same bytes: `HARNESS_ROOT` is
    derived from the script's own location, so the sibling resolves a different
    manifest, a different root cross-check, and a different path prefix. Two
    copies that drift mint two identities for one prompt generation, which is
    the failure this whole mechanism exists to prevent.
    """
    sibling = REPO_ROOT / ".codex/skills/critique/scripts" / "prompt-stack-hash.js"
    assert sibling.read_bytes() == SCRIPT.read_bytes()
