# Shared budget protocol; rendered into the three state helpers.
def _budget_validate(value: dict[str, Any]) -> None:
    budget = value.get("reviewBudget")
    if value["version"] == LEGACY_STATE_VERSION:
        if budget is not None:
            _fail("legacy state cannot carry an active budget")
        return
    if not isinstance(budget, dict) or set(budget) != {"limit", "remaining", "attempts", "migration"}:
        _fail("invalid active review budget")
    limit, remaining = budget["limit"], budget["remaining"]
    if type(limit) is not int or limit < 1 or type(remaining) is not int or not 0 <= remaining <= limit:
        _fail("invalid remaining review budget")
    if "reviewDeadlineEpoch" in value or type(value.get("reviewMaxRounds")) is not int or not 1 <= value["reviewMaxRounds"] <= 4:
        _fail("active budget requires a round cap and no absolute deadline")
    attempts = budget["attempts"]
    if not isinstance(attempts, list):
        _fail("invalid budget attempt history")
    charged = 0
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict) or set(attempt) != {"id", "reserved", "charged", "startedNs", "boot", "owner", "status", "reason"}:
            _fail("invalid budget attempt")
        if attempt["id"] != index + 1 or attempt["status"] not in {"active", "settled", "abandoned"}:
            _fail("invalid budget attempt sequence")
        for key in ("reserved", "charged", "startedNs", "owner"):
            if type(attempt[key]) is not int or attempt[key] < 1:
                _fail("invalid budget attempt counter")
        if attempt["charged"] > attempt["reserved"] or not isinstance(attempt["boot"], str) or not attempt["boot"]:
            _fail("invalid budget attempt charge")
        if not isinstance(attempt["reason"], str):
            _fail("invalid budget attempt reason")
        if attempt["status"] != "settled" and attempt["charged"] != attempt["reserved"]:
            _fail("unfinished execution must retain its full reservation")
        charged += attempt["charged"]
    migration = budget["migration"]
    initial = limit
    if migration is not None:
        if not isinstance(migration, dict) or set(migration) != {"sha256", "remaining", "reason", "actor", "epoch", "deadline"}:
            _fail("invalid budget migration audit")
        initial = migration["remaining"]
        if type(initial) is not int or not 0 <= initial <= limit or not SHA256_RE.fullmatch(str(migration["sha256"])):
            _fail("invalid migrated budget")
        if not all(isinstance(migration[k], str) and migration[k].strip() for k in ("reason", "actor")):
            _fail("invalid migration operator evidence")
    if initial - charged != remaining:
        _fail("review budget accounting does not balance")


def _budget_new(seconds: int) -> dict[str, Any]:
    return {"limit": seconds, "remaining": seconds, "attempts": [], "migration": None}


def _budget_boot() -> str:
    import subprocess

    # Boot identity prevents refunds across unrelated monotonic-clock origins.
    try:
        if sys.platform == "darwin":
            boot = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                                  check=True, capture_output=True, text=True, timeout=5).stdout.strip().lower()
        elif sys.platform.startswith("linux"):
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        else:
            _fail("review budget boot identity is unavailable on this platform")
    except (OSError, subprocess.SubprocessError):
        _fail("review budget boot identity is unavailable")
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot):
        _fail("review budget boot identity is invalid")
    return boot


def _budget_owner_exists(pid: int) -> bool:
    if sys.platform.startswith("linux"):
        return Path(f"/proc/{pid}").exists()
    if sys.platform != "darwin":
        _fail("review budget controller probe is unavailable on this platform")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        _fail("review budget controller probe is unavailable")
    return True


@contextmanager
def _budget_lock(path: Path, inherited: int | None) -> Iterator[None]:
    metadata = os.lstat(path.parent)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        _fail("budget requires an owner-controlled private log directory")
    descriptor = inherited if inherited is not None else os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail("budget lock does not name the run log directory")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("another process already owns this agent-loop run")
        yield
    finally:
        if inherited is None:
            os.close(descriptor)


