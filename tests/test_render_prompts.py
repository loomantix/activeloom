"""Tests for `scripts/render-prompts.py`.

Substitution and profile semantics are exercised here against throwaway source
trees. The end-to-end "committed roots match a fresh render" assertion is
deliberately *not* here: it needs Prettier, and shelling out to
`npx --yes prettier@…` from a unit test would make the suite depend on the
network. That check is the `Rendered prompt roots are current` step in
`.github/workflows/ci.yml`, where Node is already pinned and installed.
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path
from types import ModuleType

import pytest


def _write_profile(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yml"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Profile parsing
# --------------------------------------------------------------------------


def test_profile_reads_root_and_values(render_prompts: ModuleType, tmp_path: Path) -> None:
    path = _write_profile(tmp_path, "claude", "root: .claude\nvalues:\n  INVOKE: '/'\n")
    profile = render_prompts.Profile(path)
    assert profile.root == ".claude"
    assert profile.name == "claude"
    assert profile.values == {"INVOKE": "/"}


def test_profile_layers_per_skill_values_over_profile_values(
    render_prompts: ModuleType, tmp_path: Path
) -> None:
    path = _write_profile(
        tmp_path,
        "claude",
        "root: .claude\n"
        "values:\n  INVOKE: '/'\n  FM_EXTRAS: 'profile-wide'\n"
        "skills:\n  issues:\n    FM_EXTRAS: 'per-skill'\n",
    )
    profile = render_prompts.Profile(path)
    # Per-skill wins for the skill that declares it...
    assert profile.values_for("issues")["FM_EXTRAS"] == "per-skill"
    assert profile.values_for("issues")["INVOKE"] == "/"
    # ...and the profile-wide value still applies to a skill that does not.
    assert profile.values_for("grill")["FM_EXTRAS"] == "profile-wide"


def test_profile_with_no_skills_block_still_resolves_values(
    render_prompts: ModuleType, tmp_path: Path
) -> None:
    path = _write_profile(tmp_path, "claude", "root: .claude\nvalues:\n  INVOKE: '/'\n")
    assert render_prompts.Profile(path).values_for("anything") == {"INVOKE": "/"}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[]\n", "must be a mapping"),
        ("values:\n  A: 1\n", "`root` must be a non-empty string"),
        ("root: ''\nvalues: {}\n", "`root` must be a non-empty string"),
        ("root: .claude\nvalues: []\n", "`values` must be a mapping"),
        ("root: .claude\nvalues: {}\nskills: []\n", "`skills` must be a mapping"),
        ("root: .claude\nvalues: {}\nskills:\n  issues: 3\n", "`skills.issues` must be a mapping"),
    ],
)
def test_profile_rejects_malformed_documents(
    render_prompts: ModuleType, tmp_path: Path, body: str, expected: str
) -> None:
    path = _write_profile(tmp_path, "claude", body)
    with pytest.raises(ValueError, match=expected):
        render_prompts.Profile(path)


@pytest.mark.parametrize("root", ["/etc", "~/elsewhere", "../outside", ".claude/../.."])
def test_profile_rejects_a_root_that_escapes_the_repo(
    render_prompts: ModuleType, tmp_path: Path, root: str
) -> None:
    """A profile root is joined to the repo root, so it must stay inside it.

    Profiles are checked-in config rather than user input, but the renderer
    writes whole trees — a root of `/` from a typo should fail loudly instead of
    being rendered into.
    """
    path = _write_profile(tmp_path, "claude", f"root: {root!r}\nvalues: {{}}\n")
    with pytest.raises(ValueError, match="relative path inside the repo"):
        render_prompts.Profile(path)


@pytest.mark.parametrize("duplicate_root", [".claude", "./.claude/"])
def test_load_profiles_rejects_two_profiles_sharing_a_root(
    render_prompts: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_root: str,
) -> None:
    """Two profiles with one root means the second silently overwrites the first.

    The second spelling is the same root written differently: roots are
    normalized before the duplicate check, so it must be rejected too.
    """
    _write_profile(tmp_path, "claude", "root: .claude\nvalues: {}\n")
    _write_profile(tmp_path, "clone", f"root: {duplicate_root}\nvalues: {{}}\n")
    monkeypatch.setattr(render_prompts, "PROFILES_DIR", tmp_path)
    with pytest.raises(ValueError, match="share a root"):
        render_prompts.load_profiles()


def test_load_profiles_rejects_an_empty_profiles_directory(
    render_prompts: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render_prompts, "PROFILES_DIR", tmp_path)
    with pytest.raises(ValueError, match="no profiles found"):
        render_prompts.load_profiles()


# --------------------------------------------------------------------------
# Roster discovery
# --------------------------------------------------------------------------


def test_roster_is_derived_from_the_source_directory(
    render_prompts: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a rendered skill must be a source directory and nothing else.

    If the roster were a manifest, adding a skill would mean editing a third
    place, and forgetting it would silently drop the skill from every harness.
    """
    (tmp_path / "beta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "not-a-skill.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(render_prompts, "SKILLS_SRC", tmp_path)
    assert render_prompts.rendered_roster() == ["alpha", "beta"]


def test_roster_is_empty_when_the_source_tree_is_absent(
    render_prompts: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render_prompts, "SKILLS_SRC", tmp_path / "nope")
    assert render_prompts.rendered_roster() == []


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


ONE_SKILL = {"demo/SKILL.md": "---\nname: demo\n<<FM_EXTRAS>>\n---\n\nSee `<<INVOKE>>other`.\n"}
TWO_PROFILES = {
    "claude": "root: .claude\nvalues:\n  INVOKE: '/'\nskills:\n  demo:\n    FM_EXTRAS: 'argument-hint: x'\n",
    "codex": "root: .codex\nvalues:\n  INVOKE: ''\nskills:\n  demo:\n    FM_EXTRAS: ''\n",
}


class Harness:
    """A throwaway source tree + profiles, renderable as many times as needed.

    Rendering more than once matters for the mode and binary cases, which set
    the source up, render, change it, and render again.
    """

    def __init__(
        self,
        render_prompts: ModuleType,
        sync_engine: ModuleType,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._rp = render_prompts
        self._se = sync_engine
        self.root = root
        self.src = root / "src"
        self.profiles_dir = root / "profiles"
        self._renders = 0
        monkeypatch.setattr(render_prompts, "SKILLS_SRC", self.src)
        monkeypatch.setattr(render_prompts, "PROFILES_DIR", self.profiles_dir)
        monkeypatch.setattr(render_prompts, "REPO_ROOT", root)
        monkeypatch.setattr(
            render_prompts, "MANIFEST_PATH", root / "prompts/rendered-files.txt"
        )

    def write_source(self, relative: str, text: str | bytes) -> Path:
        path = self.src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def write_profiles(self, profiles: dict[str, str]) -> None:
        for name, body in profiles.items():
            _write_profile(self.profiles_dir, name, body)

    def render(self) -> tuple[Path, list[Path]]:
        self._renders += 1
        out = self.root / f"out-{self._renders}"
        written = self._rp.render_tree(self._se, self._rp.load_profiles(), out)
        return out, written

    def report_drift(self, staging: Path, written: list[Path]) -> int:
        """`--check`'s comparison, against this harness's fake repo root."""
        # `int(...)`: `_rp` is a ModuleType, so the call is `Any` under --strict.
        return int(self._rp._report_drift(staging, written, self._rp._load_manifest()))

    def publish(self, staging: Path, written: list[Path], mode: int | None = None) -> None:
        """Copy a render into the fake repo root, as a commit would."""
        for relative in written:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((staging / relative).read_bytes())
            target.chmod(
                mode if mode is not None else (staging / relative).stat().st_mode & 0o777
            )
        self._rp.MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._rp.MANIFEST_PATH.write_text(
            self._rp._manifest_text(written), encoding="utf-8"
        )


@pytest.fixture
def harness(
    render_prompts: ModuleType,
    sync_engine: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    return Harness(render_prompts, sync_engine, tmp_path, monkeypatch)


@pytest.fixture
def one_skill(harness: Harness) -> Harness:
    """The default fixture: one skill, two profiles, nothing rendered yet."""
    for relative, text in ONE_SKILL.items():
        harness.write_source(relative, text)
    harness.write_profiles(TWO_PROFILES)
    return harness


def test_render_substitutes_per_profile_vocabulary(one_skill: Harness) -> None:
    out, written = one_skill.render()
    assert sorted(str(p) for p in written) == [
        ".claude/skills/demo/SKILL.md",
        ".codex/skills/demo/SKILL.md",
    ]
    assert "See `/other`." in (out / ".claude/skills/demo/SKILL.md").read_text()
    assert "See `other`." in (out / ".codex/skills/demo/SKILL.md").read_text()


def test_render_collapses_the_frontmatter_slot_when_a_harness_has_no_extras(
    one_skill: Harness,
) -> None:
    """An empty `FM_EXTRAS` must take its line, not leave a blank one.

    A blank line inside the YAML frontmatter block is the failure this guards:
    it is the difference between valid frontmatter and a skill the harness
    refuses to load.
    """
    out, _ = one_skill.render()
    assert (out / ".claude/skills/demo/SKILL.md").read_text().startswith(
        "---\nname: demo\nargument-hint: x\n---\n"
    )
    assert (out / ".codex/skills/demo/SKILL.md").read_text().startswith(
        "---\nname: demo\n---\n"
    )


def test_render_keeps_a_multiline_value_inside_the_frontmatter_block(
    harness: Harness,
) -> None:
    """`FM_EXTRAS` carries two keys for `grill`; both must land in the block."""
    harness.write_source("demo/SKILL.md", "---\nname: demo\n<<FM_EXTRAS>>\n---\n")
    harness.write_profiles(
        {
            "claude": "root: .claude\nvalues: {}\n"
            "skills:\n  demo:\n    FM_EXTRAS: |-\n      a: 1\n      b: 2\n",
        }
    )
    out, _ = harness.render()
    assert (out / ".claude/skills/demo/SKILL.md").read_text() == (
        "---\nname: demo\na: 1\nb: 2\n---\n"
    )


def test_render_fails_on_a_placeholder_no_profile_defines(harness: Harness) -> None:
    """A typo'd or new placeholder must fail the render, not ship verbatim.

    Rendering `<<TYPO>>` through into a harness root would put a literal
    `<<TYPO>>` in a live prompt and, via sync, in every consumer repo.
    """
    harness.write_source("demo/SKILL.md", "<<INVOKE>> and <<TYPO>>\n")
    harness.write_profiles(TWO_PROFILES)
    with pytest.raises(ValueError, match="placeholders with no value"):
        harness.render()


def test_render_preserves_the_executable_bit(one_skill: Harness) -> None:
    """The issues scripts are executed from the skill and synced `mode: 0755`."""
    script = one_skill.write_source("demo/scripts/run.py", "#!/usr/bin/env python3\n")
    script.chmod(0o755)
    out, _ = one_skill.render()
    mode = (out / ".claude/skills/demo/scripts/run.py").stat().st_mode
    assert mode & stat.S_IXUSR


def test_render_leaves_a_non_executable_source_non_executable(
    one_skill: Harness,
) -> None:
    one_skill.write_source("demo/scripts/lib.py", "X = 1\n").chmod(0o644)
    out, _ = one_skill.render()
    mode = (out / ".claude/skills/demo/scripts/lib.py").stat().st_mode
    assert not mode & stat.S_IXUSR


def test_render_copies_a_non_utf8_file_through_untouched(one_skill: Harness) -> None:
    """A binary asset is not substitutable; it must survive byte-for-byte."""
    payload = b"\x89PNG\r\n\x1a\n\xff\xfe binary"
    one_skill.write_source("demo/logo.png", payload)
    out, _ = one_skill.render()
    assert (out / ".claude/skills/demo/logo.png").read_bytes() == payload


def test_render_walks_nested_source_directories(one_skill: Harness) -> None:
    one_skill.write_source("demo/scripts/nested/deep.md", "`<<INVOKE>>x`\n")
    out, written = one_skill.render()
    assert Path(".claude/skills/demo/scripts/nested/deep.md") in written
    assert (out / ".claude/skills/demo/scripts/nested/deep.md").read_text() == "`/x`\n"


def test_render_rejects_a_symlinked_source_file(
    one_skill: Harness, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private host content", encoding="utf-8")
    link = one_skill.src / "demo" / "linked.txt"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="must not be symlinks"):
        one_skill.render()


# --------------------------------------------------------------------------
# Drift reporting
# --------------------------------------------------------------------------


def test_check_reports_a_missing_committed_file(
    one_skill: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    out, written = one_skill.render()
    # `_report_drift` compares the staging tree against REPO_ROOT, which the
    # harness pointed at the temp dir — nothing has been copied there yet.
    assert one_skill.report_drift(out, written) == 1
    assert "missing:" in capsys.readouterr().err


def test_check_passes_when_the_committed_tree_matches(
    one_skill: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    out, written = one_skill.render()
    one_skill.publish(out, written)
    assert one_skill.report_drift(out, written) == 0
    assert "matches all" in capsys.readouterr().out


def test_check_reports_content_drift(
    one_skill: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-edit to a generated harness root must fail the gate."""
    out, written = one_skill.render()
    one_skill.publish(out, written)
    hand_edited = one_skill.root / written[0]
    hand_edited.write_text(
        hand_edited.read_text(encoding="utf-8") + "\nedited by hand\n", encoding="utf-8"
    )
    assert one_skill.report_drift(out, written) == 1
    captured = capsys.readouterr()
    assert "differs:" in captured.err
    assert "render-prompts.py" in captured.err


def test_check_reports_an_output_whose_source_was_deleted(
    one_skill: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    source = one_skill.write_source("demo/scripts/retired.py", "x = 1\n")
    out, written = one_skill.render()
    one_skill.publish(out, written)
    source.unlink()

    refreshed, refreshed_written = one_skill.render()
    assert one_skill.report_drift(refreshed, refreshed_written) == 1
    assert "stale:" in capsys.readouterr().err


def test_check_reports_a_mode_only_change(one_skill: Harness) -> None:
    """A committed file that lost its executable bit is drift too.

    Content-only comparison would pass it, and the skill would then ship a
    script the harness cannot execute.
    """
    script = one_skill.write_source("demo/scripts/run.py", "#!/usr/bin/env python3\n")
    script.chmod(0o755)
    out, written = one_skill.render()
    one_skill.publish(out, written, mode=0o644)
    assert one_skill.report_drift(out, written) == 1


# --------------------------------------------------------------------------
# Placeholder-residue guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<<KEY>>",
        "<<FM_EXTRAS>>",
        # The real mangling this guard was added for: Prettier paired the
        # underscores inside `<<REVIEW_CHAIN_POINTER>>` with a neighbouring
        # `_emphasis_` span and rewrote the key, so it matched the engine's
        # `<<KEY>>` pattern no longer and substituted nothing.
        "<<REVIEW*CHAIN_POINTER>>",
        r"<<REVIEW\_CHAIN_POINTER>>",
    ],
)
def test_residue_guard_matches_placeholder_shaped_tokens(
    render_prompts: ModuleType, text: str
) -> None:
    assert render_prompts._delimiter_residue(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        # `issues/SKILL.md` is a rendered skill and contains a heredoc. If the
        # guard matched these it would fail every render of the roster.
        "cat > /tmp/issue-body.md << 'BODY'\nsomething >> elsewhere\n",
        "python3 - <<'PY'\nprint(1 >> 2)\n",
        'read -r a <<<"$PR_JSON" >> log\n',
        "a << b >> c\n",
    ],
)
def test_residue_guard_ignores_shell_heredocs_and_shifts(
    render_prompts: ModuleType, text: str
) -> None:
    assert render_prompts._delimiter_residue(text) == []


def test_render_fails_when_a_placeholder_survives_mangled(harness: Harness) -> None:
    """A mangled key substitutes nothing and would ship verbatim to consumers.

    `<<BAD*KEY>>` does not match the engine's `<<KEY>>` pattern, so neither the
    engine nor the undefined-placeholder check sees it. Only the delimiter sweep
    over rendered output does.
    """
    harness.write_source("demo/SKILL.md", "text <<BAD*KEY>> more\n")
    harness.write_profiles(TWO_PROFILES)
    with pytest.raises(ValueError, match="still contains placeholder delimiters"):
        harness.render()


# --------------------------------------------------------------------------
# Markdown formatting
# --------------------------------------------------------------------------


def test_format_markdown_is_a_noop_with_no_markdown_to_format(
    render_prompts: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Markdown written means no `npx` call — a scripts-only skill is cheap."""
    calls: list[object] = []
    monkeypatch.setattr(render_prompts.subprocess, "run", lambda *a, **k: calls.append(a))
    render_prompts.format_markdown(tmp_path, [Path("a/b/run.py")])
    assert calls == []


def test_format_markdown_passes_only_markdown_and_pins_the_config(
    render_prompts: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered tree can sit outside the repo, where config discovery finds
    nothing and Prettier would silently fall back to its defaults."""
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        return None

    monkeypatch.setattr(render_prompts.subprocess, "run", fake_run)
    render_prompts.format_markdown(tmp_path, [Path("a/x.md"), Path("a/run.py")])
    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert render_prompts.PRETTIER in cmd
    assert "--config" in cmd
    assert str(tmp_path / "a/x.md") in cmd
    assert str(tmp_path / "a/run.py") not in cmd


def test_format_markdown_explains_a_missing_npx(
    render_prompts: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("npx")

    monkeypatch.setattr(render_prompts.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="npx not found"):
        render_prompts.format_markdown(tmp_path, [Path("a/x.md")])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@pytest.fixture
def cli(one_skill: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """`main()` against the harness's fake repo, with Prettier stubbed out.

    Stubbing the formatter keeps the suite off the network — `npx --yes` would
    fetch Prettier. The real formatter is exercised by CI's
    `Rendered prompt roots are current` step.
    """
    monkeypatch.setattr(one_skill._rp, "format_markdown", lambda *a, **k: None)
    return one_skill


def test_main_writes_the_harness_roots(cli: Harness) -> None:
    assert cli._rp.main([]) == 0
    assert (cli.root / ".claude/skills/demo/SKILL.md").exists()
    assert (cli.root / ".codex/skills/demo/SKILL.md").exists()
    assert cli._rp.MANIFEST_PATH.exists()


def test_main_removes_outputs_for_a_deleted_source_file(cli: Harness) -> None:
    source = cli.write_source("demo/scripts/retired.py", "x = 1\n")
    assert cli._rp.main([]) == 0
    target = cli.root / ".claude/skills/demo/scripts/retired.py"
    assert target.exists()
    source.unlink()
    assert cli._rp.main([]) == 0
    assert not target.exists()


def test_main_removes_outputs_for_a_deleted_skill(cli: Harness) -> None:
    cli.write_source("keep/SKILL.md", "---\nname: keep\n---\n")
    assert cli._rp.main([]) == 0
    target = cli.root / ".claude/skills/demo/SKILL.md"
    assert target.exists()
    shutil.rmtree(cli.src / "demo")
    assert cli._rp.main([]) == 0
    assert not target.exists()


def test_main_removes_outputs_for_a_deleted_profile(cli: Harness) -> None:
    assert cli._rp.main([]) == 0
    target = cli.root / ".codex/skills/demo/SKILL.md"
    assert target.exists()
    (cli.profiles_dir / "codex.yml").unlink()
    assert cli._rp.main([]) == 0
    assert not target.exists()


def test_main_check_fails_before_anything_is_written(cli: Harness) -> None:
    assert cli._rp.main(["--check"]) == 1
    assert not (cli.root / ".claude/skills/demo/SKILL.md").exists()


def test_main_check_passes_after_a_write(cli: Harness) -> None:
    assert cli._rp.main([]) == 0
    assert cli._rp.main(["--check"]) == 0


def test_main_check_writes_nothing_even_when_it_passes(cli: Harness) -> None:
    """`--check` is what CI runs; it must never mutate the tree it inspects."""
    assert cli._rp.main([]) == 0
    target = cli.root / ".claude/skills/demo/SKILL.md"
    before = target.stat().st_mtime_ns
    assert cli._rp.main(["--check"]) == 0
    assert target.stat().st_mtime_ns == before


def test_main_fails_when_there_are_no_sources(
    cli: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli._rp, "SKILLS_SRC", cli.root / "absent")
    assert cli._rp.main([]) == 1
    assert "no skill sources found" in capsys.readouterr().err


def test_render_skips_bytecode_left_in_the_source_tree(one_skill: Harness) -> None:
    """`prompts/skills/**/scripts/*.py` are real Python, so anything that
    imports or compiles them drops `__pycache__` alongside. Those artifacts are
    gitignored — invisible in review — and `rglob` would otherwise copy stale
    bytecode into all three harness roots. CI caught exactly this.
    """
    one_skill.write_source("demo/scripts/run.py", "X = 1\n")
    one_skill.write_source("demo/scripts/__pycache__/run.cpython-312.pyc", b"\x00stale")
    one_skill.write_source("demo/scripts/loose.pyc", b"\x00also stale")
    out, written = one_skill.render()
    assert Path(".claude/skills/demo/scripts/run.py") in written
    assert not any("__pycache__" in str(p) for p in written)
    assert not any(p.suffix == ".pyc" for p in written)
    assert not (out / ".claude/skills/demo/scripts/loose.pyc").exists()
