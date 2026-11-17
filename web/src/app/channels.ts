// Channel settings of a multichannel image (milestone 8): defaults from the
// server (`Image.channel_defaults`, read from `<image>.channels.json`), automatic
// contrast from the image's histogram, and the viewer's changes remembered per
// image in this browser (localStorage; nothing is written beside the slides).

import { useCallback, useEffect, useState } from "react";
import { getJSON, type ImageInfo, type Schemas } from "@/api";
import { MAX_SHOWN, type ChannelSetting } from "@/viewer/channels";

export type Histogram = Schemas["Histogram"];

/** Colours for channels without a default: DAPI-like names blue, then these in turn. */
const COLORS = ["#00ff00", "#ff0000", "#ff00ff", "#00ffff", "#ffff00", "#ff8000", "#ffffff", "#8080ff"];
const NUCLEAR = /dapi|hoechst|nuclear|nuclei/i;
/** automatic contrast: these percentiles of the histogram's level */
export const AUTO_LOW = "1", AUTO_HIGH = "99.9";
const DEFAULT_SHOWN = 4;

const key = (id: string) => `cs.channels.${id}`;

function load(id: string, n: number): ChannelSetting[] | null {
  try {
    const v = JSON.parse(localStorage.getItem(key(id)) ?? "null");
    return Array.isArray(v) && v.length === n ? v : null;
  } catch {
    return null;
  }
}

function save(id: string, s: ChannelSetting[] | null) {
  try {
    if (s) localStorage.setItem(key(id), JSON.stringify(s));
    else localStorage.removeItem(key(id));
  } catch { /* not available */ }
}

export function maxValue(info: ImageInfo) {
  return info.dtype === "uint8" ? 255 : 65535;
}

/** Automatic window of channel `c`: [p1, p99.9], at least 1 apart. */
export function autoWindow(h: Histogram | null, c: number, info: ImageInfo): [number, number] {
  const e = h?.channels[c];
  if (!e) return [0, maxValue(info)];
  const lo = Math.round(e.percentiles[AUTO_LOW] ?? e.min), hi = Math.round(e.percentiles[AUTO_HIGH] ?? e.max);
  return [lo, Math.max(hi, lo + 1)];
}

/** Settings from the defaults file and the histogram (no remembered changes). */
export function initialSettings(info: ImageInfo, h: Histogram | null): ChannelSetting[] {
  const d = info.channel_defaults ?? [];
  const anyVisible = d.some((x) => x.visible != null);
  let next = 0;
  return info.channels.map((name, c) => {
    const def = d[c];
    const [lo, hi] = autoWindow(h, c, info);
    const color = def?.color ?? (NUCLEAR.test(name) ? "#0080ff" : COLORS[next++ % COLORS.length]);
    return {
      visible: anyVisible ? !!def?.visible : c < DEFAULT_SHOWN,
      color, min: def?.min ?? lo, max: def?.max ?? hi,
    };
  }).map((s, c, all) => ({ ...s, visible: s.visible && all.slice(0, c).filter((x) => x.visible).length < MAX_SHOWN }));
}

export interface Channels {
  settings: ChannelSetting[];
  histogram: Histogram | null;
  set: (c: number, patch: Partial<ChannelSetting>) => void;
  setAll: (f: (s: ChannelSetting, c: number) => ChannelSetting) => void;
  /** back to the defaults file and automatic contrast; forgets this browser's changes */
  reset: () => void;
}

export function useChannels(info: ImageInfo | null): Channels {
  const multi = info?.display === "channels" ? info : null;
  const [histogram, setHistogram] = useState<Histogram | null>(null);
  const [settings, setSettings] = useState<ChannelSetting[]>([]);
  useEffect(() => {
    setHistogram(null);
    setSettings([]);
    if (!multi) return;
    let live = true;
    const saved = load(multi.id, multi.channels.length);
    if (saved) setSettings(saved);
    // the histogram is needed for automatic contrast and the panel
    getJSON<Histogram>(`images/${encodeURIComponent(multi.id)}/histogram`).then((h) => {
      if (!live) return;
      setHistogram(h);
      if (!saved) setSettings(initialSettings(multi, h));
    }, () => live && !saved && setSettings(initialSettings(multi, null)));
    return () => {
      live = false;
    };
  }, [multi]);
  const update = useCallback((f: (old: ChannelSetting[]) => ChannelSetting[]) => {
    setSettings((old) => {
      const s = f(old);
      if (multi) save(multi.id, s);
      return s;
    });
  }, [multi]);
  const set = useCallback((c: number, patch: Partial<ChannelSetting>) =>
    update((old) => old.map((s, i) => (i === c ? { ...s, ...patch } : s))), [update]);
  const setAll = useCallback((f: (s: ChannelSetting, c: number) => ChannelSetting) => update((old) => old.map(f)), [update]);
  const reset = useCallback(() => {
    if (!multi) return;
    save(multi.id, null);
    setSettings(initialSettings(multi, histogram));
  }, [multi, histogram]);
  return { settings, histogram, set, setAll, reset };
}
