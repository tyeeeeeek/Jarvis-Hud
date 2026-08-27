import { useEffect, useState } from "react";

// Pinned explicitly to New York (Eastern) time -- not just inherited from
// whatever timezone the OS happens to be set to, so it stays correct even
// if that ever changes.
const TIME_ZONE = "America/New_York";

export function TimeWidget() {
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <>
      <div className="w-header">
        <span className="w-label">NEW YORK, NY</span>
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
    </>
  );
}
