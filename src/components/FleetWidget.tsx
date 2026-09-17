import { useEffect, useState, useCallback } from "react";

const API = "http://localhost:8766";

interface TailscalePeer {
  name: string;
  ip: string | null;
  os: string;
  online: boolean;
  exit_node_option: boolean;
  is_exit_node: boolean;
  last_seen: string | null;
  key_expiry: string | null;
  relay: string;
}

interface FleetDevice {
  name: string;
  os: string;
  configured: boolean;
  actions: string[];
  tailscale?: TailscalePeer | null;
}

type StatusResult = { ok: true; output: string } | { ok: false; error: string };

const DEVICE_LABELS: Record<string, string> = {
  nas: "NAS",
  pc1: "PC1",
  pc2: "PC2",
  pc3: "PC3",
};

function DetailRow({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <div className="w-row w-row--spread">
      <span className="w-stat-lbl">{label}</span>
      <span className={`w-stat-val ${bad ? "w-stat-val--bad" : ""}`}>{value}</span>
    </div>
  );
}

// Tailscale reports a zero-value placeholder ("0001-01-01T...") rather
// than omitting the field when it has no real last-seen/expiry data for a
// peer yet -- treat that the same as null instead of printing "56000
// years ago".
function agoLabel(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t) || t < 0) return null;
  const mins = (Date.now() - t) / 60000;
  if (mins < 0) return null;
  if (mins < 1) return "JUST NOW";
  if (mins < 60) return `${Math.round(mins)}M AGO`;
  const hours = mins / 60;
  if (hours < 48) return `${hours.toFixed(1)}H AGO`;
  return `${Math.round(hours / 24)}D AGO`;
}

function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return null;
  const days = (t - Date.now()) / 86400000;
  return Number.isFinite(days) ? Math.round(days) : null;
}

// Fleet devices reachable over the tailnet -- not just the online/offline
// dot HomelabWidget/homepage already show (that's tailscale_service, pure
// status), but actual curated remote actions via fleet_service's OpenSSH
// bridge (Tailscale's own SSH server doesn't run under Synology's
// packaging, so even the "nas" slot uses OpenSSH-over-Tailscale like the
// Windows slots). See fleet_service.py's module docstring for what "not
// configured yet" means per device and how to fix it.
export function FleetWidget() {
  const [devices, setDevices] = useState<FleetDevice[]>([]);
  const [statusByDevice, setStatusByDevice] = useState<Record<string, StatusResult | "loading" | null>>({});
  const [busyDevice, setBusyDevice] = useState<string | null>(null);
  const [actionNote, setActionNote] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${API}/homelab/fleet`);
      if (r.ok) {
        const data = await r.json();
        setDevices(data.devices ?? []);
      }
    } catch { /* widget just shows NO DATA below */ }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const checkStatus = async (device: string) => {
    setStatusByDevice(prev => ({ ...prev, [device]: "loading" }));
    let result: StatusResult;
    try {
      const r = await fetch(`${API}/homelab/fleet/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device }),
      });
      const data = await r.json();
      result = r.ok ? { ok: true, output: data.output } : { ok: false, error: data.error ?? "request failed" };
    } catch {
      result = { ok: false, error: "request failed" };
    }
    setStatusByDevice(prev => ({ ...prev, [device]: result }));
  };

  const runFleetAction = async (device: string, path: "restart" | "cancel_restart") => {
    setBusyDevice(device);
    setActionNote(prev => ({ ...prev, [device]: "" }));
    try {
      const r = await fetch(`${API}/homelab/fleet/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device }),
      });
      const data = await r.json();
      setActionNote(prev => ({ ...prev, [device]: data.result ?? "" }));
    } catch {
      setActionNote(prev => ({ ...prev, [device]: "Request failed." }));
    }
    setBusyDevice(null);
  };

  return (
    <>
      <div className="w-header">
        <span className="w-label">FLEET</span>
        <span className="w-sublabel">TAILNET DEVICE CONTROL</span>
      </div>

      {devices.length === 0 && <span className="w-idle">NO DATA YET</span>}

      {devices.map(d => {
        const status = statusByDevice[d.name];
        return (
          <div key={d.name} className="w-rack-detail">
            <div className="w-rack-detail__title">{DEVICE_LABELS[d.name] ?? d.name.toUpperCase()}</div>

            <DetailRow
              label={`${d.os.toUpperCase()} · OPENSSH`}
              value={d.configured ? "READY" : "NOT CONFIGURED"}
              bad={!d.configured}
            />

            {d.tailscale && (
              <>
                <DetailRow
                  label="TAILSCALE"
                  value={d.tailscale.online ? "ONLINE" : `OFFLINE${agoLabel(d.tailscale.last_seen) ? " · LAST SEEN " + agoLabel(d.tailscale.last_seen) : ""}`}
                  bad={!d.tailscale.online}
                />
                <DetailRow label="TAILSCALE IP" value={d.tailscale.ip ?? "–"} />
                <DetailRow
                  label="CONNECTION"
                  value={d.tailscale.online ? (d.tailscale.relay ? `RELAYED (${d.tailscale.relay.toUpperCase()})` : "DIRECT") : "–"}
                />
                {(() => {
                  const days = daysUntil(d.tailscale.key_expiry);
                  return days !== null ? (
                    <DetailRow label="KEY EXPIRES" value={days <= 0 ? "EXPIRED" : `IN ${days}D`} bad={days <= 14} />
                  ) : null;
                })()}
              </>
            )}

            <div className="w-row">
              <button
                className="w-rack-detail__scan-btn"
                onClick={() => checkStatus(d.name)}
                disabled={status === "loading" || !d.configured}
              >
                {status === "loading" ? "CHECKING…" : "CHECK STATUS"}
              </button>
              {d.actions.includes("restart") && (
                <button
                  className="w-rack-detail__scan-btn"
                  onClick={() => runFleetAction(d.name, "restart")}
                  disabled={busyDevice === d.name || !d.configured}
                >
                  {busyDevice === d.name ? "ASKING…" : "RESTART"}
                </button>
              )}
              {d.actions.includes("cancel_restart") && (
                <button
                  className="w-rack-detail__scan-btn"
                  onClick={() => runFleetAction(d.name, "cancel_restart")}
                  disabled={busyDevice === d.name || !d.configured}
                >
                  CANCEL
                </button>
              )}
            </div>

            {status && status !== "loading" && (
              status.ok
                ? <span className="w-idle">{status.output}</span>
                : <span className="w-error">{status.error}</span>
            )}
            {actionNote[d.name] && <span className="w-idle">{actionNote[d.name]}</span>}

            {!d.configured && (
              <span className="w-idle">
                Set up in .env / on-device -- see fleet_service.py's module docstring for steps.
              </span>
            )}
          </div>
        );
      })}
    </>
  );
}
