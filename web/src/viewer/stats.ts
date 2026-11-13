// Summary statistics for timings.

export function percentiles(xs: number[], ps = [50, 95, 99]): Record<string, number> {
  const out: Record<string, number> = { n: xs.length };
  if (!xs.length) return out;
  const s = [...xs].sort((a, b) => a - b);
  for (const p of ps) out[`p${p}`] = +s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))].toFixed(2);
  out.max = +s[s.length - 1].toFixed(2);
  out.mean = +(s.reduce((a, b) => a + b, 0) / s.length).toFixed(2);
  return out;
}
