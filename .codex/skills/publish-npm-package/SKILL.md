---
name: publish-npm-package
description: Prepare, bootstrap, publish, and verify npm package releases with GitHub Actions Trusted Publishing or a tightly gated first manual publication. Use when Codex needs to create or harden an npm release workflow, configure npm package trust and GitHub environments, publish a new or existing package, create release tags, compare built and registry artifacts, or verify registry signatures and SLSA provenance.
---

# Publish an npm package

Treat each release as an immutable supply-chain event. Build one tarball, inspect
it, publish that exact file, and compare it with the registry copy. Prefer npm
Trusted Publishing through OIDC. Use a manual credential only when a package does
not exist yet and npm therefore cannot accept a Trusted Publisher configuration.

## Preserve authority boundaries

Keep these as separate approval gates. Do not infer one from another:

1. **Merge:** stop before merging the release PR.
2. **Tag push:** stop before pushing the signed annotated release tag.
3. **Environment approval:** stop when the GitHub Actions job reaches a protected
   environment. Never approve or bypass it without explicit authorization.
4. **Credential use:** stop before logging in, creating or reading a token, or
   using an existing npm session.
5. **Manual bootstrap publish:** after credential approval, show the exact package,
   version, tarball path, digest, registry, access, command, impact, and recovery;
   obtain a separate explicit approval before running `npm publish`.

Treat deployment/environment approval and npm website configuration as external
state changes. Ask for authorization even if the current identity can perform
them. Never merge, push a tag, approve an environment, or publish as a side effect
of “prepare,” “review,” or “verify.”

## Reconstruct the release contract

1. Read repository instructions and release documentation.
2. Work in a dedicated linked worktree. Preserve unrelated changes.
3. Identify the package directory, package name, version, public/private status,
   repository URL, package manager, build command, test command, release workflow,
   tag convention, registry, and npm access mode.
4. Determine whether the exact package already exists by running `npm view
<name> --registry=<registry>`. Do not treat an authentication or network error
   as “package absent.”
5. Select one path:
   - Existing package: use Trusted Publisher.
   - New package with no npm settings page: use the bootstrap exception, then
     configure Trusted Publisher immediately after the first publication.
6. Read [references/upstream-docs.md](references/upstream-docs.md) and verify every
   linked upstream contract live. Record the URLs, access date, chosen Node/npm
   versions, action revisions, runner requirement, and any discrepancy in the PR
   or handoff. Do not copy stale version examples from this skill.

## Design the Trusted Publisher workflow

Prefer staged publishing for every existing package when the live npm contracts
support the required artifact, provenance, and registry controls. Use an
unprivileged build job and a minimal protected stage job with forward-only
artifact transfer, then stop for an independent maintainer to inspect and
approve the staged artifact with 2FA. Run unprivileged verification only after
the approval makes the package public. Use direct publishing only for a
brand-new package or when a recorded live-contract discrepancy makes staging
unavailable without weakening the build-once or no-package-code guarantees.

Use a GitHub-hosted runner supported by npm. Grant `id-token: write` only to the
protected stage or publish job; the build and verification jobs must explicitly
omit it. Reference the protected GitHub environment only on that OIDC job.
Configure the environment with required reviewers, prevent self-review when
available, restrict deployment tags to the package's release-tag pattern, and
disable administrator bypass when the repository's policy supports it. The
release author must never be the sole human approver. Treat the npm staged-package
approval as an additional proof-of-presence gate, not as a replacement for the
independent environment review.

Pin every action, including `actions/checkout`, `actions/setup-node`, artifact
upload, and artifact download, to reviewed full commit SHAs while retaining
release comments. Select Node and npm versions that satisfy the current npm
Trusted Publisher documentation. This skill's verifier has a stricter npm floor:
require npm 11.12.0 or newer so `npm audit signatures --include-attestations`
exists before the irreversible publish. Staged publishing currently requires a
still newer CLI; select a version that satisfies the live `npm stage`
documentation rather than treating the verifier floor as sufficient. The minimal
stage or publish job may execute only reviewed pinned platform actions, the
selected npm CLI, and runner-provided
checksum tooling. It must not check out the repository, install dependencies,
run package lifecycle scripts, execute repository-controlled helpers, or accept a
cache restored from another job. Establish its Node/npm toolchain without running
package code while the OIDC request capability is present.

