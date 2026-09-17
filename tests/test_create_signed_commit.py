"""Unit tests for `scripts/create-signed-commit.py`.

Network-bound code paths (`github_api`, `github_api_optional`, ref
creation) are not tested here — they require a real installation token
and a live GitHub repo. Coverage focuses on:

- `parse_status` against a real git working tree (the porcelain-v1
  format with `-z` is the contract; we drive it through actual `git
  status` invocations rather than mocked output, so any future git
  output drift is caught here).
- `derive_signoff_trailer` slug formatting.
- `with_signoff` idempotency.
- The `--new-branch == --base-branch` self-merge guard in `main()`.
"""
from __future__ import annotations

import os
import subprocess
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest


def _http_error(path: str, code: int, msg: str) -> urllib.error.HTTPError:
    """Build a synthetic GitHub-API HTTPError for tests.

    `hdrs=None` is documented-accepted at runtime but typed as
    `email.message.Message` (not `Message | None`) in typeshed —
    a stdlib stub gap. Scoping the `type: ignore` to this builder keeps
    the four call sites clean.
    """
    return urllib.error.HTTPError(
        url="https://api.github.com" + path,
        code=code,
        msg=msg,
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# Helpers: drive parse_status through a real git working tree


def _git(*args: str, cwd: Path) -> str:
    """Run a git command in `cwd` and return stdout (raises on failure)."""
    env = os.environ.copy()
    # Force deterministic commit identity.
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.invalid"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "seed.txt").write_text("seed\n")
    _git("add", "seed.txt", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


# parse_status


def test_parse_status_empty_tree(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    changes = create_signed_commit.parse_status(git_repo)
    assert changes.upserts == []
    assert changes.deletes == []


def test_parse_status_modified_file(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    (git_repo / "seed.txt").write_text("modified\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert changes.upserts == ["seed.txt"]
    assert changes.deletes == []


def test_parse_status_new_untracked_file_via_uall(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    # -uall reports each file in a new directory individually.
    nested = git_repo / "newdir" / "sub"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("a\n")
    (nested / "b.txt").write_text("b\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert sorted(changes.upserts) == ["newdir/sub/a.txt", "newdir/sub/b.txt"]
    assert changes.deletes == []


def test_parse_status_deleted_file(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    (git_repo / "seed.txt").unlink()
    changes = create_signed_commit.parse_status(git_repo)
    assert changes.upserts == []
    assert changes.deletes == ["seed.txt"]


def test_parse_status_rename_emits_both_upsert_and_delete(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    # Renames require old path marked deleted to prevent copy semantics.
    (git_repo / "old.txt").write_text("content\n")
    _git("add", "old.txt", cwd=git_repo)
    _git("commit", "-q", "-m", "add", cwd=git_repo)
    _git("mv", "old.txt", "new.txt", cwd=git_repo)
    changes = create_signed_commit.parse_status(git_repo)
    assert "new.txt" in changes.upserts
    assert "old.txt" in changes.deletes


def test_parse_status_bytes_rejects_missing_terminal_nul(
    create_signed_commit: ModuleType,
) -> None:
    with pytest.raises(ValueError, match="missing terminal NUL"):
        create_signed_commit.parse_status_bytes("?? new.txt")


def test_parse_status_bytes_rejects_truncated_rename(
    create_signed_commit: ModuleType,
) -> None:
    with pytest.raises(ValueError, match="rename/copy source is missing"):
        create_signed_commit.parse_status_bytes("R  new.txt\0")


def test_parse_status_handles_paths_with_spaces(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    # Verify parser handles spaces in NUL-separated paths.
    spaced = git_repo / "file with spaces.txt"
    spaced.write_text("x\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert "file with spaces.txt" in changes.upserts


def test_parse_status_handles_path_with_special_chars(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    weird = git_repo / "file'with\"quotes.txt"
    weird.write_text("x\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert "file'with\"quotes.txt" in changes.upserts


def test_parse_status_mixed_upsert_and_delete(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    (git_repo / "seed.txt").unlink()
    (git_repo / "new.txt").write_text("x\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert "new.txt" in changes.upserts
    assert "seed.txt" in changes.deletes


def test_parse_status_d_entry_trusts_git_code_not_disk_state(
    create_signed_commit: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D entry must be classified as delete from git status alone, not disk state."""
    # File present on disk to test TOCTOU resilience against git delete status.
    (tmp_path / "ghost.txt").write_text("oops still here\n")

    # Synthetic D entry with -z format.
    def fake_run(*args: str, cwd: Path | None = None) -> str:
        return "D  ghost.txt\0"

    monkeypatch.setattr(create_signed_commit, "run", fake_run)
    changes = create_signed_commit.parse_status(tmp_path)
    assert changes.deletes == ["ghost.txt"]
    assert changes.upserts == []


# derive_signoff_trailer + with_signoff


def test_derive_signoff_trailer_uses_bot_suffix(create_signed_commit: ModuleType) -> None:
    out = create_signed_commit.derive_signoff_trailer("loomantix")
    assert out == "Signed-off-by: loomantix[bot] <loomantix[bot]@users.noreply.github.com>"


def test_derive_signoff_trailer_empty_slug_documents_current_behavior(
    create_signed_commit: ModuleType,
) -> None:
    """Empty --app-slug produces a [bot] trailer."""
    out = create_signed_commit.derive_signoff_trailer("")
    assert out == "Signed-off-by: [bot] <[bot]@users.noreply.github.com>"


def test_with_signoff_appends_when_absent(create_signed_commit: ModuleType) -> None:
    trailer = "Signed-off-by: bot[bot] <bot[bot]@users.noreply.github.com>"
    out = create_signed_commit.with_signoff("feat: do thing", trailer)
    assert out == f"feat: do thing\n\n{trailer}\n"


def test_with_signoff_idempotent_when_present(create_signed_commit: ModuleType) -> None:
    msg = "feat: do thing\n\nSigned-off-by: other <other@example.com>"
    out = create_signed_commit.with_signoff(msg, "Signed-off-by: ignored <i@x>")
    assert out == msg


def test_with_signoff_strips_message_trailing_newlines(
    create_signed_commit: ModuleType,
) -> None:
    # Normalize trailing newlines before trailer.
    out = create_signed_commit.with_signoff("feat: x\n\n\n", "Signed-off-by: a <a@b>")
    assert out == "feat: x\n\nSigned-off-by: a <a@b>\n"


# main(): new_branch == base_branch guard


def test_main_refuses_new_branch_equals_base_branch(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse --new-branch == --base-branch to prevent overwriting base."""
    # Provide token to reach branch guard.
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token-not-used")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "main",
            "--message", "test",
            "--consumer-dir", str(tmp_path),
        ],
    )
    rc = create_signed_commit.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to operate" in err
    assert "--new-branch and --base-branch are the same" in err


def test_main_requires_token_env(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GH_APP_TOKEN", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/upstream-2026-05-16",
            "--message", "test",
            "--consumer-dir", str(tmp_path),
        ],
    )
    rc = create_signed_commit.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing token" in err


# _github_request: JSON object shape check


def test_github_request_rejects_non_object_json(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-object JSON response fails closed."""
    import urllib.request

    class FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req: object, timeout: int = 30) -> FakeResp:
        return FakeResp(b'["not", "an", "object"]')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as exc:
        create_signed_commit._github_request("GET", "/test", "tok", None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "expected JSON object" in err


# github_api / github_api_optional: HTTPError + 404 handling


def test_github_api_optional_returns_none_on_404(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> object:
        raise _http_error(path, 404, "Not Found")

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    result = create_signed_commit.github_api_optional("GET", "/test", "tok")
    assert result is None


def test_github_api_optional_exits_on_other_errors(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> object:
        raise _http_error(path, 500, "Internal Server Error")

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    with pytest.raises(SystemExit) as exc:
        create_signed_commit.github_api_optional("GET", "/test", "tok")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "500" in err


def test_github_api_exits_on_http_error(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> object:
        raise _http_error(path, 422, "Unprocessable Entity")

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    with pytest.raises(SystemExit) as exc:
        create_signed_commit.github_api("POST", "/test", "tok", {"foo": "bar"})
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "422" in err


def test_github_api_returns_parsed_body_on_success(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> dict[str, object]:
        return {"sha": "abc123", "tree": {"sha": "deadbeef"}}

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    result = create_signed_commit.github_api("GET", "/test", "tok")
    assert result == {"sha": "abc123", "tree": {"sha": "deadbeef"}}


def test_run_exits_on_command_failure(
    create_signed_commit: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        create_signed_commit.run("false")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "command failed" in err


# main() with mocked _github_request


class _ApiRecorder:
    """Captures (method, path, body) tuples; returns scripted responses by path-prefix."""

    def __init__(self, responses: list[tuple[str, str, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, object]] = []

    @property
    def remaining(self) -> int:
        """Return the number of scripted responses not consumed."""
        return len(self._responses)

    def __call__(self, method: str, path: str, token: str, body: object) -> object:
        self.calls.append((method, path, body))
        if not self._responses:
            raise AssertionError(f"unexpected API call: {method} {path}")
        exp_method, exp_prefix, value = self._responses.pop(0)
        # Explicit raise survives PYTHONOPTIMIZE.
        if method != exp_method:
            raise AssertionError(f"expected {exp_method} {exp_prefix}, got {method} {path}")
        if not path.startswith(exp_prefix):
            raise AssertionError(f"expected path prefix {exp_prefix}, got {path}")
        if isinstance(value, Exception):
            raise value
        return value


def _commit_main_argv(
    consumer_dir: Path,
    new_branch: str = "sync/upstream-2026-05-16",
    app_slug: str | None = None,
) -> list[str]:
    argv = [
        "create-signed-commit.py",
        "--owner", "loomantix",
        "--repo", "test",
        "--base-branch", "main",
        "--new-branch", new_branch,
        "--message", "feat: sync from upstream",
        "--consumer-dir", str(consumer_dir),
    ]
    if app_slug:
        argv.extend(["--app-slug", app_slug])
    return argv


def test_main_no_changes_exits_zero_without_api_calls(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty diff exits 0 before making API calls."""
    recorder = _ApiRecorder([])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo))

    rc = create_signed_commit.main()
    assert rc == 0
    assert recorder.calls == []
    assert "No changes to commit." in capsys.readouterr().out


def test_main_full_flow_creates_new_branch_when_absent(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full API sequence for one upsert and one delete."""
    _git("mv", "seed.txt", "renamed.txt", cwd=git_repo)
    (git_repo / "new.txt").write_text("brand new\n")

    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "base-sha"}}),
        ("GET", "/repos/loomantix/test/git/commits/base-sha",
         {"tree": {"sha": "base-tree-sha"}}),
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-renamed"}),
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-new"}),
        ("POST", "/repos/loomantix/test/git/trees", {"sha": "new-tree-sha"}),
        ("POST", "/repos/loomantix/test/git/commits", {"sha": "new-commit-sha"}),
        ("GET", "/repos/loomantix/test/git/commits/new-commit-sha",
         {"sha": "new-commit-sha", "verification": {"verified": True, "reason": "valid"}}),
        ("GET", "/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16",
         _http_error("/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16", 404, "Not Found")),
        ("POST", "/repos/loomantix/test/git/refs", {"ref": "refs/heads/x"}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo, app_slug="loomantix"))

    rc = create_signed_commit.main()
    assert rc == 0, capsys.readouterr().err

    commit_call = next(c for c in recorder.calls if c[0] == "POST" and c[1].endswith("/commits"))
    assert isinstance(commit_call[2], dict)
    assert "Signed-off-by: loomantix[bot]" in commit_call[2]["message"]

    tree_call = next(c for c in recorder.calls if c[0] == "POST" and c[1].endswith("/trees"))
    assert isinstance(tree_call[2], dict)
    paths_in_tree = {e["path"]: e for e in tree_call[2]["tree"]}
    assert "renamed.txt" in paths_in_tree and paths_in_tree["renamed.txt"]["sha"] is not None
    assert "new.txt" in paths_in_tree and paths_in_tree["new.txt"]["sha"] is not None
    assert "seed.txt" in paths_in_tree and paths_in_tree["seed.txt"]["sha"] is None
    assert recorder.remaining == 0


def test_main_force_updates_branch_when_already_exists(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force-update branch when it already exists."""
    (git_repo / "new.txt").write_text("x\n")

    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "base-sha"}}),
        ("GET", "/repos/loomantix/test/git/commits/base-sha",
         {"tree": {"sha": "base-tree-sha"}}),
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-1"}),
        ("POST", "/repos/loomantix/test/git/trees", {"sha": "new-tree-sha"}),
        ("POST", "/repos/loomantix/test/git/commits", {"sha": "new-commit-sha"}),
        ("GET", "/repos/loomantix/test/git/commits/new-commit-sha",
         {"sha": "new-commit-sha", "verification": {"verified": True, "reason": "valid"}}),
        ("GET", "/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16",
         {"object": {"sha": "old-sha"}}),
        ("PATCH", "/repos/loomantix/test/git/refs/heads/sync/upstream-2026-05-16",
         {"ref": "refs/heads/x"}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo))

    rc = create_signed_commit.main()
    assert rc == 0

    patch_call = next(c for c in recorder.calls if c[0] == "PATCH")
    assert isinstance(patch_call[2], dict)
    assert patch_call[2].get("force") is True
    assert patch_call[2].get("sha") == "new-commit-sha"


def test_main_rejects_non_file_upsert_path(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject upsert paths that resolve to non-regular files."""
    (git_repo / "dangling").symlink_to(git_repo / "nope")

    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "base-sha"}}),
        ("GET", "/repos/loomantix/test/git/commits/base-sha",
         {"tree": {"sha": "base-tree-sha"}}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo))

    rc = create_signed_commit.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a regular file" in err


def test_main_full_flow_with_payload_dir_and_manifest(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_dir = tmp_path / "payload_tree"
    payload_dir.mkdir()
    (payload_dir / "new.txt").write_text("hello from payload\n")

    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(b"?? new.txt\0")

    base_sha_file = tmp_path / "base-sha"
    base_sha_file.write_text("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\n")
    config_path = tmp_path / ".platform-config.yml"
    config_path.write_text("allowed_destinations:\n  - new.txt\n")

    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"}}),
        ("GET", "/repos/loomantix/test/git/commits/a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
         {"tree": {"sha": "base-tree-sha"}}),
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-payload-new"}),
        ("POST", "/repos/loomantix/test/git/trees", {"sha": "new-tree-sha"}),
        ("POST", "/repos/loomantix/test/git/commits", {"sha": "new-commit-sha"}),
        ("GET", "/repos/loomantix/test/git/commits/new-commit-sha",
         {"sha": "new-commit-sha", "verification": {"verified": True, "reason": "valid"}}),
        ("GET", "/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16",
         _http_error("/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16", 404, "Not Found")),
        ("POST", "/repos/loomantix/test/git/refs", {"ref": "refs/heads/x"}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/upstream-2026-05-16",
            "--message", "feat: sync from upstream",
            "--payload-dir", str(payload_dir),
            "--manifest", str(manifest_path),
            "--config", str(config_path),
            "--config-destination", ".platform-config.yml",
            "--base-sha-file", str(base_sha_file),
            "--app-slug", "loomantix",
        ],
    )

    rc = create_signed_commit.main()
    assert rc == 0, capsys.readouterr().err


def test_main_rejects_diverged_base_sha(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_dir = tmp_path / "payload_tree"
    payload_dir.mkdir()
    (payload_dir / "new.txt").write_text("hello\n")

    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(b"?? new.txt\0")

    base_sha_file = tmp_path / "base-sha"
    base_sha_file.write_text("1111111111111111111111111111111111111111\n")
    config_path = tmp_path / ".platform-config.yml"
    config_path.write_text("allowed_destinations:\n  - new.txt\n")

    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "2222222222222222222222222222222222222222"}}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/upstream-2026-05-16",
            "--message", "feat: sync from upstream",
            "--payload-dir", str(payload_dir),
            "--manifest", str(manifest_path),
            "--config", str(config_path),
            "--config-destination", ".platform-config.yml",
            "--base-sha-file", str(base_sha_file),
        ],
    )

    rc = create_signed_commit.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "has diverged from expected base" in err


def test_main_requires_payload_or_consumer_dir(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/upstream-2026-05-16",
            "--message", "feat: sync from upstream",
        ],
    )
    rc = create_signed_commit.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "choose exactly one mode" in err


def test_main_requires_base_sha_in_payload_mode(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    manifest = tmp_path / "manifest"
    manifest.write_bytes(b"?? new.txt\0")
    config = tmp_path / ".platform-config.yml"
    config.write_text("allowed_destinations:\n  - new.txt\n")
    recorder = _ApiRecorder([])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/test",
            "--message", "test",
            "--payload-dir", str(payload_dir),
            "--manifest", str(manifest),
            "--config", str(config),
            "--config-destination", ".platform-config.yml",
        ],
    )

    assert create_signed_commit.main() == 2
    assert recorder.calls == []
    assert "requires --base-sha-file or --expected-base-sha" in capsys.readouterr().err


def test_main_rejects_mixed_payload_and_consumer_modes(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_dir = tmp_path / "payload"
    consumer_dir = tmp_path / "consumer"
    payload_dir.mkdir()
    consumer_dir.mkdir()
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/test",
            "--message", "test",
            "--payload-dir", str(payload_dir),
            "--consumer-dir", str(consumer_dir),
        ],
    )

    assert create_signed_commit.main() == 2
    assert "choose exactly one mode" in capsys.readouterr().err


def test_main_rejects_unverified_commit_before_publishing_ref(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (git_repo / "new.txt").write_text("new\n")
    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "base-sha"}}),
        ("GET", "/repos/loomantix/test/git/commits/base-sha",
         {"tree": {"sha": "base-tree-sha"}}),
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-sha"}),
        ("POST", "/repos/loomantix/test/git/trees", {"sha": "tree-sha"}),
        ("POST", "/repos/loomantix/test/git/commits", {"sha": "commit-sha"}),
        ("GET", "/repos/loomantix/test/git/commits/commit-sha",
         {"sha": "commit-sha", "verification": {"verified": False, "reason": "unsigned"}}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo))

    assert create_signed_commit.main() == 1
    assert recorder.remaining == 0
    assert not any("/git/refs" in path for _, path, _ in recorder.calls)
    assert "did not verify commit" in capsys.readouterr().err


def test_payload_mode_rejects_disallowed_manifest_path_before_api(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    (payload_dir / "CODEOWNERS").write_text("* @attacker\n")
    manifest = tmp_path / "manifest"
    manifest.write_bytes(b"?? CODEOWNERS\0")
    config = tmp_path / ".platform-config.yml"
    config.write_text("allowed_destinations:\n  - .agents/**\n")
    recorder = _ApiRecorder([])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/test",
            "--message", "test",
            "--payload-dir", str(payload_dir),
            "--manifest", str(manifest),
            "--config", str(config),
            "--config-destination", ".platform-config.yml",
            "--expected-base-sha", "1111111111111111111111111111111111111111",
        ],
    )

    assert create_signed_commit.main() == 1
    assert recorder.calls == []
    assert "not allowed" in capsys.readouterr().err


def test_glob_to_regex_stays_in_lockstep_with_sync_engine_dialect(
    create_signed_commit: ModuleType,
    sync_engine: ModuleType,
) -> None:
    # Ensure create-signed-commit and sync-engine glob dialects match.
    patterns = [
        ".agents/**",
        "**/SKILL.md",
        ".github/workflows/*.yml",
        "docs/**/README.md",
        "a/*/b?",
        "a**b",
        "**",
        "*",
        "?",
        "a.b+c(d)[e]",
        "skills/*/scripts/**",
    ]
    for pattern in patterns:
        assert (
            create_signed_commit.glob_to_regex(pattern).pattern
            == sync_engine.glob_to_regex(pattern).pattern
        ), pattern


def test_sensitive_pattern_sets_match_sync_engine(
    create_signed_commit: ModuleType,
    sync_engine: ModuleType,
) -> None:
    # Pin sensitive pattern tuples to sync engine policy.
    assert (
        create_signed_commit.SENSITIVE_WRITE_PATTERNS == sync_engine.SENSITIVE_WRITE_PATTERNS
    )
    assert (
        create_signed_commit.SENSITIVE_DELETE_PATTERNS == sync_engine.SENSITIVE_DELETE_PATTERNS
    )
    assert (
        create_signed_commit.ENGINE_SURFACE_PATTERNS == sync_engine.ENGINE_SURFACE_PATTERNS
    )


def _payload_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / ".platform-config.yml"
    config.write_text(body, encoding="utf-8")
    return config


def test_validate_payload_rejects_unconsented_sensitive_write(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _payload_config(tmp_path, "allowed_destinations:\n  - .github/**\n")
    changes = create_signed_commit.StatusChanges(
        upserts=[".github/workflows/ci.yml"], deletes=[]
    )
    with pytest.raises(ValueError):
        create_signed_commit.validate_payload_paths(changes, config)
    assert "sensitive path without an `allow_sensitive_writes` grant" in capsys.readouterr().err


def test_validate_payload_allows_consented_sensitive_write(
    create_signed_commit: ModuleType,
    tmp_path: Path,
) -> None:
    config = _payload_config(
        tmp_path,
        "allowed_destinations:\n  - .github/**\n"
        "allow_sensitive_writes:\n  - .github/workflows/ci.yml\n",
    )
    changes = create_signed_commit.StatusChanges(
        upserts=[".github/workflows/ci.yml"], deletes=[]
    )
    assert create_signed_commit.validate_payload_paths(changes, config) == changes


def test_validate_payload_rejects_glob_sensitive_grant(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _payload_config(
        tmp_path,
        "allowed_destinations:\n  - .github/**\n"
        "allow_sensitive_writes:\n  - .github/workflows/**\n",
    )
    changes = create_signed_commit.StatusChanges(
        upserts=[".github/workflows/ci.yml"], deletes=[]
    )
    with pytest.raises(ValueError):
        create_signed_commit.validate_payload_paths(changes, config)
    assert "literal canonical sensitive path" in capsys.readouterr().err


def test_validate_payload_rejects_sensitive_delete(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _payload_config(tmp_path, "allowed_destinations:\n  - .github/**\n")
    changes = create_signed_commit.StatusChanges(
        upserts=[], deletes=[".github/workflows/ci.yml"]
    )
    with pytest.raises(ValueError):
        create_signed_commit.validate_payload_paths(changes, config)
    assert "refusing to delete sensitive path" in capsys.readouterr().err


def test_validate_payload_allows_engine_surface_write_without_grant(
    create_signed_commit: ModuleType,
    tmp_path: Path,
) -> None:
    config = _payload_config(tmp_path, "allowed_destinations:\n  - .claude/**\n")
    changes = create_signed_commit.StatusChanges(
        upserts=[".claude/skills/x/SKILL.md"], deletes=[]
    )
    assert create_signed_commit.validate_payload_paths(changes, config) == changes


def test_validate_payload_refuses_config_self_write(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _payload_config(tmp_path, "allowed_destinations:\n  - '**'\n")
    changes = create_signed_commit.StatusChanges(
        upserts=[".platform-config.yml"], deletes=[]
    )
    with pytest.raises(ValueError):
        create_signed_commit.validate_payload_paths(changes, config)
    assert "consumer's own sync config" in capsys.readouterr().err


def test_validate_payload_refuses_nested_config_self_write(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _payload_config(tmp_path, "allowed_destinations:\n  - '**'\n")
    changes = create_signed_commit.StatusChanges(
        upserts=["config/sync.yml"], deletes=[]
    )
    with pytest.raises(ValueError):
        create_signed_commit.validate_payload_paths(
            changes,
            config,
            "config/sync.yml",
        )
    assert "consumer's own sync config" in capsys.readouterr().err


def test_validate_payload_rejects_duplicate_upsert_and_delete(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _payload_config(tmp_path, "allowed_destinations:\n  - '**'\n")
    changes = create_signed_commit.StatusChanges(upserts=["x.txt"], deletes=["x.txt"])
    with pytest.raises(ValueError):
        create_signed_commit.validate_payload_paths(changes, config)
    assert "both an upsert and a delete" in capsys.readouterr().err


def test_validate_payload_rejects_falsy_scalar_skip_targets(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _payload_config(
        tmp_path, "allowed_destinations:\n  - new.txt\nskip_targets: \"\"\n"
    )
    changes = create_signed_commit.StatusChanges(upserts=["new.txt"], deletes=[])
    with pytest.raises(ValueError):
        create_signed_commit.validate_payload_paths(changes, config)
    assert "`skip_targets` must be a list of strings" in capsys.readouterr().err


def test_main_rejects_empty_base_sha_file(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_dir = tmp_path / "payload_tree"
    payload_dir.mkdir()
    (payload_dir / "new.txt").write_text("hello\n")
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(b"?? new.txt\0")
    base_sha_file = tmp_path / "base-sha"
    base_sha_file.write_text("   \n")
    config_path = tmp_path / ".platform-config.yml"
    config_path.write_text("allowed_destinations:\n  - new.txt\n")

    recorder = _ApiRecorder([])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/x",
            "--message", "m",
            "--payload-dir", str(payload_dir),
            "--manifest", str(manifest_path),
            "--config", str(config_path),
            "--config-destination", ".platform-config.yml",
            "--base-sha-file", str(base_sha_file),
        ],
    )

    assert create_signed_commit.main() == 1
    assert recorder.calls == []
    assert "not a 40-character hex commit id" in capsys.readouterr().err


def test_payload_mode_rejects_a_config_inside_the_payload_tree(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Config must not come from within the payload tree."""
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    (payload_dir / "CODEOWNERS").write_text("* @attacker\n")
    manifest = tmp_path / "manifest"
    manifest.write_bytes(b"?? CODEOWNERS\0")
    # Self-authorizing config inside payload.
    config = payload_dir / ".platform-config.yml"
    config.write_text(
        "allowed_destinations:\n  - '**'\nallow_sensitive_writes:\n  - CODEOWNERS\n"
    )
    recorder = _ApiRecorder([])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/test",
            "--message", "test",
            "--payload-dir", str(payload_dir),
            "--manifest", str(manifest),
            "--config", str(config),
            "--config-destination", ".platform-config.yml",
            "--expected-base-sha", "1111111111111111111111111111111111111111",
        ],
    )

    assert create_signed_commit.main() == 2
    assert recorder.calls == []
    assert "must live outside the payload tree" in capsys.readouterr().err
