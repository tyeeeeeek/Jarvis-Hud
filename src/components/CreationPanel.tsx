export interface Creation {
  id: number;
  title: string;
  kind: string;
  html: string;
}

interface CreationPanelProps {
  creation: Creation;
  offset: number;
  onClose: (id: number) => void;
}

// Renders something Jarvis just built. The iframe is deliberately sandboxed
// to allow-scripts ONLY -- no allow-same-origin, no allow-top-navigation, no
// allow-popups -- so generated HTML/JS can run visually but can never reach
// Electron/preload APIs, cookies, or navigate the real app.
export function CreationPanel({ creation, offset, onClose }: CreationPanelProps) {
  const openInBrowser = () => {
    try {
      const blob = new Blob([creation.html], { type: "text/html" });
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
    } catch (e) {
      console.error("Could not open creation in browser:", e);
    }
  };

  return (
    <div
      className="creation-panel"
      style={{ transform: `translate(calc(-50% + ${offset * 22}px), ${offset * 22}px)` }}
    >
      <div className="creation-panel__bar">
        <span className="creation-panel__title">
          {creation.kind === "webpage" ? "WEBSITE" : "CREATION"} · {creation.title}
        </span>
        <div className="creation-panel__actions">
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
