import { useEffect, useState } from "react";

/**
 * Persists a value to localStorage under `key`. Re-reads from storage
 * whenever `key` itself changes (e.g. switching problems) instead of only
 * on mount, so <CodeEditor> can key this per-problem and get that
 * problem's own saved buffer rather than carrying over the previous one.
 */
export function useLocalStorage(key: string, initial: string) {
  const [value, setValue] = useState<string>(() => localStorage.getItem(key) ?? initial);

  useEffect(() => {
    setValue(localStorage.getItem(key) ?? initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    localStorage.setItem(key, value);
  }, [key, value]);

  return [value, setValue] as const;
}
