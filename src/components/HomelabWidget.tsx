import { useEffect, useState, useCallback } from "react";
import { HomelabRack, type RackPart } from "./HomelabRack";

interface IpInfo {
  ip: string;
  org: string;
  city: string;
  region: string;
  country: string;
  asn: string;
}

const API = "http://localhost:8766";

interface NasDisk { id: string; model: string | null; status: string; temp_c: number | null; smart_status: string | null }
interface NasNetIface { device: string; rx_bps: number; tx_bps: number }

interface NasStatus {
  configured: boolean;
  cpu_pct?: number;
  mem_used_pct?: number | null;
  mem_total_mb?: number | null;
  volumes?: { id: string; status: string; used_pct: number | null }[];
  disks?: NasDisk[];
  network?: NasNetIface[];
  model?: string | null;
  dsm_version?: string | null;
  serial?: string | null;
  temp_c?: number | null;
  uptime?: string | number | null;
  error?: string;
}

interface HealthStatus {
  thermal_state?: "ok" | "warning" | "critical" | "unknown";
  disk_critical?: boolean;
  critical?: boolean;
  free_gb?: number;
  hottest_c?: number | null;
}

interface PortResult { port: number; proto: string; service: string }
interface ProbeResult { reachable: boolean; port?: number; server?: string | null; title?: string | null }
interface DeviceInspection { hostname: string | null; ports: PortResult[]; probe: ProbeResult }

