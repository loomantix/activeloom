# Releasing activeloom

The release workflow builds one tarball, validates the signed tag against the
approved release signer, and stages the tarball with npm. Staging does not make
the version public. A separate maintainer reviews and approves it with 2FA.

The release signer is pinned in the workflow to `8B680106EACC77AA538529E61E2DF3CE6E27C317`.
The public key is fetched from `https://github.com/BaxterDevs.gpg`; the fingerprint
check, rather than that account's current key list, grants trust. Preflight and
verification helpers are located in the repository under
`.codex/skills/publish-npm-package/scripts/`.

## Preparation and approval boundaries

1. Validate the package: run `node --test tests/cli/*.test.js`, check `npm pack --dry-run`
   in `cli/`, verify version and changelog. Merge the reviewed release PR only with
   explicit merge authorization.
2. Create and verify a signed annotated `activeloom-v<version>` tag on the release
   commit. Obtain tag-push authorization before pushing it. Never move or reuse a
   release tag.
3. Review the build job and its `activeloom-package` artifact: it contains the exact
   tarball, `preflight.json`, and `SHA256SUMS`. The protected `publish` job — "Stage
   for independent npm approval", gated by the `npm-publish` environment — has no
   checkout, dependency installation, package scripts, or cache.
4. An independent reviewer approves the GitHub environment. The job stages the
   artifact and prints the npm stage identifier. Inspect it with
   `npm stage view <stage-id>` and download it with
   `npm stage download <stage-id>`, using `--registry=https://registry.npmjs.org`.
   Compare its bytes against the original workflow tarball before approving.
5. Obtain explicit publication approval, then let the authorized maintainer use
   `npm stage approve <stage-id> --registry=https://registry.npmjs.org` and complete
   npm's interactive 2FA. Never put an OTP in a command or an artifact.
6. Verify publication without publish credentials or OIDC permission. Download the
   original workflow artifact into a temporary directory; do not repack. From the
   signed release checkout, run `verify-published-package.py` with
   `--provenance required`. Retain its JSON beside the original preflight and checksums.

```bash
python3 .codex/skills/publish-npm-package/scripts/verify-published-package.py \
  --package activeloom --version <version> \
  --artifact <original-workflow-tarball> --access public \
  --provenance required \
  --source-repository https://github.com/loomantix/activeloom \
  --workflow-path .github/workflows/publish-activeloom.yml \
  --tag activeloom-v<version> --commit <release-commit> \
  --repository-dir <release-checkout> --remote origin \
  --signer-fingerprint 8B680106EACC77AA538529E61E2DF3CE6E27C317 \
  --output <temporary-directory>/verification.json
```

## First-publication bootstrap (`activeloom@0.1.0`)

Because npm OIDC Trusted Publishing requires the package to exist on npm before
package settings and OIDC trust can be configured, the initial release
`activeloom@0.1.0` requires a tightly gated manual bootstrap publication:

Use Node 24.18.0 and npm 11.16.0 for the local helpers as well as CI.

1. Confirm package absence on the registry:
   `npm view activeloom --registry=https://registry.npmjs.org` must return 404.
2. Build the release tarball once:
   ```bash
   mkdir -p /tmp/activeloom-bootstrap
   (cd cli && npm pack --pack-destination /tmp/activeloom-bootstrap)
   ```
3. Run preflight in prepare phase outside the git worktree:
   ```bash
   python3 .codex/skills/publish-npm-package/scripts/release-preflight.py \
     --package-dir cli \
     --artifact /tmp/activeloom-bootstrap/activeloom-0.1.0.tgz \
     --tag activeloom-v0.1.0 \
     --phase prepare \
     --access public \
     --output /tmp/activeloom-bootstrap/preflight.json
   ```
4. Merge the release PR to `main` with explicit merge authorization. Continue in a
   clean, isolated checkout of that merged release commit, retaining the original
   tarball outside the checkout. Verify the packed package still matches the
   release source; if merge changes affected the package, restart preparation.
5. Create and sign the annotated tag:
   ```bash
   git tag -s activeloom-v0.1.0 -m "activeloom 0.1.0" <commit>
   git verify-tag activeloom-v0.1.0
   ```
6. Validate the signed tag against the approved signer and release checkout:
   ```bash
   python3 .codex/skills/publish-npm-package/scripts/release-preflight.py \
     --package-dir cli \
     --artifact /tmp/activeloom-bootstrap/activeloom-0.1.0.tgz \
     --tag activeloom-v0.1.0 --phase tag --access public \
     --signer-fingerprint 8B680106EACC77AA538529E61E2DF3CE6E27C317 \
     --output /tmp/activeloom-bootstrap/tag-preflight.json
   ```
7. Obtain explicit tag-push authorization, then run:
   ```bash
   git push origin refs/tags/activeloom-v0.1.0
   ```
   This starts the tag-triggered workflow. Leave its `npm-publish` environment
   unapproved for bootstrap; this version uses the manual path below. Do not stage
   the same version before or after the manual publication. Preserve the tag and
   workflow run as release history.
8. Before credential use, verify the remote tag matches and the version remains
   unpublished:
   ```bash
   python3 .codex/skills/publish-npm-package/scripts/release-preflight.py \
     --package-dir cli \
     --artifact /tmp/activeloom-bootstrap/activeloom-0.1.0.tgz \
     --tag activeloom-v0.1.0 --phase publish --access public \
     --signer-fingerprint 8B680106EACC77AA538529E61E2DF3CE6E27C317 \
     --output /tmp/activeloom-bootstrap/publish-preflight.json
   ```
9. Stop for credential-use authorization before `npm login` with interactive 2FA.
   Verify identity with `npm whoami --registry=https://registry.npmjs.org`.
10. Stop for manual-publish authorization, then publish the inspected tarball:

```bash
npm publish /tmp/activeloom-bootstrap/activeloom-0.1.0.tgz \
  --ignore-scripts \
  --access public \
  --registry=https://registry.npmjs.org
```

11. Verify artifact integrity using `--provenance unavailable` (manual publish carries
    registry signatures but no CI SLSA attestation):

```bash
python3 .codex/skills/publish-npm-package/scripts/verify-published-package.py \
  --package activeloom --version 0.1.0 \
  --artifact /tmp/activeloom-bootstrap/activeloom-0.1.0.tgz --access public \
  --provenance unavailable \
  --source-repository https://github.com/loomantix/activeloom \
  --tag activeloom-v0.1.0 --commit <commit> \
  --repository-dir . --remote origin \
  --signer-fingerprint 8B680106EACC77AA538529E61E2DF3CE6E27C317 \
  --output /tmp/activeloom-bootstrap/verification.json
```

12. Configure Trusted Publisher on npm:

- GitHub Owner: `loomantix`
- Repository: `activeloom`
- Workflow: `publish-activeloom.yml`
- Environment: `npm-publish`
- Allowed action: `npm stage publish`

13. Log out via `npm logout --registry=https://registry.npmjs.org` and revoke any
    temporary credentials. Subsequent releases use the automated staged workflow.

## Current upstream contracts

- Node 24.18.0 includes npm 11.16.0; both build and stage assert that CLI version.
- [npm staged publishing](https://docs.npmjs.com/staged-publishing/) requires an existing
  package, Node >=22.14.0 and npm >=11.15.0.
- [Trusted publishing](https://docs.npmjs.com/trusted-publishers/) supports GitHub-hosted
  runners and binds owner, repository, workflow filename, and the `npm-publish` environment.
- The GitHub environment `npm-publish` must require an independent reviewer, prevent
  self-review, restrict tags to `activeloom-v*`, and disallow administrator bypass.
