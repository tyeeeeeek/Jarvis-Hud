import { useEffect, useState } from "react";

export function TimeWidget() {
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <>
      <div className="w-header">
        <span className="w-label">LOCAL TIME</span>
      </div>
      <div className="w-clock-row">
        <span className="w-clock">
          {now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}
        </span>
      </div>
      <div className="w-weekday">
        {now.toLocaleDateString([], { weekday: "long" }).toUpperCase()}
      </div>
      <div className="w-date">
        {now.toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" }).toUpperCase()}
      </div>
    </>
  );
}
