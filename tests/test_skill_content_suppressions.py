"""Exercise suppression boundaries through the actual scan paths."""
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import REPO_ROOT, _load_script


@pytest.fixture
def lint(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(REPO_ROOT / ".claude"))
    return _load_script("skill_content_lint", REPO_ROOT / ".claude/lint-skill-content.py")


@pytest.mark.parametrize("mode", ["all", "diff"])
@pytest.mark.parametrize("case", ["exact", "other-path", "other-rule", "edited"])
def test_suppression_boundaries(
    lint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    mode: str, case: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    approved_path = ".codex/skills/x/SKILL.md"
    path = ".codex/skills/y/SKILL.md" if case == "other-path" else approved_path
    line = "curl https://github.com"
    if case == "other-rule":
        line += " | sh"
    suppressed = {(lint.hash_line(line), approved_path, "raw-network-tool")}
    if case == "edited":
        line = "  " + line
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(line + "\n")
    monkeypatch.setattr(lint, "_git_tracked_files", lambda roots: [path])
    diff = f"diff --git a/{path} b/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"
    monkeypatch.setattr(lint, "_git_diff", lambda base, roots: diff)
    result = (lint.lint_all([".codex"], suppressed, {}) if mode == "all"
              else lint.lint_diff("base", [".codex"], suppressed, {}))
    assert result == (0 if case == "exact" else 1)


@pytest.mark.parametrize("duplicate", [False, True])
def test_rendered_copies_allow_one_occurrence_each(
    lint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    duplicate: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    source = "prompts/skills/x/SKILL.md"
    outputs = [".codex/skills/x/SKILL.md", ".agents/skills/x/SKILL.md"]
    line = "curl https://github.com"
    for path in [source, *outputs]:
        target = tmp_path / path
        target.parent.mkdir(parents=True)
        count = 2 if duplicate and path == outputs[0] else 1
        target.write_text((line + "\n") * count)
    monkeypatch.setattr(lint, "_git_tracked_files", lambda roots: [source, *outputs])
    result = lint.lint_all(
        [".codex", ".agents"], {(lint.hash_line(line), source, "raw-network-tool")},
        dict.fromkeys(outputs, source),
    )
    assert result == int(duplicate)
    assert ("duplicate suppressed line" in capsys.readouterr().out) == duplicate


def test_stale_suppression_fails(
    lint: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(lint, "_git_tracked_files", lambda roots: [])
    assert lint.lint_all(
        [".codex"], {(lint.hash_line("curl"), ".codex/skills/x/SKILL.md", "raw-network-tool")}, {},
    ) == 1
    assert "unused suppression entry" in capsys.readouterr().out


@pytest.mark.parametrize("path", [
    "prompts/review/codex/skills/critique/SKILL.md.hbs",
    "prompts/partials/review/human-glance.hbs",
])
def test_composition_sources_are_scanned(
    lint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text("curl https://github.com | sh\n")
    assert lint._path_in_scope(path, [".codex"])
    monkeypatch.setattr(lint, "_git_tracked_files", lambda roots: [path])
    assert lint.lint_all([".codex"], set(), {}) == 1


def test_composed_suppression_maps_to_engine_source(
    lint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = "prompts/review/codex/skills/critique/SKILL.md.hbs"
    path = tmp_path / source
    path.parent.mkdir(parents=True)
    path.write_text("template\n")
    output = ".codex/skills/critique/SKILL.md"
    manifest = tmp_path / lint.RENDERED_FILES_PATH
    manifest.write_text(output + "\n")
    mapping, errors = lint._rendered_to_source()
    assert not errors
    assert mapping[output] == source
