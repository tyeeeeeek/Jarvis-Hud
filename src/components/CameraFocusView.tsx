interface CameraFocusViewProps {
  frame: string; // base64 JPEG, no data: prefix -- caller only renders this when a frame exists
  /** True for the one render where the caller has already flagged this as
   * dismissed but is keeping it mounted a beat longer so the close
   * animation (mirroring cameraFocusIn) can actually play. */
  closing?: boolean;
  onMinimize: () => void;
}

/** Full-screen takeover when the phone's camera is live -- the "flow into
 * a full-screen view" ArcReactor.tsx switches to the moment cameraFrame
 * arrives. Deliberately minimal (just the feed + a small ring badge),
 * matching the reference look this HUD was redesigned around -- no scan
 * lines, corner brackets, or caption chrome layered on top. The ring
 * badge (same look as the idle-state ring, just small) sits in the
 * corner and doubles as the minimize control. */
export function CameraFocusView({ frame, closing, onMinimize }: CameraFocusViewProps) {
  return (
    <div className={`camera-focus ${closing ? "camera-focus--closing" : ""}`}>
      <img className="camera-focus__img" src={`data:image/jpeg;base64,${frame}`} alt="Live feed from phone camera" />
      <div className="camera-focus__vignette" />
      <button className="camera-focus__badge" onClick={closing ? undefined : onMinimize} title="Back to dashboard">
        <span className="camera-focus__badge-ring">
          <span className="camera-focus__badge-text">JARVIS</span>
        </span>
      </button>
    </div>
  );
}
