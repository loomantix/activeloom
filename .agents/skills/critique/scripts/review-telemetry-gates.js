// Shared gate reader; synced beside each harness's usage helper.
//
// Two gates, because extraction and emission are different decisions.
//
// `LOOM_REVIEW_TELEMETRY` keeps its name and its meaning: it governs whether a
// pass **emits** a record to a pull request. `LOOM_REVIEW_TELEMETRY_EXTRACT`
// governs whether the usage helper **measures** at all, and defaults to the
// emission gate so no existing configuration changes meaning.
//
// Splitting them is what makes measurement usable without publication: a cost
// join, an offline run, or any local analysis can set the extraction gate on
// and leave the emission gate off, and emission is then structurally
// unreachable rather than merely unrequested. One variable meaning both
// forecloses that combination entirely.
//
// Both gates are read here and nowhere else, so widening either is a one-line
// change in a synced file rather than an edit in every skill, and no model
// decides the question by reading the environment itself.
import { readFileSync } from 'node:fs';

const EMISSION_GATE = 'LOOM_REVIEW_TELEMETRY';
const EXTRACTION_GATE = 'LOOM_REVIEW_TELEMETRY_EXTRACT';

export function resolveGates() {
  let repository = {};
  let configError = null;
  try {
    repository = JSON.parse(
      readFileSync(new URL('./review-telemetry.json', import.meta.url), 'utf8'),
    );
    if (
      !repository ||
      typeof repository !== 'object' ||
      Array.isArray(repository) ||
      // Normalize before the membership test so a file value is accepted on
      // the same terms as an environment value. Without this, `"Off"` in the
      // file is not `off` — it fails whole-file validation and hard-disables
      // both gates, which no environment variable can then restore.
      Object.entries(repository).some(
        ([key, value]) =>
          ![EMISSION_GATE, EXTRACTION_GATE].includes(key) ||
          typeof value !== 'string' ||
          !['on', 'off'].includes(value.trim().toLowerCase()),
      )
    ) {
      throw new Error('invalid configuration');
    }
  } catch (error) {
    if (error.code !== 'ENOENT')
      configError = 'review telemetry configuration is invalid or unreadable';
  }
  // A misconfigured file is not an opt-out, and it disables both gates. Handled
  // here rather than inside `gate` so the partially parsed `repository` above
  // is unreachable once validation has failed.
  if (configError) {
    const failed = {
      set: true,
      enabled: false,
      reason: configError,
      error: configError,
    };
    return { emission: failed, extraction: failed };
  }
  function gate(name, defaultEnabled = false) {
    const raw = process.env[name]?.trim() || repository[name];
    if (raw === undefined || raw.trim() === '')
      return {
        set: false,
        enabled: defaultEnabled,
        reason: defaultEnabled ? null : `${name} is unset`,
      };
    const value = raw.trim().toLowerCase();
    if (value === 'on') return { set: true, enabled: true, reason: null };
    if (value === 'off')
      return { set: true, enabled: false, reason: `${name} is off` };
    // A misconfigured value is not an opt-out either. Reporting it only as a
    // reason would make a typo that disables the whole rollout
    // indistinguishable from a deliberate `off`.
    const reason = `${name} must be exactly "on" or "off"`;
    return { set: true, enabled: false, reason, error: reason };
  }
  const emission = gate(EMISSION_GATE, true);
  const declared = gate(EXTRACTION_GATE);
  return { emission, extraction: declared.set ? declared : emission };
}
