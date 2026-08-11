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

Use a GitHub-hosted runner supported by npm and grant only `contents: read` and
`id-token: write` unless the repository proves another permission is necessary.
Reference a protected GitHub environment on the publish job. Configure that
environment with required reviewers, prevent self-review when available, restrict
deployment tags to the package's release-tag pattern, and disable administrator
bypass when the repository's policy supports it.

Pin `actions/checkout` and `actions/setup-node` to reviewed full commit SHAs while
retaining release comments. Select Node and npm versions that satisfy the current
npm Trusted Publisher documentation. This skill's verifier has a stricter npm
floor: require npm 11.12.0 or newer so `npm audit signatures
--include-attestations` exists before the irreversible publish. Disable
release-job package-manager caching unless the current upstream contract and
repository threat model justify it.

Configure npm trust on the package settings page with the exact GitHub owner,
repository, workflow filename, environment name, and allowed publish action. npm
fields are exact and case-sensitive, and npm may not validate them until publish.
After a successful OIDC release, require two-factor authentication and disallow
traditional publish tokens when the current npm controls allow it. Keep any token
needed solely to install private dependencies read-only and expose it only to the
install step, never the publish step.

Build once in the release job:

1. Install from the committed lockfile.
2. Run the repository's tests and build.
3. Run `npm pack` once, write the tarball outside the Git worktree, and capture
   its exact output path.
4. Run `scripts/release-preflight.py --phase publish` against that tarball and
   release tag. Ensure checkout fetched the annotated tag and configure the
   signer's public verification material so `git verify-tag` can succeed. Pass
   the approved signer fingerprint explicitly.
5. Upload the tarball and preflight JSON as workflow artifacts before publishing.
6. For a public source repository, publish the exact tarball path with
   `--provenance`; this CLI argument must override repository or user
   configuration that disables provenance. For a private source repository
   publishing a public package, npm Trusted Publishing works but provenance is
   unsupported: publish with `--provenance=false` and record that limitation
   before publication. Do not run `npm publish` against a directory, rerun `npm
pack`, or rebuild between inspection and publication.
7. Run `verify-published-package.py --provenance required` for a public source,
   or `--provenance unavailable` for the explicit private-source/public-package
   branch, in the same protected job. Fail the job if it cannot verify the
   artifact, registry signatures, applicable provenance certificate/workflow
   binding, and live release tag. Upload the verification JSON as a workflow
   artifact.

Do not provide `NODE_AUTH_TOKEN` to the OIDC publish step. Decide whether to set
`actions/setup-node`'s `registry-url` only after reading its current documentation:
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
   npm publish <built-package.tgz> --access public --registry=<registry>
   ```

7. Publish only after explicit approval. Do not imply that a local manual publish
   has CI SLSA provenance. Registry signatures and provenance are different
   claims.
8. Immediately verify artifact integrity with
   `scripts/verify-published-package.py --provenance unavailable`, configure the
   package's Trusted Publisher and GitHub environment, and restore the strongest
   token restrictions available. `unavailable` waives only the SLSA attestation:
   the full argument set below still applies, npm must still return the exact
   name@version as verified, the signed annotated tag is still checked against
   the approved signer and the remote, and verification fails if the package
   turns out to carry provenance after all.
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
audit signatures` returns the exact target name@version in its `verified`
  results. This is required in both provenance modes.
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
