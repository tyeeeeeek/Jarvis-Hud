import { useEffect, useState } from "react";

// ─── Edit these to your location ──────────────────────────────────────────
const LAT = 34.0522;
const LON = -118.2437;
const LOCATION_LABEL = "LOS ANGELES, CA";

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

interface HourPoint { time: string; temp: number; condition: string; precipProb: number | null }
interface DayPoint { label: string; condition: string; hi: number; lo: number; precipProb: number | null }

interface WeatherData {
  temp: number; feelsLike: number; condition: string;
  humidity: number; windSpeed: number; aqi: number;
  windDir: number | null; windGust: number | null;
  pressure: number | null; uvIndex: number | null; dewPoint: number | null;
  sunrise: string | null; sunset: string | null;
  hourly: HourPoint[]; daily: DayPoint[];
}

function windDirLabel(deg: number | null): string {
  if (deg === null) return "–";
  const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  return dirs[Math.round(deg / 45) % 8];
}
function hourLabel(iso: string): string {
  const d = new Date(iso);
  const h = d.getHours();
  return `${h % 12 === 0 ? 12 : h % 12}${h < 12 ? "A" : "P"}`;
}
function dayLabel(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-US", { weekday: "short" }).toUpperCase().slice(0, 3);
}
function clockLabel(iso: string | null): string {
  if (!iso) return "–";
  const d = new Date(iso);
  let h = d.getHours();
  const m = d.getMinutes();
  const ap = h < 12 ? "AM" : "PM";
  h = h % 12 || 12;
  return `${h}:${m.toString().padStart(2, "0")}${ap}`;
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
            `&current=temperature_2m,weather_code,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,surface_pressure,uv_index,dew_point_2m` +
            `&hourly=temperature_2m,weather_code,precipitation_probability` +
            `&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset` +
            `&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto&forecast_days=7`
          ),
          fetch(
            `https://air-quality-api.open-meteo.com/v1/air-quality` +
            `?latitude=${LAT}&longitude=${LON}&current=us_aqi`
          ),
        ]);
        const w = await wRes.json();
        const a = await aRes.json();
        const c = w.current;

        const nowIdx = (w.hourly?.time ?? []).findIndex((t: string) => new Date(t) >= new Date());
        const startIdx = nowIdx >= 0 ? nowIdx : 0;
        const hourly: HourPoint[] = (w.hourly?.time ?? []).slice(startIdx, startIdx + 8).map((t: string, i: number) => ({
          time: hourLabel(t),
          temp: Math.round(w.hourly.temperature_2m[startIdx + i]),
          condition: WMO[w.hourly.weather_code[startIdx + i]] ?? "–",
          precipProb: w.hourly.precipitation_probability?.[startIdx + i] ?? null,
        }));
        const daily: DayPoint[] = (w.daily?.time ?? []).slice(0, 6).map((t: string, i: number) => ({
          label: i === 0 ? "TODAY" : dayLabel(t),
          condition: WMO[w.daily.weather_code[i]] ?? "–",
          hi: Math.round(w.daily.temperature_2m_max[i]),
          lo: Math.round(w.daily.temperature_2m_min[i]),
          precipProb: w.daily.precipitation_probability_max?.[i] ?? null,
        }));

        setData({
          temp: Math.round(c.temperature_2m),
          feelsLike: Math.round(c.apparent_temperature),
          condition: WMO[c.weather_code] ?? "UNKNOWN",
          humidity: c.relative_humidity_2m,
          windSpeed: Math.round(c.wind_speed_10m),
          aqi: Math.round(a.current?.us_aqi ?? 0),
          windDir: c.wind_direction_10m ?? null,
          windGust: c.wind_gusts_10m !== undefined ? Math.round(c.wind_gusts_10m) : null,
          pressure: c.surface_pressure ?? null,
          uvIndex: c.uv_index ?? null,
          dewPoint: c.dew_point_2m !== undefined ? Math.round(c.dew_point_2m) : null,
          sunrise: w.daily?.sunrise?.[0] ?? null,
          sunset: w.daily?.sunset?.[0] ?? null,
          hourly,
          daily,
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
              <span className="w-stat-val">{data.windSpeed} MPH {windDirLabel(data.windDir)}</span>
            </div>
            <div className="w-stat">
              <span className="w-stat-lbl">GUSTS</span>
              <span className="w-stat-val">{data.windGust !== null ? `${data.windGust} MPH` : "–"}</span>
            </div>
            <div className="w-stat">
              <span className="w-stat-lbl">DEW PT</span>
              <span className="w-stat-val">{data.dewPoint !== null ? `${data.dewPoint}°` : "–"}</span>
            </div>
          </div>

          <div className="w-divider" />

          <div className="w-row w-row--spread">
            <div className="w-stat">
              <span className="w-stat-lbl">PRESSURE</span>
              <span className="w-stat-val">{data.pressure !== null ? `${Math.round(data.pressure)} HPA` : "–"}</span>
            </div>
            <div className="w-stat">
              <span className="w-stat-lbl">UV INDEX</span>
              <span className="w-stat-val">{data.uvIndex !== null ? data.uvIndex.toFixed(0) : "–"}</span>
            </div>
            <div className="w-stat">
              <span className="w-stat-lbl">SUNRISE</span>
              <span className="w-stat-val">{clockLabel(data.sunrise)}</span>
            </div>
            <div className="w-stat">
              <span className="w-stat-lbl">SUNSET</span>
              <span className="w-stat-val">{clockLabel(data.sunset)}</span>
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

          {data.hourly.length > 0 && (
            <>
              <div className="w-divider" />
              <span className="w-sublabel">NEXT HOURS</span>
              <div className="w-weather-hourly">
                {data.hourly.map((h, i) => (
                  <div key={i} className="w-weather-hourly__col">
                    <span className="w-stat-lbl">{h.time}</span>
                    <span className="w-weather-hourly__temp">{h.temp}°</span>
                    {h.precipProb !== null && h.precipProb > 0 && (
                      <span className="w-weather-hourly__precip">{h.precipProb}%</span>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}

          {data.daily.length > 0 && (
            <>
              <div className="w-divider" />
              <span className="w-sublabel">6-DAY OUTLOOK</span>
              <div className="w-weather-daily">
                {data.daily.map((d, i) => (
                  <div key={i} className="w-weather-daily__row">
                    <span className="w-weather-daily__label">{d.label}</span>
                    <span className="w-weather-daily__cond">{d.condition}</span>
                    {d.precipProb !== null && d.precipProb > 0 && (
                      <span className="w-weather-daily__precip">{d.precipProb}%</span>
                    )}
                    <span className="w-weather-daily__temps">
                      <span className="w-weather-daily__hi">{d.hi}°</span>
                      <span className="w-weather-daily__lo">{d.lo}°</span>
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </>
  );
}
