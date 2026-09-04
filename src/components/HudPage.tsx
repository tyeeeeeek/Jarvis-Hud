import { useEffect, useRef } from "react";
import { animate } from "motion";
import { ParticleField } from "./ParticleField";

interface HudPageProps {
  title: string;
  /** Viewport coordinates of the dock icon that opened this -- the
   * iris-wipe reveal is anchored here, so the page genuinely flows out of
   * the icon you clicked rather than just fading in. */
  origin: { x: number; y: number } | null;
  /** True for the one render where the caller has already cleared its own
   * "which page is open" state but is keeping this mounted a beat longer
   * so the close animation (mirroring the open one) can actually play. */
  closing?: boolean;
  /** Edge-to-edge, no header/close-button chrome, no padded content
   * column -- for content that IS the window (the map, matching the
   * reference: the map fills the whole screen with nothing but the thin
   * top ticker and the corner ring/dock over it) rather than a page of
   * data displayed within a frame. Closing still works the same way
   * (Esc, or clicking the minimized ring) even with no visible close
   * button. */
  fullBleed?: boolean;
  onClose: () => void;
  children: React.ReactNode;
}

/** The single "focused content" surface -- takes over the whole window
 * (same iris-wipe language as CameraFocusView's full-bleed camera
 * takeover) rather than floating a bordered card over a dimmed backdrop.
 * There's no "outside" to click here on purpose: this IS the window now,
 * not a box sitting on top of it, so closing only happens via Esc, the
 * close control, or clicking the reactor ring (now minimized to the
 * corner) back home. */
export function HudPage({ title, origin, closing, fullBleed, onClose, children }: HudPageProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (closing) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, closing]);

  // Iris-wipe open/close anchored at the dock icon that was tapped --
  // Motion-driven (replacing the old @keyframes hudPageIn/Out) specifically
  // so re-running this on every origin change (switching directly between
  // two open pages, not just opening/closing) blends smoothly instead of
  // either snapping instantly or needing a full remount -- a plain CSS
  // `animation` never retriggers just because a custom property it reads
  // changes value. Respects prefers-reduced-motion the same way the rest
  // of this file's animations already do (see ArcReactor.css).
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const x = origin ? `${origin.x}px` : "50%";
    const y = origin ? `${origin.y}px` : "50%";
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      el.style.clipPath = closing ? `circle(0% at ${x} ${y})` : `circle(150% at ${x} ${y})`;
      el.style.opacity = closing ? "0" : "1";
      return;
    }
    if (closing) {
      animate(el, { clipPath: [`circle(150% at ${x} ${y})`, `circle(0% at ${x} ${y})`], opacity: [1, 0] },
        { duration: 0.42, ease: [0.7, 0, 0.84, 0] });
    } else {
      animate(el, { clipPath: [`circle(0% at ${x} ${y})`, `circle(150% at ${x} ${y})`], opacity: [0, 1] },
        { duration: 0.5, ease: [0.16, 1, 0.3, 1] });
    }
  }, [origin?.x, origin?.y, closing]);

  return (
    <div ref={rootRef} className={`hud-page ${fullBleed ? "hud-page--full-bleed" : ""} ${closing ? "hud-page--closing" : ""}`}>
      {!fullBleed && <ParticleField />}
      {!fullBleed && <div className="hud-page__glow" />}
      {fullBleed ? (
        <div className="hud-page__ticker" />
      ) : (
        <div className="hud-page__header">
          <span className="hud-page__title">{title}</span>
          <button className="hud-page__close" onClick={onClose} title="Close (Esc)">✕</button>
        </div>
      )}
      <div className={`hud-page__body ${fullBleed ? "hud-page__body--full-bleed" : ""}`}>{children}</div>
    </div>
  );
}
