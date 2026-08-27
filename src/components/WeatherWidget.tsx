import { useEffect, useState } from "react";

// ─── Edit these to your location ──────────────────────────────────────────
const LAT = 41.7658;
const LON = -72.6734;
const LOCATION_LABEL = "HARTFORD, CT";

const WMO: Record<number, string> = {
  0: "CLEAR SKY", 1: "MAINLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST",
  45: "FOG", 48: "RIME FOG",
  51: "LIGHT DRIZZLE", 53: "DRIZZLE", 55: "HEAVY DRIZZLE",
  61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN",
  71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW", 77: "SNOW GRAINS",
  80: "LIGHT SHOWERS", 81: "SHOWERS", 82: "HEAVY SHOWERS",
  85: "SNOW SHOWERS", 86: "HEAVY SNOW SHOWERS",
  95: "THUNDERSTORM", 96: "T-STORM + HAIL", 99: "HEAVY T-STORM",
};

function aqiInfo(aqi: number): { label: string; color: string } {
  if (aqi <= 50) return { label: "GOOD", color: "#00e676" };
  if (aqi <= 100) return { label: "MODERATE", color: "#ffee58" };
  if (aqi <= 150) return { label: "UNHEALTHY*", color: "#ff9800" };
  if (aqi <= 200) return { label: "UNHEALTHY", color: "#ef5350" };
  if (aqi <= 300) return { label: "VERY UNHLT.", color: "#ab47bc" };
  return { label: "HAZARDOUS", color: "#b71c1c" };
}

interface WeatherData {
  temp: number; feelsLike: number; condition: string;
  humidity: number; windSpeed: number; aqi: number;
}

export function WeatherWidget() {
  const [data, setData] = useState<WeatherData | null>(null);
  const [status, setStatus] = useState<"loading" | "ok" | "error">("loading");

  useEffect(() => {
    async function fetchWeather() {
      setStatus("loading");
      try {
        const [wRes, aRes] = await Promise.all([
          fetch(
            `https://api.open-meteo.com/v1/forecast` +
            `?latitude=${LAT}&longitude=${LON}` +
            `&current=temperature_2m,weather_code,apparent_temperature,relative_humidity_2m,wind_speed_10m` +
            `&temperature_unit=fahrenheit&wind_speed_unit=mph`
          ),
          fetch(
            `https://air-quality-api.open-meteo.com/v1/air-quality` +
            `?latitude=${LAT}&longitude=${LON}&current=us_aqi`
          ),
        ]);
        const w = await wRes.json();
        const a = await aRes.json();
        const c = w.current;
        setData({
          temp: Math.round(c.temperature_2m),
          feelsLike: Math.round(c.apparent_temperature),
          condition: WMO[c.weather_code] ?? "UNKNOWN",
          humidity: c.relative_humidity_2m,
          windSpeed: Math.round(c.wind_speed_10m),
          aqi: Math.round(a.current?.us_aqi ?? 0),
        });
        setStatus("ok");
      } catch {
        setStatus("error");
      }
    }

    fetchWeather();
    const t = setInterval(fetchWeather, 10 * 60 * 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <>
      <div className="w-header">
        <span className="w-label">WEATHER</span>
        <span className="w-sublabel">{LOCATION_LABEL}</span>
      </div>

      {status === "error" && <span className="w-error">SENSOR OFFLINE</span>}
      {status === "loading" && !data && <span className="w-loading">SCANNING…</span>}

      {data && (
        <>
          <div className="w-row w-row--bottom">
            <span className="w-temp">{data.temp}°</span>
            <div className="w-cond-block">
              <span className="w-cond">{data.condition}</span>
              <span className="w-feels">FEELS {data.feelsLike}°</span>
            </div>
          </div>

          <div className="w-divider" />

          <div className="w-row w-row--spread">
            <div className="w-stat">
              <span className="w-stat-lbl">HUMID</span>
              <span className="w-stat-val">{data.humidity}%</span>
            </div>
            <div className="w-stat">
              <span className="w-stat-lbl">WIND</span>
              <span className="w-stat-val">{data.windSpeed} MPH</span>
            </div>
          </div>

          <div className="w-divider" />

          {(() => {
            const { label, color } = aqiInfo(data.aqi);
            return (
              <div className="w-row w-row--spread">
                <span className="w-stat-lbl">AIR QUALITY</span>
                <span className="w-aqi" style={{ color }}>
                  {data.aqi} — {label}
                </span>
              </div>
            );
          })()}
        </>
      )}
    </>
  );
}