function mbps(bps: number): string {
  return `${(bps * 8 / 1_000_000).toFixed(1)} MBPS`;
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

const RACK_PART_LABELS: Record<RackPart, string> = {
  router: "ACCESS POINT / ROUTER",
  switch: "NETWORK SWITCH",
  drives: "OPEN-RAIL DRIVES",
  nas: "NAS",
  pdu: "POWER STRIP / PDU",
};

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="w-row w-row--spread">
      <span className="w-stat-lbl">{label}</span>
      <span className="w-stat-val">{value}</span>
    </div>
  );
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
  const [selectedPart, setSelectedPart] = useState<RackPart | null>(null);
  const [ipInfo, setIpInfo] = useState<IpInfo | null>(null);
  const [ipLoading, setIpLoading] = useState(false);
  const [ipError, setIpError] = useState(false);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [deviceInfo, setDeviceInfo] = useState<Record<string, DeviceInspection | "loading" | "error">>({});
  const [gateway, setGateway] = useState<string | null>(null);
  const [gatewayProbe, setGatewayProbe] = useState<ProbeResult | "loading" | null>(null);

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

    try {
      const r = await fetch(`${API}/homelab/health`);
      if (r.ok) setHealth(await r.json());
    } catch { /* ignore -- JARVIS HOST section just shows NO DATA */ }
  }, []);

  // Combines a port scan + reverse-DNS hostname lookup + HTTP banner probe
  // into one "INSPECT" action per device -- three separate on-demand
  // backend calls (see network_watch.py), fired in parallel, never run
  // automatically for every device on a scan.
  const inspectDevice = async (ip: string) => {
    setDeviceInfo(prev => ({ ...prev, [ip]: "loading" }));
    try {
      const [portsRes, hostRes, probeRes] = await Promise.all([
        fetch(`${API}/homelab/network/ports?ip=${encodeURIComponent(ip)}`).then(r => r.json()),
        fetch(`${API}/homelab/network/hostname?ip=${encodeURIComponent(ip)}`).then(r => r.json()),
        fetch(`${API}/homelab/network/probe?ip=${encodeURIComponent(ip)}`).then(r => r.json()),
      ]);
      if (portsRes.error) throw new Error(portsRes.error);
      setDeviceInfo(prev => ({
        ...prev,
        [ip]: { hostname: hostRes.hostname ?? null, ports: portsRes.ports ?? [], probe: probeRes },
      }));
    } catch {
      setDeviceInfo(prev => ({ ...prev, [ip]: "error" }));
    }
  };

  const probeGateway = async () => {
    if (!gateway) return;
    setGatewayProbe("loading");
    try {
      const r = await fetch(`${API}/homelab/network/probe?ip=${encodeURIComponent(gateway)}`);
      setGatewayProbe(await r.json());
    } catch {
      setGatewayProbe({ reachable: false });
    }
  };

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

  const selectPart = (part: RackPart) => setSelectedPart(prev => (prev === part ? null : part));

  // Public-IP/ISP lookup for the "router" rack part -- free, keyless,
  // fetched once and cached for the session rather than on every click.
  useEffect(() => {
    if (selectedPart !== "router" || ipInfo || ipLoading || ipError) return;
    setIpLoading(true);
    fetch("https://ipapi.co/json/")
      .then(r => r.json())
      .then(d => {
        if (d?.error) throw new Error(d.reason || "lookup failed");
        setIpInfo({
          ip: d.ip ?? "–",
          org: d.org ?? "–",
          city: d.city ?? "–",
          region: d.region ?? "–",
          country: d.country_name ?? "–",
          asn: d.asn ?? "–",
        });
      })
      .catch(() => setIpError(true))
      .finally(() => setIpLoading(false));
  }, [selectedPart, ipInfo, ipLoading, ipError]);

  // This machine's real default-route gateway IP -- the actual router/AP
  // hardware's LAN address, not a guess. Fetched once, same lazy-on-select
  // pattern as the public-IP lookup above.
  useEffect(() => {
    if (selectedPart !== "router" || gateway !== null) return;
    fetch(`${API}/homelab/network/gateway`)
      .then(r => r.json())
      .then(d => setGateway(d.gateway ?? ""))
      .catch(() => setGateway(""));
  }, [selectedPart, gateway]);

  const newCount = devices.filter(d => d.new).length;
  const nasVolume = nas?.volumes?.[0];

  return (
    <>
      <div className="w-header">
        <span className="w-label">HOMELAB</span>
        <span className="w-sublabel">NAS · NETWORK</span>
      </div>

      {nas && !nas.configured && (
        <span className="w-idle">NAS NOT CONFIGURED — SEE .ENV</span>
      )}
      {nas?.configured && nas.error && (
        <span className="w-error">NAS UNREACHABLE</span>
      )}

      <div className="w-rack-layout">
        <HomelabRack
          nasOk={!!nas?.configured && !nas.error}
          nasConfigured={!!nas?.configured}
          cpuPct={nas?.configured && !nas.error ? nas.cpu_pct ?? null : null}
          storagePct={nas?.configured && !nas.error ? nasVolume?.used_pct ?? null : null}
          downMbps={speed?.download_mbps ?? null}
          upMbps={speed?.upload_mbps ?? null}
          deviceCount={devices.length}
          selected={selectedPart}
          onSelect={selectPart}
        />

      {selectedPart && (
        <div className="w-rack-detail">
          <div className="w-rack-detail__title">{RACK_PART_LABELS[selectedPart]}</div>

          {selectedPart === "router" && (
            <>
              <DetailRow label="GATEWAY (LAN)" value={gateway || "–"} />
              {gateway && (
                <div className="w-row">
                  <button className="w-rack-detail__scan-btn" onClick={probeGateway} disabled={gatewayProbe === "loading"}>
                    {gatewayProbe === "loading" ? "PROBING…" : "PROBE WEB UI"}
                  </button>
                </div>
              )}
              {gatewayProbe && gatewayProbe !== "loading" && (
                gatewayProbe.reachable
                  ? (
                    <>
                      <DetailRow label="WEB UI" value={`REACHABLE ON PORT ${gatewayProbe.port}`} />
                      {gatewayProbe.server && <DetailRow label="SERVER HEADER" value={gatewayProbe.server} />}
                      {gatewayProbe.title && <DetailRow label="PAGE TITLE" value={gatewayProbe.title} />}
                    </>
                  )
                  : <span className="w-idle">NO WEB UI RESPONDING ON 80/443</span>
              )}
              <div className="w-divider" />
              {ipLoading && <span className="w-loading">LOOKING UP ISP…</span>}
              {ipError && <span className="w-error">LOOKUP FAILED</span>}
              {ipInfo && (
                <>
                  <DetailRow label="PUBLIC IP" value={ipInfo.ip} />
                  <DetailRow label="ISP / ORG" value={ipInfo.org} />
                  <DetailRow label="LOCATION" value={`${ipInfo.city}, ${ipInfo.region}, ${ipInfo.country}`} />
                  <DetailRow label="ASN" value={ipInfo.asn} />
                </>
              )}
              <DetailRow
                label="DOWN / UP"
                value={speed ? `${speed.download_mbps.toFixed(0)} / ${speed.upload_mbps.toFixed(0)} MBPS` : "NO DATA YET"}
              />
              <span className="w-idle">Wireless access point, mounted above the rack — tell Jarvis its make/model to have it remembered.</span>
            </>
          )}

          {selectedPart === "switch" && (
            <>
              <DetailRow label="MODEL" value="NETGEAR ProSAFE GS116" />
              <DetailRow label="TYPE" value="16-PORT UNMANAGED GIGABIT" />
              <DetailRow label="LAN DEVICES" value={`${devices.length}`} />
              <span className="w-idle">No per-port telemetry on the switch itself — it's unmanaged. Scan a device below to see what's actually open on it.</span>
              {devices.length > 0 && (
                <div className="w-rack-detail__list">
                  {devices.map(d => {
                    const info = deviceInfo[d.ip];
                    return (
                      <div key={d.mac} className="w-rack-detail__device">
                        <div className="w-rack-detail__row">
                          <span>{d.ip}</span><span>{d.mac}</span><span>{agoLabel(d.first_seen)}</span>
                          <button
                            className="w-rack-detail__scan-btn"
                            onClick={() => inspectDevice(d.ip)}
                            disabled={info === "loading"}
                          >
                            {info === "loading" ? "INSPECTING…" : "INSPECT"}
                          </button>
                        </div>
                        {info === "error" && <span className="w-error">INSPECT FAILED</span>}
                        {info && info !== "loading" && info !== "error" && (
                          <>
                            {info.hostname && <div className="w-rack-detail__hostname">{info.hostname}</div>}
                            {info.probe.reachable && (
                              <div className="w-rack-detail__hostname">
                                WEB UI: {info.probe.server || info.probe.title || `PORT ${info.probe.port}`}
                              </div>
                            )}
                            <div className="w-rack-detail__ports">
                              {info.ports.length === 0
                                ? <span className="w-idle">NO OPEN PORTS FOUND (TOP 100)</span>
                                : info.ports.map(p => (
                                  <span key={p.port} className="w-rack-detail__port-chip">
                                    {p.port}/{p.proto} {p.service.toUpperCase()}
                                  </span>
                                ))}
                            </div>
                          </>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}

          {selectedPart === "drives" && (
            <>
              <DetailRow label="QTY" value="2" />
              <DetailRow label="MOUNT" value="OPEN RAIL, NOT IN NAS BAYS" />
              <span className="w-idle">No SMART/health telemetry available for bare, unmounted drives.</span>
            </>
          )}

          {selectedPart === "nas" && (
            <>
              <DetailRow label="STATUS" value={!nas?.configured ? "NOT CONFIGURED" : nas.error ? "UNREACHABLE" : "ONLINE"} />
              {nas?.configured && !nas.error && (
                <>
                  <DetailRow label="CPU" value={`${nas.cpu_pct?.toFixed(0) ?? "–"}%`} />
                  <DetailRow
                    label="MEMORY"
                    value={
                      nas.mem_used_pct !== null && nas.mem_used_pct !== undefined
                        ? `${nas.mem_used_pct.toFixed(0)}%${nas.mem_total_mb ? ` OF ${(nas.mem_total_mb / 1024).toFixed(1)}GB` : ""}`
                        : "–"
                    }
                  />
                  <DetailRow label="MODEL" value={nas.model || "–"} />
                  <DetailRow label="DSM VERSION" value={nas.dsm_version || "–"} />
                  <DetailRow label="SERIAL" value={nas.serial || "–"} />
                  <DetailRow label="TEMP" value={nas.temp_c !== null && nas.temp_c !== undefined ? `${nas.temp_c}°C` : "–"} />
                  <DetailRow label="UPTIME" value={nas.uptime !== null && nas.uptime !== undefined ? String(nas.uptime) : "–"} />

                  {(nas.volumes ?? []).length > 0 && (
                    <>
                      <div className="w-divider" />
                      <span className="w-sublabel">VOLUMES</span>
                      {(nas.volumes ?? []).map(v => (
                        <DetailRow
                          key={v.id}
                          label={`VOLUME ${v.id}`}
                          value={`${v.status.toUpperCase()} · ${v.used_pct !== null ? v.used_pct.toFixed(0) + "%" : "–"} USED`}
                        />
                      ))}
                    </>
                  )}

                  {(nas.disks ?? []).length > 0 && (
                    <>
                      <div className="w-divider" />
                      <span className="w-sublabel">PHYSICAL DISKS</span>
                      {(nas.disks ?? []).map(d => (
                        <DetailRow
                          key={d.id}
                          label={`${d.id.toUpperCase()}${d.model ? ` · ${d.model}` : ""}`}
                          value={`${d.status.toUpperCase()}${d.temp_c !== null ? ` · ${d.temp_c}°C` : ""}${d.smart_status ? ` · SMART ${d.smart_status.toUpperCase()}` : ""}`}
                        />
                      ))}
                    </>
                  )}

                  {(nas.network ?? []).length > 0 && (
                    <>
                      <div className="w-divider" />
                      <span className="w-sublabel">NETWORK INTERFACES</span>
                      {(nas.network ?? []).map(n => (
                        <DetailRow key={n.device} label={n.device.toUpperCase()} value={`${mbps(n.rx_bps)} DOWN · ${mbps(n.tx_bps)} UP`} />
                      ))}
                    </>
                  )}
                </>
              )}
            </>
          )}

          {selectedPart === "pdu" && (
            <>
              <DetailRow label="TYPE" value="8-OUTLET SWITCHED POWER STRIP" />
              <span className="w-idle">No live power-draw/load telemetry available for this unit.</span>
            </>
          )}
        </div>
      )}
      </div>

      <div className="w-divider" />
      <span className="w-sublabel">JARVIS HOST</span>
      {health ? (
        <div className="w-row w-row--spread">
          <div className="w-stat">
            <span className="w-stat-lbl">THERMAL</span>
            <span className={`w-stat-val ${health.thermal_state === "critical" ? "w-stat-val--bad" : health.thermal_state === "warning" ? "w-stat-val--warn" : ""}`}>
              {health.hottest_c !== null && health.hottest_c !== undefined ? `${health.hottest_c.toFixed(0)}°C` : "–"} · {(health.thermal_state ?? "unknown").toUpperCase()}
            </span>
          </div>
          <div className="w-stat">
            <span className="w-stat-lbl">DISK FREE</span>
            <span className={`w-stat-val ${health.disk_critical ? "w-stat-val--bad" : ""}`}>
              {health.free_gb !== undefined ? `${health.free_gb.toFixed(0)} GB` : "–"}
            </span>
          </div>
        </div>
      ) : (
        <span className="w-idle">NO HEALTH DATA YET — RUNS AUTOMATICALLY EVERY 2H</span>
      )}

      <div className="w-divider" />

      <div className="w-row w-row--spread">
        <span className="w-idle">
          {speed ? `LAST TEST ${agoLabel(speed.ts)}` : "NO SPEED DATA YET"}
          {newCount > 0 && ` · ${newCount} NEW DEVICE${newCount === 1 ? "" : "S"}`}
        </span>
      </div>
      {networkError && <span className="w-error">{networkError}</span>}
      <div className="w-row">
        {speedAvailable ? (
          <button className="w-fin-connect" onClick={runSpeedTest} disabled={testingSpeed}>
            {testingSpeed ? "TESTING…" : "RUN SPEED TEST"}
          </button>
        ) : (
          <span className="w-idle">SPEEDTEST-CLI NOT INSTALLED</span>
        )}
        <button className="w-fin-disconnect" onClick={scanNetwork} disabled={scanning}>
          {scanning ? "SCANNING…" : "SCAN NOW"}
        </button>
      </div>
    </>
  );
}
