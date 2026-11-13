# Protected autonomy policy

This foundation provides a protected policy reader, a human-run installer/setter,
and restart provenance. It does not enable automatic herd orchestration or wrap
worker launches. The orchestration integration and containment are separate work.

With no installation, `herd-config.py policy` reports manual behavior and network
unenforced. A partial or invalid installation disables automatic authority and
blocks restricted launches. A protected `full`/`restricted` policy also blocks
launches: containment preflight is **unavailable** for every worker/reviewer engine.
Only an explicitly installed `network.mode: open` can currently yield effective
`full`. Open mode reports a warning. These files are protected from edits, not yet
from same-user workers; unwrapped herders remain trusted. GitHub records provide
audit evidence, not proof against someone with the same account credentials.

## Human installation and changes

The canonical artifact is `prompts/skills/hreview/scripts/autonomy-policy.py`;
installed consumers receive it beside `herd-config.py` in their harness root.
`horch` may print the following command with the absolute path of the reviewed
artifact. **Only the user runs it in their own terminal.**

```text
sudo /usr/bin/python3 -I <absolute-path>/autonomy-policy.py install
```

The installer uses `/etc/activeloom/` on Linux and `/private/etc/activeloom/`
on macOS, with group `wheel` on macOS. It installs a root-owned setter named
`activeloom-policy` (0755), `policy.json` (0644), and
`/etc/sudoers.d/activeloom-policy` (0444; `/private/etc` on macOS).
The setter uses the system Python in isolated mode. Symlinks, non-root ownership,
writable ancestors, and unexpected file permissions are refused. The sudoers file
contains `Defaults!<absolute-setter-path> timestamp_timeout=0`; it grants no new
sudo permission. The user's existing sudo policy must permit the setter.

The drop-in contains no secrets and is publicly readable so the unprivileged
reader can verify it. It is writable by nobody; replacement requires root and a
protected parent. Sudo's [secure-path check](https://github.com/sudo-project/sudo/blob/main/lib/util/secure_path.c)
rejects unsafe ownership and write permissions, rather than public readability.
Sites requiring a different drop-in mode remain unsupported by this reader.

Public readability is not enough when the sudoers directory itself cannot be
searched by the reader, as with the `root:root 0750` `/etc/sudoers.d` shipped by
Debian and Ubuntu. There the reader cannot verify the drop-in at all, and it does
not guess:

- With no `/etc/activeloom/`, the report is never-installed: manual behavior,
  network unenforced, not launch-blocked. The reason notes the unsearchable
  directory. The installer creates `/etc/activeloom/` before the drop-in and a
  failed install removes the drop-in before the directory, so on such hosts a
  drop-in without `/etc/activeloom/` is reported as never installed. Where the
  drop-in is visible, it alone still counts as a partial installation.
- With `/etc/activeloom/` present, the report is installed, unprotected and
  launch-blocked, with reason `sudoers drop-in unverifiable`. Automatic
  authority stays disabled on such hosts until the drop-in can be verified.

Each file is replaced atomically and synced. The installer checks the staged
sudoers fragment and the complete sudoers configuration with `visudo -c`, including
evidence that the fragment was included, before activating the initial policy.
Inclusion evidence depends on the `visudo` in use, identified by `visudo -V`:

- Classic sudo lists every file it parsed; the output must contain the line
  `<drop-in path>: parsed OK`.
- sudo-rs (`visudo-rs`) parses included files but prints only
  `<sudoers path>: parsed OK`. The output must be exactly that line, and the
  sudoers file must carry a top-level `@includedir` or `#includedir` directive
  naming the drop-in's directory. A drop-in reached only through a nested or
  single-file `@include` is refused.
- sudo-rs reads `/etc/sudoers-rs` instead of `/etc/sudoers` when it exists.
  Inclusion is proven only in `/etc/sudoers`, so installation is refused while
  `/etc/sudoers-rs` exists.

Initial values are `full`, no stops, restart budget 2, and `restricted` with an
empty additional allowlist. Installation refuses to overwrite an existing or
partial installation. A failed installation removes the drop-in first, then
everything else it created. A crash can leave protected partial artifacts.
If drop-in removal fails, rollback stops and retains the policy directory so an
unprivileged reader still detects the partial installation. Both the
incomplete-rollback error and the existing-installation refusal name the exact
cleanup (`sudo rm -f /etc/sudoers.d/activeloom-policy && sudo rm -rf
/etc/activeloom`; `/private/etc` on macOS). A complete installation is changed
with `set`, not removed; run the cleanup only after confirming the artifacts are
a failed install. Partial artifacts are never treated as never installed, except
for a lone drop-in in an unsearchable sudoers directory (above).

