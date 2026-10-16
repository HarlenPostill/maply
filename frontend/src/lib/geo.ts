// Drawing-only geometry helpers (local equirectangular approximation).
// Never used for planning maths — the backend does that in a projected CRS.

const R = 6371008.8;
const toRad = (d: number) => (d * Math.PI) / 180;
const toDeg = (r: number) => (r * 180) / Math.PI;

/** Point `distM` metres from (lat,lng) along `headingDeg` (clockwise from north). */
export function destination(lat: number, lng: number, headingDeg: number, distM: number): [number, number] {
  const h = toRad(headingDeg);
  const dLat = (distM * Math.cos(h)) / R;
  const dLng = (distM * Math.sin(h)) / (R * Math.cos(toRad(lat)));
  return [lat + toDeg(dLat), lng + toDeg(dLng)];
}

/** Approximate bearing (deg, clockwise from north) from point a to point b. */
export function bearing(aLat: number, aLng: number, bLat: number, bLng: number): number {
  const x = (bLng - aLng) * Math.cos(toRad((aLat + bLat) / 2));
  const y = bLat - aLat;
  return (toDeg(Math.atan2(x, y)) + 360) % 360;
}

/** Point at half the length of a [lng,lat] polyline. */
export function lineMidpoint(coords: [number, number][]): [number, number] | null {
  if (!coords || coords.length === 0) return null;
  if (coords.length === 1) return coords[0];
  const lat0 = toRad(coords[0][1]);
  const segLen = (a: [number, number], b: [number, number]) =>
    Math.hypot((b[0] - a[0]) * Math.cos(lat0), b[1] - a[1]);
  let total = 0;
  for (let i = 1; i < coords.length; i++) total += segLen(coords[i - 1], coords[i]);
  let remaining = total / 2;
  for (let i = 1; i < coords.length; i++) {
    const l = segLen(coords[i - 1], coords[i]);
    if (l >= remaining && l > 0) {
      const t = remaining / l;
      return [coords[i - 1][0] + t * (coords[i][0] - coords[i - 1][0]), coords[i - 1][1] + t * (coords[i][1] - coords[i - 1][1])];
    }
    remaining -= l;
  }
  return coords[coords.length - 1];
}

/**
 * Parse "lng,lat" pairs, one per line or separated by ';'.
 * Returns coordinates or a list of problems.
 */
export function parseLngLatPairs(text: string): { coords: [number, number][]; errors: string[] } {
  const parts = text
    .split(/[;\n\r]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const coords: [number, number][] = [];
  const errors: string[] = [];
  parts.forEach((p, i) => {
    const m = p.split(/[,\s]+/).filter(Boolean);
    if (m.length !== 2) {
      errors.push(`Entry ${i + 1} ("${p}") is not a "lng,lat" pair`);
      return;
    }
    const lng = Number(m[0]);
    const lat = Number(m[1]);
    if (!Number.isFinite(lng) || !Number.isFinite(lat)) {
      errors.push(`Entry ${i + 1} ("${p}") has a non-numeric value`);
      return;
    }
    // Catch swapped order (Sydney: lng ~151, lat ~-33 -> "lat,lng" puts 151 in the lat slot).
    if (Math.abs(lat) > 90 && Math.abs(lng) <= 90) {
      errors.push(`Entry ${i + 1} ("${p}") looks like "lat,lng"; expected "lng,lat"`);
      return;
    }
    if (Math.abs(lat) > 90 || Math.abs(lng) > 180) {
      errors.push(`Entry ${i + 1} ("${p}") is out of range`);
      return;
    }
    coords.push([lng, lat]);
  });
  if (parts.length === 0) errors.push('Enter at least two "lng,lat" pairs');
  else if (errors.length === 0 && coords.length < 2) errors.push('A route needs at least two points');
  return { coords, errors };
}
