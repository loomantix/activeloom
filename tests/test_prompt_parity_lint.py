"""Unit tests for `scripts/lint-prompt-parity.py`.

The lint is the only thing standing between "these two copies differ because
somebody decided they should" and "these two copies differ because a fix landed
in one root and not the others". Its two jobs are therefore tested separately:
that normalization erases dialect and nothing else, and that the allowlist can
only be satisfied by a citation someone can go and read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


@dataclass
class StubProfile:
    """A `render-prompts.Profile` reduced to what `build_rules` reads."""

    root: str
    values: dict[str, object] = field(default_factory=dict)


def _profiles() -> list[StubProfile]:
    """Three harnesses whose vocabulary mirrors the shipped profiles' shape."""
    return [
        StubProfile(
            ".claude",
            {
                "SKILLS_ROOT": ".claude/skills",
                "INVOKE": "/",
                "ENGINE_ID": "claude",
                "ENGINE_CLI": "claude",
                "TODO_TOOL": "TodoWrite",
                "AGENT_DOC": "AGENTS.md or CLAUDE.md",
                "Q_BULLET": "Q: ",
            },
        ),
        StubProfile(
            ".codex",
            {
                "SKILLS_ROOT": ".codex/skills",
                "INVOKE": "",
                "ENGINE_ID": "codex",
                "ENGINE_CLI": "codex",
                "TODO_TOOL": "update_plan",
                "AGENT_DOC": "AGENTS.md",
                "Q_BULLET": "",
            },
        ),
        StubProfile(
            ".agents",
            {
                "SKILLS_ROOT": ".agents/skills",
                "INVOKE": "",
                "ENGINE_ID": "gemini",
                "ENGINE_CLI": "agy",
                "TODO_TOOL": "update_plan",
                "AGENT_DOC": "AGENTS.md",
                "Q_BULLET": "",
            },
        ),
    ]


@pytest.fixture
def rules(lint_prompt_parity: ModuleType) -> dict[str, list[Any]]:
    built: dict[str, list[Any]] = lint_prompt_parity.build_rules(
        _profiles(), {"critique", "deepcritique", "agent-loop"}
    )
    return built


def _norm(lint: ModuleType, rules: dict[str, list[Any]], root: str, text: str) -> str:
    normalized: str = lint.normalize(text, rules[root])
    return normalized


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


