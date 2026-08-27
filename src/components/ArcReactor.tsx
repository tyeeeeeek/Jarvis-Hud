import { useEffect, useMemo, useRef, useState } from "react";
import { WidgetProvider } from "./WidgetContext";
import { DraggableWidget } from "./DraggableWidget";
import { WeatherWidget } from "./WeatherWidget";
import { TimeWidget } from "./TimeWidget";
import { FinanceWidget } from "./FinanceWidget";
import { JarvisConsole, type LogEntry } from "./JarvisConsole";
import { CreationPanel, type Creation } from "./CreationPanel";
import { ContextMenu } from "./ContextMenu";
import "./ArcReactor.css";

const MAX_LOG_ENTRIES = 8;

const VIS_KEY = "jarvis-widget-visibility";

type WidgetId = "weather" | "time" | "jarvisConsole" | "finance";
type VisibilityMap = Record<WidgetId, boolean>;

const DEFAULT_VISIBILITY: VisibilityMap = {
  weather: true,
  time: true,
  jarvisConsole: true,
  finance: true,
};

function loadVisibility(): VisibilityMap {
  try {
    const saved = JSON.parse(localStorage.getItem(VIS_KEY) ?? "{}");
    return { ...DEFAULT_VISIBILITY, ...saved };
  } catch { return { ...DEFAULT_VISIBILITY }; }
}

function saveVisibility(v: VisibilityMap) {
  try { localStorage.setItem(VIS_KEY, JSON.stringify(v)); } catch {}
}

const WIDGET_LABELS: Record<WidgetId, string> = {
  weather: "Weather",
  time: "Time",
  jarvisConsole: "Jarvis Console",
  finance: "Finance",
};

type CtxState =
  | { x: number; y: number; type: "widget"; widgetId: WidgetId }
  | { x: number; y: number; type: "empty" }
  | null;

function defaultPositions() {
  const W = window.innerWidth;
  const H = window.innerHeight;
  return {
    weather: { x: W * 0.05, y: H * 0.12 },
    time: { x: W * 0.72, y: H * 0.10 },
    jarvisConsole: { x: W * 0.72, y: H * 0.55 },
    finance: { x: W * 0.05, y: H * 0.55 },
  };
}

interface JarvisMsg {
  type: string;
  value?: any;
  text?: string;
  title?: string;
  kind?: string;
  html?: string;
}

