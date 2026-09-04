import { useRef, useState } from "react";
import { Canvas, useFrame, type ThreeEvent } from "@react-three/fiber";
import { OrbitControls, Edges } from "@react-three/drei";
import type { Mesh } from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";

export type RackPart = "router" | "switch" | "drives" | "nas" | "pdu";

interface HomelabRackProps {
  nasOk: boolean;       // NAS configured and reachable
  nasConfigured: boolean;
  cpuPct: number | null;
  storagePct: number | null;
  downMbps: number | null;
  upMbps: number | null;
  deviceCount: number;
  selected: RackPart | null;
  onSelect: (part: RackPart) => void;
}

const CYAN = "#00f0ff";
const GREEN = "#35f0a0";
const RED = "#ff5a46";
const BODY_COLOR = "#0b2530";

/** A real, solid 3D model of the actual rack (AP on top, switch, open rail
 * space, two bare drives, a 2-bay NAS, power strip at the bottom) -- lit,
 * opaque meshStandardMaterial bodies (not glass/wireframe) with a thin
 * glowing cyan edge trim for accent, so it reads as filled hardware rather
 * than a hologram. Drag to orbit, scroll to zoom, click any part for its
 * detail panel (see HomelabWidget.tsx). The live callout readings and
 * per-part click targets are all real data bound to HomelabWidget's
 * actual polled state -- nothing on this model is invented. */
export function HomelabRack({ nasOk, nasConfigured, cpuPct, storagePct, downMbps, upMbps, deviceCount, selected, onSelect }: HomelabRackProps) {
  const [expanded, setExpanded] = useState(false);
  const [turned, setTurned] = useState(false);
  const controlsRef = useRef<OrbitControlsImpl | null>(null);
  const nasLed = !nasConfigured ? "dim" : nasOk ? "ok" : "err";

  const resetView = () => controlsRef.current?.reset();

  return (
    <div className="w-rack-stage">
      <div className="w-rack-controls">
        <button className="w-rack-chip" onClick={() => setExpanded(v => !v)}>
          {expanded ? "⤡ COLLAPSE" : "⤢ EXPAND"}
        </button>
        {turned && <button className="w-rack-chip" onClick={resetView}>RESET VIEW</button>}
        <span className="w-rack-chip w-rack-chip--hint">DRAG TO ROTATE · SCROLL TO ZOOM · CLICK A PART</span>
      </div>

      <div className={`w-rack__canvas3d ${expanded ? "w-rack__canvas3d--expanded" : ""}`}>
        <Canvas camera={{ position: [3.6, 1.5, 9.5], fov: 30 }} onPointerDown={() => setTurned(true)}>
          <ambientLight intensity={0.55} />
          <directionalLight position={[6, 9, 6]} intensity={1.3} />
          <directionalLight position={[-6, -3, -4]} intensity={0.35} />
          <pointLight position={[0, 1, 7]} intensity={0.7} color={CYAN} />
          <RackScene nasLed={nasLed} selected={selected} onSelect={onSelect} />
          <OrbitControls
            ref={controlsRef}
            enablePan={false}
            minDistance={5.5}
            maxDistance={17}
            autoRotate={!turned}
            autoRotateSpeed={0.7}
            target={[0, 0, 0]}
          />
        </Canvas>
      </div>

      <div className="w-rack__callouts-row">
        <RackCallout label="NAS CPU" value={cpuPct !== null ? `${cpuPct.toFixed(0)}%` : "–"} />
        <RackCallout label="STORAGE" value={storagePct !== null ? `${storagePct.toFixed(0)}%` : "–"} />
        <RackCallout label="LAN" value={`${deviceCount} DEVICE${deviceCount === 1 ? "" : "S"}`} />
        <RackCallout
          label="SPEED"
          value={downMbps !== null ? `${downMbps.toFixed(0)}↓ / ${(upMbps ?? 0).toFixed(0)}↑` : "–"}
        />
      </div>
    </div>
  );
}

function RackCallout({ label, value }: { label: string; value: string }) {
  return (
    <div className="w-rack__callout">
      <span className="w-stat-lbl">{label}</span>
      <span className="w-stat-val">{value}</span>
    </div>
  );
}

// ── 3D scene ────────────────────────────────────────────────────────────

interface SceneProps {
  nasLed: "ok" | "err" | "dim";
  selected: RackPart | null;
  onSelect: (part: RackPart) => void;
}

/** Shared "solid HUD hardware" material props -- a dark opaque body with an
 * emissive cyan tint that brightens on hover/select, so state feedback
 * doesn't rely on transparency at all. */
function bodyProps(isSelected: boolean, hovered: boolean) {
  return {
    color: BODY_COLOR,
    metalness: 0.55,
    roughness: 0.4,
    emissive: isSelected ? "#00c4e6" : hovered ? "#036575" : "#01181e",
    emissiveIntensity: isSelected ? 0.85 : hovered ? 0.5 : 0.3,
  };
}