def test_two_dialects_of_one_sentence_normalize_alike(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    claude = "Track it with TodoWrite, then run /critique."
    codex = "Track it with update_plan, then run critique."
    assert _norm(lint_prompt_parity, rules, ".claude", claude) == _norm(
        lint_prompt_parity, rules, ".codex", codex
    )


def test_engine_id_and_cli_collapse_to_one_token(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    # `ENGINE_ID` and `ENGINE_CLI` are the same word on the Claude profile and
    # different words on the Gemini one. Without the alias, `claude` would be
    # tagged with whichever key sorted first and `gemini`/`agy` with two other
    # tokens, so an exact match would read as a divergence.
    assert (
        _norm(lint_prompt_parity, rules, ".claude", "run claude")
        == _norm(lint_prompt_parity, rules, ".agents", "run agy")
        == _norm(lint_prompt_parity, rules, ".agents", "run gemini")
    )


def test_engine_name_normalizes_regardless_of_case(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    assert _norm(lint_prompt_parity, rules, ".claude", "Claude reviews it.") == _norm(
        lint_prompt_parity, rules, ".codex", "Codex reviews it."
    )


def test_longer_values_win_so_a_path_is_not_eaten_by_the_engine_name(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    # `.claude/skills` must be consumed as `SKILLS_ROOT` before the bare
    # `claude` rule gets a look at it.
    assert (
        _norm(lint_prompt_parity, rules, ".claude", "see .claude/skills/critique/")
        == "see <<SKILLS_ROOT>>/critique/"
    )


def test_the_prompt_root_normalizes_even_though_no_profile_declares_it(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    assert _norm(lint_prompt_parity, rules, ".claude", "see `.claude/NOTES.md`") == _norm(
        lint_prompt_parity, rules, ".codex", "see `.codex/NOTES.md`"
    )


def test_a_key_empty_on_one_profile_is_deleted_not_tagged(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    # `Q_BULLET` is decoration on one harness and absent on another. Tagging
    # the side that has it would report the *slot* as a difference.
    assert _norm(lint_prompt_parity, rules, ".claude", "Q: why?") == _norm(
        lint_prompt_parity, rules, ".codex", "why?"
    )


def test_a_value_rewrapped_across_a_line_break_still_matches(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    # Prettier re-wraps rendered Markdown, so a multi-word value can straddle a
    # newline in one root and not in another.
    wrapped = "read AGENTS.md or\nCLAUDE.md first"
    assert "<<AGENT_DOC>>" in _norm(lint_prompt_parity, rules, ".claude", wrapped)


def test_the_invoke_slash_is_stripped_only_from_a_skill_reference(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    text = "run /critique, not /usr/bin/critique or scripts/critique"
    assert (
        _norm(lint_prompt_parity, rules, ".claude", text)
        == "run critique, not /usr/bin/critique or scripts/critique"
    )


def test_an_unknown_slash_word_is_left_alone(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    assert (
        _norm(lint_prompt_parity, rules, ".claude", "run /not-a-skill")
        == "run /not-a-skill"
    )


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------


def test_a_declared_root_nobody_compares_is_an_error(
    lint_prompt_parity: ModuleType,
) -> None:
    # The pinned order is the authority, but a harness added to the profiles
    # and not to it would render a whole skill roster into a root this lint
    # never looks at — and report that every copy agreed.
    profiles = [*_profiles(), StubProfile(".cursor", {})]
    with pytest.raises(lint_prompt_parity.ParityError, match=r"absent from ROOT_ORDER"):
        lint_prompt_parity.check_root_coverage(profiles)


def test_a_pinned_root_with_no_profile_is_an_error(
    lint_prompt_parity: ModuleType,
) -> None:
    # Without a profile there is no vocabulary to normalize that root with, so
    # comparing it would diff two roots in different dialects and call the
    # dialect divergence.
    with pytest.raises(lint_prompt_parity.ParityError, match=r"no profile declares"):
        lint_prompt_parity.check_root_coverage(_profiles()[:2])


def test_the_shipped_profiles_cover_every_compared_root(
    lint_prompt_parity: ModuleType,
) -> None:
    renderer = lint_prompt_parity._load_render_prompts()
    lint_prompt_parity.check_root_coverage(renderer.load_profiles())


def test_an_unknown_profile_key_does_not_disturb_the_vocabulary(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]]
) -> None:
    # The profile schema is shared with the renderer and grows keys this lint
    # has no opinion about (a prompt-stack declaration, say). Reading a slice
    # of someone else's schema means tolerating the rest of it.
    extended = _profiles()
    extended[0].values["PROMPT_STACK"] = {"files": ["a.md"]}
    extended[0].values["FUTURE_LIST"] = ["x"]
    widened: dict[str, list[Any]] = lint_prompt_parity.build_rules(extended, {"critique"})
    keys = {rule.key for rule in widened[".claude"]}
    assert "PROMPT_STACK" not in keys and "FUTURE_LIST" not in keys
    assert keys == {rule.key for rule in rules[".claude"]}


# --------------------------------------------------------------------------
# measurement against a scratch tree
# --------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A repo-shaped scratch tree with an empty rendered-source directory."""
    (tmp_path / "prompts" / "skills").mkdir(parents=True)
    return tmp_path


def test_dialect_only_differences_measure_zero(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]], tree: Path
) -> None:
    _write(tree / ".claude/skills/demo/SKILL.md", "Track it with TodoWrite.\n")
    _write(tree / ".codex/skills/demo/SKILL.md", "Track it with update_plan.\n")
    pair = lint_prompt_parity.compare_pair("demo", ".claude", ".codex", rules, tree)
    assert pair.lines == 0


def test_a_real_difference_is_counted(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]], tree: Path
) -> None:
    _write(tree / ".claude/skills/demo/SKILL.md", "Guard the empty case.\n")
    _write(tree / ".codex/skills/demo/SKILL.md", "\n")
    pair = lint_prompt_parity.compare_pair("demo", ".claude", ".codex", rules, tree)
    assert pair.lines == 2  # one line removed, one added


def test_a_file_only_one_root_has_counts_every_line(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]], tree: Path
) -> None:
    # A whole document one harness has and the other does not is the largest
    # divergence there is; it must not hide behind a small number.
    _write(tree / ".claude/skills/demo/SKILL.md", "same\n")
    _write(tree / ".codex/skills/demo/SKILL.md", "same\n")
    _write(tree / ".codex/skills/demo/scripts/run.sh", "a\nb\nc\n")
    pair = lint_prompt_parity.compare_pair("demo", ".claude", ".codex", rules, tree)
    assert pair.lines == 3
    assert pair.only_right == ["scripts/run.sh"]


def test_build_artifacts_inside_a_skill_are_not_compared(
    lint_prompt_parity: ModuleType, rules: dict[str, list[Any]], tree: Path
) -> None:
    _write(tree / ".claude/skills/demo/SKILL.md", "same\n")
    _write(tree / ".codex/skills/demo/SKILL.md", "same\n")
    _write(tree / ".codex/skills/demo/scripts/__pycache__/x.cpython-312.pyc", "junk\n")
    pair = lint_prompt_parity.compare_pair("demo", ".claude", ".codex", rules, tree)
    assert pair.lines == 0


@pytest.mark.parametrize("content", ["---\n", "--strict\n", "++counter\n"])
@pytest.mark.parametrize("added", [False, True])
def test_header_shaped_content_is_still_divergence(
    lint_prompt_parity: ModuleType,
    rules: dict[str, list[Any]],
    tree: Path,
    content: str,
    added: bool,
) -> None:
    left, right = ("same\n", content + "same\n")
    if not added:
        left, right = right, left
    _write(tree / ".claude/skills/demo/SKILL.md", left)
    _write(tree / ".codex/skills/demo/SKILL.md", right)
    pair = lint_prompt_parity.compare_pair("demo", ".claude", ".codex", rules, tree)
    assert pair.lines == 1
    results = [lint_prompt_parity.SkillResult("demo", [".claude", ".codex"], [pair])]
    violations, candidates = lint_prompt_parity.evaluate(results, {})
    assert violations and not candidates


@pytest.mark.parametrize("root", [".claude", ".codex"])
def test_unmatched_empty_file_is_still_divergence(
    lint_prompt_parity: ModuleType,
    rules: dict[str, list[Any]],
    tree: Path,
    root: str,
) -> None:
    _write(tree / ".claude/skills/demo/SKILL.md", "same\n")
    _write(tree / ".codex/skills/demo/SKILL.md", "same\n")
    _write(tree / root / "skills/demo/scripts/__init__.py", "")
    pair = lint_prompt_parity.compare_pair("demo", ".claude", ".codex", rules, tree)
    assert pair.lines == 1
    results = [lint_prompt_parity.SkillResult("demo", [".claude", ".codex"], [pair])]
    violations, candidates = lint_prompt_parity.evaluate(results, {})
    assert violations and not candidates


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "skill"])
def test_symlinked_skill_payload_is_rejected(
    lint_prompt_parity: ModuleType,
    rules: dict[str, list[Any]],
    tree: Path,
    kind: str,
) -> None:
    _write(tree / ".claude/skills/demo/SKILL.md", "same\n")
    base = tree / ".codex/skills/demo"
    if kind == "skill":
        base.parent.mkdir(parents=True)
        base.symlink_to(tree / ".claude/skills/demo", target_is_directory=True)
    else:
        _write(base / "SKILL.md", "same\n")
        if kind == "directory":
            (base / "scripts").symlink_to(tree, target_is_directory=True)
        else:
            (base / "run.sh").symlink_to("SKILL.md" if kind == "file" else "missing")
    with pytest.raises(lint_prompt_parity.ParityError, match="must not be a symlink"):
        lint_prompt_parity.compare_pair("demo", ".claude", ".codex", rules, tree)