Treat the runner image, executable search path, Git installation, npm CLI,
OpenSSL binary, Git configuration, keyring, and approved signer material as the
trusted computing base. Use a trusted ephemeral host, resolve the selected
executables from a reviewed toolchain, and record their paths and versions before
relying on helper output. The helpers scrub credential and TLS-related ambient
configuration; they do not sandbox or authenticate the host tools they invoke.

Configure npm trust on the package settings page with the exact GitHub owner,
repository, workflow filename, environment name, and allowed action. For an
existing package, allow `npm stage publish` and disallow direct `npm publish`.
Permit direct publish only for the explicit fallback above. npm fields are exact
and case-sensitive, and npm may not validate them until publish.
After a successful OIDC release, require two-factor authentication and disallow
traditional publish tokens when the current npm controls allow it. Keep any token
needed solely to install private dependencies read-only and expose it only to the
install step, never the publish step.

Build once in the unprivileged build job:

1. Install from the committed lockfile.
2. Run the repository's tests and build.
3. Run `npm pack` once, write the tarball outside the Git worktree, and capture
   its exact output path.
4. Run `scripts/release-preflight.py --phase publish` against that tarball and
   release tag. Ensure checkout fetched the annotated tag and configure the
   signer's public verification material so `git verify-tag` can succeed. Pass
   the approved signer fingerprint explicitly.
5. Upload the tarball and preflight JSON together as a uniquely named workflow
   artifact. Record their digests. The protected stage or publish job must depend
   on this job and download only that exact artifact from the same workflow run.

In the protected stage job, verify the downloaded digests with runner-provided
tooling and stage the exact tarball path with `npm stage publish`,
`--ignore-scripts`, and `--registry=<preflight-approved-registry>`. Confirm the
current CLI accepts the tarball path and flags before granting OIDC. The registry
CLI argument is mandatory:
it must override repository, package, user, or runner configuration, and preflight
must reject a conflicting `publishConfig.registry` in either the source manifest
or packed tarball. For a public source repository also pass `--provenance`; this
CLI argument must override repository or user configuration that disables
provenance. For a private source repository publishing a public package, npm
Trusted Publishing works but provenance is unsupported: pass `--provenance=false`
and record that limitation before staging. Do not run the stage or publish
command against a directory, rerun `npm pack`, rebuild, install dependencies, or
execute any file from the repository or package artifact in this job.

```bash
npm stage publish <built-package.tgz> \
  --ignore-scripts \
  --access public \
  --registry=<preflight-approved-registry> \
  --provenance
```

After staging, stop and report the package, version, stage identifier, registry,
release tag and commit, workflow run, original artifact digests, and exact review
steps. An independent maintainer must inspect the staged metadata, download the
staged tarball, prove it is byte-identical to the build-once artifact, and then
approve it through npm with 2FA. The release author must not approve alone. Reject
instead of approve on any mismatch, but obtain explicit authorization before
either irreversible npm action. Never copy or transmit an OTP.

```bash
npm stage view <stage-id> --json --registry=<preflight-approved-registry>
npm stage download <stage-id> --registry=<preflight-approved-registry>
# Compare the downloaded bytes with the recorded build-once artifact.
npm stage approve <stage-id> --registry=<preflight-approved-registry>
```

Show the exact approval command and stop for authorization before running it.
Let npm prompt the authorized maintainer for 2FA; never put the OTP on the command
line or into agent-visible input.

Because approval happens outside the OIDC workflow, start verification as a
separate credential-free workflow or explicitly authorized local check after the
version becomes public. Bind it to the original run, artifact digests, tag, and
commit. Do not keep a privileged job polling for approval.

