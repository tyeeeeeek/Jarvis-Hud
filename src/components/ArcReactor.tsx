import { useEffect, useRef, useState } from "react";
import { motion, useMotionValue, useSpring } from "motion/react";
import { animate as animeAnimate, stagger as animeStagger } from "animejs";
import { WeatherWidget } from "./WeatherWidget";
import { AuroraField } from "./AuroraField";
import { TimeWidget } from "./TimeWidget";
import { JarvisCore } from "./JarvisCore";
import { JarvisConsole, type LogEntry } from "./JarvisConsole";
import { MapWidget, type MapTarget } from "./MapWidget";
import { HudPage } from "./HudPage";
import { useScramble } from "../hooks/useScramble";
import { isMuted, playRespond, playWake, setMuted, startThinking, stopThinking } from "../sound";
import "./ArcReactor.css";

const MAX_LOG_ENTRIES = 8;
const MAX_VISION_ENTRIES = 6;

type PageId = "weather" | "time" | "jarvisConsole" | "map" | "jarvisCore";

// Order here is the dock's left-to-right order too.
const PAGE_LABELS: Record<PageId, string> = {
  weather: "Weather",
  time: "Time",
  jarvisConsole: "Jarvis Console",
  map: "Map",
  jarvisCore: "Jarvis Core",
};
const DOCK_ORDER = Object.keys(PAGE_LABELS) as PageId[];

// How long the panel's close animation (detailPanelOut/detailScrimOut in
// ArcReactor.css) actually takes -- kept mounted this long after closing so
// the reverse animation gets to play instead of the panel just vanishing.
const CLOSE_ANIM_MS = 420;

const ICON_PROPS = { width: 14, height: 14, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
const DOCK_ICONS: Record<PageId, React.ReactNode> = {
  weather: <svg {...ICON_PROPS}><path d="M7 18a4.5 4.5 0 0 1-.5-8.98A5.5 5.5 0 0 1 17.3 8.02 4 4 0 0 1 17 16H7Z" /></svg>,
  time: <svg {...ICON_PROPS}><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></svg>,
  jarvisConsole: <svg {...ICON_PROPS}><path d="M12 3v4M8 5.5v3M16 5.5v3" /><rect x="5" y="9" width="14" height="9" rx="3" /><path d="M9.5 21h5" /></svg>,
  map: <svg {...ICON_PROPS}><path d="M9 4 4 6v14l5-2 6 2 5-2V4l-5 2-6-2Z" /><path d="M9 4v14M15 6v14" /></svg>,
  jarvisCore: <svg {...ICON_PROPS}><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="3" /><path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2" /></svg>,
};

// Magnetic hover: the icon leans slightly toward the cursor within its own
// bounds, springing back on mouse-leave -- the React-Bits "Magnet"
// interaction pattern, built directly on Motion's hooks rather than
// pulling in a whole extra component for one effect.
function MagneticDockIcon({
  dockRef, active, title, onClick, children,
}: {
  dockRef: (el: HTMLButtonElement | null) => void;
  active: boolean;
  title: string;
  onClick: (e: React.MouseEvent<HTMLButtonElement>) => void;
  children: React.ReactNode;
}) {
  const x = useMotionValue(0);
  const y = useMotionValue(0);
  const springX = useSpring(x, { stiffness: 320, damping: 22 });
  const springY = useSpring(y, { stiffness: 320, damping: 22 });

  return (
    <motion.button
      ref={dockRef}
      className={`hud-dock__icon ${active ? "hud-dock__icon--on" : ""}`}
      title={title}
      onClick={onClick}
      onMouseMove={(e) => {
        const rect = e.currentTarget.getBoundingClientRect();
        x.set((e.clientX - (rect.left + rect.width / 2)) * 0.35);
        y.set((e.clientY - (rect.top + rect.height / 2)) * 0.35);
      }}
      onMouseLeave={() => { x.set(0); y.set(0); }}
      style={{ x: springX, y: springY }}
    >
      {children}
    </motion.button>
  );
}

// Radial tick marks around the main ring -- detail *on* the single ring
// (not a second ring), lighting up in a center-out stagger on boot via
// anime.js. Every 4th tick is drawn longer ("major") for a instrument-dial
// read rather than a uniform dashed circle.
const TICK_COUNT = 48;
const TICK_ANGLES = Array.from({ length: TICK_COUNT }, (_, i) => (i / TICK_COUNT) * 360);

function TickRing() {
  const svgRef = useRef<SVGSVGElement>(null);
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const ticks = el.querySelectorAll<SVGLineElement>(".ring-tick");
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      ticks.forEach(t => { t.style.opacity = "1"; });
      return;
    }
    animeAnimate(ticks, { opacity: [0, 1], duration: 900, delay: animeStagger(14, { from: "center" }), ease: "outExpo" });
  }, []);

  return (
    <svg ref={svgRef} className="arc-reactor__ticks" viewBox="0 0 100 100">
      {TICK_ANGLES.map((angle, i) => (
        <line
          key={i}
          className={`ring-tick ${i % 4 === 0 ? "ring-tick--major" : ""}`}
          x1="50" y1="2.2" x2="50" y2={i % 4 === 0 ? "6.8" : "4.8"}
          transform={`rotate(${angle} 50 50)`}
        />
      ))}
    </svg>
  );
}

