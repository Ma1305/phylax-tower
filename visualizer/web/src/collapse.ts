import { useCallback, useState } from 'react';

/** A boolean kept in localStorage (panel and section collapse state). */
export function useStored(key: string, initial = false): [boolean, (v: boolean) => void] {
  const [v, setV] = useState(() => {
    try {
      const s = localStorage.getItem(`phylax.${key}`);
      return s == null ? initial : s === '1';
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (n: boolean) => {
      setV(n);
      try {
        localStorage.setItem(`phylax.${key}`, n ? '1' : '0');
      } catch {
        /* private mode */
      }
    },
    [key],
  );
  return [v, set];
}