Run `verify-published-package.py --provenance required` for a public source, or
`--provenance unavailable` for the explicit private-source/public-package branch,
in that separate verification path with no `id-token: write`. It may check out
the tagged source and download the same build artifact. Fail it if it cannot
verify the artifact, registry signatures, applicable provenance
certificate/workflow binding, and live release tag. Upload the verification JSON
as a workflow artifact.

For the explicit direct-publish fallback, retain the same privilege separation:
use an unprivileged build job, a minimal independently approved publish job, and
an unprivileged verification job. Publish the exact tarball with
`npm publish <built-package.tgz> --ignore-scripts --access public
--registry=<preflight-approved-registry>` plus the required provenance flag.

Do not provide `NODE_AUTH_TOKEN` to the OIDC stage or publish step. Decide whether
to set `actions/setup-node`'s `registry-url` only after reading its current documentation:
that input writes package-manager authentication configuration, and its behavior
has changed between action releases. Pass `--registry` to npm commands explicitly
when that avoids ambiguous ambient configuration.

## Prepare and sign the immutable release tag

Run preflight in `prepare` phase before creating the tag:

Write the tarball and every generated JSON report to a protected temporary or
artifact-staging directory outside the Git worktree. The helpers reject in-tree
outputs and reject an output path that aliases the build-once tarball.

```bash
python3 <skill-dir>/scripts/release-preflight.py \
  --package-dir <package-dir> \
  --artifact <built-package.tgz> \
  --tag <release-tag> \
  --phase prepare \
  --access public \
  --output <preflight.json>
```

After merge authorization and a clean, verified release commit, create a signed
annotated tag, never a lightweight tag:

```bash
git tag -s '<release-tag>' -m '<package-name> <version>' '<release-commit>'
git verify-tag '<release-tag>'
```

Run preflight again with `--phase tag`; it verifies that the tag is annotated,
signed by the explicitly approved `--signer-fingerprint`, targets `HEAD`, is not
already on the remote, and that the package version is still unpublished. Show
the tag object, target commit, signature result, and
exact `git push origin 'refs/tags/<release-tag>'` command, then stop for tag-push
approval.

The workflow's `publish` phase requires the remote tag to exist and match the
local annotated tag object. The `prepare` and `tag` phases require it to be absent.

Never move, delete, recreate, or force-push a failed release tag. Diagnose the
failure, fix it in a new commit, increment the package version, and create a new
signed annotated tag. Preserve the failed run and tag as release history.

## Bootstrap a brand-new package

Use this exception only when npm cannot configure package trust because the exact
package has never been published.

The bundled helpers intentionally support anonymously readable public packages
only. Stop for a private package or private registry access path; do not interpret
an anonymous 404 as version absence and do not pass read credentials into these
verification helpers.

1. Complete the same clean-worktree, version-absence, test, build-once, tarball
   inspection, PR, merge, and signed-tag gates.
2. Confirm package ownership/scope, public access, name availability, and npm's
   current first-publication rules live.
3. Prefer interactive `npm login` with two-factor authentication over copying a
   token. Never print, inspect, persist, or transmit `.npmrc`, cookies, OTPs, or
   credential values.
4. Stop at the credential-use gate. State why OIDC cannot yet be configured, what
   credential mechanism will be used, its scope/lifetime, and the logout/revocation
   plan.
5. Once authorized, run `npm whoami --registry=<registry>` without exposing the
   credential. This confirms identity, not permission to publish.
6. Show the exact command for the already-inspected tarball and stop again at the
   manual-publish gate:

   ```bash
   npm publish <built-package.tgz> --ignore-scripts --access public --registry=<registry>
   ```

7. Publish only after explicit approval. Do not imply that a local manual publish
   has CI SLSA provenance. Registry signatures and provenance are different
   claims.
