import { useEffect, useState } from "react";

// Pinned explicitly to Eastern time -- not just inherited from whatever
// timezone the OS happens to be set to, so it stays correct even if that
// ever changes. Bethel, CT is on the same Eastern zone as before.
const TIME_ZONE = "America/New_York";

const WORLD_CLOCKS: { label: string; tz: string }[] = [
  { label: "LOS ANGELES", tz: "America/Los_Angeles" },
  { label: "LONDON", tz: "Europe/London" },
  { label: "TOKYO", tz: "Asia/Tokyo" },
  { label: "SYDNEY", tz: "Australia/Sydney" },
];

function dayOfYear(d: Date): number {
  const start = new Date(d.getFullYear(), 0, 0);
  return Math.floor((d.getTime() - start.getTime()) / 86400000);
}
function weekOfYear(d: Date): number {
  const start = new Date(d.getFullYear(), 0, 1);
  return Math.ceil(((d.getTime() - start.getTime()) / 86400000 + start.getDay() + 1) / 7);
}

export function TimeWidget() {
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <>
      <div className="w-header">
        <span className="w-label">BETHEL, CT</span>
      </div>
      <div className="w-clock-row">
        <span className="w-clock">
          {now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true, timeZone: TIME_ZONE })}
        </span>
      </div>
      <div className="w-weekday">
        {now.toLocaleDateString([], { weekday: "long", timeZone: TIME_ZONE }).toUpperCase()}
      </div>
      <div className="w-date">
        {now.toLocaleDateString([], { month: "long", day: "numeric", year: "numeric", timeZone: TIME_ZONE }).toUpperCase()}
      </div>

      <div className="w-divider" />
      <div className="w-row w-row--spread">
        <div className="w-stat">
          <span className="w-stat-lbl">DAY OF YEAR</span>
          <span className="w-stat-val">{dayOfYear(now)} / 365</span>
        </div>
        <div className="w-stat">
          <span className="w-stat-lbl">WEEK</span>
          <span className="w-stat-val">{weekOfYear(now)}</span>
        </div>
      </div>

      <div className="w-divider" />
      <span className="w-sublabel">WORLD CLOCKS</span>
      <div className="w-time-world">
        {WORLD_CLOCKS.map(c => (
          <div key={c.tz} className="w-time-world__row">
            <span className="w-time-world__label">{c.label}</span>
            <span className="w-time-world__val">
              {now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", hour12: true, timeZone: c.tz })}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}
