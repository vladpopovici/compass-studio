// Values that change every frame (the view, the cursor, the object under it)
// live outside React state: only the components that show them subscribe, so
// panning, zooming and hovering do not re-render the application.

import { useCallback, useLayoutEffect, useRef, useSyncExternalStore } from "react";

export class Live<T> {
  private subs = new Set<() => void>();
  constructor(private value: T) {}
  get = () => this.value;
  set(v: T) {
    if (Object.is(v, this.value)) return;
    this.value = v;
    for (const f of this.subs) f();
  }
  subscribe = (f: () => void) => {
    this.subs.add(f);
    return () => this.subs.delete(f);
  };
}

export function useLive<T>(live: Live<T>): T {
  return useSyncExternalStore(live.subscribe, live.get);
}

/** A callback whose identity never changes and that always calls the latest `f` (for memoized children). */
export function useStable<A extends unknown[], R>(f: (...a: A) => R): (...a: A) => R {
  const ref = useRef(f);
  useLayoutEffect(() => {
    ref.current = f;
  });
  return useCallback((...a: A) => ref.current(...a), []);
}
