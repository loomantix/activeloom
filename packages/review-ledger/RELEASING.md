# Releasing review-ledger

The release workflow builds one tarball, validates the signed tag against the
approved release signer, and stages the same tarball with npm. Staging does not
make the version public. A separate maintainer reviews and approves it with 2FA.

The signer fingerprint is pinned in the workflow to the signer of
`review-ledger-v1.4.0`. The public key is fetched from the signer's GitHub account;
the fingerprint check, rather than that account's current key list, grants trust.
Signer rotation requires a reviewed workflow change. Preflight is fetched from
an immutable ActiveLoom commit and checked against its SHA-256 before execution.

## Preparation and approval boundaries

1. Validate the package's typecheck, tests, build, tarball contents, and version.
   Merge the reviewed version PR only with explicit merge authorization.
2. Create and verify a signed annotated `review-ledger-v<version>` tag on the
   release commit. Obtain tag-push authorization before pushing it. Never move
   or reuse a release tag.
3. Review the build job and its `review-ledger-package` artifact: it contains the
   exact tarball, `preflight.json`, and `SHA256SUMS`. The protected `publish`
   job — "Stage for independent npm approval", gated by the `npm-publish`
   environment — has no checkout, dependency installation, package scripts, or
   cache.
4. An independent reviewer approves the GitHub environment. The job stages the
   artifact and prints the npm stage identifier. Inspect it with
   `npm stage view <stage-id>` and download it with
   `npm stage download <stage-id>`, using `--registry=https://registry.npmjs.org`.
   Compare its bytes against the original workflow tarball before approving.
5. Obtain explicit publication approval, then let the authorized maintainer use
   `npm stage approve <stage-id> --registry=https://registry.npmjs.org` and complete
   npm's interactive 2FA. Never put an OTP in a command or an artifact.
6. Verify publication without publish credentials or OIDC permission. Download
   the original workflow artifact into a temporary directory; do not repack.
   From the signed release checkout, run the verifier below. Retain its JSON
   beside the original preflight and checksums. Only this verified artifact may
   be used to refresh vendored copies.

Use the `verify-published-package.py` helper at ActiveLoom commit
`64a44bb0ad03ecddbc07eb2abdfc4e1ea46be2ae`, path
`.codex/skills/publish-npm-package/scripts/verify-published-package.py`.
Check SHA-256
`8668ff1c423750aaac56ab17fd3246f993e26f603d303963e8a6be1bdf772356`
before executing it. Substitute the approved release values:

Import the approved signer's public key into the verification host's GPG keyring
before running it; the workflow takes that key from
`https://github.com/BaxterDevs.gpg`, and trust comes from the pinned fingerprint
below rather than from that account's current key list. Pass the full
40-character release commit — the verifier rejects an abbreviated one — and keep
both `--artifact` and `--output` outside the release checkout's worktree, which
the verifier also enforces.

```bash
python3 /tmp/verify-published-package.py \
  --package @loomantix/review-ledger --version <version> \
  --artifact <original-workflow-tarball> --access public \
  --provenance required \
  --source-repository https://github.com/loomantix/platform-oss \
  --workflow-path .github/workflows/publish-review-ledger.yml \
  --tag review-ledger-v<version> --commit <release-commit> \
  --repository-dir <release-checkout> --remote origin \
  --signer-fingerprint 8B680106EACC77AA538529E61E2DF3CE6E27C317 \
  --output <temporary-directory>/verification.json
```

The verifier requires the tag signature, live remote tag, registry bytes,
registry signatures and the source/workflow-bound SLSA attestation to agree.
An unavailable or mismatched attestation is a failed verification, not a reason
to waive provenance for this public-source package.

## Current upstream contracts

Checked 2026-09-08: [npm staged publishing](https://docs.npmjs.com/staged-publishing/)
requires an existing package, Node >=22.14.0 and npm >=11.15.0.
Node 24.18.0 includes npm 11.16.0; both build and stage assert that CLI version.
[Trusted publishing](https://docs.npmjs.com/trusted-publishers/) supports
GitHub-hosted runners and binds owner, repository, workflow filename and the
`npm-publish` environment. Configure its allowed actions to stage only, with
direct publication disabled. This npm setting is maintained separately and
must be verified before release.

The pinned [setup-node v7 action](https://github.com/actions/setup-node/tree/820762786026740c76f36085b0efc47a31fe5020)
requires runner >=2.327.1. Automatic cache is disabled in the privileged job.
`registry-url` is deliberately absent; it writes token authentication
configuration. The npm command supplies the registry explicitly.

The GitHub environment must require an independent reviewer, prevent self-review,
restrict tags to `review-ledger-v*`, and disallow administrator bypass. Environment
approval and npm's later 2FA approval are separate gates. No credential is needed
for local artifact, registry, signature or provenance verification.