For a complete replacement on Linux (use `/private/etc` on macOS):

```text
sudo /etc/activeloom/activeloom-policy set --autonomy full --network open --restart-budget 2
sudo /etc/activeloom/activeloom-policy set --autonomy manual --network restricted --stop restart
```

`--stop` and `--allow` repeat; omitted lists are empty. Allowed stops are `restart`,
`restart-after-exhausted`, `scope-decision`, and `author`. Unknown stops fail.
Additional allowlist entries are lowercase DNS hosts, without schemes, paths,
ports or wildcards. This foundation stores them but enforces no network filter.

## Read-only report and restart records

```text
python3 <harness>/skills/hreview/scripts/herd-config.py policy --repo owner/repo --pr 123 --pr 456
```

The report includes configured and effective mode, stops, network, protection
reason, containment status, and the requested PR budgets. An unavailable inventory
reports `used: null`, never zero. Without `--pr`, no GitHub budget inventory is
requested. Reading the report writes no configuration or approval files.

After checking protected files and their ancestors, the reader invokes only
noninteractive probes: `sudo -n -l <setter> --check` and
`sudo -n <setter> --check`. Success of the latter means passwordless protection
and disables automatic authority. A password-required diagnostic is accepted
only with positive exact-command permission evidence from the former. Denials,
missing executables, configuration errors, unfamiliar/localized diagnostics and
inconclusive listing disable authority. The accepted password-required
diagnostics are exactly `sudo: a password is required` (classic sudo) and
`sudo: interactive authentication is required` (sudo-rs). Some sudo configurations cannot supply
that evidence noninteractively; those remain disabled. No cached-ticket claim is
inferred from unit tests.

The reader also requires a complete `sudo -n -l` permission listing with no
`NOPASSWD` or `!authenticate` entries. Otherwise a passwordless `set` rule could
hide behind a password-required `--check`. This deliberately rejects even
unrelated passwordless rules; uncertain protection never enables authority.

```text
python3 <harness>/skills/hreview/scripts/herd-config.py record-policy-restart --repo owner/repo --pr 123 --head <full-sha> --scope-decision keep
```

This command checks current protected policy, the exact live head, authenticated
ended run, complete GitHub run history, and remaining per-PR automatic budget.
Exhausted runs also require a changed head and no `restart-after-exhausted` stop.
The record contains policy digest, ended run/outcome, and `budget_used` as the
ordinal the new automatic restart will consume. It does not imply Deep review.
Pending records are preserved; resume the original transaction instead of
minting another. A consumed record is archived before a later ended run at the
same head receives its next budgeted record. Scope choices and their
orchestration belong to the caller.

`start-run --restart --restart-approval-id <id>` stamps
`local-review-restart:v1` into the hashed run content after scope/plan markers.
Authorization files remain plain prose and reject `<!-- local-review` markers.
User approvals stamp `source=user-approval`, with no policy digest or automatic
budget ordinal; legacy local user approvals remain readable. Historical restarts
without provenance make the automatic inventory inconclusive and block it.
Manual user approval still works and does not reset the automatic count.

Policy, containment/open mode, budget and parent binding are rechecked inside
transaction reservation immediately before publication. A changed policy or spent
budget prevents publication even for an existing intent. Reconciliation of an
already-published identical run does not reserve again and remains possible after
policy revocation or expiry. It cannot publish another run.

## Evidence still required

Interactive Claude `!` sudo authentication: **unrun**. That route is not supported
by these instructions. Live privileged installation and authentication on Linux
sudo, sudo-rs and macOS sudo: **unrun**. Offline fixtures exercise classification,
path protection and failed installation; they are not platform demonstrations.
An unprivileged syntax-only check of the drop-in passed with local `visudo`
(sudo-rs) and `visudo.ws`; it did not install the fragment or authenticate.
The sudo-rs fixtures use strings and output shapes from sudo-rs 0.2.13 binaries
and an unprivileged `visudo -c` run. Whether sudo-rs's `sudo -n -l <setter>
--check` can list without a cached credential is **unrun**. Where it cannot, the
reader reports authority disabled.
Containment, scoped credentials and same-user tamper resistance are unimplemented
and unavailable here.