def test_a_skill_in_one_root_only_is_out_of_scope(
    lint_prompt_parity: ModuleType, tree: Path
) -> None:
    # A per-harness skill has nothing to be in parity *with* and owes no
    # justification; only copies do.
    _write(tree / ".claude/skills/solo/SKILL.md", "x\n")
    _write(tree / ".claude/skills/pair/SKILL.md", "x\n")
    _write(tree / ".codex/skills/pair/SKILL.md", "x\n")
    assert lint_prompt_parity.shared_skills(tree) == {"pair": [".claude", ".codex"]}


# --------------------------------------------------------------------------
# the allowlist
# --------------------------------------------------------------------------


def _allowlist(lint: ModuleType, tmp_path: Path, document: object) -> dict[str, object]:
    path = tmp_path / "parity-allowlist.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    loaded: dict[str, object] = lint.load_allowlist(path)
    return loaded


def test_a_recorded_entry_may_cite_one_record_or_several(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    entries = _allowlist(
        lint_prompt_parity,
        tmp_path,
        {
            "recorded": [
                {
                    "skill": "critique",
                    "record": [
                        "0002-reviewer-precision-postures.md",
                        "0006-review-chains-never-converge.md",
                    ],
                    "reason": "per-model calibration",
                },
                {
                    "skill": "agent-loop",
                    "record": "0007-agent-loop-per-harness-launch.md",
                    "reason": "three launch models",
                },
            ]
        },
    )
    assert set(entries) == {"critique", "agent-loop"}


def test_a_citation_to_a_record_nobody_wrote_is_fatal(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    # The record is the entry's whole substance, so it is resolved against the
    # filesystem rather than trusted as a string.
    with pytest.raises(lint_prompt_parity.ParityError, match="does not exist"):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {"recorded": [{"skill": "x", "record": "0099-invented.md", "reason": "r"}]},
        )


def test_the_index_page_is_not_a_record(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(lint_prompt_parity.ParityError):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {"recorded": [{"skill": "x", "record": "README.md", "reason": "r"}]},
        )


def test_a_record_path_may_not_escape_the_decisions_directory(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(lint_prompt_parity.ParityError, match="bare filename"):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {"recorded": [{"skill": "x", "record": "../../README.md", "reason": "r"}]},
        )


def test_a_held_entry_needs_an_issue_and_a_ceiling(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(lint_prompt_parity.ParityError, match="ceiling"):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {"held": [{"skill": "x", "issue": 12, "reason": "drift"}]},
        )


def test_a_boolean_is_not_a_ceiling(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    # `True` is an `int` in Python and would otherwise pass as a ceiling of 1.
    with pytest.raises(lint_prompt_parity.ParityError, match="ceiling"):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {"held": [{"skill": "x", "issue": 12, "ceiling": True, "reason": "d"}]},
        )


def test_every_entry_needs_a_reason(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(lint_prompt_parity.ParityError, match="reason"):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {"held": [{"skill": "x", "issue": 12, "ceiling": 3}]},
        )


def test_an_unknown_key_is_fatal_rather_than_ignored(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    # A silently ignored key is how a `ceiling:` typo turns a ratchet into a
    # permanent exemption.
    with pytest.raises(lint_prompt_parity.ParityError, match="unknown keys"):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {"held": [{"skill": "x", "issue": 1, "ceiling": 1, "reason": "d", "why": "!"}]},
        )


def test_a_skill_may_not_be_listed_twice(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(lint_prompt_parity.ParityError, match="listed twice"):
        _allowlist(
            lint_prompt_parity,
            tmp_path,
            {
                "recorded": [
                    {"skill": "x", "record": "0006-review-chains-never-converge.md", "reason": "r"}
                ],
                "held": [{"skill": "x", "issue": 1, "ceiling": 1, "reason": "d"}],
            },
        )


def test_a_missing_allowlist_is_an_error_not_an_empty_one(
    lint_prompt_parity: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(lint_prompt_parity.ParityError, match="not found"):
        lint_prompt_parity.load_allowlist(tmp_path / "absent.yml")


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------


def _result(lint: ModuleType, skill: str, lines: int) -> object:
    pair = lint.PairResult(left=".claude", right=".codex", lines=lines)
    return lint.SkillResult(skill=skill, roots=[".claude", ".codex"], pairs=[pair])


def _held(lint: ModuleType, skill: str, ceiling: int) -> object:
    return lint.Entry(skill, "held", "#1", "drift", ceiling)


def test_a_divergence_with_no_entry_fails(lint_prompt_parity: ModuleType) -> None:
    violations, candidates = lint_prompt_parity.evaluate(
        [_result(lint_prompt_parity, "demo", 7)], {}
    )
    assert candidates == []
    assert len(violations) == 1
    assert "no allowlist entry" in violations[0]


def test_an_unlisted_skill_at_zero_is_a_candidate_not_a_failure(
    lint_prompt_parity: ModuleType,
) -> None:
    violations, candidates = lint_prompt_parity.evaluate(
        [_result(lint_prompt_parity, "demo", 0)], {}
    )
    assert (violations, candidates) == ([], ["demo"])


def test_an_allowlisted_skill_at_zero_is_stale_and_fails(
    lint_prompt_parity: ModuleType,
) -> None:
    # This is the promotion trigger: the debt reached zero, so the entry has to
    # go and the skill has to move into the rendered roster.
    violations, candidates = lint_prompt_parity.evaluate(
        [_result(lint_prompt_parity, "demo", 0)],
        {"demo": _held(lint_prompt_parity, "demo", 4)},
    )
    assert candidates == []
    assert "stale entry" in violations[0]


def test_held_drift_may_not_grow(lint_prompt_parity: ModuleType) -> None:
    violations, _ = lint_prompt_parity.evaluate(
        [_result(lint_prompt_parity, "demo", 9)],
        {"demo": _held(lint_prompt_parity, "demo", 4)},
    )
    assert "grew to 9" in violations[0]


def test_held_drift_that_shrank_must_lower_its_ceiling(
    lint_prompt_parity: ModuleType,
) -> None:
    # A ceiling left above the real number stops being a ratchet and becomes
    # headroom for the next regression.
    violations, _ = lint_prompt_parity.evaluate(
        [_result(lint_prompt_parity, "demo", 2)],
        {"demo": _held(lint_prompt_parity, "demo", 4)},
    )
    assert "Lower the ceiling" in violations[0]


def test_held_drift_at_its_ceiling_passes(lint_prompt_parity: ModuleType) -> None:
    violations, candidates = lint_prompt_parity.evaluate(
        [_result(lint_prompt_parity, "demo", 4)],
        {"demo": _held(lint_prompt_parity, "demo", 4)},
    )
    assert (violations, candidates) == ([], [])


def test_a_recorded_entry_lets_the_residual_move(lint_prompt_parity: ModuleType) -> None:
    entry = lint_prompt_parity.Entry("demo", "recorded", "0006-x.md", "deliberate", None)
    violations, _ = lint_prompt_parity.evaluate(
        [_result(lint_prompt_parity, "demo", 4000)], {"demo": entry}
    )
    assert violations == []


def test_an_entry_for_a_skill_that_is_not_in_scope_fails(
    lint_prompt_parity: ModuleType,
) -> None:
    violations, _ = lint_prompt_parity.evaluate([], {"gone": _held(lint_prompt_parity, "gone", 1)})
    assert "not a shared unrendered skill" in violations[0]


# --------------------------------------------------------------------------
# the shipped state
# --------------------------------------------------------------------------


def test_the_repository_passes_its_own_parity_gate(lint_prompt_parity: ModuleType) -> None:
    assert lint_prompt_parity.main([]) == 0


def test_every_shipped_entry_names_a_skill_that_still_exists(
    lint_prompt_parity: ModuleType,
) -> None:
    entries = lint_prompt_parity.load_allowlist()
    assert set(entries) <= set(lint_prompt_parity.shared_skills())


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def test_the_table_labels_every_disposition(lint_prompt_parity: ModuleType) -> None:
    results = [
        _result(lint_prompt_parity, "unlisted", 5),
        _result(lint_prompt_parity, "clean", 0),
        _result(lint_prompt_parity, "drifting", 3),
        _result(lint_prompt_parity, "deliberate", 900),
    ]
    entries = {
        "drifting": _held(lint_prompt_parity, "drifting", 3),
        "deliberate": lint_prompt_parity.Entry(
            "deliberate", "recorded", "0006-x.md", "why", None
        ),
    }
    table = lint_prompt_parity.format_table(results, entries)
    assert "unlisted" in table and "UNLISTED" in table
    assert "promotion candidate" in table
    assert "held (#1, ceiling 3)" in table
    assert "recorded (0006-x.md)" in table


def test_the_table_names_a_file_only_one_root_has(lint_prompt_parity: ModuleType) -> None:
    pair = lint_prompt_parity.PairResult(
        left=".claude", right=".codex", lines=3, only_right=["scripts/run.sh"]
    )
    result = lint_prompt_parity.SkillResult(
        skill="demo", roots=[".claude", ".codex"], pairs=[pair]
    )
    assert "only in .codex:scripts/run.sh" in lint_prompt_parity.format_table([result], {})


def test_promotion_candidates_reach_the_job_summary(
    lint_prompt_parity: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-fatal finding printed into a green job's log is read by nobody; the
    # summary is where a zero-residual skill actually gets noticed.
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    lint_prompt_parity._write_step_summary(["ship-staging"])
    written = summary.read_text(encoding="utf-8")
    assert "ship-staging" in written
    assert lint_prompt_parity.PROMOTION_CAVEAT in written


def test_no_summary_is_written_off_ci_or_with_nothing_to_say(
    lint_prompt_parity: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    lint_prompt_parity._write_step_summary(["ship-staging"])
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    lint_prompt_parity._write_step_summary([])
    assert not summary.exists()


def test_an_unwritable_summary_does_not_fail_a_clean_gate(
    lint_prompt_parity: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "absent-dir" / "summary.md"))
    lint_prompt_parity._write_step_summary(["ship-staging"])  # must not raise


def test_report_mode_never_fails(
    lint_prompt_parity: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint_prompt_parity.main(["--report"]) == 0
    assert "residual" in capsys.readouterr().out


def test_diff_mode_prints_the_normalized_diff(
    lint_prompt_parity: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint_prompt_parity.main(["--diff", "copilot-review"]) == 0
    assert ".claude → .codex" in capsys.readouterr().out


def test_diff_mode_rejects_a_skill_that_is_not_in_scope(
    lint_prompt_parity: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint_prompt_parity.main(["--diff", "not-a-skill"]) == 2
    assert "not a shared unrendered skill" in capsys.readouterr().err


def test_an_empty_allowlist_fails_the_real_repository(
    lint_prompt_parity: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The gate has to bite on the tree as it actually is; a lint that only
    # passes is indistinguishable from one that never runs.
    monkeypatch.setattr(lint_prompt_parity, "load_allowlist", lambda: {})
    assert lint_prompt_parity.main([]) == 1
    assert "no allowlist entry" in capsys.readouterr().err


def test_a_malformed_allowlist_is_a_config_error_not_a_violation(
    lint_prompt_parity: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> dict[str, object]:
        raise lint_prompt_parity.ParityError("bad allowlist")

    monkeypatch.setattr(lint_prompt_parity, "load_allowlist", boom)
    assert lint_prompt_parity.main([]) == 2
