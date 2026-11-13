// A collapsible side panel with a drag handle on its inner edge; the width
// is remembered per side in this browser (a convenience, may be unavailable).
import { useCallback, useState } from "react";
import type * as React from "react";
import { cn } from "@/lib/utils";

const MIN = 200, MAX = 560;

function stored(key: string, fallback: number): number {
  try {
    const v = Number(localStorage.getItem(key));
    return v >= MIN && v <= MAX ? v : fallback;
  } catch {
    return fallback;
  }
}

export function Sidebar({ side, label, defaultWidth = 272, children }: {
  side: "left" | "right"; label: string; defaultWidth?: number; children: React.ReactNode;
}) {
  const key = `compass-studio.sidebar.${side}`;
  const [width, setWidth] = useState(() => stored(key, defaultWidth));
  const onDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    const x0 = e.clientX, w0 = width;
    const move = (ev: PointerEvent) => {
      const d = side === "left" ? ev.clientX - x0 : x0 - ev.clientX;
      setWidth(Math.min(MAX, Math.max(MIN, w0 + d)));
    };
    const up = () => {
      removeEventListener("pointermove", move);
      removeEventListener("pointerup", up);
      setWidth((w) => {
        try { localStorage.setItem(key, String(w)); } catch { /* not available */ }
        return w;
      });
    };
    addEventListener("pointermove", move);
    addEventListener("pointerup", up);
  }, [key, side, width]);
  return (
    <aside aria-label={label} style={{ width }}
           className={cn("relative flex shrink-0 flex-col", side === "left" ? "border-r" : "border-l", "border-border")}>
      {children}
      <div role="separator" aria-orientation="vertical" aria-label={`Resize ${label}`} onPointerDown={onDown}
           className={cn("absolute inset-y-0 z-10 w-1.5 cursor-col-resize hover:bg-primary/40",
                         side === "left" ? "-right-1" : "-left-1")} />
    </aside>
  );
}
