"""Contract for the `simplify-comments` density audit and comment-only verifier."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prompts/skills/simplify-comments/scripts/comment-density.py"
SYMBOLS = {"code": "x", "comment": "#", "blank": "."}


@pytest.fixture(scope="module")
def cd() -> ModuleType:
    spec = importlib.util.spec_from_file_location("comment_density", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolves string annotations
    # through `sys.modules`.
    sys.modules["comment_density"] = module
    spec.loader.exec_module(module)
    return module


def kinds(cd: ModuleType, source: str, ext: str) -> str:
    scan = cd.scan_text(textwrap.dedent(source).lstrip("\n"), cd.LANGUAGES[ext])
    return "".join(SYMBOLS[kind] for kind in scan.kinds)


def fingerprint(cd: ModuleType, source: str, ext: str) -> str:
    scan = cd.scan_text(textwrap.dedent(source).lstrip("\n"), cd.LANGUAGES[ext])
    assert not scan.approximate
    return str(scan.fingerprint)


# --- line classification -----------------------------------------------------


def test_single_line_comments_and_trailing_comments(cd: ModuleType) -> None:
    source = """
        // leading
        const a = 1; // trailing comment keeps the line code

        const url = "http://example.com";
    """
    assert kinds(cd, source, ".ts") == "#x.x"


def test_comment_markers_inside_strings_are_not_comments(cd: ModuleType) -> None:
    source = """
        const s = "/* not a comment";
        const t = '/* nor this';
        const u = 2;
        // real
    """
    assert kinds(cd, source, ".js") == "xxx#"


def test_multi_line_block_comments(cd: ModuleType) -> None:
    source = """
        /**
         * Doc.
         *
         */
        export function f() {} /* trailing */
        /* a */ const b = 1;
        const c = 1; /* opens
        still comment
        */
    """
    assert kinds(cd, source, ".ts") == "####xxx##"


def test_blank_line_inside_block_comment_counts_as_comment(cd: ModuleType) -> None:
    assert kinds(cd, "/*\n\n*/\n\nx();\n", ".ts") == "###.x"


def test_template_literals_with_nested_interpolation(cd: ModuleType) -> None:
    source = """
        const t = `line one
        // inside template
        ${ "}" + `nested // x` } /* still template */`;
        // after
    """
    assert kinds(cd, source, ".ts") == "xxx#"


def test_regex_literals_and_division(cd: ModuleType) -> None:
    source = """
        const re = /[/]\\/*/g; // tail
        const r = a / b; // tail
        // only
    """
    assert kinds(cd, source, ".ts") == "xx#"


def test_go_raw_strings_and_non_nesting_block_comments(cd: ModuleType) -> None:
    source = """
        s := `raw // not
        /* not */`
        /* a /* b */ x := 1
        // done
    """
    assert kinds(cd, source, ".go") == "xxx#"


def test_rust_nested_comments_lifetimes_chars_and_raw_strings(cd: ModuleType) -> None:
    source = """
        /* outer /* inner */ still comment */
        fn f<'a>(x: &'a str) -> char { '"' } // quote
        let s = r#"// not a comment"#;
        /// doc
    """
    assert kinds(cd, source, ".rs") == "#xx#"


def test_nesting_is_per_language(cd: ModuleType) -> None:
    assert kinds(cd, "/* a /* b */ c */\n", ".kt") == "#"
    assert kinds(cd, "/* a /* b */ c */\n", ".java") == "x"


def test_triple_quoted_strings_hide_comment_markers(cd: ModuleType) -> None:
    source = '''
        val s = """
          // inside raw
        """
    '''
    assert kinds(cd, source, ".kt") == "xxx"


def test_python_comments_docstrings_and_strings(cd: ModuleType) -> None:
    source = '''
        #!/usr/bin/env python3
        """Module doc.

        More.
        """
        import os  # trailing

        class A:
            """Class doc."""

            x = 1
            """Attribute doc."""

            def f(self):
                # comment
                s = """not a doc

                still string"""
                return "#not comment"
    '''
    assert kinds(cd, source, ".py") == "#####x.x#.x#.x#xxxx"


def test_unparseable_python_is_marked_approximate(cd: ModuleType) -> None:
    scan = cd.scan_text("def f(:\n    # c\n", cd.LANGUAGES[".py"])
    assert scan.approximate
    assert [k for k in scan.kinds] == ["code", "comment"]


def test_crlf_and_missing_trailing_newline(cd: ModuleType) -> None:
    assert kinds(cd, "// a\r\nx();\r\n// b", ".ts") == "#x#"
    assert kinds(cd, "", ".ts") == ""


def test_density_of_empty_input_is_zero(cd: ModuleType) -> None:
    assert cd.density(0, 0) == 0.0
    assert cd.density(3, 1) == 25.0


# --- fingerprint --------------------------------------------------------------


def test_comment_edits_and_reflow_keep_the_fingerprint(cd: ModuleType) -> None:
    before = """
        // ─── Reconnect ───
        // Fix for #2528: long story.
        await lock.acquire(); // hold it
        foo(
          a, // first
          b,
        );
    """
    after = """
        // Hold the lock across reconnect.
        await lock.acquire();
        foo(a, b);
    """
    assert fingerprint(cd, before, ".ts") == fingerprint(cd, after, ".ts")


def test_code_edits_change_the_fingerprint(cd: ModuleType) -> None:
    assert fingerprint(cd, "foo(a, b);\n", ".ts") != fingerprint(cd, "foo(a, c);\n", ".ts")


def test_removing_a_comment_between_words_is_a_code_change(cd: ModuleType) -> None:
    assert fingerprint(cd, "a/**/b\n", ".ts") != fingerprint(cd, "ab\n", ".ts")


def test_formatter_unwrapping_a_return_expression_keeps_the_fingerprint(cd: ModuleType) -> None:
    before = """
        function f() {
          return (
            // explain the chain
            a.replace(/x/g, "")
              .trim()
          );
        }
        const g = () => { return(b); };
        const h = () => { throw (-c) };
    """
    after = """
        function f() {
          return a.replace(/x/g, "").trim();
        }
        const g = () => { return b; };
        const h = () => { throw -c };
    """
    assert fingerprint(cd, before, ".ts") == fingerprint(cd, after, ".ts")


def test_parentheses_that_change_meaning_are_kept(cd: ModuleType) -> None:
    pairs = [
        ("return (a).b;\n", "return a.b;\n"),
        ("x = (a, b);\n", "x = a, b;\n"),
        ("f = () => ({ a: 1 });\n", "f = () => { a: 1 };\n"),
        ("return (a)(b);\n", "return a(b);\n"),
    ]
    for before, after in pairs:
        assert fingerprint(cd, before, ".ts") != fingerprint(cd, after, ".ts"), before


def test_array_holes_are_not_trailing_commas(cd: ModuleType) -> None:
    assert fingerprint(cd, "x = [,];\n", ".js") != fingerprint(cd, "x = [];\n", ".js")


def test_template_whitespace_is_significant(cd: ModuleType) -> None:
    assert fingerprint(cd, "x = `a  b`;\n", ".ts") != fingerprint(cd, "x = `a b`;\n", ".ts")


def test_removing_a_directive_changes_the_fingerprint(cd: ModuleType) -> None:
    before = "// @ts-expect-error legacy\nx.y = 1;\n"
    assert fingerprint(cd, before, ".ts") != fingerprint(cd, "x.y = 1;\n", ".ts")


def test_condensing_prose_around_a_tag_keeps_the_fingerprint(cd: ModuleType) -> None:
    before = """
        /**
         * Long paragraph about history.
         * More history.
         * @deprecated Use bar.
         */
        export function foo() {}
    """
    after = """
        /** @deprecated Use bar. */
        export function foo() {}
    """
    assert fingerprint(cd, before, ".ts") == fingerprint(cd, after, ".ts")


def test_python_docstrings_comments_and_pass_are_inert(cd: ModuleType) -> None:
    before = '''
        def f():
            """Doc that goes on."""
            # explain
            pass
    '''
    after = """
        def f():
            pass
    """
    assert fingerprint(cd, before, ".py") == fingerprint(cd, after, ".py")


def test_python_directives_doctests_and_dunder_doc_are_not_inert(cd: ModuleType) -> None:
    assert fingerprint(cd, "x = f()  # type: ignore[misc]\n", ".py") != fingerprint(
        cd, "x = f()\n", ".py"
    )
    doctest = '''
        def f():
            """
            >>> f()
            """
    '''
    assert fingerprint(cd, doctest, ".py") != fingerprint(cd, "def f():\n    pass\n", ".py")
    assert fingerprint(cd, '"""Usage."""\nprint(__doc__)\n', ".py") != fingerprint(
        cd, "print(__doc__)\n", ".py"
    )


# --- CLI: audit ---------------------------------------------------------------


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write(
        tmp_path,
        {
            "src/app.ts": "// a\n// b\nconst x = 1;\nconst y = 2;\n",
            "src/quiet.ts": "const z = 3;\n",
            "src/gen.ts": "// @generated\n// x\nconst g = 1;\n",
            "src/lib.min.js": "var a=1;\n",
            "src/types.d.ts": "/** doc */\nexport type T = 1;\n",
            "src/tool.py": '"""Doc."""\nx = 1\n',
            "node_modules/pkg/index.js": "// c\n",
            "dist/out.js": "// c\n",
            "README.md": "# heading\n",
        },
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _audit(cd: ModuleType, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, object]:
    assert cd.main([*argv, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert isinstance(report, dict)
    return report


def _paths(report: dict[str, object]) -> list[str]:
    files = report["files"]
    assert isinstance(files, list)
    return [f["path"] for f in files]


def test_audit_skips_dependencies_build_output_and_generated_files(
    cd: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _audit(cd, capsys, ".")
    assert sorted(_paths(report)) == ["src/app.ts", "src/quiet.ts", "src/tool.py"]
    assert report["skipped"] == {"generated": 3}
    assert report["totals"] == {
        "files": 3,
        "code": 4,
        "comment": 3,
        "blank": 0,
        "total": 7,
        "density": 42.86,
    }
    by_language = report["by_language"]
    assert isinstance(by_language, dict)
    assert set(by_language) == {"python", "typescript"}


def test_audit_filters_ranks_and_limits(
    cd: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _paths(_audit(cd, capsys, ".", "--min-density", "45")) == ["src/app.ts", "src/tool.py"]
    assert _paths(_audit(cd, capsys, ".", "--top", "1")) == ["src/app.ts"]
    assert _paths(_audit(cd, capsys, ".", "--glob", "src/q*")) == ["src/quiet.ts"]
    assert _paths(_audit(cd, capsys, ".", "--ext", "py")) == ["src/tool.py"]
    assert "src/app.ts" not in _paths(_audit(cd, capsys, ".", "--exclude", "app.ts"))


def test_audit_default_excludes_can_be_disabled(
    cd: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _paths(_audit(cd, capsys, ".", "--no-default-excludes", "--top", "0"))
    assert {"node_modules/pkg/index.js", "dist/out.js", "src/gen.ts", "src/types.d.ts"} <= set(paths)


def test_explicit_files_bypass_filters(
    cd: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _paths(_audit(cd, capsys, "src/gen.ts")) == ["src/gen.ts"]
    report = _audit(cd, capsys, "README.md")
    assert report["skipped"] == {"unsupported": 1}


def test_audit_table_marks_high_density_files(
    cd: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cd.main([".", "--no-color", "--highlight", "40"]) == 0
    out = capsys.readouterr().out
    rows = {line.split()[-1]: line for line in out.splitlines() if line.endswith((".ts", ".py"))}
    assert rows["src/app.ts"].startswith("*") and "50.0%" in rows["src/app.ts"]
    assert rows["src/quiet.ts"].startswith(" ")


def test_cli_rejects_bad_arguments(cd: ModuleType, tree: Path) -> None:
    for argv in (["--ext", ".rb"], ["missing-dir"], ["--verify-against", "--output=x"]):
        with pytest.raises(SystemExit) as exc:
            cd.main(argv)
        assert exc.value.code == 2


# --- CLI: verify --------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write(
        tmp_path,
        {
            "a.ts": "// ─── Banner ───\n// Fix for #12: history.\nexport const a = f(1);\n",
            "b.py": '"""Doc."""\n\n\ndef g():\n    # why\n    return 1\n',
        },
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _verify(cd: ModuleType, capsys: pytest.CaptureFixture[str], *paths: str) -> tuple[int, dict[str, object]]:
    code = cd.main(["--verify-against", "HEAD", "--json", *paths])
    return code, json.loads(capsys.readouterr().out)


def test_verify_passes_comment_only_edits(
    cd: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "a.ts").write_text("export const a = f(1);\n")
    (repo / "b.py").write_text("def g():\n    return 1\n")
    code, report = _verify(cd, capsys, "a.ts", "b.py")
    assert code == 0
    files = report["files"]
    assert isinstance(files, list)
    assert [f["status"] for f in files] == ["unchanged", "unchanged"]
    assert files[0]["before"] == {"total": 3, "comment": 2, "density": 66.67}
    assert files[0]["after"] == {"total": 1, "comment": 0, "density": 0.0}


def test_verify_fails_on_code_changes_and_new_files(
    cd: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "a.ts").write_text("export const a = f(2);\n")
    (repo / "c.ts").write_text("export const c = 1;\n")
    code, report = _verify(cd, capsys, "a.ts", "c.ts")
    assert code == 1
    files = report["files"]
    assert isinstance(files, list)
    assert [f["status"] for f in files] == ["changed", "added"]
    assert "f(1)" in files[0]["divergence"]["before"]
    assert "f(2)" in files[0]["divergence"]["after"]


def test_verify_rejects_unknown_revisions(
    cd: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cd.main(["--verify-against", "no-such-ref", "a.ts"]) == 2
    assert "unknown git revision" in capsys.readouterr().err
