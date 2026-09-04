import { useEffect, useRef } from "react";

export interface VisionEntry {
  id: number;
  question: string;
  answer: string;
}

interface VisionWidgetProps {
  entries: VisionEntry[];
}

/** Shows the phone HUD's camera Q&A (see dashboard/jarvis_hud.html) live
 * on the desktop -- fed over the same WebSocket every other widget here
 * uses (jarvis.py's `_on_bot_event` forwards `vision_qa` events from the
 * shared bot_events bus). Read-only mirror; nothing here can trigger a
 * camera capture from the desktop side. */
export function VisionWidget({ entries }: VisionWidgetProps) {
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries]);

  return (
    <div className="w-vision">
      <div className="w-header">
        <span className="w-label">PHONE VISION</span>
        <span className={`w-jc-dot ${entries.length ? "w-jc-dot--on" : "w-jc-dot--off"}`} />
      </div>

      <div className="w-vision-log" ref={logRef}>
        {entries.length === 0 && (
          <div className="w-idle">Ask a question through the camera on your phone at /hud.</div>
        )}
        {entries.map((e) => (
          <div key={e.id} className="w-vision-row">
            <div className="w-vision-q"><span className="w-jc-tag">SEEN</span> {e.question}</div>
            <div className="w-vision-a">{e.answer}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