export default function ArcReactor() {
  const [micActive, setMicActive] = useState(false);
  const [clock, setClock] = useState(new Date());
  const [visibility, setVisibility] = useState<VisibilityMap>(loadVisibility);
  const [contextMenu, setContextMenu] = useState<CtxState>(null);
  const [jarvisConnected, setJarvisConnected] = useState(false);
  const [jarvisStatus, setJarvisStatus] = useState("idle");
  const [jarvisSpeaking, setJarvisSpeaking] = useState(false);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [creations, setCreations] = useState<Creation[]>([]);
  const [wakeFlash, setWakeFlash] = useState(false);
  const [barVisible, setBarVisible] = useState(false);
  const [widgetStack, setWidgetStack] = useState<WidgetId[]>(["weather", "time", "jarvisConsole", "finance"]);

  const logIdRef = useRef(0);
  const creationIdRef = useRef(0);
  const pushLog = (kind: LogEntry["kind"], text: string) => {
    if (!text) return;
    setLog(prev => [...prev, { id: logIdRef.current++, kind, text }].slice(-MAX_LOG_ENTRIES));
  };

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const ampRef = useRef(0);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const dataRef = useRef<Uint8Array | null>(null);

  const pos = useMemo(defaultPositions, []);

  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (e.clientY <= 10) setBarVisible(true);
      else if (e.clientY > 38) setBarVisible(false);
    };
    window.addEventListener("mousemove", onMove);
    return () => window.removeEventListener("mousemove", onMove);
  }, []);

  const bringToFront = (id: WidgetId) => {
    setWidgetStack(prev => [...prev.filter(w => w !== id), id]);
  };
  const getZ = (id: WidgetId): number => {
    const idx = widgetStack.indexOf(id);
    return 100 + (idx === -1 ? 0 : idx);
  };

  const hideWidget = (id: WidgetId) => {
    setVisibility(prev => {
      const next = { ...prev, [id]: false };
      saveVisibility(next);
      return next;
    });
  };

  const showWidget = (id: WidgetId) => {
    setVisibility(prev => {
      const next = { ...prev, [id]: true };
      saveVisibility(next);
      return next;
    });
  };

  const openWidgetMenu = (id: string, x: number, y: number) =>
    setContextMenu({ x, y, type: "widget", widgetId: id as WidgetId });

  const openEmptyMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, type: "empty" });
  };

  const closeMenu = () => setContextMenu(null);

  const buildMenuItems = () => {
    if (!contextMenu) return [];
    if (contextMenu.type === "widget") {
      return [{
        label: "Delete Widget", icon: "✕", variant: "danger" as const,
        action: () => hideWidget(contextMenu.widgetId),
      }];
    }
    const hiddenIds = (Object.keys(visibility) as WidgetId[]).filter(id => !visibility[id]);
    if (hiddenIds.length === 0)
      return [{ label: "All widgets visible", disabled: true, icon: "", action: () => {} }];
    return hiddenIds.map(id => ({
      label: WIDGET_LABELS[id], icon: "⊕", variant: "restore" as const,
      action: () => showWidget(id),
    }));
  };

  const menuTitle = contextMenu?.type === "widget"
    ? WIDGET_LABELS[(contextMenu as { widgetId: WidgetId }).widgetId]
    : "Restore Widget";

  const startMic = async () => {
    if (micActive) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
      const audioCtx = new AudioCtx();
      await audioCtx.resume();
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      audioCtx.createMediaStreamSource(stream).connect(analyser);
      analyserRef.current = analyser;
      dataRef.current = new Uint8Array(analyser.frequencyBinCount);
      setMicActive(true);
    } catch (e) { console.error("Mic error:", e); }
  };

  useEffect(() => {
    let frame: number;
    const loop = (ts: number) => {
      const el = containerRef.current;
      const analyser = analyserRef.current;
      const data = dataRef.current;
      if (el) {
        let amp = ampRef.current, isSpeaking = false;
        if (analyser && data) {
          analyser.getByteFrequencyData(data as Uint8Array<ArrayBuffer>);
          let sum = 0;
          for (let i = 0; i < data.length; i++) sum += data[i];
          const boosted = Math.min(1, (sum / data.length / 255) * 5);
          amp = amp * 0.85 + boosted * 0.15;
          ampRef.current = amp;
          isSpeaking = boosted > 0.05;
        }
        const jb = jarvisSpeaking ? 0.5 : 0;
        const breath = 0.5 + Math.sin(ts * 0.002) * 0.5;
        const scale = (isSpeaking || jarvisSpeaking)
          ? 0.85 + Math.max(amp, jb) * 0.35
          : 0.85 + breath * 0.15;
        el.style.setProperty("--core-scale", scale.toFixed(4));
        el.style.setProperty("--glow-opacity", (0.3 + Math.max(amp, jb) * 0.7).toFixed(4));
        el.style.setProperty("--glow-radius", `${(20 + Math.max(amp, jb) * 80).toFixed(1)}px`);
      }
      frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
  }, [jarvisSpeaking]);

  useEffect(() => {
    let cancelled = false;

    function connect() {
      if (cancelled) return;
      const ws = new WebSocket("ws://localhost:8765");
      wsRef.current = ws;

      ws.onopen = () => {
        setJarvisConnected(true);
        ws.send(JSON.stringify({ type: "hud_ready" }));
      };

      ws.onmessage = (event) => {
        let data: JarvisMsg;
        try { data = JSON.parse(event.data); }
        catch { return; }

        switch (data.type) {
          case "status":
            setJarvisStatus(String(data.value ?? "idle"));
            break;
          case "speaking":
            setJarvisSpeaking(!!data.value);
            if (data.value && data.text) pushLog("said", data.text);
            break;
          case "transcript":
            if (data.text) pushLog("heard", data.text);
            break;
          case "activity":
            if (data.text) pushLog("activity", data.text);
            break;
          case "wake_word":
            setWakeFlash(true);
            setTimeout(() => setWakeFlash(false), 400);
            break;
          case "response":
            if (data.text) pushLog("said", data.text);
            break;
          case "creation_ready": {
            const id = creationIdRef.current++;
            setCreations(prev => [...prev, {
              id, title: data.title ?? "Untitled",
              kind: data.kind ?? "dashboard",
              html: data.html ?? "",
            }]);
            break;
          }
          default:
            break;
        }
      };

      ws.onerror = () => ws.close();

      ws.onclose = () => {
        setJarvisConnected(false);
        wsRef.current = null;
        if (!cancelled) {
          reconnectTimerRef.current = setTimeout(connect, 3000);
        }
      };
    }

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, []);

  const bottomLabel = !jarvisConnected
    ? (micActive ? "LISTENING" : "JARVIS")
    : jarvisSpeaking ? "SPEAKING"
    : jarvisStatus.toUpperCase();

  return (
    <WidgetProvider>
      <div className="hud-reveal__line hud-reveal__line--top" />
      <div className="hud-reveal__line hud-reveal__line--bottom" />

      <div className="hud-reveal">

        <DraggableWidget id="weather" visible={visibility.weather}
          initialX={pos.weather.x} initialY={pos.weather.y}
          onRightClick={openWidgetMenu}
          zIndex={getZ("weather")} onFocus={() => bringToFront("weather")}>
          <WeatherWidget />
        </DraggableWidget>

        <DraggableWidget id="time" visible={visibility.time}
          initialX={pos.time.x} initialY={pos.time.y}
          onRightClick={openWidgetMenu}
          zIndex={getZ("time")} onFocus={() => bringToFront("time")}>
          <TimeWidget />
        </DraggableWidget>

        <DraggableWidget id="finance" visible={visibility.finance}
          initialX={pos.finance.x} initialY={pos.finance.y}
          onRightClick={openWidgetMenu}
          zIndex={getZ("finance")} onFocus={() => bringToFront("finance")}>
          <FinanceWidget />
        </DraggableWidget>

        <DraggableWidget id="jarvisConsole" visible={visibility.jarvisConsole}
          initialX={pos.jarvisConsole.x} initialY={pos.jarvisConsole.y}
          onRightClick={openWidgetMenu}
          zIndex={getZ("jarvisConsole")} onFocus={() => bringToFront("jarvisConsole")}>
          <JarvisConsole
            status={jarvisStatus}
            log={log}
            connected={jarvisConnected}
          />
        </DraggableWidget>

        {creations.map((c, i) => (
          <CreationPanel
            key={c.id}
            creation={c}
            offset={i}
            onClose={(id) => setCreations(prev => prev.filter(cr => cr.id !== id))}
          />
        ))}

        {contextMenu && (
          <ContextMenu
            x={contextMenu.x}
            y={contextMenu.y}
            items={buildMenuItems()}
            onClose={closeMenu}
            title={menuTitle}
          />
        )}

        <div
          className={`arc-reactor ${wakeFlash ? "arc-reactor--wake-flash" : ""}`}
          ref={containerRef}
          onContextMenu={openEmptyMenu}
          data-status={jarvisSpeaking ? "speaking" : jarvisStatus}
        >
          <div className="hud-scan-line" />
          <div className="hud-corner hud-corner-tl" />
          <div className="hud-corner hud-corner-tr" />
          <div className="hud-corner hud-corner-bl" />
          <div className="hud-corner hud-corner-br" />

          <div className="arc-reactor__hologram">
            <div className="arc-reactor__core">
              <div className="arc-reactor__core-shell" />
              <div className="arc-reactor__core-inner">
                <div className="holo-text">J.A.R.V.I.S</div>
              </div>
            </div>
          </div>

          <div className="arc-reactor__orbit-rings">
            <div className="arc-reactor__orbit arc-reactor__orbit--1" />
            <div className="arc-reactor__orbit arc-reactor__orbit--2" />
            <div className="arc-reactor__orbit arc-reactor__orbit--3" />
          </div>

          <div className="arc-reactor__orb" onClick={startMic} />
          <div className="arc-reactor__pulse-ring" />
          <div className="arc-reactor__haze" />

          <div className="hud-background-time">
            <div className="hud-time">
              {clock.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}
            </div>
            <div className="hud-date">
              {clock.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" })}
            </div>
          </div>

          <div className="arc-reactor__label">{bottomLabel}</div>
        </div>

      </div>

      {(window as any).electronAPI && (
        <div
          style={{
            position: "fixed", top: 0, left: 0, right: 0, height: "36px", zIndex: 99999,
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "0 12px", WebkitAppRegion: "drag" as any,
            background: "rgba(0,0,0,0.6)", borderBottom: "1px solid rgba(0,240,255,0.15)",
            backdropFilter: "blur(8px)",
            transform: barVisible ? "translateY(0)" : "translateY(-100%)",
            transition: "transform 0.2s ease",
            pointerEvents: barVisible ? "all" : "none",
          } as React.CSSProperties}
        >
          <div style={{ fontSize: "10px", letterSpacing: "3px", color: "rgba(0,240,255,0.45)", fontFamily: "monospace", userSelect: "none" }}>
            J.A.R.V.I.S · COMMAND INTERFACE
          </div>
          <div style={{ display: "flex", gap: "6px", WebkitAppRegion: "no-drag" } as React.CSSProperties}>
            <button onClick={() => (window as any).electronAPI.minimise()}
              title="Minimise"
              style={{ background: "rgba(0,240,255,0.08)", border: "1px solid rgba(0,240,255,0.2)", borderRadius: "3px", color: "rgba(0,240,255,0.6)", width: "28px", height: "20px", cursor: "pointer", fontSize: "14px", fontFamily: "monospace", lineHeight: "1" }}>
              −
            </button>
            <button onClick={() => (window as any).electronAPI.maximise()}
              title="Maximise"
              style={{ background: "rgba(0,240,255,0.08)", border: "1px solid rgba(0,240,255,0.2)", borderRadius: "3px", color: "rgba(0,240,255,0.6)", width: "28px", height: "20px", cursor: "pointer", fontSize: "10px", fontFamily: "monospace" }}>
              □
            </button>
            <button onClick={() => (window as any).electronAPI.close()}
              title="Close"
              style={{ background: "rgba(255,40,40,0.1)", border: "1px solid rgba(255,60,60,0.3)", borderRadius: "3px", color: "rgba(255,80,80,0.75)", width: "28px", height: "20px", cursor: "pointer", fontSize: "12px", fontFamily: "monospace" }}>
              ✕
            </button>
          </div>
        </div>
      )}
    </WidgetProvider>
  );
}
