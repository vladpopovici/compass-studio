// Channels of a multichannel image: which are shown (at most 8, added
// together), their colour and contrast window, over each channel's histogram.
import { memo } from "react";
import { Wand2 } from "lucide-react";
import type { ImageInfo } from "@/api";
import { Button } from "@/components/ui/button";
import { MAX_SHOWN, type ChannelSetting } from "@/viewer/channels";
import { autoWindow, maxValue, type Channels, type Histogram } from "./channels";

const W = 220, H = 36;

/** Log-scaled histogram with the window [min, max] shaded, over [0, top]. */
function Hist({ h, c, s, top }: { h: Histogram | null; c: number; s: ChannelSetting; top: number }) {
  const e = h?.channels[c];
  const x = (v: number) => (Math.min(Math.max(v, 0), top) / top) * W;
  let path = "";
  if (e) {
    const peak = Math.log1p(Math.max(...e.counts));
    const bw = (e.max - e.min) / e.counts.length || 1;
    path = e.counts.map((n, i) => {
      const y = H - (peak ? (Math.log1p(n) / peak) * H : 0);
      return `${i ? "L" : "M"}${x(e.min + i * bw).toFixed(1)},${y.toFixed(1)} L${x(e.min + (i + 1) * bw).toFixed(1)},${y.toFixed(1)}`;
    }).join(" ") + ` L${x(e.max).toFixed(1)},${H} L${x(e.min).toFixed(1)},${H} Z`;
  }
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-9 rounded-sm bg-black" aria-hidden>
      <rect x={x(s.min)} width={Math.max(1, x(s.max) - x(s.min))} y={0} height={H} fill={s.color} opacity={0.18} />
      {path && <path d={path} fill="currentColor" className="text-muted-foreground" opacity={0.8} />}
      <line x1={x(s.min)} x2={x(s.min)} y1={0} y2={H} stroke={s.color} strokeWidth={1} />
      <line x1={x(s.max)} x2={x(s.max)} y1={0} y2={H} stroke={s.color} strokeWidth={1} />
    </svg>
  );
}

function NumberField({ value, label, onCommit }: { value: number; label: string; onCommit: (v: number) => void }) {
  return (
    <input key={value} defaultValue={value} aria-label={label} inputMode="numeric"
           className="w-16 rounded border border-border bg-transparent px-1 text-right font-mono tabular-nums"
           onBlur={(e) => {
             const v = Math.round(+e.target.value);
             if (Number.isFinite(v) && v !== value) onCommit(v);
             else e.target.value = String(value);
           }}
           onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
  );
}

function ChannelRow({ info, c, s, h, canShow, set }: {
  info: ImageInfo; c: number; s: ChannelSetting; h: Histogram | null; canShow: boolean;
  set: (patch: Partial<ChannelSetting>) => void;
}) {
  const name = info.channels[c];
  const limit = maxValue(info);
  // sliders span the data (and the window), not the whole value range
  const top = Math.min(limit, Math.max(h?.channels[c]?.max ?? limit, s.max, 1));
  const clamp = (v: number) => Math.min(limit, Math.max(0, v));
  return (
    <div className="border-b border-border/60 px-1 py-1" data-channel={c} data-visible={s.visible || undefined}>
      <div className="flex items-center gap-2">
        <input type="checkbox" checked={s.visible} disabled={!s.visible && !canShow} className="accent-primary"
               aria-label={`Show ${name}`} title={!s.visible && !canShow ? `At most ${MAX_SHOWN} channels at once` : undefined}
               onChange={() => set({ visible: !s.visible })} />
        <input type="color" value={s.color} aria-label={`Colour of ${name}`} onChange={(e) => set({ color: e.target.value })}
               className="size-4 shrink-0 cursor-pointer rounded-sm border border-border bg-transparent p-0"
               style={{ opacity: s.visible ? 1 : 0.4 }} />
        <span className={`flex-1 truncate ${s.visible ? "" : "text-muted-foreground"}`} title={name}>{name}</span>
        <span className="font-mono text-muted-foreground tabular-nums">{s.min}–{s.max}</span>
      </div>
      {s.visible && (
        <div className="mt-1 flex flex-col gap-1 pl-6">
          <Hist h={h} c={c} s={s} top={top} />
          <label className="flex items-center gap-2 text-muted-foreground" title="Values at or below this are black">
            <span className="w-8">Min</span>
            <input type="range" min={0} max={top} step={1} value={Math.min(s.min, top)} className="min-w-0 flex-1 accent-primary"
                   aria-label={`${name} min`} onChange={(e) => set({ min: Math.min(+e.target.value, s.max - 1) })} />
            <NumberField value={s.min} label={`${name} min value`} onCommit={(v) => set({ min: Math.min(clamp(v), s.max - 1) })} />
          </label>
          <label className="flex items-center gap-2 text-muted-foreground" title="Values at or above this are full colour">
            <span className="w-8">Max</span>
            <input type="range" min={0} max={top} step={1} value={Math.min(s.max, top)} className="min-w-0 flex-1 accent-primary"
                   aria-label={`${name} max`} onChange={(e) => set({ max: Math.max(+e.target.value, s.min + 1) })} />
            <NumberField value={s.max} label={`${name} max value`} onCommit={(v) => set({ max: Math.max(clamp(v), s.min + 1) })} />
          </label>
          <div className="flex justify-end">
            <Button variant="outline" className="h-6 px-2" disabled={!h}
                    title="Contrast from the histogram: 1st to 99.9th percentile"
                    onClick={() => { const [min, max] = autoWindow(h, c, info); set({ min, max }); }}>
              <Wand2 className="size-3.5" />Auto
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export const ChannelPanel = memo(function ChannelPanel({ info, ch }: { info: ImageInfo; ch: Channels }) {
  const shown = ch.settings.filter((s) => s.visible).length;
  if (!ch.settings.length) return <p className="p-2 text-muted-foreground">Loading channels…</p>;
  return (
    <div>
      <div className="flex flex-wrap items-center gap-1 border-b border-border p-2">
        <span className="flex-1 text-muted-foreground" data-shown={shown}>
          {shown} of {info.channels.length} shown{shown >= MAX_SHOWN ? ` (at most ${MAX_SHOWN})` : ""}
        </span>
        <Button variant="outline" disabled={!ch.histogram} title="Automatic contrast for every channel"
                onClick={() => ch.setAll((s, c) => { const [min, max] = autoWindow(ch.histogram, c, info); return { ...s, min, max }; })}>
          Auto all
        </Button>
        <Button variant="outline" title="Hide every channel" onClick={() => ch.setAll((s) => ({ ...s, visible: false }))}>None</Button>
        <Button variant="outline" title="Back to the default channels, colours and contrast (forgets this browser's changes)"
                onClick={ch.reset}>Reset</Button>
      </div>
      <div>
        {ch.settings.map((s, c) => (
          <ChannelRow key={c} info={info} c={c} s={s} h={ch.histogram} canShow={shown < MAX_SHOWN}
                      set={(patch) => ch.set(c, patch)} />
        ))}
      </div>
    </div>
  );
});
