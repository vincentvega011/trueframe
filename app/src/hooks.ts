import { useCallback, useEffect, useRef, useState } from "react";

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

/** Runs `fn` now and then every `ms`; keeps the last good value while a refresh fails. `stop(value)` ends the polling. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[], stop?: (v: T) => boolean) {
  const [value, setValue] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const stopRef = useRef(stop);
  stopRef.current = stop;
  const reload = useRef<() => void>(() => {});

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setValue(undefined);
    setLoading(true);
    const run = async () => {
      try {
        const v = await fnRef.current();
        if (!alive) return;
        setValue(v);
        setError(null);
        setLoading(false);
        if (stopRef.current?.(v)) return;
      } catch (e) {
        if (!alive) return;
        setError(e);
        setLoading(false);
      }
      timer = setTimeout(run, ms);
    };
    reload.current = () => { clearTimeout(timer); void run(); };
    void run();
    return () => { alive = false; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { value, error, loading, reload: useCallback(() => reload.current(), []) };
}

export function useHashRoute(): [string, (to: string) => void] {
  const read = () => window.location.hash.replace(/^#/, "") || "/create";
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [route, (to) => { window.location.hash = to; }];
}
