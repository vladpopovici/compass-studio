// Environment record attached to every spike result.

export function environment(gl?: WebGL2RenderingContext | null) {
  let renderer = "", vendor = "";
  if (gl) {
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    renderer = String(gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
    vendor = String(gl.getParameter(ext ? ext.UNMASKED_VENDOR_WEBGL : gl.VENDOR));
  }
  return {
    userAgent: navigator.userAgent,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: (navigator as unknown as { deviceMemory?: number }).deviceMemory ?? null,
    viewport: [innerWidth, innerHeight], devicePixelRatio: devicePixelRatio,
    webgl: { renderer, vendor },
    time: new Date().toISOString(),
  };
}

export function percentiles(xs: number[], ps = [50, 95, 99]): Record<string, number> {
  const out: Record<string, number> = { n: xs.length };
  if (!xs.length) return out;
  const s = [...xs].sort((a, b) => a - b);
  for (const p of ps) out[`p${p}`] = +s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))].toFixed(2);
  out.max = +s[s.length - 1].toFixed(2);
  out.mean = +(s.reduce((a, b) => a + b, 0) / s.length).toFixed(2);
  return out;
}

export const nextFrame = () => new Promise<number>((r) => requestAnimationFrame(r));
