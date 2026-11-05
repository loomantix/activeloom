#!/usr/bin/env python3
"""Launch a Claude Cloud session from a non-interactive shell and print its URL.

The Claude CLI's `--cloud` mode refuses to run without an interactive terminal,
and an agent's shell tool is not one. This helper runs the CLI on a
pseudo-terminal (Python's `pty`, so the same code works on Linux and macOS
without `script`), reads the prompt from a file so a multi-line prompt needs no
shell quoting, strips ANSI escapes from what the CLI prints, and reports the
first claude.ai session URL.

    cloud-launch.py --prompt-file <file> [--model <m>] [--effort <e>]

`--model inherit` (or no `--model`) leaves the flag out, so the CLI's own
default applies. Run it from the checkout the cloud session should start from.

stdout is one JSON object: `{"url": ..., "exit_code": ..., "timed_out": ...}`,
plus `output_tail` (the last cleaned output) when no URL was found.

Exit status: 0 a URL was found; 1 the CLI ran but printed no URL (alert with
`output_tail`); 2 invalid input, or the CLI could not be started.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# CSI and OSC sequences, then any other two-byte escape.
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
URL_RE = re.compile(r"https://" r"(?:[A-Za-z0-9-]+\.)*claude\.ai/code/[^\s\"'<>)\]]+")
TAIL_CHARS = 2000
# The CLI prints the URL and exits; give it this long to exit once the URL is out.
EXIT_GRACE_SECONDS = 10.0


def clean(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def find_url(text: str) -> str | None:
    match = URL_RE.search(clean(text))
    return match.group(0).rstrip(".,;:") if match else None


def build_argv(claude: str, prompt: str, model: str | None, effort: str | None) -> list[str]:
    argv = [claude, "--cloud", prompt]
    if model and model != "inherit":
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv


def stop(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_on_pty(argv: list[str], timeout: float) -> tuple[str, int | None, bool, str | None]:
    """Run argv with a pty as its terminal; return (output, exit code, timed out, url)."""
    import pty

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(slave)
    chunks: list[bytes] = []
    grace_started = False
    deadline = time.monotonic() + timeout
    deadline_expired = False
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                deadline_expired = True
                break
            ready, _, _ = select.select([master], [], [], min(0.5, deadline - now))
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:  # Linux reports EIO once the child closes the pty.
                    data = b""
                if not data:
                    break
                chunks.append(data)
                if not grace_started and b"\n" in data:
                    completed_lines = b"".join(chunks).decode("utf-8", "replace").rsplit("\n", 1)[0]
                    if find_url(completed_lines) is not None:
                        grace_started = True
                        deadline = min(deadline, time.monotonic() + EXIT_GRACE_SECONDS)
            elif proc.poll() is not None:
                break
    finally:
        os.close(master)
        if proc.poll() is None:
            stop(proc)
    output = b"".join(chunks).decode("utf-8", "replace")
    url = find_url(output)
    return output, proc.returncode, deadline_expired and url is None, url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds (default 180)")
    parser.add_argument(
        "--claude",
        default=os.environ.get("ACTIVELOOM_CLAUDE_BIN", "claude"),
        help="CLI to run (default: claude on PATH)",
    )
    args = parser.parse_args(argv)

    try:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cloud-launch: cannot read prompt file: {exc}", file=sys.stderr)
        return 2
    if not prompt.strip():
        print("cloud-launch: prompt file is empty", file=sys.stderr)
        return 2
    claude = shutil.which(args.claude)
    if claude is None:
        print(f"cloud-launch: {args.claude} not found on PATH", file=sys.stderr)
        return 2
    if os.name != "posix":
        print("cloud-launch: needs a POSIX pseudo-terminal (Linux or macOS)", file=sys.stderr)
        return 2

    try:
        output, code, timed_out, url = run_on_pty(
            build_argv(claude, prompt, args.model, args.effort), args.timeout
        )
    except OSError as exc:
        print(f"cloud-launch: could not start {claude}: {exc}", file=sys.stderr)
        return 2

    result: dict[str, object] = {"url": url, "exit_code": code, "timed_out": timed_out}
    if url is None:
        result["output_tail"] = clean(output).strip()[-TAIL_CHARS:]
    print(json.dumps(result))
    return 0 if url else 1


if __name__ == "__main__":
    sys.exit(main())
