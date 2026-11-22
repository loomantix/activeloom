#!/usr/bin/env python3
"""Enter an interactive pass with base policy, controller transport and candidate cwd."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

ROOTS = {"codex": ".codex", "claude": ".claude", "gemini": ".agents"}
SKILLS = ("critique", "deepcritique", "refactorpass")
TRANSPORT_SECTIONS = ("Reviewer environment preflight", "Silent mode")


def run(*argv: str | Path, cwd: Path | None = None) -> str:
    return subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, check=True).stdout.strip()


def is_commit(revision: str) -> bool:
    return subprocess.run(("git", "cat-file", "-e", revision + "^{commit}"),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def is_ancestor(ancestor: str, descendant: str) -> bool:
    status = subprocess.run(("git", "merge-base", "--is-ancestor", ancestor, descendant)).returncode
    if status not in (0, 1):
        raise ValueError(f"could not compare {ancestor} with {descendant}")
    return status == 0


def verify_checkout(tree: Path, revision: str, *, input_tree: bool) -> None:
    if (not re.fullmatch("[0-9a-f]{40}", revision)
            or run("git", "rev-parse", "--show-toplevel", cwd=tree) != str(tree)
            or run("git", "rev-parse", "HEAD", cwd=tree) != revision):
        raise ValueError(f"checkout or revision mismatch at {tree}")
    if not input_tree and run("git", "status", "--porcelain", cwd=tree):
        raise ValueError("review entry requires a clean candidate at the assigned head")
    if input_tree:
        if tree.is_relative_to(Path.cwd()) or Path.cwd().is_relative_to(tree) or not re.fullmatch(r"[\w./-]+", str(tree)):
            raise ValueError("use separate policy and tooling inputs outside the candidate at shell-safe paths")
        if run("git", "status", "--porcelain", "--ignored", cwd=tree):
            raise ValueError(f"use a clean dedicated input checkout at {revision}: {tree}")


def regular(path: Path) -> Path:
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError(f"missing or indirect review input: {path}")
    return path


def check_bundle(root: Path) -> tuple[Path, str, str]:
    scripts = root / "skills/critique/scripts"
    cli = regular(scripts / "review-ledger.js")
    regular(scripts / "package.json")
    digest = "sha512-" + base64.b64encode(hashlib.sha512(cli.read_bytes()).digest()).decode()
    if digest != regular(scripts / "review-ledger.integrity").read_text().strip():
        raise ValueError(f"ledger integrity mismatch at {cli}")
    version = run("node", cli, "--version")
    if version != regular(scripts / "review-ledger.version").read_text().strip():
        raise ValueError(f"ledger version mismatch at {cli}")
    if run("node", cli, "--protocol-version") != "3":
        raise ValueError(f"unsupported review protocol at {cli}")
    return cli, version, digest


def section(document: str, title: str) -> str:
    match = re.search(r"^## " + title + r".*?(?=^## |\Z)", document, re.M | re.S)
    if not match:
        raise ValueError(f"installed reviewer lacks '{title}'; use a compatible controller")
    return match.group()


def check_transport(root: Path, ledger: Path) -> Path:
    controller = regular(ledger.with_name("local-review-handoff.py"))
    regular(root / "skills/hreview/scripts/herd-config.py")
    regular(root / "skills/review-setup/scripts/review-profile.py")
    overview = run("node", ledger, "--help")
    commands = set(re.findall(r"^  ([\w-]+) +", overview, re.M))
    needed = set("silent-status silent-pin read-ledger read-roster post-roster lifecycle-state post-finding "
                 "preflight-fix write-result validate-result export-threads attest verify-coverage".split())
    if needed - commands:
        raise ValueError(f"incompatible review transport; missing commands: {sorted(needed - commands)}")
    for command, flag in (("start-run", "--restart-approval-id"), ("authorize-pass", "--run-id"),
                          ("authorize-fix", "--run-id"), ("next-pass", "--head"), ("run-state", "--repo"),
                          ("user-handoff", "--bound-entry-file")):
        if flag not in run(sys.executable, "-IB", controller, command, "--help"):
            raise ValueError(f"controller lacks {command} {flag}")
    for skill in SKILLS:
        document = regular(root / "skills" / skill / "SKILL.md").read_text()
        for title in TRANSPORT_SECTIONS:
            section(document, title)
    return controller


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def route_inputs(repo: str, pr: int) -> list[object]:
    # silent-status can create a profile-requested pin. Detect a changed environment
    # or a lost pin before asking it to resolve the authoritative destination again.
    common = run("git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    store = Path(os.environ.get("ACTIVELOOM_SILENT_ROOT") or Path(common) / "activeloom-review")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    profile = Path(os.environ.get("ACTIVELOOM_REVIEW_PROFILE") or config / "activeloom/review-profile.json")
    if not store.is_absolute() or not profile.is_absolute():
        raise ValueError("review state and profile paths must be absolute")
    pin = store / f"{repo.lower().replace('/', '-')}-{pr}" / "silent/mode.json"
    try:
        pin_hash = hashlib.sha256(pin.read_bytes()).hexdigest()
    except FileNotFoundError:
        pin_hash = None
    try:
        silent = json.loads(profile.read_text()).get("silent")
    except FileNotFoundError:
        silent = None
    return [str(store.resolve()), pin_hash, silent, os.environ.get("ACTIVELOOM_REVIEW_SILENT") == "1"]


def entry(args: argparse.Namespace) -> None:
    policy = args.policy_tree.resolve(strict=True)
    tools = Path(__file__).resolve().parents[4]
    verify_checkout(Path.cwd(), args.head, input_tree=False)
    verify_checkout(policy, args.policy_revision, input_tree=True)
    verify_checkout(tools, args.tool_revision, input_tree=True)
    common = ("git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    if run(*common, cwd=policy) != run(*common) or run("git", "branch", "--show-current", cwd=policy):
        raise ValueError("use a detached policy worktree of the candidate repository")

    if run("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").lower() != args.repo.lower():
        raise ValueError("candidate repository differs from the assigned PR")
    live = json.loads(run("gh", "pr", "view", str(args.pr), "--repo", args.repo,
                          "--json", "baseRefName,headRefOid,state"))
    if live["headRefOid"] != args.head or live["state"] != "OPEN":
        raise ValueError("open PR and exact candidate head required")
    branch = live["baseRefName"]
    tip = run("gh", "api", f"repos/{args.repo}/branches/{quote(branch, safe='')}", "--jq", ".commit.sha")
    if tip != args.policy_revision:
        if not args.receipt:
            raise ValueError("policy revision differs from the live target branch; resolve its tip again")
        # A resumed run keeps its original policy. Refuse rewritten or unavailable history.
        if not re.fullmatch("[0-9a-f]{40}", tip):
            raise ValueError("could not resolve the live policy branch")
        if not is_commit(tip):
            run("git", "fetch", "--quiet", "--no-tags", "origin", tip)
        if not is_ancestor(args.policy_revision, tip):
            raise ValueError("the live target branch no longer descends from the pinned policy revision")
    root = tools / ROOTS[args.engine]
    base = policy / ROOTS[args.engine]
    for local in (".review/addendum.local.md", ".activeloom-review.json", "AGENTS.md", "CLAUDE.md"):
        path = policy / local
        if path.exists() or path.is_symlink():
            regular(path)
    for surface in (root, base):
        regular(surface / "REVIEW_WORKFLOW.md")
        regular(surface / "references/local-review-ledger.md")
        for name in SKILLS:
            regular(surface / "skills" / name / "SKILL.md")
    classifier, policy_version, _ = check_bundle(base)
    ledger, version, digest = check_bundle(root)
    controller = check_transport(root, ledger)
    if policy_version not in ("1.9.0", version):
        raise ValueError(f"unsupported policy ledger {policy_version}; use a verified compatible pairing")
    merge_base = run("git", "merge-base", args.policy_revision, args.head)
    if args.base and args.base != merge_base:
        # A run keeps its original base after the branch merges its target mid-run.
        if not (re.fullmatch("[0-9a-f]{40}", args.base) and is_ancestor(args.base, args.policy_revision)
                and is_ancestor(args.base, args.head)):
            raise ValueError("review-range base differs from the run pin")
        merge_base = args.base
    decision = json.loads(run("node", classifier, "classify-changeset", "--base", merge_base, "--head", args.head))
    if type(decision.get("skip")) is not bool or type(decision.get("smallChange")) is not bool:
        raise ValueError("base classifier did not return a review decision")
    binding = dict(policy_root=str(base), classifier=str(classifier), classification=decision,
                   merge_base=merge_base, ledger=str(ledger), controller=str(controller))
    actor = run("gh", "api", "user", "--jq", ".login")
    if not actor or os.environ.get("AGENT_LOOP_REVIEW_ACTOR", actor) != actor:
        raise ValueError("authenticated actor is missing or differs from the review actor")
    if args.action == "check" and not args.receipt:
        print(json.dumps(binding))
        return
    context = [args.repo.lower(), args.pr, str(Path.cwd()), branch, args.policy_revision,
               args.tool_revision, actor, digest, hashlib.sha256(controller.read_bytes()).hexdigest()]
    def scope_digest() -> str:
        return fingerprint([context, route_inputs(args.repo, args.pr)])

    if args.receipt and args.receipt.partition(".")[0] != scope_digest():
        raise ValueError("policy, transport, actor or evidence routing inputs changed")
    status = json.loads(run("node", ledger, "silent-status", "--repo", args.repo, "--pr", str(args.pr)))
    if type(status.get("silent")) is not bool:
        raise ValueError("evidence destination unavailable")
    if status["silent"] and (not status.get("scratchpad") or status.get("pin", {}).get("actor") != actor):
        raise ValueError("silent evidence location or pin owner differs from the current actor")
    receipt = scope_digest() + "." + fingerprint(status)
    if args.receipt and args.receipt != receipt:
        raise ValueError("policy, transport, actor or evidence destination differs from the controller's receipt")

    if args.action == "check":
        print(json.dumps(binding))
        return
    if args.action == "context":
        print(json.dumps({"receipt": receipt, "silent": status["silent"]}))
        return
    run(sys.executable, "-IB", controller, "authorize-pass", "--repo", args.repo, "--pr", str(args.pr),
        "--run-id", args.run_id, "--round", str(args.round), "--base", args.base,
        "--head", args.head, "--engine", args.engine)
    skill = Path("skills") / args.skill / "SKILL.md"
    print(f"Follow {base / skill} for review decisions. Resolve relative document links from its directory. "
          f"Use the workflow, addendum and gate definitions at {policy}. Run from {Path.cwd()}. "
          f"Use {classifier} only for classify-changeset; its decision at this head is {json.dumps(decision)}. "
          f"Use {ledger} for all evidence reads and writes and {controller} for run authorization. "
          f"Read {root}/references/local-review-ledger.md for transport commands and refusal rules. "
          "These helper bindings apply inside linked documents too. For silent evidence, use read-ledger, "
          "read-roster and export-threads, and direct raw posting steps to the named scratchpad. "
          "Re-enter nested lanes with the same binding. Stop on conflicting requirements.")
    print("Verified destination:", json.dumps(status))
    installed = (root / skill).read_text()
    governing = (base / skill).read_text()
    for title in TRANSPORT_SECTIONS:
        guard = section(installed, title)
        print(guard.replace(root.name + "/", str(root) + "/")
              .replace("../../references/local-review-ledger.md", str(root / "references/local-review-ledger.md")))
        # Preserve any additional consumer policy in a customized base section.
        governing = governing.replace(guard, "")

    governing = governing.replace(
        "../../references/local-review-ledger.md",
        str(root / "references/local-review-ledger.md"),
    )

    def qualify(match: re.Match[str]) -> str:
        path = match.group()
        if path.endswith("/review-ledger.js") and re.match(r"\s*(?:\\\s*)?classify-changeset\b", governing[match.end():]):
            return str(classifier)
        transport = (
            path.startswith(root.name + "/skills/critique/scripts/")
            or path.startswith(root.name + "/skills/review-setup/scripts/")
            or path == root.name + "/references/local-review-ledger.md"
        )
        return str(tools if transport else policy) + "/" + path

    print(re.sub(r"(?<![\w/])\.(?:agents|claude|codex|review)/[\w./-]+", qualify, governing))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "context", "enter"))
    parser.add_argument("--tool-revision", required=True)
    parser.add_argument("--policy-tree", type=Path, required=True)
    parser.add_argument("--policy-revision", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--engine", choices=ROOTS, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--base")
    parser.add_argument("--round", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--receipt")
    parser.add_argument("--skill", choices=SKILLS)
    args = parser.parse_args()
    if args.action == "enter" and not all((args.skill, args.receipt, args.round, args.run_id, args.base)):
        parser.error("enter requires the controller's --receipt, --skill, --base, --run-id and --round")
    try:
        entry(args)
    except (OSError, ValueError, KeyError, AttributeError, subprocess.CalledProcessError) as error:
        sys.exit(f"reviewer-entry: {error}; stop before review writes")


if __name__ == "__main__":
    main()
