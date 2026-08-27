import { useEffect, useRef } from "react";
import { useWidgetContext } from "./WidgetContext";
import { useMountTransition } from "./useMountTransition";

const LS_KEY = "jarvis-widget-positions";

function loadPositions(): Record<string, { x: number; y: number }> {
  try { return JSON.parse(localStorage.getItem(LS_KEY) ?? "{}"); }
  catch { return {}; }
}

function savePosition(id: string, x: number, y: number) {
  try {
    const p = loadPositions();
    p[id] = { x, y };
    localStorage.setItem(LS_KEY, JSON.stringify(p));
  } catch {}
}

const SNAP_THRESHOLD = 22;
const SNAP_GAP = 10;

function calcSnap(
  x: number, y: number, w: number, h: number,
  others: Map<string, { left: number; top: number; width: number; height: number }>
): { x: number; y: number } {
  let bestX = x, bestY = y;
  let minDX = SNAP_THRESHOLD, minDY = SNAP_THRESHOLD;

  for (const r of others.values()) {
    const r2 = r.left + r.width;
    const r4 = r.top + r.height;

    for (const cx of [r.left, r2 - w, r.left - w - SNAP_GAP, r2 + SNAP_GAP]) {
      const d = Math.abs(x - cx);
      if (d < minDX) { minDX = d; bestX = cx; }
    }

    for (const cy of [r.top, r4 - h, r.top - h - SNAP_GAP, r4 + SNAP_GAP]) {
      const d = Math.abs(y - cy);
      if (d < minDY) { minDY = d; bestY = cy; }
    }
  }

  return { x: bestX, y: bestY };
}

interface DraggableWidgetProps {
  id: string;
  children: React.ReactNode;
  initialX?: number;
  initialY?: number;
  onRightClick?: (id: string, x: number, y: number) => void;
  zIndex?: number;
  onFocus?: () => void;
  visible?: boolean;
}

export function DraggableWidget({
  id,
  children,
  initialX = 100,
  initialY = 100,
  onRightClick,
  zIndex = 10,
  onFocus,
  visible = true,
}: DraggableWidgetProps) {
  const { registerRect, unregisterRect, getAllRects } = useWidgetContext();

  const elRef = useRef<HTMLDivElement>(null);
  const dragState = useRef<{ offsetX: number; offsetY: number } | null>(null);
  const zIndexRef = useRef(zIndex);
  const mounted = useMountTransition(visible, 220);

  useEffect(() => {
    if (mounted && elRef.current) {
      const rect = elRef.current.getBoundingClientRect();
      registerRect(id, { left: rect.left, top: rect.top, width: rect.width, height: rect.height });
    } else if (!mounted) {
      unregisterRect(id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mounted]);

  useEffect(() => {
    zIndexRef.current = zIndex;
    const el = elRef.current;
    if (el && !dragState.current) {
      el.style.zIndex = String(zIndex);
    }
  }, [zIndex]);

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;

    const saved = loadPositions()[id];
    const x = saved?.x ?? initialX;
    const y = saved?.y ?? initialY;

    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
    el.style.zIndex = String(zIndex);

    const rect = el.getBoundingClientRect();
    registerRect(id, {
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height,
    });

    return () => unregisterRect(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onMouseDown = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).tagName === "BUTTON") return;
    e.preventDefault();
    const el = elRef.current;
    if (!el) return;

    onFocus?.();

    const rect = el.getBoundingClientRect();
    dragState.current = {
      offsetX: e.clientX - rect.left,
      offsetY: e.clientY - rect.top,
    };

    el.style.cursor = "grabbing";
    el.style.zIndex = "9999";
    el.style.animationPlayState = "paused";
    el.style.boxShadow = "0 0 30px rgba(0,240,255,0.55), 0 0 60px rgba(0,240,255,0.15), inset 0 0 20px rgba(0,0,0,0.4)";
    el.style.borderColor = "rgba(0,240,255,0.75)";
  };

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragState.current) return;
      const el = elRef.current;
      if (!el) return;

      let x = e.clientX - dragState.current.offsetX;
      let y = e.clientY - dragState.current.offsetY;

      const snapped = calcSnap(x, y, el.offsetWidth, el.offsetHeight, getAllRects(id));
      x = snapped.x;
      y = snapped.y;

      x = Math.max(0, Math.min(x, window.innerWidth - el.offsetWidth));
      y = Math.max(0, Math.min(y, window.innerHeight - el.offsetHeight));

      el.style.left = `${x}px`;
      el.style.top = `${y}px`;

      registerRect(id, {
        left: x,
        top: y,
        width: el.offsetWidth,
        height: el.offsetHeight,
      });
    };

    const onUp = () => {
      if (!dragState.current) return;
      dragState.current = null;

      const el = elRef.current;
      if (!el) return;

      const x = parseFloat(el.style.left);
      const y = parseFloat(el.style.top);
      savePosition(id, x, y);

      el.style.cursor = "grab";
      el.style.zIndex = String(zIndexRef.current);
      el.style.animationPlayState = "running";
      el.style.boxShadow = "";
      el.style.borderColor = "";
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [id, getAllRects, registerRect]);

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    onRightClick?.(id, e.clientX, e.clientY);
  };

  if (!mounted) return null;

  return (
    <div
      ref={elRef}
      className={`hud-widget hud-widget--${id} ${visible ? "hud-widget--enter" : "hud-widget--exit"}`}
      onMouseDown={onMouseDown}
      onContextMenu={handleContextMenu}
    >
      <span className="hud-widget__handle" aria-hidden>⠿</span>
      {children}
    </div>
  );
}
