import { useEffect, useRef } from "react";

export interface LogEntry {
  id: number;
  kind: "heard" | "said" | "activity";
  text: string;
}

interface JarvisConsoleProps {
  status: string;
  connected: boolean;
  log: LogEntry[];
}

const TAGS: Record<LogEntry["kind"], string> = {
  heard: "YOU",
  said: "JARVIS",
  activity: "•",
};

export function JarvisConsole({ status, connected, log }: JarvisConsoleProps) {
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  return (
    <div className="w-jarvis-console">
      <div className="w-header">
        <span className="w-label">JARVIS LINK</span>
        <span className={`w-jc-dot ${connected ? "w-jc-dot--on" : "w-jc-dot--off"}`} />
      </div>

      <div className="w-jc-status">
        {connected ? status.toUpperCase() : "OFFLINE"}
      </div>

      <div className="w-jc-log" ref={logRef}>
        {log.length === 0 && <div className="w-idle">Say "Jarvis" to begin.</div>}
        {log.map((entry) => (
          <div key={entry.id} className={`w-jc-row w-jc-row--${entry.kind}`}>
            <span className="w-jc-tag">{TAGS[entry.kind]}</span>
            <span className="w-jc-text">{entry.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
