import { useEffect, useState, useCallback } from "react";

const API = "http://localhost:8766";

interface NasStatus {
  configured: boolean;
  cpu_pct?: number;
  mem_used_pct?: number | null;
  volumes?: { id: string; status: string; used_pct: number | null }[];
  error?: string;
}

interface SpeedResult {
  ts: number;
  download_mbps: number;
  upload_mbps: number;
  ping_ms: number | null;
  server: string;
}

interface NetworkDevice {
  mac: string;
  ip: string;
  first_seen: number;
  new?: boolean;
}

function agoLabel(ts: number): string {
  const mins = (Date.now() / 1000 - ts) / 60;
  if (mins < 1) return "JUST NOW";
  if (mins < 60) return `${Math.round(mins)}M AGO`;
  const hours = mins / 60;
  if (hours < 48) return `${hours.toFixed(1)}H AGO`;
  return `${Math.round(hours / 24)}D AGO`;
}

export function HomelabWidget() {
  const [nas, setNas] = useState<NasStatus | null>(null);
  const [speed, setSpeed] = useState<SpeedResult | null>(null);
  const [speedAvailable, setSpeedAvailable] = useState(true);
  const [devices, setDevices] = useState<NetworkDevice[]>([]);
  const [testingSpeed, setTestingSpeed] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [networkError, setNetworkError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${API}/homelab/nas`);
      if (r.ok) setNas(await r.json());
    } catch { /* NAS section just shows its idle/error state below */ }

    try {
      const r = await fetch(`${API}/homelab/speedtest`);
      if (r.ok) {
        const data = await r.json();
        setSpeedAvailable(!!data.available);
        setSpeed(data.last_result ?? null);
      }
    } catch { /* ignore -- widget shows NO DATA */ }

    try {
      const r = await fetch(`${API}/homelab/network`);
      if (r.ok) {
        const data = await r.json();
        setDevices(data.devices ?? []);
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5 * 60 * 1000);
    return () => clearInterval(t);
  }, [refresh]);

  const runSpeedTest = async () => {
    setTestingSpeed(true);
    try {
      const r = await fetch(`${API}/homelab/speedtest/run`, { method: "POST" });
      const data = await r.json();
      if (r.ok) setSpeed(data);
    } catch { /* ignore -- last known result stays shown */ }
    setTestingSpeed(false);
  };

  const scanNetwork = async () => {
    setScanning(true);
    setNetworkError(null);
    try {
      const r = await fetch(`${API}/homelab/network/scan`, { method: "POST" });
      const data = await r.json();
      if (data.error) setNetworkError(data.error);
      else setDevices(data.devices ?? []);
    } catch {
      setNetworkError("SCAN FAILED");
    }
    setScanning(false);
  };

  const newCount = devices.filter(d => d.new).length;
  const nasVolume = nas?.volumes?.[0];

  return (
    <>
      <div className="w-header">
        <span className="w-label">HOMELAB</span>
        <span className="w-sublabel">NAS · NETWORK</span>
      </div>

      {/* --- NAS --- */}
      {nas?.configured && !nas.error && (
        <div className="w-row w-row--spread">
          <span className="w-stat-lbl">NAS CPU</span>
          <span className="w-stat-val">{nas.cpu_pct?.toFixed(0) ?? "–"}%</span>
        </div>
      )}
      {nas?.configured && !nas.error && nasVolume && (
        <div className="w-row w-row--spread">
          <span className="w-stat-lbl">NAS STORAGE</span>
          <span className="w-stat-val">
            {nasVolume.used_pct !== null ? `${nasVolume.used_pct.toFixed(0)}%` : "–"}
          </span>
        </div>
      )}
      {nas?.configured && nas.error && (
        <span className="w-error">NAS UNREACHABLE</span>
      )}
      {nas && !nas.configured && (
        <span className="w-idle">NAS NOT CONFIGURED — SEE .ENV</span>
      )}

      <div className="w-divider" />

      {/* --- Internet speed --- */}
      <div className="w-row w-row--bottom">
        <span className="w-temp">{speed ? speed.download_mbps.toFixed(0) : "–"}</span>
        <div className="w-cond-block">
          <span className="w-cond">MBPS DOWN</span>
          <span className="w-feels">
            {speed ? `${speed.upload_mbps.toFixed(0)} UP · ${agoLabel(speed.ts)}` : "NO DATA YET"}
          </span>
        </div>
      </div>
      {speedAvailable ? (
        <button className="w-fin-connect" onClick={runSpeedTest} disabled={testingSpeed}>
          {testingSpeed ? "TESTING…" : "RUN SPEED TEST"}
        </button>
      ) : (
        <span className="w-idle">SPEEDTEST-CLI NOT INSTALLED</span>
      )}

      <div className="w-divider" />

      {/* --- Network devices --- */}
      <div className="w-row w-row--spread">
        <span className="w-stat-lbl">DEVICES ON LAN</span>
        <span className="w-stat-val">
          {devices.length}{newCount > 0 && <> · {newCount} NEW</>}
        </span>
      </div>
      {networkError && <span className="w-error">{networkError}</span>}
      <button className="w-fin-disconnect" onClick={scanNetwork} disabled={scanning}>
        {scanning ? "SCANNING…" : "SCAN NOW"}
      </button>
    </>
  );
}
