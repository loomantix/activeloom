"""Composition must preserve engine text and the renderer's exact ownership."""

from pathlib import Path
from types import ModuleType

import pytest

from tests.test_render_prompts import Harness


def write(root: Path, path: str, content: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


@pytest.fixture
def composed(
    render_prompts: ModuleType,
    sync_engine: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    harness = Harness(render_prompts, sync_engine, tmp_path, monkeypatch)
    outputs = frozenset(
        f".{engine}/skills/critique/SKILL.md" for engine in ("claude", "codex")
    )
    monkeypatch.setattr(render_prompts, "COMPOSED_DOCUMENTS", outputs)
    harness.write_profiles(
        {
            engine: f"root: .{engine}\nvalues:\n  ENGINE: {engine}\n"
            for engine in ("claude", "codex")
        }
    )
    for engine in ("claude", "codex"):
        write(
            tmp_path,
            f"prompts/review/{engine}/skills/critique/SKILL.md.hbs",
            f"# {engine}\n\n{{{{> review/gate}}}}\n\n{engine} instructions.\n",
        )
    write(tmp_path, "prompts/partials/review/gate.hbs", "Policy for <<ENGINE>>.\n")
    return harness


def test_policy_edit_reaches_engines_preserving_their_body(composed: Harness) -> None:
    before, _ = composed.render()
    write(
        composed.root,
        "prompts/partials/review/gate.hbs",
        "New policy for <<ENGINE>>.\n",
    )
    after, written = composed.render()
    assert len(written) == 2
    for engine in ("claude", "codex"):
        path = Path(f".{engine}/skills/critique/SKILL.md")
        assert (
            (before / path).read_text()
            == f"# {engine}\n\nPolicy for {engine}.\n\n{engine} instructions.\n"
        )
        assert (after / path).read_text() == (before / path).read_text().replace(
            "Policy", "New policy"
        )


def test_owns_only_the_declared_markdown_file(
    composed: Harness,
    render_prompts: ModuleType,
) -> None:
    sibling = write(
        composed.root, ".codex/skills/critique/scripts/hand-maintained.py", "keep\n"
    )
    out, written = composed.render()
    render_prompts._publish_outputs(
        out, written, [], render_prompts.load_profiles(), []
    )
    assert sibling.read_text() == "keep\n"
    assert composed.report_drift(out, written) == 0
    with pytest.raises(ValueError, match="ownership domain"):
        render_prompts._validate_generated_path(sibling.relative_to(composed.root))


def test_modified_output_is_reported_and_restored(composed: Harness) -> None:
    out, written = composed.render()
    composed.publish(out, written)
    output = composed.root / ".codex/skills/critique/SKILL.md"
    output.write_text("Hand edit\n")
    assert composed.report_drift(out, written) == 1
    composed.publish(out, written)
    assert composed.report_drift(out, written) == 0


@pytest.mark.parametrize(
    "path",
    [
        "prompts/partials/review/gate.hbs",
        "prompts/review/codex/skills/critique/SKILL.md.hbs",
    ],
)
def test_missing_source_fails(composed: Harness, path: str) -> None:
    (composed.root / path).unlink()
    with pytest.raises(
        (RuntimeError, ValueError), match="Missing partial|does not exist"
    ):
        composed.render()


@pytest.mark.parametrize(
    "path",
    [
        "prompts",
        "prompts/partials",
        "prompts/partials/review",
        "prompts/review",
        "prompts/review/codex",
        "prompts/review/codex/skills/critique/SKILL.md.hbs",
        "prompts/partials/review/gate.hbs",
    ],
)
def test_symlinked_source_components_fail(composed: Harness, path: str) -> None:
    target = composed.root / path
    moved = composed.root / "external"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(ValueError, match="must not (contain|be) symlinks"):
        composed.render()


def test_authoring_error_names_the_template_without_install_hint(
    composed: Harness,
) -> None:
    write(
        composed.root,
        "prompts/review/codex/skills/critique/SKILL.md.hbs",
        "{{#if engine}}hidden{{/if}}\n",
    )
    with pytest.raises(RuntimeError) as excinfo:
        composed.render()
    assert ".codex/skills/critique/SKILL.md: only static partial" in str(excinfo.value)
    assert "npm ci" not in str(excinfo.value)


def test_missing_dependency_names_the_install_command(
    composed: Harness,
    render_prompts: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composer = write(composed.root, "compose.cjs", "require('./absent-module');\n")
    monkeypatch.setattr(render_prompts, "COMPOSER_PATH", composer)
    with pytest.raises(RuntimeError, match="npm ci --prefix prompts --ignore-scripts"):
        composed.render()


def test_unregistered_template_fails(composed: Harness) -> None:
    write(composed.root, "prompts/review/codex/EXTRA.md.hbs", "unowned\n")
    with pytest.raises(ValueError, match="no declared destination"):
        composed.render()


def test_destination_symlink_does_not_write_outside_repo(
    composed: Harness,
    render_prompts: ModuleType,
) -> None:
    out, written = composed.render()
    outside = write(composed.root, "outside", "untouched\n")
    target = composed.root / ".codex/skills/critique/SKILL.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="must not contain symlinks"):
        render_prompts._publish_outputs(
            out, written, [], render_prompts.load_profiles(), []
        )
    assert outside.read_text() == "untouched\n"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        ".other/REVIEW_WORKFLOW.md",
        ".codex/../outside.md",
        ".codex/skills/critique/scripts/tool.py",
        ".codex//REVIEW_WORKFLOW.md",
    ],
)
def test_invalid_output_declaration_fails(
    render_prompts: ModuleType, path: str
) -> None:
    with pytest.raises(ValueError, match="invalid composed document destination"):
        render_prompts.composed_source_path(path)


def test_ownership_collision_fails(composed: Harness) -> None:
    composed.write_source("critique/SKILL.md", "shared skill\n")
    with pytest.raises(ValueError, match="multiple prompt sources"):
        composed.render()


def test_missing_vocabulary_fails(composed: Harness) -> None:
    write(composed.root, "prompts/partials/review/gate.hbs", "<<UNDEFINED>>\n")
    with pytest.raises(ValueError, match="no value"):
        composed.render()


def test_retiring_a_document_preserves_its_siblings(
    composed: Harness,
    render_prompts: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, written = composed.render()
    composed.publish(out, written)
    retired = ".codex/skills/critique/SKILL.md"
    sibling = write(composed.root, ".codex/skills/critique/scripts/helper.py", "keep\n")
    (composed.root / render_prompts.composed_source_path(retired)).unlink()
    monkeypatch.setattr(
        render_prompts,
        "COMPOSED_DOCUMENTS",
        render_prompts.COMPOSED_DOCUMENTS - {retired},
    )
    monkeypatch.setattr(
        render_prompts, "RETIRED_COMPOSED_DOCUMENTS", frozenset({retired})
    )
    out, current = composed.render()
    profiles = render_prompts.load_profiles()
    previous = render_prompts._load_manifest(profiles)
    render_prompts._publish_outputs(out, current, previous, profiles, [])
    assert not (composed.root / retired).exists()
    assert sibling.read_text() == "keep\n"
