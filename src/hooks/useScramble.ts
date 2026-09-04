import { useEffect, useRef, useState } from "react";

const GLYPHS = "!<>-_\\/[]{}—=+*^?#0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";

/**
 * Holographic "materializing" text effect -- scrambles through random
 * glyphs before settling into the target string, left to right. Re-runs
 * whenever `text` changes, so it doubles as a transition for status-label
 * swaps (IDLE -> LISTENING -> THINKING -> ...), not just first mount.
 */
export function useScramble(text: string, speedMs = 28): string {
  const [display, setDisplay] = useState(text);
  const frameRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let iteration = 0;
    if (frameRef.current) clearInterval(frameRef.current);

    frameRef.current = setInterval(() => {
      setDisplay(
        text
          .split("")
          .map((ch, i) => {
            if (ch === " ") return " ";
            if (i < iteration) return text[i];
            return GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
          })
          .join("")
      );
      iteration += text.length / 12;
      if (iteration >= text.length) {
        if (frameRef.current) clearInterval(frameRef.current);
        setDisplay(text);
      }
    }, speedMs);

    return () => { if (frameRef.current) clearInterval(frameRef.current); };
  }, [text, speedMs]);

  return display;
}