function RackScene({ nasLed, selected, onSelect }: SceneProps) {
  return (
    <group>
      <RackFrame />

      <EdgeGroup part="router" selected={selected} onSelect={onSelect} position={[0, 4.0, 0]}>
        {(s) => (
          <mesh>
            <cylinderGeometry args={[0.85, 0.85, 1.7, 24]} />
            <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
            <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
          </mesh>
        )}
      </EdgeGroup>

      <EdgeGroup part="switch" selected={selected} onSelect={onSelect} position={[0, 2.35, 0]}>
        {(s) => (
          <>
            <mesh>
              <boxGeometry args={[5.5, 0.7, 1.6]} />
              <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
              <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
            </mesh>
            <Led position={[-2.5, 0, 0.85]} state={nasLed === "err" ? "err" : "ok"} />
            {Array.from({ length: 16 }).map((_, i) => (
              <mesh key={i} position={[-2.15 + i * 0.29, 0, 0.85]}>
                <boxGeometry args={[0.14, 0.3, 0.05]} />
                <meshStandardMaterial color="#04323d" emissive={CYAN} emissiveIntensity={0.4} />
              </mesh>
            ))}
          </>
        )}
      </EdgeGroup>

      <EdgeGroup part="drives" selected={selected} onSelect={onSelect} position={[0, 0.85, 0]}>
        {(s) => (
          <>
            <mesh position={[-0.15, 0.28, 0]}>
              <boxGeometry args={[2.4, 0.55, 1.5]} />
              <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
              <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
            </mesh>
            <mesh position={[0.15, -0.15, 0]}>
              <boxGeometry args={[2.4, 0.55, 1.5]} />
              <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
              <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
            </mesh>
          </>
        )}
      </EdgeGroup>

      <EdgeGroup part="nas" selected={selected} onSelect={onSelect} position={[0, -1.0, 0]}>
        {(s) => (
          <>
            <mesh>
              <boxGeometry args={[5.5, 2.1, 1.6]} />
              <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
              <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
            </mesh>
            <mesh position={[-1.35, 0, 0.85]}>
              <boxGeometry args={[1.9, 1.65, 0.1]} />
              <meshStandardMaterial color="#031318" metalness={0.3} roughness={0.6} />
              <Edges color={CYAN} lineWidth={1} />
            </mesh>
            <mesh position={[1.35, 0, 0.85]}>
              <boxGeometry args={[1.9, 1.65, 0.1]} />
              <meshStandardMaterial color="#031318" metalness={0.3} roughness={0.6} />
              <Edges color={CYAN} lineWidth={1} />
            </mesh>
            <Led position={[2.55, 0.55, 0.85]} state={nasLed} />
            <Led position={[2.55, 0.2, 0.85]} state={nasLed} />
            <Led position={[2.55, -0.15, 0.85]} state={nasLed === "dim" ? "dim" : "ok"} />
          </>
        )}
      </EdgeGroup>

      <EdgeGroup part="pdu" selected={selected} onSelect={onSelect} position={[0, -2.75, 0]}>
        {(s) => (
          <>
            <mesh>
              <boxGeometry args={[4.6, 0.55, 1.4]} />
              <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
              <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
            </mesh>
            <Led position={[-1.95, 0, 0.72]} state="ok" />
            {[-1.1, -0.55, 0, 0.55, 1.1, 1.65].map(x => (
              <mesh key={x} position={[x, 0, 0.72]}>
                <boxGeometry args={[0.32, 0.32, 0.05]} />
                <meshStandardMaterial color="#04323d" emissive={CYAN} emissiveIntensity={0.4} />
              </mesh>
            ))}
          </>
        )}
      </EdgeGroup>
    </group>
  );
}

function RackFrame() {
  return (
    <mesh position={[0, 0.7, 0]}>
      <boxGeometry args={[5.9, 7.4, 2.0]} />
      <meshStandardMaterial color="#061419" metalness={0.4} roughness={0.7} transparent opacity={0.35} />
      <Edges color={CYAN} lineWidth={1} />
    </mesh>
  );
}

interface PartState { isSelected: boolean; hovered: boolean }

/** Every clickable rack component -- a plain group with a click/hover
 * handler; children is a render-prop so the wrapped meshes can react to
 * isSelected/hovered (brighter emissive, thicker edge line) without any
 * geometry-specific logic living outside this wrapper. */
function EdgeGroup({ part, selected, onSelect, position, children }: {
  part: RackPart; selected: RackPart | null; onSelect: (p: RackPart) => void;
  position: [number, number, number]; children: (state: PartState) => React.ReactNode;
}) {
  const [hovered, setHovered] = useState(false);
  const isSelected = selected === part;
  return (
    <group
      position={position}
      onClick={(e: ThreeEvent<MouseEvent>) => { e.stopPropagation(); onSelect(part); }}
      onPointerOver={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); setHovered(true); document.body.style.cursor = "pointer"; }}
      onPointerOut={() => { setHovered(false); document.body.style.cursor = "auto"; }}
      scale={isSelected ? 1.04 : 1}
    >
      {children({ isSelected, hovered })}
    </group>
  );
}

function Led({ position, state }: { position: [number, number, number]; state: "ok" | "err" | "dim" }) {
  const ref = useRef<Mesh>(null);
  useFrame(({ clock }) => {
    if (ref.current && state === "ok") {
      const m = ref.current.material as any;
      m.emissiveIntensity = 1.2 + Math.sin(clock.elapsedTime * 2.5) * 0.6;
    }
  });
  const color = state === "err" ? RED : state === "dim" ? "#0d3a45" : GREEN;
  return (
    <mesh ref={ref} position={position}>
      <sphereGeometry args={[0.09, 12, 12]} />
      <meshStandardMaterial color={color} emissive={color} emissiveIntensity={state === "ok" ? 1.5 : 0.6} />
    </mesh>
  );
}
