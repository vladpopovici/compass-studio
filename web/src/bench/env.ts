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

export { percentiles } from "../viewer/stats";

export const nextFrame = () => new Promise<number>((r) => requestAnimationFrame(r));
