"""Unit tests for `scripts/sync-engine.py`.

Covers the sync-engine hardening invariants:
- `resolve_under` path-traversal escapes (lexical-only check)
- `parse_mode` octal/int/None handling + bool rejection
- `substitute` placeholder warnings + missing-required failure
- `write_if_changed` content + mode divergence
- `prune_empty_parents` walk-up behavior with non-empty stop + ENOENT/ENOTEMPTY tolerance
- Manifest validation (malformed entries, strict-boolean `delete`/`create_if_missing`)
- The delete branch's `exists() or is_symlink()` dangling-link path
- The create_if_missing branch's bootstrap + preserve semantics
- `allow_sensitive_writes` per-file consent for sensitive destinations
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import ModuleType

import pytest
import yaml


# ---------------------------------------------------------------------------
# resolve_under — lexical traversal check
# ---------------------------------------------------------------------------


def test_resolve_under_accepts_normal_child(sync_engine: ModuleType, tmp_path: Path) -> None:
    result = sync_engine.resolve_under(tmp_path, "a/b/c.txt")
    assert result == tmp_path / "a" / "b" / "c.txt"


def test_resolve_under_rejects_dotdot_escape(sync_engine: ModuleType, tmp_path: Path) -> None:
    assert sync_engine.resolve_under(tmp_path, "../outside") is None
    assert sync_engine.resolve_under(tmp_path, "a/../../outside") is None


def test_resolve_under_rejects_absolute_path(sync_engine: ModuleType, tmp_path: Path) -> None:
    assert sync_engine.resolve_under(tmp_path, "/etc/passwd") is None


def test_resolve_under_rejects_path_collapsing_to_parent(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    # `foo/..` normalizes back to the parent itself — must be rejected.
    assert sync_engine.resolve_under(tmp_path, "foo/..") is None
    assert sync_engine.resolve_under(tmp_path, ".") is None


def test_resolve_under_tolerates_dangling_symlink_at_target(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    """Lexical normalization (not Path.resolve()) means a dangling symlink at
    the destination doesn't break the path-bound check — important for
    delete targets that must clean up broken links.
    """
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nope")
    result = sync_engine.resolve_under(tmp_path, "dangling")
    assert result == dangling


# ---------------------------------------------------------------------------
# parse_mode — octal coercion + type strictness
# ---------------------------------------------------------------------------


def test_parse_mode_none_returns_none(sync_engine: ModuleType) -> None:
    assert sync_engine.parse_mode(None) is None


def test_parse_mode_int_passthrough(sync_engine: ModuleType) -> None:
    assert sync_engine.parse_mode(0o755) == 0o755
    assert sync_engine.parse_mode(0o644) == 0o644


def test_parse_mode_octal_string(sync_engine: ModuleType) -> None:
    assert sync_engine.parse_mode("0755") == 0o755
    assert sync_engine.parse_mode("755") == 0o755


def test_parse_mode_rejects_bool(sync_engine: ModuleType) -> None:
    # bool subclasses int in Python; without an explicit guard, `True`
    # would become mode 1 and `False` mode 0.
    with pytest.raises(TypeError, match="bool"):
        sync_engine.parse_mode(True)
    with pytest.raises(TypeError, match="bool"):
        sync_engine.parse_mode(False)


def test_parse_mode_rejects_other_types(sync_engine: ModuleType) -> None:
    with pytest.raises(TypeError):
        sync_engine.parse_mode([0o755])
    with pytest.raises(TypeError):
        sync_engine.parse_mode({"mode": 0o755})


def test_parse_mode_rejects_non_octal_string(sync_engine: ModuleType) -> None:
    with pytest.raises(ValueError):
        sync_engine.parse_mode("9999")  # 9 isn't a valid octal digit


def test_parse_mode_rejects_negative_int(sync_engine: ModuleType) -> None:
    # `Path.chmod(-1)` raises OverflowError mid-loop, partially syncing the
    # consumer tree. Reject at the parse boundary so the sync fails before
    # any write happens.
    with pytest.raises(ValueError, match="out of range"):
        sync_engine.parse_mode(-1)


def test_parse_mode_rejects_oversized_int(sync_engine: ModuleType) -> None:
    # Values above 0o7777 are not valid POSIX file modes; reject rather
    # than silently truncate.
    with pytest.raises(ValueError, match="out of range"):
        sync_engine.parse_mode(0o10000)


# ---------------------------------------------------------------------------
# substitute — placeholder warnings + missing-required failure
# ---------------------------------------------------------------------------


def test_substitute_replaces_declared_placeholder(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "hello <<NAME>>, welcome"
    out = sync_engine.substitute(text, {"NAME": "world"}, ["NAME"], "src.md")
    assert out == "hello world, welcome"
    err = capsys.readouterr().err
    assert err == ""  # clean substitution: no warnings


def test_substitute_warns_on_declared_not_in_source(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    out = sync_engine.substitute("no placeholders", {"NAME": "x"}, ["NAME"], "src.md")
    assert out == "no placeholders"
    err = capsys.readouterr().err
    assert "declared substitutions not found in src.md" in err
    assert "NAME" in err


def test_substitute_warns_on_undeclared_placeholder_left_intact(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "hello <<NAME>>, you are <<ROLE>>"
    out = sync_engine.substitute(text, {"NAME": "world"}, ["NAME"], "src.md")
    # <<ROLE>> is left intact since it's not in the declared list.
    assert "<<ROLE>>" in out
    assert "hello world" in out
    err = capsys.readouterr().err
    assert "placeholders in src.md not declared" in err
    assert "ROLE" in err


def test_substitute_exits_on_missing_required_substitution(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        sync_engine.substitute("<<REQUIRED>>", {}, ["REQUIRED"], "src.md")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "requires placeholders missing from .platform-config.yml" in err


def test_substitute_strips_trailing_newlines_from_block_scalar(
    sync_engine: ModuleType,
) -> None:
    # YAML `|` block scalars carry a trailing \n; the engine strips it so
    # the template's explicit blank line after each placeholder controls
    # inter-section spacing.
    out = sync_engine.substitute(
        "before\n<<KEY>>\nafter",
        {"KEY": "value\n\n"},
        ["KEY"],
        "src.md",
    )
    assert out == "before\nvalue\nafter"


# ---------------------------------------------------------------------------
# drop_empty_placeholder_lines — blank-line stability of template renders
# ---------------------------------------------------------------------------
#
# The engine renders faithfully: the only whitespace it removes is a line that a
# substitution emptied. Every literal-content case below is a regression guard
# against reintroducing a whole-file normalizer, which cannot tell an
# author-written blank line from a placeholder-produced one and so rewrites
# content the pinned prettier preserves.


def test_render_collapses_blank_run_left_by_empty_placeholder(
    sync_engine: ModuleType,
) -> None:
    # The motivating case: an empty substitution on its own template line would
    # leave the blank lines around it stacked — prettier collapses the run, so
    # every sync PR would reintroduce it and every local format run revert it.
    out = sync_engine.substitute(
        "- last list item\n\n<<EXTRA>>\n\n---\n",
        {"EXTRA": ""},
        ["EXTRA"],
        "src.md",
        ["EXTRA"],
    )
    assert out == "- last list item\n\n---\n"


def test_render_drops_separator_only_when_a_run_would_form(
    sync_engine: ModuleType,
) -> None:
    # One side non-blank means removing the placeholder line leaves no run, so
    # no separator is consumed and the author's spacing survives intact.
    assert sync_engine.substitute("a\n<<E>>\n\nb\n", {"E": ""}, ["E"], "src.md", ["E"]) == "a\n\nb\n"
    assert sync_engine.substitute("a\n\n<<E>>\nb\n", {"E": ""}, ["E"], "src.md", ["E"]) == "a\n\nb\n"
    assert sync_engine.substitute("a\n<<E>>\nb\n", {"E": ""}, ["E"], "src.md", ["E"]) == "a\nb\n"


def test_render_drops_leading_and_trailing_blank_at_file_edges(
    sync_engine: ModuleType,
) -> None:
    # Start/end of file behave like a blank line: a placeholder at either edge
    # would otherwise leave a leading or trailing blank that prettier strips.
    assert sync_engine.substitute("<<E>>\n\nb\n", {"E": ""}, ["E"], "src.md", ["E"]) == "b\n"
    assert sync_engine.substitute("a\n\n<<E>>\n", {"E": ""}, ["E"], "src.md", ["E"]) == "a\n"


def test_render_handles_back_to_back_empty_placeholders(
    sync_engine: ModuleType,
) -> None:
    # Adjacent empty placeholders must not each eat a separator and glue the
    # surrounding sections together.
    assert (
        sync_engine.substitute(
            "a\n\n<<E>>\n<<F>>\n\nb\n",
            {"E": "", "F": ""},
            ["E", "F"],
            "src.md",
            ["E", "F"],
        )
        == "a\n\nb\n"
    )
    assert (
        sync_engine.substitute(
            "a\n\n<<E>>\n\n<<F>>\n\nb\n",
            {"E": "", "F": ""},
            ["E", "F"],
            "src.md",
            ["E", "F"],
        )
        == "a\n\nb\n"
    )
    assert (
        sync_engine.substitute(
            "a\n\n<<E>><<F>>\n\nb\n",
            {"E": "", "F": ""},
            ["E", "F"],
            "src.md",
            ["E", "F"],
        )
        == "a\n\nb\n"
    )
    assert (
        sync_engine.substitute(
            "a\n\n<<E>>\n<<F>>\n",
            {"E": "", "F": ""},
            ["E", "F"],
            "src.md",
            ["E", "F"],
        )
        == "a\n"
    )


def test_render_matches_whole_line_placeholder_with_surrounding_whitespace(
    sync_engine: ModuleType,
) -> None:
    # An indented or trailing-space placeholder line is still a whole-line
    # placeholder; leaving it behind would emit a whitespace-only line.
    out = sync_engine.substitute("a\n\n  <<E>>  \n\nb\n", {"E": ""}, ["E"], "src.md", ["E"])
    assert out == "a\n\nb\n"


def test_render_keeps_line_for_non_empty_and_inline_placeholders(
    sync_engine: ModuleType,
) -> None:
    # Only a whole-line placeholder that renders empty is removed. A value with
    # content keeps its line, and an inline placeholder never removes one.
    assert sync_engine.substitute("a\n\n<<E>>\n\nb\n", {"E": "X"}, ["E"], "src.md", ["E"]) == "a\n\nX\n\nb\n"
    assert sync_engine.substitute("docs: <<E>>.\n", {"E": ""}, ["E"], "src.md", ["E"]) == "docs: .\n"


def test_render_preserves_non_opted_empty_placeholder_in_literal_content(
    sync_engine: ModuleType,
) -> None:
    samples = [
        "```text\nline one\n\n<<E>>\n\nline two\n```\n",
        "<pre>\nline one\n\n<<E>>\n\nline two\n</pre>\n",
        "    line one\n\n    <<E>>\n\n    line two\n",
    ]
    for text in samples:
        expected = text.replace("<<E>>", "")
        assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md") == expected


def test_render_preserves_horizontal_whitespace_value_when_opted_in(
    sync_engine: ModuleType,
) -> None:
    text = "a\n\n<<E>>\n\nb\n"
    assert sync_engine.substitute(text, {"E": " \t "}, ["E"], "src.md", ["E"]) == "a\n\n \t \n\nb\n"


def test_render_keeps_line_when_only_some_placeholders_are_opted_in(
    sync_engine: ModuleType,
) -> None:
    text = "a\n\n<<E>><<F>>\n\nb\n"
    # `F` is substituted but not opted in, so the line is not the engine's to
    # remove even though both values render empty.
    assert sync_engine.substitute(text, {"E": "", "F": ""}, ["E", "F"], "src.md", ["E"]) == "a\n\n\n\nb\n"


def test_render_keeps_line_when_an_opted_in_sibling_is_non_empty(
    sync_engine: ModuleType,
) -> None:
    text = "a\n\n<<E>><<F>>\n\nb\n"
    assert (
        sync_engine.substitute(text, {"E": "", "F": "X"}, ["E", "F"], "src.md", ["E", "F"])
        == "a\n\nX\n\nb\n"
    )


def test_render_keeps_line_carrying_an_undeclared_placeholder(
    sync_engine: ModuleType,
) -> None:
    # `PLACEHOLDER_RE.sub("", line)` erases the undeclared token too, so the
    # line looks placeholder-only; dropping it would delete a real unsubstituted
    # token the consumer has not configured yet.
    text = "a\n\n<<E>><<UNKNOWN>>\n\nb\n"
    assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md", ["E"]) == "a\n\n<<UNKNOWN>>\n\nb\n"


def test_render_collapses_crlf_source(sync_engine: ModuleType) -> None:
    # `.split("\n")` leaves a `\r` on every line; the qualification test must
    # strip it or a CRLF checkout silently gets no collapsing at all.
    text = "a\r\n\r\n<<E>>\r\n\r\nb\r\n"
    assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md", ["E"]) == "a\r\n\r\nb\r\n"


def test_render_treats_a_null_value_as_empty(sync_engine: ModuleType) -> None:
    # `DOMAIN_RULES:` with nothing after the colon parses as None. `str(None)`
    # would render the literal word `None` into the consumer's repo.
    text = "a\n\n<<E>>\n\nb\n"
    assert sync_engine.substitute(text, {"E": None}, ["E"], "src.md", ["E"]) == "a\n\nb\n"
    assert sync_engine.substitute("x <<E>> y\n", {"E": None}, ["E"], "src.md") == "x  y\n"


def test_render_warns_when_an_opted_in_key_never_qualifies(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    # A list bullet is not a whole-line placeholder, so the opt-in cannot fire
    # and the consumer silently keeps the blank-line run it was meant to remove.
    text = "a\n\n- <<E>>\n\nb\n"
    assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md", ["E"]) == "a\n\n- \n\nb\n"
    err = capsys.readouterr().err
    assert "no line qualified" in err
    assert "E" in err


def test_render_does_not_warn_when_the_opted_in_key_collapsed(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    sync_engine.substitute("a\n\n<<E>>\n\nb\n", {"E": ""}, ["E"], "src.md", ["E"])
    assert "no line qualified" not in capsys.readouterr().err


def test_render_does_not_warn_for_a_non_empty_opted_in_key(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    sync_engine.substitute("a\n\n<<E>>\n\nb\n", {"E": "value"}, ["E"], "src.md", ["E"])
    assert "no line qualified" not in capsys.readouterr().err


def test_render_preserves_blank_runs_inside_raw_pre_html(
    sync_engine: ModuleType,
) -> None:
    # Verified against the pinned prettier: it leaves raw <pre> content alone,
    # blank runs included. Collapsing them would rewrite literal page content.
    text = "intro\n\n<pre>\nline one\n\n\n\nline two\n</pre>\n\nafter\n"
    assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md") == text


def test_render_preserves_blank_runs_inside_indented_code(
    sync_engine: ModuleType,
) -> None:
    # Same for four-space indented code blocks — the blank runs are code.
    text = "intro\n\n    code one\n\n\n\n    code two\n\nafter\n"
    assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md") == text


def test_render_preserves_fenced_code_regardless_of_fence_shape(
    sync_engine: ModuleType,
) -> None:
    # CommonMark fence closing has corner cases a normalizer gets wrong: a
    # closer carrying both leading indentation and trailing whitespace is valid
    # (and was previously missed), while a split-marker line like "``` ```" is
    # not a closer (and previously closed the block early). Rendering faithfully
    # makes both moot — no fence is parsed at all.
    samples = [
        "text\n\n   ```\ncode\n\n\n\nmore\n   ```   \n\n\n\nafter\n",
        "```\na\n``` ```\n\n\n\nb\n```\n",
        "text\n\n  ~~~\ncode\n\n\n\nmore\n  ~~~  \n\nafter\n",
        "````\n```\n\n\n~~~\n````\n\nafter\n",
        "```\nunclosed fence\n\n\n\nstill inside\n",
    ]
    for text in samples:
        assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md") == text


def test_render_leaves_empty_and_blank_only_documents_alone(
    sync_engine: ModuleType,
) -> None:
    # The pinned prettier writes an empty file for empty input, so appending a
    # newline here would be churn. A blank-only source is left verbatim: no
    # substitution emptied those lines, so they are the author's content.
    assert sync_engine.substitute("", {}, [], "src.md") == ""
    assert sync_engine.substitute("\n", {}, [], "src.md") == "\n"
    assert sync_engine.substitute("\n\n  \n", {}, [], "src.md") == "\n\n  \n"
    # A document that is nothing but an emptied placeholder renders empty.
    assert sync_engine.substitute("<<E>>\n", {"E": ""}, ["E"], "src.md", ["E"]) == ""


def test_render_is_idempotent(sync_engine: ModuleType) -> None:
    # Re-rendering an already-rendered document is a no-op: the output holds no
    # placeholders, so nothing further can be dropped.
    samples = [
        "- last list item\n\n<<EXTRA>>\n\n---\n",
        "a\n\n<<E>>\n<<F>>\n\nb\n",
        "<<E>>\n",
        "text\n\n```\ncode\n\n\nmore\n```\n\n\n---\n",
        "",
    ]
    values = {"EXTRA": "", "E": "", "F": ""}
    keys = ["EXTRA", "E", "F"]
    for text in samples:
        once = sync_engine.substitute(text, values, keys, "src.md", keys)
        assert sync_engine.substitute(once, values, keys, "src.md", keys) == once


def test_verbatim_copy_never_drops_a_placeholder_line(
    sync_engine: ModuleType,
) -> None:
    # subs == [] declares nothing, so a `<<KEY>>` line is left intact even when
    # the consumer happens to configure an empty value for that key.
    text = "a\n\n<<E>>\n\nb\n"
    assert sync_engine.substitute(text, {"E": ""}, [], "src.md") == text


def test_main_renders_substituted_md_but_leaves_verbatim_copies_alone(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Substituted render: the emptied placeholder line and one separator go.
    # Verbatim copy (subs == []): byte-identical even with ugly whitespace —
    # consumers prettier-ignore vendored files, and an engine rewrite would
    # itself be churn against the upstream source of truth.
    ugly = "# Title\n\n\n\nbody\n\n\n"
    (upstream_repo / "tpl.md").write_text("# <<NAME>>\n\n<<EXTRA>>\n\nbody\n")
    (upstream_repo / "verbatim.md").write_text(ugly)
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "tpl.md",
                    "destination": "rendered.md",
                    "substitutions": ["NAME", "EXTRA"],
                    "collapse_empty_substitutions": ["EXTRA"],
                },
                {
                    "source": "verbatim.md",
                    "destination": "verbatim.md",
                    "substitutions": [],
                },
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"substitutions": {"NAME": "Repo", "EXTRA": ""}},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "rendered.md").read_text() == "# Repo\n\nbody\n"
    assert (consumer_dir / "verbatim.md").read_text() == ugly


# ---------------------------------------------------------------------------
# write_if_changed — content + mode divergence
# ---------------------------------------------------------------------------


def test_write_if_changed_creates_new_file(sync_engine: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "sub" / "out.txt"
    changed = sync_engine.write_if_changed(target, "hello", None)
    assert changed is True
    assert target.read_text() == "hello"


def test_write_if_changed_noop_on_identical_content(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello")
    mtime_before = target.stat().st_mtime_ns
    changed = sync_engine.write_if_changed(target, "hello", None)
    assert changed is False
    assert target.stat().st_mtime_ns == mtime_before  # no rewrite


def test_write_if_changed_rewrites_on_diverged_content(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello")
    changed = sync_engine.write_if_changed(target, "world", None)
    assert changed is True
    assert target.read_text() == "world"


def test_write_if_changed_applies_mode_when_diverged(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o644)
    changed = sync_engine.write_if_changed(target, "#!/bin/sh\n", 0o755)
    # Content unchanged, mode diverged → still reports changed=True.
    assert changed is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_write_if_changed_compares_full_12bit_mode(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    """Regression lock: `parse_mode` accepts the full POSIX 12-bit range
    (setuid + setgid + sticky + rwx*3 = up to `0o7777`). The mode
    comparison in `write_if_changed` must use `stat.S_IMODE` (12-bit),
    NOT `& 0o777` (9-bit) — otherwise a file with mode `0o4755`
    (setuid + rwxr-xr-x) compared against current `0o755` would always
    appear out-of-sync and the engine would re-chmod on every run.
    """
    target = tmp_path / "setuid.sh"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o4755)  # setuid + rwxr-xr-x
    # Content identical, mode matches at the FULL 12-bit level → no change.
    changed = sync_engine.write_if_changed(target, "#!/bin/sh\n", 0o4755)
    assert changed is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o4755


def test_write_if_changed_leaves_mode_when_none(sync_engine: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello")
    target.chmod(0o600)
    sync_engine.write_if_changed(target, "world", None)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600  # mode untouched


# ---------------------------------------------------------------------------
# prune_empty_parents — walk-up with non-empty stop + ENOENT tolerance
# ---------------------------------------------------------------------------


def test_prune_empty_parents_removes_empty_chain(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    f = nested / "file.txt"
    f.write_text("x")
    f.unlink()  # simulate sync-engine's unlink
    sync_engine.prune_empty_parents(f, tmp_path)
    assert not (tmp_path / "a").exists()


def test_prune_empty_parents_stops_at_non_empty(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    sibling = tmp_path / "a" / "sibling.txt"
    sibling.write_text("keep me")
    f = tmp_path / "a" / "b" / "deleted.txt"
    f.write_text("x")
    f.unlink()
    sync_engine.prune_empty_parents(f, tmp_path)
    # `b` was empty so it's gone; `a` had a sibling so it's preserved.
    assert not (tmp_path / "a" / "b").exists()
    assert (tmp_path / "a").exists()
    assert sibling.exists()


def test_prune_empty_parents_does_not_remove_root(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    f.unlink()
    sync_engine.prune_empty_parents(f, tmp_path)
    assert tmp_path.exists()  # root is the stop condition; never removed


def test_prune_empty_parents_tolerates_concurrent_remove(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    # Simulate the file's parent dir already being gone (concurrent cleanup).
    nested = tmp_path / "a" / "b"
    f = nested / "ghost.txt"
    # No mkdir — `f.parent` doesn't exist. prune_empty_parents must not raise.
    sync_engine.prune_empty_parents(f, tmp_path)


# ---------------------------------------------------------------------------
# End-to-end main() invocation via direct call
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def _run_main(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool = False,
) -> int:
    argv = [
        "sync-engine.py",
        "--upstream-repo",
        str(upstream),
        "--consumer-dir",
        str(consumer),
    ]
    if dry_run:
        argv.append("--dry-run")
    monkeypatch.setattr("sys.argv", argv)
    return int(sync_engine.main())


def test_main_copy_target_writes_substituted_file(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "src.md").write_text("hello <<NAME>>\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md", "substitutions": ["NAME"]}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"substitutions": {"NAME": "world"}})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "dest.md").read_text() == "hello world\n"


def test_main_plain_substitution_preserves_non_markdown_literal_spacing(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "block: |\n  first\n\n  <<EMPTY>>\n\n  last\n"
    (upstream_repo / "src.yml").write_text(source)
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "src.yml",
                    "destination": "dest.yml",
                    "substitutions": ["EMPTY"],
                }
            ]
        },
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"substitutions": {"EMPTY": ""}})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "dest.yml").read_text() == source.replace("<<EMPTY>>", "")


def test_main_rejects_undeclared_collapse_empty_key(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("<<NAME>>\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "src.md",
                    "destination": "dest.md",
                    "substitutions": ["NAME"],
                    "collapse_empty_substitutions": ["EMPTY"],
                }
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "must also appear in `substitutions`" in capsys.readouterr().err


def test_main_rejects_unknown_target_field(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A misspelled optional field used to be ignored in silence, which meant a
    # typo disabled the feature it named while the engine, the collapse-site
    # lint, and the manifest schema job all stayed green — and the render
    # carried exactly the blank-line churn the opt-in exists to prevent.
    (upstream_repo / "src.md").write_text("a\n\n<<EMPTY>>\n\nb\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "src.md",
                    "destination": "dest.md",
                    "substitutions": ["EMPTY"],
                    "collapse_empty_subsitutions": ["EMPTY"],
                }
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown field(s) in target entry: collapse_empty_subsitutions" in err
    assert not (consumer_dir / "dest.md").exists()


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("substitutions", "NAME"),
        ("substitutions", ["NAME", 3]),
        ("collapse_empty_substitutions", "NAME"),
        ("collapse_empty_substitutions", ["NAME", None]),
    ],
)
def test_main_rejects_malformed_string_lists(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    bad_value: object,
) -> None:
    # `substitutions` was previously read as `target.get(...) or []`, so a
    # stringly-typed value was iterated character by character and rendered as
    # a near-verbatim copy. It must now fail closed.
    (upstream_repo / "src.md").write_text("<<NAME>>\n")
    target: dict[str, object] = {
        "source": "src.md",
        "destination": "dest.md",
        "substitutions": ["NAME"],
    }
    target[key] = bad_value
    _write_yaml(upstream_repo / "scripts" / "sync-targets.yml", {"targets": [target]})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert f"`{key}` must be a list of strings" in capsys.readouterr().err
    assert not (consumer_dir / "dest.md").exists()


@pytest.mark.parametrize("key", ["1NAME", "_NAME", "name"])
def test_main_rejects_invalid_placeholder_keys(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    (upstream_repo / "src.md").write_text(f"<<{key}>>\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "src.md",
                    "destination": "dest.md",
                    "substitutions": [key],
                    "collapse_empty_substitutions": [key],
                }
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "contains invalid placeholder keys" in capsys.readouterr().err
    assert not (consumer_dir / "dest.md").exists()


def test_main_delete_target_unlinks_real_file(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (consumer_dir / "stale.md").write_text("retired content")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "stale.md", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert not (consumer_dir / "stale.md").exists()


def test_main_delete_target_unlinks_dangling_symlink(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `exists()` alone returns False on a dangling link; the engine pairs
    # it with `is_symlink()` so retired symlinks still get cleaned up.
    dangling = consumer_dir / "dangling"
    dangling.symlink_to(consumer_dir / "absent-target")
    assert dangling.is_symlink()
    assert not dangling.exists()  # confirm it's dangling

    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "dangling", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert not dangling.is_symlink()


def test_main_delete_refuses_directory(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (consumer_dir / "subdir").mkdir()
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "subdir", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination is a directory" in err


def test_main_delete_is_idempotent_when_already_absent(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "never-existed.md", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "already absent" in out


def test_main_rejects_stringly_typed_delete_flag(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `delete: "true"` would be truthy in Python — must hard-fail.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "x.md", "delete": "true"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`delete` must be a boolean" in err


def test_main_rejects_stringly_typed_create_if_missing(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": "dest.md", "create_if_missing": "true"}
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`create_if_missing` must be a boolean" in err


def test_main_rejects_delete_and_create_if_missing_together(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": "x.md", "delete": True, "create_if_missing": True}
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_main_rejects_bare_scalar_target(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": ["just a string, not a mapping"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "malformed target entry" in err


def test_main_rejects_dot_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "."}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1


def test_main_rejects_destination_escaping_consumer_root(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "../escape.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination escapes" in err


def test_main_rejects_source_escaping_upstream_root(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "../etc/passwd", "destination": "x.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "source escapes upstream repo" in err


def test_main_rejects_mode_on_delete_target(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "x.md", "delete": True, "mode": "0755"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`mode` is not valid on a delete target" in err


def test_main_skip_targets_by_source(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md"}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"skip_targets": ["src.md"]})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert not (consumer_dir / "dest.md").exists()
    assert "skip" in capsys.readouterr().out


def test_main_skip_delete_target_by_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A consumer that wants to OPT OUT of a retirement (keep the
    # upstream-flagged-for-deletion file) puts the destination into
    # `skip_targets`. The file must stay on disk and `skipped` counts up.
    (consumer_dir / "kept.md").write_text("consumer wants to keep this\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "kept.md", "delete": True}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"skip_targets": ["kept.md"]})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "kept.md").read_text() == "consumer wants to keep this\n"
    out = capsys.readouterr().out
    assert "skip kept.md" in out


def test_main_create_if_missing_bootstraps_first_time(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "src.md").write_text("initial content")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "out.md").read_text() == "initial content"


def test_main_create_if_missing_preserves_consumer_edits(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("upstream content")
    (consumer_dir / "out.md").write_text("CONSUMER EDIT")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    # Consumer's edit must survive — that's the whole point of create_if_missing.
    assert (consumer_dir / "out.md").read_text() == "CONSUMER EDIT"
    assert "preserved" in capsys.readouterr().out


def test_main_create_if_missing_preserves_dangling_symlink(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dangling symlink counts as "present" for create_if_missing, just
    # like in the delete branch — symmetry between the two boolean branches.
    (upstream_repo / "src.md").write_text("upstream")
    dangling = consumer_dir / "out.md"
    dangling.symlink_to(consumer_dir / "absent")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert dangling.is_symlink()  # untouched


def test_main_create_if_missing_refuses_directory(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("upstream")
    (consumer_dir / "out.md").mkdir()
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination is a directory" in err


def test_main_missing_required_substitution_exits_1(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "src.md").write_text("hello <<NAME>>")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md", "substitutions": ["NAME"]}]},
    )

    # substitute() calls sys.exit(1) on missing required — that bubbles up
    # through main().
    with pytest.raises(SystemExit) as exc:
        _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert exc.value.code == 1


def test_main_applies_mode_to_copied_file(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "script.sh").write_text("#!/bin/sh\necho hi\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "script.sh", "destination": "out.sh", "mode": "0755"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert stat.S_IMODE((consumer_dir / "out.sh").stat().st_mode) == 0o755


def test_main_dry_run_does_not_write(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("hello")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch, dry_run=True)
    assert rc == 0
    assert not (consumer_dir / "dest.md").exists()
    out = capsys.readouterr().out
    assert "would write dest.md" in out


def test_main_dry_run_reports_mode_only_diff(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "script.sh").write_text("#!/bin/sh\n")
    (consumer_dir / "out.sh").write_text("#!/bin/sh\n")
    # Initial mode 0o600 (owner-only) so this test fixture stays under
    # CodeQL's overly-permissive-mode rule. The test's invariant is
    # mode-divergence detection — any initial mode that differs from
    # the target's 0o755 manifest entry exercises the dry-run reporter.
    os.chmod(consumer_dir / "out.sh", 0o600)
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "script.sh", "destination": "out.sh", "mode": "0755"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "would write out.sh (mode)" in out
    assert stat.S_IMODE((consumer_dir / "out.sh").stat().st_mode) == 0o600  # not actually changed


def test_main_missing_source_file_returns_1(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "missing.md", "destination": "dest.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "source missing in upstream" in err


def test_main_rejects_top_level_targets_not_a_list(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": {"src.md": "dest.md"}},  # mapping, not list
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`targets` must be a list" in err


# ---------------------------------------------------------------------------
# glob_to_regex + path_matches_any — pattern matcher semantics
# ---------------------------------------------------------------------------


def test_glob_to_regex_literal_path(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".github/copilot-instructions.md")
    assert pat.match(".github/copilot-instructions.md")
    assert not pat.match(".github/copilot-instructions.md.template")
    assert not pat.match("prefix/.github/copilot-instructions.md")


def test_glob_to_regex_single_star_does_not_cross_slash(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".claude/skills/*")
    assert pat.match(".claude/skills/critique")
    # `*` must NOT match across `/` segments — otherwise an allowlist of
    # `.claude/skills/*` would cover `.claude/skills/critique/SKILL.md` too.
    assert not pat.match(".claude/skills/critique/SKILL.md")


def test_glob_to_regex_double_star_crosses_slashes(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".claude/skills/**")
    assert pat.match(".claude/skills/critique")
    assert pat.match(".claude/skills/critique/SKILL.md")
    assert pat.match(".claude/skills/critique/scripts/run.sh")
    # Must not bleed past the prefix.
    assert not pat.match(".claude/agents/foo.md")


def test_glob_to_regex_double_star_in_middle(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".claude/skills/**/SKILL.md")
    assert pat.match(".claude/skills/critique/SKILL.md")
    assert pat.match(".claude/skills/issues/scripts/SKILL.md")
    # `**` matches zero segments too — direct child should match.
    assert pat.match(".claude/skills/SKILL.md")
    assert not pat.match(".claude/skills/critique/run.sh")


def test_glob_to_regex_question_mark(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex("Dockerfile.?")
    assert pat.match("Dockerfile.a")
    assert pat.match("Dockerfile.1")
    assert not pat.match("Dockerfile.ab")
    assert not pat.match("Dockerfile.")  # `?` requires exactly one char


def test_glob_to_regex_anchored_at_both_ends(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".github/workflows/sync.yml")
    # Must not match suffix or prefix injection.
    assert not pat.match("foo/.github/workflows/sync.yml")
    assert not pat.match(".github/workflows/sync.yml.bak")


def test_glob_to_regex_escapes_regex_metachars(sync_engine: ModuleType) -> None:
    # The pattern contains `.` (regex any-char) and `+` (regex quantifier).
    # Both must be treated as literal.
    pat = sync_engine.glob_to_regex("a.b+c")
    assert pat.match("a.b+c")
    assert not pat.match("axbc")  # `.` literal, not any-char
    assert not pat.match("a.bbc")  # `+` literal, not quantifier


def test_path_matches_any_empty_list_returns_false(sync_engine: ModuleType) -> None:
    assert sync_engine.path_matches_any("any/path.md", []) is False


def test_path_matches_any_matches_on_any_pattern(sync_engine: ModuleType) -> None:
    patterns = [
        sync_engine.glob_to_regex(".claude/**"),
        sync_engine.glob_to_regex(".codex/**"),
    ]
    assert sync_engine.path_matches_any(".claude/skills/critique/SKILL.md", patterns)
    assert sync_engine.path_matches_any(".codex/skills/critique/SKILL.md", patterns)
    assert not sync_engine.path_matches_any(".github/workflows/release.yml", patterns)


# ---------------------------------------------------------------------------
# allowed_destinations + SENSITIVE_DELETE_PATTERNS — main() enforcement
# ---------------------------------------------------------------------------


def test_main_allowlist_match_permits_write(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "skill.md").write_text("skill content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "skill.md", "destination": ".claude/skills/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").read_text() == "skill content\n"


def test_main_allowlist_refuses_out_of_list_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The red-team scenario: an upstream-authored manifest tries to overwrite
    # the consumer's release workflow. The allowlist (consumer-side opt-in)
    # refuses before any filesystem change.
    (upstream_repo / "template.md").write_text("malicious payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "template.md",
                    "destination": ".github/workflows/release.yml",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**", ".github/copilot-instructions.md"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert ".github/workflows/release.yml" in err
    assert not (consumer_dir / ".github" / "workflows" / "release.yml").exists()


def test_main_allowlist_absent_warns_and_proceeds_migration(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Migration semantics: when `allowed_destinations` is absent from the
    # consumer config, the engine warns but does NOT refuse — otherwise
    # every consumer's first post-deployment sync would break before they
    # had a chance to ship their allowlist. The warning is the signal for
    # the consumer to add the field.
    (upstream_repo / "src.md").write_text("content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": ".claude/skills/foo.md"}]},
    )
    # Default consumer_dir fixture has empty .platform-config.yml (no
    # `allowed_destinations` key) — exactly the pre-migration state.

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").read_text() == "content\n"
    err = capsys.readouterr().err
    assert "`allowed_destinations` not set" in err
    assert "fail-closed" in err  # migration pointer


def test_main_empty_allowlist_refuses_everything(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An empty list is a real value, not the same as the key being absent.
    # It expresses "this consumer is locked — refuse any upstream write."
    (upstream_repo / "src.md").write_text("content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": ".claude/foo.md"}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"allowed_destinations": []})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err


def test_main_allowlist_rejects_non_list_type(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml", {"targets": []}
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ".claude/**"},  # string, not list
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`allowed_destinations` must be a list of strings" in err


def test_main_allowlist_rejects_non_string_element(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml", {"targets": []}
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**", 42]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`allowed_destinations` must be a list of strings" in err


def test_main_sensitive_delete_refused_even_when_allowlisted(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The red-team scenario: a consumer legitimately syncs CI workflows
    # (so `.github/workflows/**` IS in their allowlist), but the engine
    # must still refuse to delete one — the allowlist permits writes, the
    # engine-level constant prohibits delete entries against guardrails.
    workflow_path = consumer_dir / ".github" / "workflows" / "ci.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("name: CI\non: push\n")

    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": ".github/workflows/ci.yml", "delete": True}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err
    assert workflow_path.exists()  # untouched


def test_main_sensitive_copy_allowed_when_opted_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Copying a workflow in from upstream stays a supported operation —
    # it is how consumers sync their CI — but it now needs the consumer
    # to have named that exact file in `allow_sensitive_writes`. The
    # directory grant in `allowed_destinations` is necessary and no
    # longer sufficient.
    (upstream_repo / "ci.yml.template").write_text("name: CI\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "ci.yml.template",
                    "destination": ".github/workflows/ci.yml",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/**"],
            "allow_sensitive_writes": [".github/workflows/ci.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".github" / "workflows" / "ci.yml").read_text() == "name: CI\non: push\n"


def test_main_sensitive_delete_lockfiles_and_dockerfile(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Coverage for the non-workflow entries in SENSITIVE_DELETE_PATTERNS:
    # package.json, pnpm-lock.yaml, prisma/schema.prisma, Dockerfile.
    for path in ("package.json", "pnpm-lock.yaml", "Dockerfile"):
        (consumer_dir / path).write_text("placeholder")

    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": "package.json", "delete": True},
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["package.json", "pnpm-lock.yaml", "Dockerfile"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err
    assert "package.json" in err
    assert (consumer_dir / "package.json").exists()


def test_main_allowlist_applies_to_delete_target_too(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Deletion targets a path the consumer never allowlisted — refuse via
    # the allowlist check (NOT the sensitive-path check, since `.docs/old.md`
    # isn't in SENSITIVE_DELETE_PATTERNS). This proves the allowlist gates
    # both writes AND deletes uniformly.
    (consumer_dir / ".docs").mkdir()
    (consumer_dir / ".docs" / "old.md").write_text("dead content")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": ".docs/old.md", "delete": True}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},  # .docs not in list
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert (consumer_dir / ".docs" / "old.md").exists()


def test_main_allowlist_applies_to_create_if_missing_target_too(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # create_if_missing still writes the file on first sync — the allowlist
    # must gate it too. Otherwise a manifest entry like
    # `{source: prompt.txt.template, destination: .env, create_if_missing: true}`
    # would bootstrap a destination path the consumer never opted in to.
    (upstream_repo / "tmpl.txt").write_text("bootstrap\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "tmpl.txt",
                    "destination": ".env",
                    "create_if_missing": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert not (consumer_dir / ".env").exists()


def test_main_allowlist_dual_prefix_for_dual_upstream_consumer(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Consumers that subscribe to both claude-platform and codex-platform
    # need both prefixes in their allowlist. A single sync run still hits
    # one upstream at a time; this test exercises the dual-prefix
    # allowlist's behavior against a claude-style target.
    (upstream_repo / "src.md").write_text("claude content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": ".claude/skills/foo.md"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**", ".codex/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").exists()


def test_main_allowlist_checked_before_source_read(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Defense-in-depth: even if `source` is missing in upstream, the
    # allowlist check fires first so the operator gets a policy-violation
    # message instead of a confusing "source missing in upstream" — the
    # latter would suggest the right fix is to add the file rather than to
    # refuse the manifest entry.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "never-existed.md",
                    "destination": ".github/workflows/release.yml",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert "source missing in upstream" not in err


# ---------------------------------------------------------------------------
# Adversarial path forms — destinations that exploit normalization seams
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_destination",
    [
        "./.github/workflows/release.yml",
        ".github/./workflows/release.yml",
        ".github//workflows/release.yml",
        "foo/../.github/workflows/release.yml",
        ".github/workflows/../workflows/release.yml",
    ],
)
def test_main_refuses_non_canonical_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_destination: str,
) -> None:
    # `resolve_under` collapses `./`, `//`, and `foo/../` so the on-disk
    # write target is canonical — but allowlist + sensitive-delete patterns
    # match the manifest's `destination` string. A non-canonical destination
    # would otherwise resolve to a guarded path on disk while bypassing the
    # anchored pattern matchers. The engine refuses outright.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": bad_destination}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "canonical posix form" in err
    assert not (consumer_dir / ".github" / "workflows" / "release.yml").exists()


def test_main_refuses_non_canonical_delete_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The delete variant of the same bypass attack: an allowlist that
    # legitimately covers `.github/workflows/**` still must not allow
    # `./.github/workflows/ci.yml` to slip past the sensitive-delete
    # check via its raw-string mismatch.
    workflow = consumer_dir / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": "./.github/workflows/ci.yml", "delete": True}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "canonical posix form" in err
    assert workflow.exists()


def test_main_refuses_control_char_in_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `[^/]*` in the glob compiler matches newlines, so an allowlist of
    # `.claude/skills/*` would otherwise accept `.claude/skills/foo\nbar`
    # as a valid destination — a file that sync-diff review by eye
    # could easily miss.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": ".claude/skills/foo\nbar"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/skills/*"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be a non-empty printable path string" in err


def test_main_sensitive_delete_case_insensitive(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # On case-insensitive filesystems (macOS APFS, NTFS), `dockerfile`
    # resolves to the same on-disk file as `Dockerfile`. The sensitive-
    # delete regexes are compiled with `re.IGNORECASE` so the lowercase
    # spelling is refused too, even though fleet sync runs on case-
    # sensitive Linux today (defense-in-depth for self-hosted runners).
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "dockerfile", "delete": True}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["dockerfile", "Dockerfile"]},
    )
    (consumer_dir / "dockerfile").write_text("FROM scratch\n")

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err


def test_main_sensitive_delete_blocks_github_actions(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `.github/actions/**` was added to SENSITIVE_DELETE_PATTERNS so a
    # manifest entry can't remove a composite action that a still-extant
    # workflow depends on — equivalent to removing the workflow itself.
    action_path = consumer_dir / ".github" / "actions" / "build" / "action.yml"
    action_path.parent.mkdir(parents=True)
    action_path.write_text("name: build\nruns: { using: composite }\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "destination": ".github/actions/build/action.yml",
                    "delete": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/actions/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err
    assert action_path.exists()


def test_main_sensitive_delete_blocks_codeowners(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # CODEOWNERS deletion bypasses required-reviewer gates; treat it as
    # a guardrail equivalent to a CI workflow.
    (consumer_dir / ".github").mkdir()
    (consumer_dir / ".github" / "CODEOWNERS").write_text("* @platform\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": ".github/CODEOWNERS", "delete": True}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/CODEOWNERS"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err


# ---------------------------------------------------------------------------
# Fail-open semantics — distinguishing missing key from null value
# ---------------------------------------------------------------------------


def test_main_allowlist_null_value_is_config_error(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `allowed_destinations:` with no value parses to None. That's almost
    # certainly a mid-edit accident — the consumer thinks they've turned
    # on the gate, but the engine would silently treat it as fail-open.
    # Hard-fail so the operator sees the problem rather than discovering
    # weeks later that their allowlist was never enforced.
    (upstream_repo / "src.md").write_text("payload\n")
    (upstream_repo / "scripts" / "sync-targets.yml").write_text(
        "targets:\n  - source: src.md\n    destination: .claude/foo.md\n"
    )
    (consumer_dir / ".platform-config.yml").write_text("allowed_destinations:\n")

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "present but null" in err


def test_main_fail_open_warning_uses_github_annotation(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Fail-open warning must surface in the GitHub PR UI via the
    # `::warning::` annotation prefix — otherwise a green-checkmark
    # build buries the migration prompt in stderr where nobody looks.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml", {"targets": []}
    )
    # Consumer .platform-config.yml has no `allowed_destinations` key.

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    err = capsys.readouterr().err
    assert "::warning" in err
    assert "allowed_destinations" in err


# ---------------------------------------------------------------------------
# Skip + allowlist coexistence and OR-semantics across patterns
# ---------------------------------------------------------------------------


def test_main_skip_target_short_circuits_allowlist(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Consumer opt-out via `skip_targets` fires BEFORE the allowlist
    # check — a skipped target's destination need not be in
    # `allowed_destinations`. This protects consumers that locally
    # diverge from an upstream-managed file (e.g., the agent-loop
    # skill on platform) without forcing them to allowlist a path
    # they've explicitly opted out of.
    (upstream_repo / "skill.md").write_text("upstream content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "skill.md",
                    "destination": ".claude/skills/local/SKILL.md",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "skip_targets": [".claude/skills/local/SKILL.md"],
            "allowed_destinations": [".claude/skills/permitted/**"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "skip" in out.lower()
    assert not (consumer_dir / ".claude" / "skills" / "local" / "SKILL.md").exists()


def test_main_allowlist_matches_second_pattern_in_list(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Allowlist semantics are OR (any pattern matches). Cover the
    # second-pattern-only case end-to-end through enforcement, in case a
    # future refactor accidentally turns the iteration into AND or only
    # consults the first pattern.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": ".claude/skills/foo.md"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [
                ".github/copilot-instructions.md",  # doesn't match
                ".claude/**",  # matches
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").read_text() == "payload\n"


def test_main_create_if_missing_sensitive_path_allowed_for_copy(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `create_if_missing` bootstrap of `.github/workflows/ci.yml` must
    # still succeed on the first sync once the consumer has opted in —
    # otherwise net-new consumers couldn't onboard their CI workflow via
    # sync at all. Consent is required (see the refusal test below), but
    # consent is enough.
    (upstream_repo / "ci.yml.template").write_text("name: CI\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "ci.yml.template",
                    "destination": ".github/workflows/ci.yml",
                    "create_if_missing": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/**"],
            "allow_sensitive_writes": [".github/workflows/ci.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".github" / "workflows" / "ci.yml").read_text() == "name: CI\non: push\n"


def test_main_allowlist_empty_string_pattern_matches_only_empty(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Empty-string allowlist entry compiles to `\A\Z` (matches only the
    # empty string). It never matches any real destination — refuses
    # everything. Pin this so a future maintainer who "fixes" the
    # empty-prefix edge case with a `pattern or ".*"` fallback doesn't
    # silently convert empty entries into wildcards.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": ".claude/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [""]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err


# ---------------------------------------------------------------------------
# allow_sensitive_writes — per-file consent for sensitive destinations
# ---------------------------------------------------------------------------


def test_main_sensitive_overwrite_refused_without_opt_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The scenario the gate exists for. The consumer followed the
    # documented onboarding path, so `.github/workflows/` is in their
    # allowlist because the canonical manifest ships `dco.yml`. A
    # manifest entry then aims a different payload at an existing
    # workflow. Deleting that workflow was already refused; rewriting it
    # is the higher-impact operation, because the rewrite *runs* — with
    # the consumer's secrets and the manifest's `permissions:` block.
    workflow = consumer_dir / ".github" / "workflows" / "release.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: Release\non: push\n")

    (upstream_repo / "payload.yml").write_text(
        "name: Release\non: push\npermissions: write-all\n"
    )
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "payload.yml", "destination": ".github/workflows/release.yml"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to write sensitive path without an explicit opt-in" in err
    # The error has to be actionable on its own — the consumer should not
    # need the docs open to fix it.
    assert "allow_sensitive_writes" in err
    assert ".github/workflows/release.yml" in err
    assert workflow.read_text() == "name: Release\non: push\n"  # untouched


def test_main_sensitive_write_opt_in_is_per_file(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Consenting to the file the manifest legitimately ships (`dco.yml`)
    # must not carry over to a sibling in the same directory. If it did,
    # the opt-in would be a directory grant wearing a different name.
    (upstream_repo / "payload.yml").write_text("name: Evil\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "payload.yml", "destination": ".github/workflows/release.yml"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/**"],
            "allow_sensitive_writes": [".github/workflows/dco.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "refusing to write sensitive path" in capsys.readouterr().err
    assert not (consumer_dir / ".github" / "workflows" / "release.yml").exists()


def test_main_sensitive_create_if_missing_refused_without_opt_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Creation is gated alongside overwrite. A workflow that did not exist
    # before still runs once the manifest authors it, so "the file was
    # absent" cannot be a reason to skip consent — otherwise the gate is
    # trivially bypassed by picking a filename the consumer doesn't use.
    (upstream_repo / "payload.yml").write_text("name: New\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "payload.yml",
                    "destination": ".github/workflows/new.yml",
                    "create_if_missing": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "refusing to write sensitive path" in capsys.readouterr().err
    assert not (consumer_dir / ".github" / "workflows" / "new.yml").exists()


def test_main_sensitive_write_still_bounded_by_allowed_destinations(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The two gates are independent and a destination must clear both.
    # `allow_sensitive_writes` grants consent for an operation; it does
    # not widen the surface `allowed_destinations` bounds.
    (upstream_repo / "payload.yml").write_text("name: CI\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "payload.yml", "destination": ".github/workflows/ci.yml"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".claude/**"],
            "allow_sensitive_writes": [".github/workflows/ci.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "destination not in consumer's `allowed_destinations`" in capsys.readouterr().err


def test_main_sensitive_write_opt_in_does_not_permit_delete(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Consenting to have a file rewritten is not consenting to have it
    # removed. The delete block stays unconditional.
    workflow = consumer_dir / ".github" / "workflows" / "dco.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: DCO\n")

    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": ".github/workflows/dco.yml", "delete": True}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/**"],
            "allow_sensitive_writes": [".github/workflows/dco.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "refusing to delete sensitive path" in capsys.readouterr().err
    assert workflow.exists()


def test_main_sensitive_write_dry_run_refuses_before_reporting(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `--dry-run` must surface the refusal too. A dry run that reports
    # "would write" for a destination the real run rejects would send a
    # consumer to debug the wrong thing.
    (upstream_repo / "payload.yml").write_text("name: CI\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "payload.yml", "destination": ".github/workflows/ci.yml"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch, dry_run=True)
    assert rc == 1
    captured = capsys.readouterr()
    assert "refusing to write sensitive path" in captured.err
    assert "would write" not in captured.out


def test_main_sensitive_write_opt_in_is_case_sensitive(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The block matches case-insensitively (so `dockerfile` can't slip
    # past on APFS/NTFS) while the grant compares exactly. A denial should
    # be broad; a grant should cover only the path actually written down.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "img", "destination": "dockerfile"}]},
    )
    (upstream_repo / "img").write_text("FROM scratch\n")
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": ["**"],
            "allow_sensitive_writes": ["Dockerfile"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "refusing to write sensitive path" in capsys.readouterr().err


def test_main_sensitive_write_reports_opted_in_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An opted-in sensitive write is legitimate but still worth seeing.
    # A reviewer of the sync PR should be able to tell from the job log
    # that the run rewrote a workflow, without reading the diff.
    (upstream_repo / "dco.yml").write_text("name: DCO\non: pull_request\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "dco.yml", "destination": ".github/workflows/dco.yml"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/dco.yml"],
            "allow_sensitive_writes": [".github/workflows/dco.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "sensitive destination .github/workflows/dco.yml" in out
    assert "1 sensitive destination(s) permitted by `allow_sensitive_writes`" in out


def test_main_ordinary_destination_needs_no_sensitive_opt_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The gate must not touch the paths the manifest actually spends its
    # time on. A skill file is not sensitive, so it syncs with no opt-in
    # and no extra output.
    (upstream_repo / "skill.md").write_text("skill\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "skill.md", "destination": ".claude/skills/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "sensitive destination" not in out
    assert "sensitive destination(s) permitted" not in out


def test_main_sensitive_write_allowlist_rejects_glob_entry(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A glob here would rebuild exactly the hole the gate closes: consent
    # inherited from a directory pattern rather than given per file.
    (upstream_repo / "payload.yml").write_text("name: CI\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "payload.yml", "destination": ".github/workflows/ci.yml"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/**"],
            "allow_sensitive_writes": [".github/workflows/**"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be literal paths, not globs" in err
    assert not (consumer_dir / ".github" / "workflows" / "ci.yml").exists()


def test_main_sensitive_write_allowlist_rejects_non_canonical_entry(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Destinations are compared as canonical strings, so a non-canonical
    # opt-in would never match and would read as an effective grant that
    # silently isn't one.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "x", "destination": ".github/workflows/ci.yml"}]},
    )
    (upstream_repo / "x").write_text("name: CI\n")
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/**"],
            "allow_sensitive_writes": ["./.github/workflows/ci.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "must be canonical repo-relative posix paths" in capsys.readouterr().err


def test_main_sensitive_write_allowlist_rejects_escaping_entry(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "x", "destination": ".github/workflows/ci.yml"}]},
    )
    (upstream_repo / "x").write_text("name: CI\n")
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/**"],
            "allow_sensitive_writes": ["../elsewhere/Dockerfile"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "must be canonical repo-relative posix paths" in capsys.readouterr().err


def test_main_sensitive_write_allowlist_rejects_non_sensitive_entry(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Catches the typo case at parse time. `.github/workflow/dco.yml` (no
    # `s`) would otherwise parse cleanly and leave the real destination
    # unauthorized — the consumer would then be staring at a config that
    # names the path alongside a refusal to write it.
    (upstream_repo / "skill.md").write_text("skill\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "skill.md", "destination": ".claude/skills/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".claude/**"],
            "allow_sensitive_writes": [".github/workflow/dco.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "is not a sensitive path" in capsys.readouterr().err


def test_main_sensitive_write_allowlist_null_is_config_error(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Mirrors the `allowed_destinations:` null case — a bare key with no
    # value is a mid-edit accident, and `[]` is the explicit way to say
    # "nothing is permitted."
    (upstream_repo / "skill.md").write_text("skill\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "skill.md", "destination": ".claude/skills/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"], "allow_sensitive_writes": None},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "`allow_sensitive_writes:` is present but null" in capsys.readouterr().err


def test_main_sensitive_write_allowlist_rejects_non_list(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "skill.md").write_text("skill\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "skill.md", "destination": ".claude/skills/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".claude/**"],
            "allow_sensitive_writes": ".github/workflows/dco.yml",
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "`allow_sensitive_writes` must be a list of strings" in capsys.readouterr().err


def test_main_sensitive_write_empty_list_denies(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `[]` and an absent key mean the same thing for this gate — there is
    # no fail-open migration phase to distinguish them. `[]` exists so a
    # consumer can state the decision rather than leave it implied.
    (upstream_repo / "payload.yml").write_text("name: CI\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "payload.yml", "destination": ".github/workflows/ci.yml"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"], "allow_sensitive_writes": []},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "refusing to write sensitive path" in capsys.readouterr().err


def test_main_sensitive_write_covers_lockfile_and_codeowners(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The non-workflow half of the set. Rewriting CODEOWNERS removes the
    # review gate without deleting anything; rewriting a lockfile is a
    # supply-chain edit CI installs on the next run.
    for destination in (".github/CODEOWNERS", "pnpm-lock.yaml", "package.json"):
        (upstream_repo / "payload").write_text("payload\n")
        _write_yaml(
            upstream_repo / "scripts" / "sync-targets.yml",
            {"targets": [{"source": "payload", "destination": destination}]},
        )
        _write_yaml(
            consumer_dir / ".platform-config.yml",
            {"allowed_destinations": ["**"]},
        )

        rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
        assert rc == 1, destination
        err = capsys.readouterr().err
        assert "refusing to write sensitive path" in err
        assert destination in err
        assert not (consumer_dir / destination).exists()


def test_sensitive_write_patterns_cover_the_delete_set(sync_engine: ModuleType) -> None:
    # The two sets are the same object today. Pin that: a path added to
    # the delete set because its absence breaks an invariant is, so far
    # without exception, also a path whose contents control execution,
    # review gating, or dependency resolution. If a future change makes
    # them genuinely diverge, this assertion is the place to record why.
    assert set(sync_engine.SENSITIVE_DELETE_PATTERNS) <= set(
        sync_engine.SENSITIVE_WRITE_PATTERNS
    )


def test_sensitive_write_patterns_are_all_delete_protected(
    sync_engine: ModuleType,
) -> None:
    # This is the direction that carries the pre-pass's atomicity guarantee,
    # and it is not the same claim as the assertion above.
    #
    # `unconsented_sensitive_writes` skips a `create_if_missing` target whose
    # destination already exists, on the assumption it will still be there
    # when the loop arrives. The only way it disappears mid-run is an earlier
    # `delete:` target — refused only when the path is in the *delete* set. A
    # write-sensitive path that is not delete-protected can therefore be
    # removed after admission control has cleared the run, dropping its target
    # into the in-loop gate after a write and a delete have already landed:
    # the mid-run abort the pre-pass exists to prevent.
    #
    # Both hold trivially while `SENSITIVE_WRITE_PATTERNS` aliases the delete
    # tuple. This one is what must survive the split that alias's comment
    # invites.
    assert set(sync_engine.SENSITIVE_WRITE_PATTERNS) <= set(
        sync_engine.SENSITIVE_DELETE_PATTERNS
    )


# ---------------------------------------------------------------------------
# The gate must track writes, not targets
# ---------------------------------------------------------------------------


def test_main_sensitive_write_not_reported_when_nothing_changes(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The steady-state case, which is what the daily cron actually does.
    # The destination is already byte-identical, so no write happens and
    # the run must not claim one. The audit line exists so a reviewer can
    # read the job log and conclude "this run rewrote a workflow" — if it
    # fires on every no-op sync, the false positive becomes the common
    # case and the real signal stops being read.
    workflow = consumer_dir / ".github" / "workflows" / "dco.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: DCO\non: pull_request\n")
    (upstream_repo / "dco.yml").write_text("name: DCO\non: pull_request\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "dco.yml", "destination": ".github/workflows/dco.yml"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [".github/workflows/dco.yml"],
            "allow_sensitive_writes": [".github/workflows/dco.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "sensitive destination" not in out
    assert "sensitive destination(s) permitted" not in out
    assert "1 unchanged" in out


def test_main_sensitive_create_if_missing_preserved_needs_no_opt_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `create_if_missing` with the destination already present never
    # writes — docs/sync.md promises the engine "skips the source read,
    # substitution, and write entirely". Demanding consent to write a file
    # the engine has permanently committed to leaving alone breaks that
    # contract and fails every steady-state consumer on the retag.
    workflow = consumer_dir / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\non: push\njobs:\n  consumer-customized: {}\n")
    (upstream_repo / "ci.yml.template").write_text("name: CI\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "ci.yml.template",
                    "destination": ".github/workflows/ci.yml",
                    "create_if_missing": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "preserved .github/workflows/ci.yml (create_if_missing)" in out
    # Consumer customization survives untouched.
    assert "consumer-customized" in workflow.read_text()


def test_main_sensitive_write_refused_before_any_target_is_written(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # docs/sync.md promises "Nothing is written when the gate trips".
    # The refusal must therefore be admission control, not a mid-loop
    # abort: the canonical manifest puts `.github/workflows/dco.yml` last,
    # so an un-opted-in consumer would otherwise take the entire sync
    # before the refusal — the maximum-damage ordering, not the minimum.
    (upstream_repo / "a.md").write_text("NEW upstream a\n")
    (upstream_repo / "dco.yml").write_text("name: DCO\non: pull_request\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "a.md", "destination": "docs/a.md"},
                {"source": "dco.yml", "destination": ".github/workflows/dco.yml"},
            ]
        },
    )
    ordinary = consumer_dir / "docs" / "a.md"
    ordinary.parent.mkdir(parents=True)
    ordinary.write_text("OLD consumer a\n")
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to write sensitive path" in err
    # The ordinary target ahead of the sensitive one is untouched.
    assert ordinary.read_text() == "OLD consumer a\n"
    assert not (consumer_dir / ".github" / "workflows" / "dco.yml").exists()


def test_main_sensitive_write_refusal_lists_every_denied_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A consumer adopting the gate should get one complete list to paste
    # into their config, not one path per red run.
    (upstream_repo / "dco.yml").write_text("name: DCO\n")
    (upstream_repo / "release.yml").write_text("name: Release\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "dco.yml", "destination": ".github/workflows/dco.yml"},
                {
                    "source": "release.yml",
                    "destination": ".github/workflows/release.yml",
                },
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert ".github/workflows/dco.yml" in err
    assert ".github/workflows/release.yml" in err


def test_main_sensitive_write_skipped_target_needs_no_opt_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `skip_targets` is how a private consumer opts out of the DCO
    # workflow. A target that is never processed must not require consent
    # to write — otherwise opting out becomes impossible without also
    # granting the write the opt-out exists to avoid.
    (upstream_repo / "dco.yml").write_text("name: DCO\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "dco.yml", "destination": ".github/workflows/dco.yml"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": ["**"],
            "skip_targets": [".github/workflows/dco.yml"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    # The skip line is labelled by source (`source_rel or dest_rel`).
    assert "skip dco.yml (opted out via .platform-config.yml)" in out
    assert "1 skipped" in out
    assert not (consumer_dir / ".github" / "workflows" / "dco.yml").exists()


# ---------------------------------------------------------------------------
# Sensitive patterns match at any depth, not just repository root
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "destination",
    [
        "package.json",
        "apps/web/package.json",
        "pnpm-lock.yaml",
        "packages/api/pnpm-lock.yaml",
        "Dockerfile",
        "services/api/Dockerfile",
        "Dockerfile.prod",
        "api/Dockerfile.prod",
        "prisma/schema.prisma",
        "db/prisma/schema.prisma",
        "CODEOWNERS",
        ".github/CODEOWNERS",
        "docs/CODEOWNERS",
        ".github/workflows/ci.yml",
        ".github/actions/setup/action.yml",
    ],
)
def test_sensitive_patterns_match_at_any_depth(
    sync_engine: ModuleType, destination: str
) -> None:
    # `glob_to_regex` anchors both ends, so a bare `package.json` entry would
    # cover the repository root and nothing else. A workspace-shaped consumer
    # keeps exactly these files one or two directories down, which is where
    # the gate is most needed. CODEOWNERS matters most: GitHub resolves it
    # from the root, `.github/`, and `docs/`, so gating one location would
    # leave the review gate rewritable through the other two.
    assert sync_engine.path_matches_any(
        destination, sync_engine.SENSITIVE_WRITE_REGEXES
    ), destination
    assert sync_engine.path_matches_any(
        destination, sync_engine.SENSITIVE_DELETE_REGEXES
    ), destination


@pytest.mark.parametrize(
    "destination",
    [
        "src/index.ts",
        "README.md",
        ".claude/settings.json",
        # Widening to `**/` must not start matching on substrings or
        # neighbouring extensions.
        "docs/package.json.md",
        "package.json.bak",
        "my-package.json",
        "notprisma/schema.prisma",
        "Dockerfilex",
        "docs/workflows/ci.yml",
    ],
)
def test_sensitive_patterns_do_not_over_match(
    sync_engine: ModuleType, destination: str
) -> None:
    assert not sync_engine.path_matches_any(
        destination, sync_engine.SENSITIVE_WRITE_REGEXES
    ), destination


def test_main_nested_package_json_needs_opt_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The end-to-end shape of the gap: a monorepo destination that cleared
    # both the delete block and the write gate while the patterns were
    # root-anchored. A rewritten workspace manifest is a supply-chain edit
    # the consumer's next install picks up.
    (upstream_repo / "pkg.json").write_text('{"name": "x"}\n')
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "pkg.json",
                    "destination": "apps/web/package.json",
                    "substitutions": [],
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to write sensitive path" in err
    assert "apps/web/package.json" in err
    assert not (consumer_dir / "apps" / "web" / "package.json").exists()


def test_main_root_codeowners_needs_opt_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # GitHub honours root `CODEOWNERS` when `.github/CODEOWNERS` is absent,
    # so writing it installs or replaces the review gate — the exact harm
    # this PR cites as its reason to gate writes rather than only deletes.
    (upstream_repo / "owners").write_text("* @someone\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "owners", "destination": "CODEOWNERS", "substitutions": []}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to write sensitive path" in err
    assert "CODEOWNERS" in err
    assert not (consumer_dir / "CODEOWNERS").exists()


def test_main_nested_sensitive_write_allowed_when_opted_in(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Widening the patterns must not make a nested destination unsyncable —
    # the canonical manifest ships `.claude/skills/critique/scripts/package.json`,
    # so this is the path every consumer now has to name.
    (upstream_repo / "pkg.json").write_text('{"name": "x"}\n')
    dest = ".claude/skills/critique/scripts/package.json"
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "pkg.json", "destination": dest, "substitutions": []}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"], "allow_sensitive_writes": [dest]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert f"sensitive destination {dest}" in out
    assert (consumer_dir / dest).read_text() == '{"name": "x"}\n'


def test_main_sensitive_write_refusal_is_one_pasteable_yaml_block(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The complete-list promise is only worth anything if the list pastes as
    # a unit. One `allow_sensitive_writes:` mapping per denied path looks
    # helpful and parses badly: duplicate YAML keys, `safe_load` keeps the
    # last, and the next run refuses a path the config visibly names.
    # Asserting both paths appear in the text does not catch that — this
    # asserts the emitted block round-trips through the parser.
    (upstream_repo / "dco.yml").write_text("name: DCO\n")
    (upstream_repo / "release.yml").write_text("name: Release\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "dco.yml", "destination": ".github/workflows/dco.yml"},
                {
                    "source": "release.yml",
                    "destination": ".github/workflows/release.yml",
                },
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err

    assert err.count("allow_sensitive_writes:") == 1

    # Lift the block verbatim and append it to a config that already has
    # top-level keys, which is the only paste a consumer ever performs.
    #
    # Slice on whole lines, not on the offset of the key's first character.
    # `err.index("allow_sensitive_writes:")` starts *after* whatever
    # indentation precedes the key, so it silently re-dedents the block and
    # strips the exact defect this test is named for — an indented grant
    # block pasted into a real config either raises ParserError or nests
    # silently under the preceding key. Anchoring on the newline keeps
    # column-zero placement load-bearing here.
    assert "\nallow_sensitive_writes:" in err, (
        "the grant block must start at column zero to survive being pasted "
        "into a config that already has top-level keys"
    )
    block = err[err.index("\nallow_sensitive_writes:") + 1 :]
    existing = 'substitutions:\n  FOO: bar\n\nallowed_destinations:\n  - "**"\n'
    assert yaml.safe_load(existing + block)["allow_sensitive_writes"] == [
        ".github/workflows/dco.yml",
        ".github/workflows/release.yml",
    ]


def test_main_sensitive_refusal_block_carries_existing_grants(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The steady state after the fleet migration: the consumer already holds
    # a grant and upstream adds a second sensitive destination. A block
    # listing only the *new* path is a second occurrence of a key the config
    # already has — `safe_load` keeps the last, so following the instruction
    # discards the existing grant and the next run refuses a path the config
    # visibly names. Pasting again refuses the other one, forever.
    (upstream_repo / "dco.yml").write_text("name: DCO\n")
    (upstream_repo / "pkg.json").write_text('{"type": "module"}\n')
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "dco.yml", "destination": ".github/workflows/dco.yml"},
                {"source": "pkg.json", "destination": "apps/web/package.json"},
            ]
        },
    )
    existing = (
        'substitutions:\n  FOO: bar\n\nallowed_destinations:\n  - "**"\n'
        "allow_sensitive_writes:\n  - .github/workflows/dco.yml\n"
    )
    (consumer_dir / ".platform-config.yml").write_text(existing)

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.count("allow_sensitive_writes:") == 1

    # Appending the emitted block is the worst thing a consumer can do with
    # it, so it is what the assertion has to survive: the duplicate key wins,
    # and it must carry both grants rather than only the new one.
    assert "\nallow_sensitive_writes:" in err, (
        "the grant block must start at column zero to survive being pasted "
        "into a config that already has top-level keys"
    )
    block = err[err.index("\nallow_sensitive_writes:") + 1 :]
    assert yaml.safe_load(existing + block)["allow_sensitive_writes"] == [
        ".github/workflows/dco.yml",
        "apps/web/package.json",
    ]


def test_main_config_destination_refused_and_not_grantable(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The consent store cannot be governed by the consent it stores. A
    # manifest able to rewrite `.platform-config.yml` can write its own
    # `allow_sensitive_writes` entry and then, on the next run, write any
    # sensitive path with the gate reporting an opt-in that upstream granted
    # itself. Refused unconditionally, and naming it in the allowlist must
    # not help.
    (upstream_repo / "cfg.yml").write_text(
        "allowed_destinations:\n  - '**'\n"
        "allow_sensitive_writes:\n  - .github/workflows/deploy.yml\n"
    )
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "cfg.yml", "destination": ".platform-config.yml"}]},
    )
    original = "allowed_destinations:\n  - '**'\nallow_sensitive_writes: []\n"
    (consumer_dir / ".platform-config.yml").write_text(original)

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to write the consumer's own sync config" in err
    assert (consumer_dir / ".platform-config.yml").read_text() == original

    # And the refusal is not opt-in-able: naming the config in
    # `allow_sensitive_writes` is rejected at parse time, so there is no
    # spelling of the config that authorizes rewriting the config.
    (consumer_dir / ".platform-config.yml").write_text(
        "allowed_destinations:\n  - '**'\n"
        "allow_sensitive_writes:\n  - .platform-config.yml\n"
    )
    assert _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch) == 1
    assert "which is not a sensitive path" in capsys.readouterr().err


def test_main_in_loop_sensitive_gate_refuses_when_the_pre_pass_misses(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `unconsented_sensitive_writes`' docstring justifies duplicating the
    # consent check in the loop as the fallback for drift: "if the two ever
    # drift, the loop still refuses — it just refuses less atomically."
    # Nothing exercised that fallback, so the whole in-loop gate could be
    # deleted with the suite green.
    #
    # The two agree by construction today, so the drift has to be injected:
    # blind the pre-pass and assert the loop still refuses. This is the
    # documented property, not a hypothetical one — it is the only thing
    # standing between a future pre-pass bug and an unconsented workflow
    # write.
    (upstream_repo / "dco.yml").write_text("name: DCO\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "dco.yml", "destination": ".github/workflows/dco.yml"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"]},
    )
    monkeypatch.setattr(sync_engine, "unconsented_sensitive_writes", lambda *a: [])

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "refusing to write sensitive path" in capsys.readouterr().err
    assert not (consumer_dir / ".github" / "workflows" / "dco.yml").exists()


def test_main_sensitive_directory_destination_refused_before_any_delete(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The pre-pass must mirror the loop, which preserves an existing
    # destination only when it is not a real directory. Treating a directory
    # as "preserved" clears it through admission control — and an earlier
    # delete plus `prune_empty_parents` can remove that directory before the
    # loop arrives, dropping the target into the in-loop gate after the
    # deletion has already landed.
    (upstream_repo / "Dockerfile").write_text("FROM upstream\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": "Dockerfile/child.txt", "delete": True},
                {
                    "source": "Dockerfile",
                    "destination": "Dockerfile",
                    "create_if_missing": True,
                },
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["**"]},
    )
    child = consumer_dir / "Dockerfile" / "child.txt"
    child.parent.mkdir(parents=True)
    child.write_text("consumer child\n")

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    assert "refusing to write sensitive path" in capsys.readouterr().err
    # The tree is untouched, per the guarantee in docs/sync.md.
    assert child.read_text() == "consumer child\n"


def test_render_preserves_unicode_whitespace_separator(sync_engine: ModuleType) -> None:
    text = "a\n\n<<E>>\n\u00a0\nb\n"
    assert sync_engine.substitute(text, {"E": ""}, ["E"], "src.md", ["E"]) == "a\n\n\u00a0\nb\n"


def test_render_treats_crlf_only_value_as_empty(sync_engine: ModuleType) -> None:
    text = "a\r\n\r\n<<E>>\r\n\r\nb\r\n"
    assert sync_engine.substitute(text, {"E": "\r\n"}, ["E"], "src.md", ["E"]) == "a\r\n\r\nb\r\n"


def test_main_preserves_crlf_while_collapsing_an_empty_placeholder(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "template.md").write_bytes(b"a\r\n\r\n<<E>>\r\n\r\nb\r\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "template.md",
                    "destination": "rendered.md",
                    "substitutions": ["E"],
                    "collapse_empty_substitutions": ["E"],
                }
            ]
        },
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"substitutions": {"E": ""}})

    assert _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch) == 0
    assert (consumer_dir / "rendered.md").read_bytes() == b"a\r\n\r\nb\r\n"

