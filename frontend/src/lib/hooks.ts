import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessages } from './api';

export interface Resource<T> {
  data: T | undefined;
  error: string[] | null;
  loading: boolean;
  reload: () => Promise<T | undefined>;
  setData: (d: T) => void;
}

/**
 * Fetch a resource identified by `key` (null = nothing to fetch). Data is
 * reset whenever the key changes. Optional polling (setTimeout chained after
 * each completion, so requests never overlap) while `enabled`.
 * Stale responses (older request, or a different key) are discarded.
 */
export function useResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  opts: { interval?: number | null; enabled?: boolean } = {},
): Resource<T> {
  const { interval = null, enabled = true } = opts;
  const [data, setDataState] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const keyRef = useRef<string | null>(key);
  const seq = useRef(0);

  const reload = useCallback(async (): Promise<T | undefined> => {
    const k = keyRef.current;
    if (k === null) return undefined;
    const my = ++seq.current;
    setLoading(true);
    try {
      const d = await fetcherRef.current();
      if (my === seq.current && keyRef.current === k) {
        setDataState(d);
        setError(null);
      }
      return d;
    } catch (e) {
      if (my === seq.current && keyRef.current === k) setError(errorMessages(e));
      return undefined;
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    keyRef.current = key;
    seq.current++;
    setDataState(undefined);
    setError(null);
    setLoading(false);
    if (key !== null) void reload();
  }, [key, reload]);

  // Refresh immediately when (re-)enabled, e.g. when a hidden tab is shown again.
  const firstEnable = useRef(true);
  useEffect(() => {
    if (firstEnable.current) {
      firstEnable.current = false;
      return;
    }
    if (enabled && keyRef.current !== null) void reload();
  }, [enabled, reload]);

  useEffect(() => {
    if (key === null || !enabled || !interval) return;
    let stopped = false;
    let t = 0;
    const tick = async () => {
      if (stopped) return;
      await reload();
      if (!stopped) t = window.setTimeout(tick, interval);
    };
    t = window.setTimeout(tick, interval);
    return () => {
      stopped = true;
      window.clearTimeout(t);
    };
  }, [key, enabled, interval, reload]);

  const setData = useCallback((d: T) => {
    seq.current++; // supersede any in-flight fetch
    setDataState(d);
    setError(null);
  }, []);

  return { data, error, loading, reload, setData };
}

/** Current time, re-rendered every `ms` while enabled. */
export function useNow(ms: number, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms, enabled]);
  return now;
}

/** Run an async action with busy + error state. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string[] | null>(null);
  const run = useCallback(async <R,>(fn: () => Promise<R>): Promise<R | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(errorMessages(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, setError, run };
}

/** State persisted in localStorage (UI preferences only; no data). */
export function useStoredState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (nv: T) => {
      setV(nv);
      try {
        window.localStorage.setItem(key, JSON.stringify(nv));
      } catch {
        /* ignore */
      }
    },
    [key],
  );
  return [v, set];
}
