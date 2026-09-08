/**
 * Reading design tokens from JS.
 *
 * Most of the UI can name a token in a class or a `rgb(var(--x))` string and
 * let CSS resolve it. Ramps cannot: interpolating between two colors needs the
 * actual channel values, and hardcoding them in TS is how the Fleet heatmap
 * drifted out of sync with the status swatches it is supposed to match.
 */

type Rgb = [number, number, number];

/**
 * Resolve a `--token` to its channel triple.
 *
 * Tokens are stored space-separated ("34 197 94") so Tailwind can wrap them in
 * `rgb(... / <alpha-value>)`; that same format is what makes them parseable
 * here. Falls back to mid-grey rather than throwing — a wrong color in one
 * cell beats a blank page.
 */
export function tokenRgb(token: string): Rgb {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(token)
    .trim();
  const parts = raw.split(/[\s,]+/).map(Number);
  return parts.length === 3 && parts.every((n) => Number.isFinite(n))
    ? (parts as Rgb)
    : [136, 136, 136];
}

/** Linear blend of two tokens, `t` clamped to 0..1. */
export function mixToken(from: string, to: string, t: number): string {
  const k = Math.min(1, Math.max(0, t));
  const a = tokenRgb(from);
  const b = tokenRgb(to);
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * k));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}
