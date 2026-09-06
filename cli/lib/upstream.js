'use strict';

/**
 * Getting the content this CLI installs.
 *
 * The published npm package carries the installer, not the prompts. Content is
 * fetched from a tag-pinned GitHub tarball at run time, which is what keeps
 * `npx activeloom` and the CI sync engine on **one** content gate: both read
 * the same tag, so the two doors cannot deliver different prompts. Bundling a
 * rendered copy into the npm tarball would make the package version a second
 * content channel, and two channels drift — the failure this whole
 * consolidation exists to end.
 *
 * The cost is that Tier 0 needs the network. `npx` already does, so this adds
 * no requirement a user did not already have.
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

/** The repository content is fetched from. Overridable for forks. */
const DEFAULT_UPSTREAM =
  process.env.ACTIVELOOM_UPSTREAM || 'loomantix/activeloom';

/**
 * The content gate.
 *
 * `sync-v2` is the same ref migrated consumers' sync workflows track, which is
 * the entire point of the choice: a change reaches npx users and CI consumers
 * at the moment it reaches the tag, and never before. `main` is deliberately
 * not the default — a stray push to main must not propagate.
 */
const DEFAULT_REF = 'sync-v2';

/**
 * Download and unpack an upstream tree, returning the directory it landed in.
 *
 * @param {object} [options]
 * @param {string} [options.ref]          Git ref to fetch. Defaults to `sync-v2`.
 * @param {string} [options.repo]         `owner/name`. Defaults to the upstream.
 * @param {string} [options.upstreamDir]  Use this local checkout instead of
 *   downloading. This is how the equivalence test runs the CLI against the same
 *   tree the sync engine is run against, and how a fork developer tests an
 *   unpushed change. It is not a way to skip the content gate in normal use.
 * @returns {Promise<{dir: string, ref: string, source: string, cleanup: () => void}>}
 */
async function resolveUpstream(options = {}) {
  const ref = options.ref ?? DEFAULT_REF;
  const repo = options.repo ?? DEFAULT_UPSTREAM;

  if (options.upstreamDir) {
    const dir = path.resolve(options.upstreamDir);
    if (!fs.existsSync(path.join(dir, 'scripts', 'sync-targets.yml'))) {
      throw new Error(
        `--upstream-dir ${dir} does not look like an activeloom checkout ` +
          `(no scripts/sync-targets.yml).`,
      );
    }
    return { dir, ref: 'local', source: dir, cleanup: () => {} };
  }

  // Encoded per path segment: `encodeURIComponent` on the whole ref would
  // escape `/`, so a slashed ref like `release/2.0` could not be spelled at
  // all. Tag first, then branch — the ref this CLI installs from is normally a
  // tag, but a branch has to be reachable or the remedy the 404 below prints
  // would be impossible to follow.
  const encoded = ref.split('/').map(encodeURIComponent).join('/');
  // The resolved URL is returned as `source`, so which of the two answered is
  // visible to the caller without a second field to keep in step.
  const candidates = [
    `https://codeload.github.com/${repo}/tar.gz/refs/tags/${encoded}`,
    `https://codeload.github.com/${repo}/tar.gz/refs/heads/${encoded}`,
  ];
  const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-'));
  const cleanup = () => {
    try {
      fs.rmSync(workdir, { recursive: true, force: true });
    } catch {
      // A tmpdir we cannot remove is the OS's to reap; never fail a successful
      // install over cleanup.
    }
  };

  try {
    let response = null;
    let url = candidates[0];
    for (const candidate of candidates) {
      url = candidate;
      const attempt = await fetch(url, {
        headers: { 'user-agent': 'activeloom-cli' },
        redirect: 'follow',
      });
      if (attempt.status === 404) continue;
      response = attempt;
      break;
    }

    if (response === null) {
      // Overwhelmingly the interesting failure, and worth naming precisely:
      // until the consumer cutover cuts `sync-v2`, the default ref genuinely
      // does not exist yet, and a bare "404" would read as a broken CLI. Both
      // forms 404'd, so the ref is neither a tag nor a branch.
      throw new Error(
        `no tag or branch \`${ref}\` in ${repo}.\n` +
          `  If \`${ref}\` has not been cut yet, pin an existing ref explicitly:\n` +
          `      npx activeloom <command> --ref main\n` +
          `  Available refs: https://github.com/${repo}/tags`,
      );
    }
    if (!response.ok) {
      throw new Error(
        `could not fetch ${url} — HTTP ${response.status} ${response.statusText}`,
      );
    }

    const tarball = path.join(workdir, 'upstream.tar.gz');
    fs.writeFileSync(tarball, Buffer.from(await response.arrayBuffer()));

    const dir = path.join(workdir, 'upstream');
    fs.mkdirSync(dir);
    // `--strip-components=1` drops the `<repo>-<ref>/` wrapper GitHub adds, so
    // the result is shaped exactly like a checkout and the sync engine's
    // `--upstream-repo` needs no special-casing for the two sources.
    try {
      execFileSync(
        'tar',
        ['-xzf', tarball, '-C', dir, '--strip-components=1'],
        { stdio: ['ignore', 'ignore', 'pipe'] },
      );
    } catch (err) {
      // `execFileSync` throws with a bare "Command failed: tar ...". The real
      // cause — a truncated download, a corrupt archive, an HTML error page
      // served with a 200 — is on `err.stderr`, which nothing else reads.
      const detail = String(err.stderr ?? '').trim();
      throw new Error(
        `could not unpack the archive from ${url}` +
          (detail ? `\n  tar: ${detail}` : ''),
      );
    }

    return { dir, ref, source: url, cleanup };
  } catch (err) {
    cleanup();
    throw err;
  }
}

module.exports = { resolveUpstream, DEFAULT_REF, DEFAULT_UPSTREAM };
