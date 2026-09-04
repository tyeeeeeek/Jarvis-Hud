import { useState } from "react";

export interface Creation {
  id: number;
  title: string;
  kind: string;
  html: string;
  /** LAN/Tailscale URL this creation is also reachable at (from a phone or
   * any other device on the tailnet), via dashboard_server.py's
   * /creations/<kind>/<slug>/ route -- undefined if that server isn't
   * running (e.g. Tailscale not connected). */
  url?: string;
}

interface CreationPanelProps {
  creation: Creation;
  onClose: (id: number) => void;
}

// Renders something Jarvis just built, full-window (see HudPage.tsx --
// same "one focused thing at a time" treatment, not a floating card). The
// iframe is deliberately sandboxed to allow-scripts ONLY -- no
// allow-same-origin, no allow-top-navigation, no allow-popups -- so
// generated HTML/JS can run visually but can never reach Electron/preload
// APIs, cookies, or navigate the real app.
export function CreationPanel({ creation, onClose }: CreationPanelProps) {
  const [copied, setCopied] = useState(false);

  const openInBrowser = () => {
    try {
      const blob = new Blob([creation.html], { type: "text/html" });
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
    } catch (e) {
      console.error("Could not open creation in browser:", e);
    }
  };

  const copyPhoneLink = () => {
    if (!creation.url) return;
    navigator.clipboard.writeText(creation.url)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(e => console.error("Could not copy phone link:", e));
  };

  return (
    <div className="creation-panel">
      <div className="creation-panel__bar">
        <span className="creation-panel__title">
          {creation.kind === "webpage" ? "WEBSITE" : "CREATION"} · {creation.title}
        </span>
        <div className="creation-panel__actions">
          {creation.url ? (
            <button
              onClick={copyPhoneLink}
              title={`Copy phone/tailnet link: ${creation.url}`}
            >
              {copied ? "✓" : "📱"}
            </button>
          ) : (
            <button
              className="creation-panel__no-link"
              disabled
              title="No phone/tailnet link available for this creation (phone dashboard isn't reachable)"
            >
              📵
            </button>
          )}
          <button onClick={openInBrowser} title="Open in browser">⤢</button>
          <button onClick={() => onClose(creation.id)} title="Close">✕</button>
        </div>
      </div>
      <div className="creation-panel__scan" />
      <iframe
        className="creation-panel__frame"
        sandbox="allow-scripts"
        srcDoc={creation.html}
        title={creation.title}
      />
    </div>
  );
}