interface JarvisMsg {
  type: string;
  value?: any;
  text?: string;
  location?: string;
  country?: string;
  lat?: number;
  lon?: number;
}

/**
 * The whole interface, rebuilt around one rule: only one thing is ever on
 * screen at a time. Idle is just the ring, a status line, and a dock of
 * small nav dots -- nothing floating, nothing overlapping, nothing to
 * arrange. Tapping a dock icon (or a voice command that implies one --
 * "pull up a map of ...", the phone's camera going live) flows that one
 * page in full, with the ring shrinking to a small corner mark rather
 * than swapping to a separate "minimized" component -- it's the same
 * element the whole time, just relocated, which is what actually reads
 * as "the logo flows out of the way" rather than one thing vanishing and
 * a different one appearing in its place.
 *
 * This replaced an earlier version built around draggable floating
 * widget cards (kept-forever visible, repositionable, stacking
 * z-index...) that, in practice, just looked like scattered boxes with
 * no relationship to each other -- the opposite of what a HUD should
 * read as. Nothing about the underlying data changed, only how it's
 * presented: every page below is the exact same content component
 * (WeatherWidget, TimeWidget, ...) this file always rendered, just
 * shown one at a time inside a single full page rather than eight
 * always-on cards.
 */
export default function ArcReactor() {
  const [micActive, setMicActive] = useState(false);
  const [jarvisConnected, setJarvisConnected] = useState(false);
  const [jarvisStatus, setJarvisStatus] = useState("idle");
  const [jarvisSpeaking, setJarvisSpeaking] = useState(false);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [wakeFlash, setWakeFlash] = useState(false);
  const [barVisible, setBarVisible] = useState(false);
  const [muted, setMutedState] = useState(isMuted);
  const [activePage, setActivePage] = useState<PageId | null>(null);
  // The page mid-close-animation, if any -- see CLOSE_ANIM_MS. Lets the
  // panel keep rendering (and play its reverse animation) for one beat
  // after activePage itself has already gone back to null.
  const [closingPage, setClosingPage] = useState<PageId | null>(null);
  const closingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [pageOrigin, setPageOrigin] = useState<{ x: number; y: number } | null>(null);
  const [mapTarget, setMapTarget] = useState<MapTarget | null>(null);
  const [radarRequest, setRadarRequest] = useState<{ target: MapTarget | null; nonce: number } | null>(null);
  const radarNonceRef = useRef(0);

  const logIdRef = useRef(0);
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
  const dockRefs = useRef<Partial<Record<PageId, HTMLButtonElement | null>>>({});
  const pulseRef = useRef<HTMLDivElement>(null);

  // Sound design: a wake chirp on the wake-word flash, a soft ambient tick
  // loop for as long as Jarvis is thinking/processing, and a short confirm
  // tone right as it starts speaking. All synthesized (see ../sound.ts).
  useEffect(() => { if (wakeFlash) playWake(); }, [wakeFlash]);

  // One-shot radial pulse expanding from the ring's edge on wake-word --
  // a burst of reactivity on the single ring itself, not a second
  // permanent ring, matching the "amplify, don't add rings" direction.
  useEffect(() => {
    if (!wakeFlash || !pulseRef.current) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    animeAnimate(pulseRef.current, { scale: [0.85, 2.4], opacity: [0.85, 0], duration: 900, ease: "outExpo" });
  }, [wakeFlash]);

  const statusLower = jarvisStatus.toLowerCase();
  const isThinking = !jarvisSpeaking && (statusLower.includes("think") || statusLower.includes("process"));
  useEffect(() => {
    if (isThinking) startThinking(); else stopThinking();
    return () => stopThinking();
  }, [isThinking]);

  const prevSpeakingRef = useRef(false);
  useEffect(() => {
    if (jarvisSpeaking && !prevSpeakingRef.current) playRespond();
    prevSpeakingRef.current = jarvisSpeaking;
  }, [jarvisSpeaking]);

  const toggleMuted = () => {
    const next = !muted;
    setMutedState(next);
    setMuted(next);
  };

  const openPage = (id: PageId, origin?: { x: number; y: number }) => {
    if (activePage === id) { closePage(); return; } // tapping the open page's own dock icon closes it
    // Switching straight to a different page: the panel itself stays
    // mounted throughout (only its body content swaps), so cancel any
    // in-flight close animation rather than letting it finish pointlessly.
    if (closingTimerRef.current) { clearTimeout(closingTimerRef.current); closingTimerRef.current = null; }
    setClosingPage(null);
    setActivePage(id);
    setPageOrigin(origin ?? null);
  };
  const closePage = () => {
    if (activePage) {
      setClosingPage(activePage);
      if (closingTimerRef.current) clearTimeout(closingTimerRef.current);
      closingTimerRef.current = setTimeout(() => setClosingPage(null), CLOSE_ANIM_MS);
    }
    setActivePage(null);
  };
  useEffect(() => () => { if (closingTimerRef.current) clearTimeout(closingTimerRef.current); }, []);

  const onDockClick = (id: PageId, e: React.MouseEvent<HTMLButtonElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    openPage(id, { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 });
  };

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (e.clientY <= 10) setBarVisible(true);
      else if (e.clientY > 38) setBarVisible(false);
    };
    window.addEventListener("mousemove", onMove);
    return () => window.removeEventListener("mousemove", onMove);
  }, []);

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
          case "show_map":
            if (typeof data.lat === "number" && typeof data.lon === "number") {
              setMapTarget({ lat: data.lat, lon: data.lon, name: data.location || "" });
              openPage("map");
            }
            break;
          case "show_weather_radar": {
            const target = (typeof data.lat === "number" && typeof data.lon === "number")
              ? { lat: data.lat, lon: data.lon, name: data.location || "" }
              : null;
            setRadarRequest({ target, nonce: ++radarNonceRef.current });
            openPage("map");
            break;
          }
          case "wake_word":
            setWakeFlash(true);
            setTimeout(() => setWakeFlash(false), 400);
            break;
          case "response":
            if (data.text) pushLog("said", data.text);
            break;
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const bottomLabel = !jarvisConnected
    ? (micActive ? "LISTENING" : "JARVIS")
    : jarvisSpeaking ? "SPEAKING"
    : jarvisStatus.toUpperCase();
  const scrambledLabel = useScramble(bottomLabel);
  const scrambledHoloText = useScramble("J.A.R.V.I.S", 22);

  const reactorDataStatus = (jarvisSpeaking ? "speaking" : jarvisStatus).toLowerCase();
  // Minimize-to-corner applies whenever a page is open (or mid-close-
  // animation, via closingPage) -- see CLOSE_ANIM_MS.
  const minimized = activePage !== null || closingPage !== null;
  const displayedPage = activePage ?? closingPage;

  const pageContent: Partial<Record<PageId, React.ReactNode>> = {
    weather: <WeatherWidget />,
    time: <TimeWidget />,
    jarvisCore: <JarvisCore />,
    jarvisConsole: <JarvisConsole status={jarvisStatus} log={log} connected={jarvisConnected} />,
    map: <MapWidget target={mapTarget} radarRequest={radarRequest} onClose={closePage} />,
  };

  return (
    <>
      <div className="hud-reveal__line hud-reveal__line--top" />
      <div className="hud-reveal__line hud-reveal__line--bottom" />

      <div className="hud-reveal">
        <div
          className={`arc-reactor ${wakeFlash ? "arc-reactor--wake-flash" : ""} ${minimized ? "arc-reactor--minimized" : ""}`}
          ref={containerRef}
          data-status={reactorDataStatus}
        >
          {!minimized && <AuroraField />}
          <div className="arc-reactor__ring" onClick={() => (minimized ? closePage() : startMic())}>
            {!minimized && <TickRing />}
            {!minimized && <div ref={pulseRef} className="arc-reactor__pulse" />}
            <div className="arc-reactor__ring-inner">
              {!minimized && <div className="holo-text">{scrambledHoloText}</div>}
            </div>
          </div>

          {!minimized && <div className="arc-reactor__label">{scrambledLabel}</div>}

          <div className="hud-dock">
            {DOCK_ORDER.map(id => (
              <MagneticDockIcon
                key={id}
                dockRef={(el) => { dockRefs.current[id] = el; }}
                active={activePage === id}
                title={PAGE_LABELS[id]}
                onClick={(e) => onDockClick(id, e)}
              >
                {DOCK_ICONS[id]}
              </MagneticDockIcon>
            ))}
          </div>
        </div>
      </div>

      {displayedPage && (
        <HudPage
          title={PAGE_LABELS[displayedPage]}
          origin={pageOrigin}
          closing={!activePage}
          fullBleed={displayedPage === "map"}
          onClose={closePage}
        >
          {pageContent[displayedPage]}
        </HudPage>
      )}

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
          <div style={{ fontSize: "10px", letterSpacing: "3px", color: "rgba(0,240,255,0.45)", fontFamily: "var(--hud-font)", userSelect: "none" }}>
            J.A.R.V.I.S · COMMAND INTERFACE
          </div>
          <div style={{ display: "flex", gap: "6px", WebkitAppRegion: "no-drag" } as React.CSSProperties}>
            <button onClick={toggleMuted}
              title={muted ? "Unmute HUD sounds" : "Mute HUD sounds"}
              className={`hud-mute-btn ${muted ? "hud-mute-btn--muted" : ""}`}>
              {muted ? "♪̸" : "♪"}
            </button>
            <button onClick={() => (window as any).electronAPI.minimise()}
              title="Minimise"
              style={{ background: "rgba(0,240,255,0.08)", border: "1px solid rgba(0,240,255,0.2)", borderRadius: "3px", color: "rgba(0,240,255,0.6)", width: "28px", height: "20px", cursor: "pointer", fontSize: "14px", fontFamily: "var(--hud-font)", lineHeight: "1" }}>
              −
            </button>
            <button onClick={() => (window as any).electronAPI.maximise()}
              title="Maximise"
              style={{ background: "rgba(0,240,255,0.08)", border: "1px solid rgba(0,240,255,0.2)", borderRadius: "3px", color: "rgba(0,240,255,0.6)", width: "28px", height: "20px", cursor: "pointer", fontSize: "10px", fontFamily: "var(--hud-font)" }}>
              □
            </button>
            <button onClick={() => (window as any).electronAPI.close()}
              title="Close"
              style={{ background: "rgba(255,40,40,0.1)", border: "1px solid rgba(255,60,60,0.3)", borderRadius: "3px", color: "rgba(255,80,80,0.75)", width: "28px", height: "20px", cursor: "pointer", fontSize: "12px", fontFamily: "var(--hud-font)" }}>
              ✕
            </button>
          </div>
        </div>
      )}
    </>
  );
}
