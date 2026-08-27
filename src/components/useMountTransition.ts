import { useEffect, useState } from "react";

// Keeps a component mounted for `exitMs` after `shouldMount` goes false, so
// an exit animation can play instead of the element vanishing instantly.
export function useMountTransition(shouldMount: boolean, exitMs = 220): boolean {
  const [mounted, setMounted] = useState(shouldMount);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (shouldMount) {
      setMounted(true);
    } else {
      timer = setTimeout(() => setMounted(false), exitMs);
    }
    return () => { if (timer) clearTimeout(timer); };
  }, [shouldMount, exitMs]);

  return mounted;
}
