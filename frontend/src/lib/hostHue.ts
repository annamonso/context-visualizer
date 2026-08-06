/**
 * One place for the host→color recipe, so a host looks the same in the
 * scenario topology, the node inspector, and the trace waterfall.
 */

/** Hue-wheel walk parameters: start blue-ish, spread hosts evenly. */
const HUE_OFFSET = 200;
/** HSL saturation/lightness used for host fills everywhere. */
export const HOST_SAT = 62;
export const HOST_LIGHT = 48;

/**
 * Evenly-spaced hue per host within a set. With N hosts we walk around the
 * color wheel in N equal steps, starting at a pleasant blue so the first
 * host isn't fire-engine red. Any two hosts in the same set are visibly
 * distinct regardless of how similar their hostnames look.
 */
export function buildHostHueMap(hosts: Iterable<string>): Map<string, number> {
  const unique = Array.from(new Set(hosts)).sort();
  const step = 360 / Math.max(unique.length, 1);
  const map = new Map<string, number>();
  unique.forEach((h, i) => {
    map.set(h, Math.round((HUE_OFFSET + i * step) % 360));
  });
  return map;
}

/**
 * String-hash fallback for views with no set-level context (a lone agent
 * card that only knows its own host). Multiplied by Knuth's golden-ratio
 * constant so strings that differ by one char (ares-comp-16 vs -17) don't
 * collide in adjacent hues.
 */
export function stableHostHue(host: string): number {
  let h = 0;
  for (let i = 0; i < host.length; i++) h = (h * 31 + host.charCodeAt(i)) | 0;
  return Math.abs(Math.imul(h, 2654435761)) % 360;
}

/** CSS color for a host hue, optionally translucent. */
export function hostColor(hue: number, alpha?: number): string {
  return alpha != null
    ? `hsl(${hue} ${HOST_SAT}% ${HOST_LIGHT}% / ${alpha})`
    : `hsl(${hue} ${HOST_SAT}% ${HOST_LIGHT}%)`;
}
