interface CameraFeedWidgetProps {
  frame: string | null; // base64 JPEG (no data: prefix), or null when the phone camera is closed
}

/** Live mirror of the phone HUD's camera (see dashboard/jarvis_hud.html
 * and dashboard_server.py's `register_frame_handler`) -- a plain <img>
 * whose src swaps on every frame the phone posts (~5fps). Read-only:
 * nothing here can open/close the phone's camera, it only shows what's
 * already streaming. `frame` goes back to null the moment the phone
 * closes its camera (an explicit signal, not a guessed timeout), so this
 * never shows a stale, frozen last frame as if it were still live. */
export function CameraFeedWidget({ frame }: CameraFeedWidgetProps) {
  return (
    <div className="w-camerafeed">
      <div className="w-header">
        <span className="w-label">PHONE CAMERA</span>
        <span className={`w-jc-dot ${frame ? "w-jc-dot--on" : "w-jc-dot--off"}`} />
      </div>
      <div className="w-camerafeed-frame">
        {frame ? (
          <>
            <img src={`data:image/jpeg;base64,${frame}`} alt="Live feed from phone camera" />
            <span className="w-camerafeed-live">LIVE</span>
          </>
        ) : (
          <div className="w-idle">Camera inactive on phone -- open /hud and activate it.</div>
        )}
      </div>
    </div>
  );
}
