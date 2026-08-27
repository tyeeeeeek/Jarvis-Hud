import { useEffect, useState, useCallback } from "react";

const API = "http://localhost:8766";

declare global {
  interface Window {
    Plaid?: {
      create: (config: {
        token: string;
        onSuccess: (public_token: string) => void;
        onExit?: () => void;
      }) => { open: () => void };
    };
  }
}

interface Summary {
  period_days: number;
  total_spent: number;
  by_category: { name: string; amount: number }[];
  top_merchants: { name: string; amount: number }[];
}

type Source = "statements" | "plaid";
type Status = "loading" | "not_configured" | "not_linked" | "linking" | "linked" | "syncing" | "error";

export function FinanceWidget() {
  const [status, setStatus] = useState<Status>("loading");
  const [source, setSource] = useState<Source | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);

  const refresh = useCallback(async () => {
    try {
      const stRes = await fetch(`${API}/statements/summary`);
      if (stRes.ok) {
        setSummary(await stRes.json());
        setSource("statements");
        setStatus("linked");
        return;
      }
    } catch { /* fall through to Plaid check */ }

    try {
      const plRes = await fetch(`${API}/plaid/status`);
      const plData = await plRes.json();
      if (!plData.configured) { setStatus("not_configured"); return; }
      if (!plData.linked) { setStatus("not_linked"); return; }
      const sumRes = await fetch(`${API}/plaid/summary?days=30`);
      if (sumRes.ok) {
        setSummary(await sumRes.json());
        setSource("plaid");
        setStatus("linked");
      } else {
        setStatus("linked");
      }
    } catch {
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10 * 60 * 1000);
    return () => clearInterval(t);
  }, [refresh]);

  const syncStatements = async () => {
    setStatus("syncing");
    try {
      await fetch(`${API}/statements/sync`, { method: "POST" });
    } catch { /* ignore, refresh() below will surface any real problem */ }
    await refresh();
  };

  const connectPlaid = async () => {
    setStatus("linking");
    try {
      const res = await fetch(`${API}/plaid/link_token`);
      const data = await res.json();
      if (!data.link_token || !window.Plaid) { setStatus("error"); return; }
      const handler = window.Plaid.create({
        token: data.link_token,
        onSuccess: async (public_token: string) => {
          await fetch(`${API}/plaid/exchange`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ public_token }),
          });
          refresh();
        },
        onExit: () => refresh(),
      });
      handler.open();
    } catch {
      setStatus("error");
    }
  };

  const disconnect = async () => {
    await fetch(`${API}/plaid/unlink`, { method: "POST" });
    setSummary(null);
    setSource(null);
    refresh();
  };

  return (
    <>
      <div className="w-header">
        <span className="w-label">FINANCE</span>
        <span className="w-sublabel">
          {source === "statements" ? "LOCAL STATEMENTS" : source === "plaid" ? "PLAID (SANDBOX)" : "LAST 30 DAYS"}
        </span>
      </div>

      {status === "loading" && <span className="w-loading">SCANNING…</span>}
      {status === "error" && <span className="w-error">SENSOR OFFLINE</span>}
      {status === "not_configured" && (
        <>
          <span className="w-idle">DROP CSVs IN JARVISSTATEMENTS FOLDER</span>
          <div className="w-divider" />
          <button className="w-fin-connect" onClick={syncStatements}>SYNC STATEMENTS</button>
        </>
      )}

      {(status === "not_linked") && (
        <>
          <button className="w-fin-connect" onClick={syncStatements}>SYNC STATEMENTS</button>
          <div className="w-divider" />
          <button className="w-fin-connect" onClick={connectPlaid}>OR CONNECT BANK (PLAID)</button>
        </>
      )}

      {(status === "linking" || status === "syncing") && (
        <span className="w-loading">{status === "linking" ? "OPENING PLAID…" : "SYNCING…"}</span>
      )}

      {status === "linked" && summary && (
        <>
          <div className="w-row w-row--bottom">
            <span className="w-temp">${summary.total_spent.toFixed(0)}</span>
            <div className="w-cond-block">
              <span className="w-cond">TOTAL SPENT</span>
              <span className="w-feels">{summary.by_category.length} CATEGORIES</span>
            </div>
          </div>

          <div className="w-divider" />

          {summary.by_category.slice(0, 5).map((c) => (
            <div className="w-row w-row--spread" key={c.name}>
              <span className="w-stat-lbl">{c.name.toUpperCase()}</span>
              <span className="w-stat-val">${c.amount.toFixed(0)}</span>
            </div>
          ))}

          <div className="w-divider" />
          <div className="w-row w-row--spread">
            {source === "statements" ? (
              <button className="w-fin-disconnect" onClick={syncStatements}>SYNC AGAIN</button>
            ) : (
              <button className="w-fin-disconnect" onClick={disconnect}>DISCONNECT</button>
            )}
          </div>
        </>
      )}

      {status === "linked" && !summary && <span className="w-loading">LOADING TRANSACTIONS…</span>}
    </>
  );
}