8. Immediately verify artifact integrity with the `unavailable` invocation
   below, configure the package's Trusted Publisher and GitHub environment, and
   restore the strongest token restrictions available. `unavailable` waives only
   the SLSA attestation. Everything else still applies: the artifact must be
   byte-identical to the registry tarball, npm distribution metadata must
   declare at least one registry signature, the isolated signature audit must
   not report the target as `invalid` or `missing`, the signed annotated tag is
   still checked against the approved signer and the remote, and verification
   fails if the package turns out to carry provenance after all. It does not
   require the target in npm's `verified` list — npm populates that only for
   attested packages — so this mode proves integrity and tag provenance, not a
   verified registry signature for the exact name@version.

   Use the `required` command block below minus `--workflow-path`, which the
   verifier rejects outside `--provenance required`:

   ```bash
   python3 <skill-dir>/scripts/verify-published-package.py \
     --package <package-name> \
     --version <version> \
     --artifact <built-package.tgz> \
     --access public \
     --provenance unavailable \
     --source-repository https://github.com/<owner>/<repository> \
     --tag <release-tag> \
     --commit <release-commit> \
     --repository-dir <release-checkout> \
     --remote origin \
     --signer-fingerprint <approved-fingerprint> \
     --output <verification.json>
   ```

9. If the login/session was created for bootstrap, run `npm logout
--registry=<registry>` and confirm `npm whoami --registry=<registry>` no longer
   authenticates. Revoke any separately created token through the approved npm
   account path. Do not delete a pre-existing user session without permission.
10. Publish a subsequent version through OIDC and require provenance verification
    before calling the release path established.

## Verify the registry result

Download and verify; never trust the workflow's success status alone:

```bash
python3 <skill-dir>/scripts/verify-published-package.py \
  --package <package-name> \
  --version <version> \
  --artifact <built-package.tgz> \
  --access public \
  --provenance required \
  --source-repository https://github.com/<owner>/<repository> \
  --workflow-path .github/workflows/<publish-workflow.yml> \
  --tag <release-tag> \
  --commit <release-commit> \
  --repository-dir <release-checkout> \
  --remote origin \
  --signer-fingerprint <approved-fingerprint> \
  --output <verification.json>
```

Require all of these for a normal Trusted Publisher release:

- `npm view <name>@<version>` reports the intended version and registry tarball.
- The local build-once artifact, npm `dist.shasum`, npm `dist.integrity`, and the
  downloaded registry tarball agree.
- The tarball's embedded `package.json` has the intended name and version.
- npm distribution metadata declares at least one registry signature, and `npm
audit signatures` completes without reporting the target as `invalid` or
  `missing`. With `--provenance required` the exact target name@version must
  also appear in the `verified` results. With `--provenance unavailable` an
  empty `verified` list is accepted: npm lists a package there only once it
  carries attestations, so requiring it would reject every genuinely
  unprovenanced release the mode exists for.
- The decoded SLSA statement binds the intended source repository, workflow,
  release tag, commit, package identity, artifact SHA-512, GitHub-hosted builder,
  and signing-certificate workflow identity plus the GitHub Actions OIDC issuer.
  Confirm npm's UI shows the same source as a defense-in-depth manual check.
- The remote tag remains the original signed annotated tag and targets the release
  commit.

`--source-repository` is required in both provenance modes: it binds the release
checkout used for tag verification to the published package's source repository.

Provenance is available only under npm's current supported conditions. A manual
bootstrap publication and the explicit private-source/public-package Trusted
Publishing branch report the unavailable control. Private packages are outside
these credential-free helpers and require a separately designed, explicitly
authorized verification path. Do not downgrade `required` to `unavailable`
merely to make verification pass.

## Fail closed and hand off

Stop on a dirty worktree, existing version, existing remote tag, tag-signature
failure, artifact mismatch, missing registry signature, missing required
attestation, wrong workflow/environment trust binding, unexpected credential
prompt, publish error, or verification error. Do not retry a publish until npm
confirms whether the version exists. Never reuse the failed tag or version if npm
accepted it.

Report the package/version, release commit, tag object and target, workflow run,
artifact SHA-256/SHA-512, registry checksum comparison, signature/attestation
counts, credential/logout state, and every authorization gate actually granted.
Distinguish completed verification from manual checks and unavailable controls.
