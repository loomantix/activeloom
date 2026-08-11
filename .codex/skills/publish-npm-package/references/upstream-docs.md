# Live upstream documentation checks

Read these primary sources immediately before changing or executing a release.
They are intentionally URLs rather than copied version numbers because npm and
GitHub Actions contracts change.

## npm

- Trusted Publishers: <https://docs.npmjs.com/trusted-publishers/>
- Provenance statements and verification: <https://docs.npmjs.com/generating-provenance-statements/>
- `npm publish`: <https://docs.npmjs.com/cli/commands/npm-publish/>
- `npm audit signatures`: <https://docs.npmjs.com/cli/commands/npm-audit/>
- `npm login`: <https://docs.npmjs.com/cli/commands/npm-login/>
- `npm logout`: <https://docs.npmjs.com/cli/commands/npm-logout/>
- Package removal policy: <https://docs.npmjs.com/policies/unpublish/>

Confirm and record:

1. Minimum Node and npm CLI versions for Trusted Publishing.
2. Supported CI providers and runner types.
3. Required OIDC permissions and supported npm operations.
4. Exact Trusted Publisher fields, allowed actions, and package-limit rules.
5. Public/private repository and package limits for automatic provenance.
6. Whether `--provenance` is automatic, required, optional, or unsupported for
   the selected path.
7. Current token restriction, staged-publish, two-factor, first-publication, and
   unpublish behavior.
8. Current signature and attestation verification command/output.

## GitHub Actions

- `actions/setup-node` README and current release notes:
  <https://github.com/actions/setup-node/blob/main/README.md>
- `actions/setup-node` releases: <https://github.com/actions/setup-node/releases>
- Environments and protection rules:
  <https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>
- Managing environments:
  <https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments>
- Reviewing deployments:
  <https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments>
- Pinning actions to a full commit SHA:
  <https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions>

Confirm and record:

1. Current `actions/setup-node` major release and reviewed full commit SHA.
2. `registry-url`, `NODE_AUTH_TOKEN`, `always-auth`, and automatic-cache behavior
   for that exact action revision. Do not assume npm's example uses the latest
   action or that an older `setup-node` behavior still applies.
3. Current GitHub-hosted runner and runner-version requirements.
4. Environment reviewer, self-review, tag-policy, secret-release, and
   administrator-bypass behavior for the repository's plan and visibility.
5. Workflow-call and manual-dispatch identity behavior if a reusable workflow is
   involved; npm may bind trust to the caller rather than the called workflow.

## Reconcile discrepancies

Prefer the most specific current primary source for its own component. If npm's
example references an older `actions/setup-node` major than the action's current
README, do not silently choose either example. Record the mismatch, review release
notes, select a compatible immutable action revision, and validate it in a
non-publishing workflow before release.

Do not use search snippets, marketplace summaries, blog posts, or a prior release
as authority when the primary sources are available. Do not include credentials,
private repository details, or terminal output in the documentation record.
