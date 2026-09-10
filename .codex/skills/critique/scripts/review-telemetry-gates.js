// Shared gate reader; synced beside each harness's usage helper.
import { readFileSync } from 'node:fs';

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
      Object.entries(repository).some(
        ([key, value]) =>
          !['LOOM_REVIEW_TELEMETRY', 'LOOM_REVIEW_TELEMETRY_EXTRACT'].includes(
            key,
          ) || !['on', 'off'].includes(value),
      )
    ) {
      throw new Error('invalid configuration');
    }
  } catch (error) {
    if (error.code !== 'ENOENT')
      configError = 'review telemetry configuration is invalid or unreadable';
  }
  function gate(name, defaultEnabled = false) {
    if (configError)
      return {
        set: true,
        enabled: false,
        reason: configError,
        error: configError,
      };
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
    const reason = `${name} must be exactly "on" or "off"`;
    return { set: true, enabled: false, reason, error: reason };
  }
  const emission = gate('LOOM_REVIEW_TELEMETRY', true);
  const declared = gate('LOOM_REVIEW_TELEMETRY_EXTRACT');
  return { emission, extraction: declared.set ? declared : emission };
}
