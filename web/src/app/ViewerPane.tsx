// Hosts the framework-free Viewer in a React-managed element.
import { useEffect, useRef } from "react";
import { Viewer, type ViewerOptions } from "@/viewer/viewer";

export function ViewerPane({ onReady, ...opts }: ViewerOptions & { onReady: (v: Viewer | null) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const optsRef = useRef(opts);
  optsRef.current = opts;
  useEffect(() => {
    const v = new Viewer(ref.current!, {
      ...optsRef.current,
      onView: (view) => optsRef.current.onView?.(view),
      onHover: (p) => optsRef.current.onHover?.(p),
    });
    onReady(v);
    return () => {
      onReady(null);
      v.destroy();
    };
  }, [onReady]);
  return <div ref={ref} className="relative h-full w-full overflow-hidden bg-canvas" />;
}
