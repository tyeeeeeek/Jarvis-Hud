import { createContext, useCallback, useContext, useRef } from "react";

interface WidgetRect { left: number; top: number; width: number; height: number; }

interface WidgetContextValue {
  registerRect: (id: string, rect: WidgetRect) => void;
  unregisterRect: (id: string) => void;
  getAllRects: (excludeId: string) => Map<string, WidgetRect>;
}

const WidgetContext = createContext<WidgetContextValue | null>(null);

export function WidgetProvider({ children }: { children: React.ReactNode }) {
  const rectsRef = useRef<Map<string, WidgetRect>>(new Map());

  const registerRect = useCallback((id: string, rect: WidgetRect) => {
    rectsRef.current.set(id, rect);
  }, []);

  const unregisterRect = useCallback((id: string) => {
    rectsRef.current.delete(id);
  }, []);

  const getAllRects = useCallback((excludeId: string) => {
    const out = new Map<string, WidgetRect>();
    rectsRef.current.forEach((r, id) => { if (id !== excludeId) out.set(id, r); });
    return out;
  }, []);

  return (
    <WidgetContext.Provider value={{ registerRect, unregisterRect, getAllRects }}>
      {children}
    </WidgetContext.Provider>
  );
}

export function useWidgetContext() {
  const ctx = useContext(WidgetContext);
  if (!ctx) throw new Error("useWidgetContext must be inside <WidgetProvider>");
  return ctx;
}