def _budget_command(args: argparse.Namespace) -> None:
    import hashlib
    import time

    path = Path(args.file)
    with _budget_lock(path, args.lock_fd):
        value = _read(path)
        if str(path.parent.resolve()) != value["logDir"]:
            _fail("run state file is outside its recorded log directory")
        if args.command == "budget-migrate":
            if value["version"] != LEGACY_STATE_VERSION:
                _fail("only a legacy checkpoint can migrate; active budgets cannot be replenished")
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != args.expected_sha256:
                _fail("checkpoint changed since operator inspection")
            if not args.reason.strip() or not 1 <= args.limit_seconds <= 86400 or not 0 <= args.remaining_seconds <= args.limit_seconds:
                _fail("migration requires a reason and 0 <= remaining <= limit <= 86400 seconds")
            cap = value.get("reviewMaxRounds")
            if cap is None:
                cap = args.max_rounds
            elif args.max_rounds is not None and args.max_rounds != cap:
                _fail("migration cannot change the saved round cap")
            if type(cap) is not int or not 1 <= cap <= 4:
                _fail("legacy checkpoint without a round cap requires --max-rounds (1..4)")
            backup = path.with_name(path.name + ".legacy-" + digest + ".json")
            if backup.exists():
                if backup.is_symlink() or backup.read_bytes() != raw:
                    _fail("legacy backup differs")
            else:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            value["version"] = STATE_VERSION
            value["reviewMaxRounds"] = cap
            value["reviewBudget"] = _budget_new(args.limit_seconds)
            value["reviewBudget"]["remaining"] = args.remaining_seconds
            value["reviewBudget"]["migration"] = {
                "sha256": digest, "remaining": args.remaining_seconds,
                "reason": args.reason, "actor": f"uid:{os.getuid()}",
                "epoch": int(time.time()), "deadline": value.pop("reviewDeadlineEpoch", None),
            }
            _atomic_write(path, value)
            print(json.dumps(value["reviewBudget"], sort_keys=True))
            return
        if value["version"] != STATE_VERSION or "reviewBudget" not in value:
            _fail("legacy review budget requires explicit budget-migrate; no time is inferred from deadlines or mtimes")
        budget = value["reviewBudget"]
        pending = [a for a in budget["attempts"] if a["status"] == "active"]
        if args.command == "budget-reconcile":
            if hashlib.sha256(path.read_bytes()).hexdigest() != args.expected_sha256 or not args.reason.strip():
                _fail("reconciliation requires an unchanged checkpoint and an operator reason")
            for attempt in pending:
                if attempt["boot"] == _budget_boot() and _budget_owner_exists(attempt["owner"]):
                    _fail("recorded controller still exists; stop it before reconciliation")
                attempt["status"] = "abandoned"
                attempt["reason"] = f"uid:{os.getuid()} epoch:{int(time.time())} {args.reason}"
            _atomic_write(path, value)
        elif args.command == "budget-finish":
            if len(pending) != 1 or pending[0]["id"] != args.attempt:
                _fail("budget completion does not match the active reservation")
            attempt = pending[0]
            now = time.monotonic_ns()
            if attempt["boot"] != _budget_boot() or now < attempt["startedNs"] or attempt["owner"] != args.owner:
                _fail("budget clock or execution owner changed; full reservation retained")
            elapsed = max(1, (now - attempt["startedNs"] + 999999999) // 1000000000)
            charged = min(attempt["reserved"], elapsed)
            budget["remaining"] += attempt["reserved"] - charged
            attempt.update(status="settled", charged=charged)
            _atomic_write(path, value)
        else:
            if pending:
                _fail("unfinished budget reservation: confirm workers stopped, then budget-reconcile; no refund is available")
            if args.command == "budget-begin":
                if type(args.seconds) is not int or args.seconds < 1 or args.seconds > budget["remaining"]:
                    _fail("review execution budget exhausted")
                attempt = {"id": len(budget["attempts"]) + 1, "reserved": args.seconds,
                           "charged": args.seconds, "startedNs": time.monotonic_ns(),
                           "boot": _budget_boot(), "owner": args.owner, "status": "active", "reason": ""}
                budget["remaining"] -= args.seconds
                budget["attempts"].append(attempt)
                _atomic_write(path, value)
                print(attempt["id"])
                return
        print(budget["remaining"])


def _budget_parser(commands: Any) -> None:
    for name in ("budget-show", "budget-begin", "budget-finish", "budget-migrate", "budget-reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--file", required=True)
        command.add_argument("--lock-fd", type=int)
        if name in {"budget-migrate", "budget-reconcile"}:
            command.add_argument("--expected-sha256", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--confirm-stopped", action="store_true", required=True)
        if name == "budget-migrate":
            command.add_argument("--remaining-seconds", type=int, required=True)
            command.add_argument("--limit-seconds", type=int, required=True)
            command.add_argument("--max-rounds", type=int)
        if name in {"budget-begin", "budget-finish"}:
            command.add_argument("--owner", type=int, required=True)
        if name == "budget-begin":
            command.add_argument("--seconds", type=int, required=True)
        if name == "budget-finish":
            command.add_argument("--attempt", type=int, required=True)
        command.set_defaults(handler=_budget_command)
