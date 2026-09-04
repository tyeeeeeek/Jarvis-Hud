import { useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, Edges } from "@react-three/drei";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import type { Mesh } from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { ExplodablePart, type PartState } from "./Explodable";

export type PcPart = "cpu" | "ram" | "drive";

/** Live readings from jarvis.py's new PC-health watcher thread (see
 * jarvis.py's _pc_health_watcher_thread and the "pc_health" WebSocket
 * message it broadcasts, same _ws_broadcast mechanism every other live
 * value already uses) -- real numbers, not placeholders. Null until the
 * first message arrives after connect. */
export interface PcHealth {
  cpuPct: number;
  memPct: number;
  diskPct: number;
  tempC: number | null;
}

const CYAN = "#00f0ff";
const AMBER = "#f2b23c";
const RED = "#ff5a46";
const BODY_COLOR = "#0b2530";

const PART_LABELS: Record<PcPart, string> = { cpu: "CPU", ram: "MEMORY", drive: "STORAGE (nvme0)" };

const ASSEMBLED: [number, number, number] = [0, 0, 0];
const EXPLODED: Record<PcPart, [number, number, number]> = {
  cpu: [0, 0.9, 0],
  ram: [1.7, 0, 0],
  drive: [0, -1.3, 0],
};

// Load-reactive color -- same "the part itself tells you its state" idea
// as HomelabRack's Led, just continuous instead of a fixed ok/err/dim set.
function loadColor(pct: number): string {
  if (pct >= 85) return RED;
  if (pct >= 60) return AMBER;
  return CYAN;
}

function bodyProps(isSelected: boolean, hovered: boolean, accent: string) {
  return {
    color: BODY_COLOR, metalness: 0.55, roughness: 0.4,
    emissive: isSelected ? accent : hovered ? accent : "#01181e",
    emissiveIntensity: isSelected ? 0.85 : hovered ? 0.5 : 0.3,
  };
}

/** This machine's own CPU/memory/storage, as a real solid disassemblable
 * 3D model -- same construction (Explodable, meshStandardMaterial, cyan
 * Edges) as JarvisCore.tsx, but every reading shown is live and real (see
 * PcHealth above), and each part's own color shifts toward amber/red under
 * load instead of staying a fixed cyan. No discrete GPU on this machine
 * (integrated Intel UHD 620, confirmed via lspci), so CPU/RAM/storage only. */
export function PcInternals({ health }: { health: PcHealth | null }) {
  const [exploded, setExploded] = useState(false);
  const [turned, setTurned] = useState(false);
  const [selected, setSelected] = useState<PcPart | null>(null);
  const onSelect = (part: PcPart) => setSelected(prev => (prev === part ? null : part));
  const controlsRef = useRef<OrbitControlsImpl | null>(null);
  const resetView = () => controlsRef.current?.reset();

  const cpuColor = loadColor(health?.cpuPct ?? 0);
  const ramColor = loadColor(health?.memPct ?? 0);
  const driveColor = loadColor(health?.diskPct ?? 0);

  return (
    <div className="w-rack-stage">
      <div className="w-rack-controls">
        <button className="w-rack-chip" onClick={() => setExploded(v => !v)}>
          {exploded ? "⤢ REASSEMBLE" : "⤡ DISASSEMBLE"}
        </button>
        {turned && <button className="w-rack-chip" onClick={resetView}>RESET VIEW</button>}
        <span className="w-rack-chip w-rack-chip--hint">DRAG TO ROTATE · SCROLL TO ZOOM · CLICK A PART</span>
      </div>

      <div className="w-rack__canvas3d w-rack__canvas3d--expanded">
        <Canvas camera={{ position: [3.6, 1.6, 5.6], fov: 32 }} onPointerDown={() => setTurned(true)}>
          <ambientLight intensity={0.55} />
          <directionalLight position={[6, 9, 6]} intensity={1.3} />
          <directionalLight position={[-6, -3, -4]} intensity={0.35} />
          <pointLight position={[0, 0.5, 4]} intensity={0.7} color={CYAN} />
          <PcScene exploded={exploded} selected={selected} onSelect={onSelect}
            cpuColor={cpuColor} ramColor={ramColor} driveColor={driveColor}
            cpuPct={health?.cpuPct ?? 0} />
          <OrbitControls
            ref={controlsRef}
            enablePan={false}
            minDistance={3.5}
            maxDistance={12}
            autoRotate={!turned}
            autoRotateSpeed={0.6}
            target={[0, 0, 0]}
          />
          <EffectComposer>
            <Bloom intensity={0.8} luminanceThreshold={0.25} luminanceSmoothing={0.3} mipmapBlur />
          </EffectComposer>
        </Canvas>
      </div>

      <div className="w-rack__callouts-row">
        <RackCallout label="CPU" value={health ? `${health.cpuPct.toFixed(0)}%` : "–"} />
        <RackCallout label="MEMORY" value={health ? `${health.memPct.toFixed(0)}%` : "–"} />
        <RackCallout label="STORAGE" value={health ? `${health.diskPct.toFixed(0)}%` : "–"} />
        <RackCallout label="TEMP" value={health?.tempC != null ? `${health.tempC.toFixed(0)}°C` : "–"} />
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

function PcScene({ exploded, selected, onSelect, cpuColor, ramColor, driveColor, cpuPct }: {
  exploded: boolean; selected: PcPart | null; onSelect: (p: PcPart) => void;
  cpuColor: string; ramColor: string; driveColor: string; cpuPct: number;
}) {
  const part = (id: PcPart, children: (s: PartState) => React.ReactNode) => (
    <ExplodablePart
      part={id} label={PART_LABELS[id]} assembled={ASSEMBLED} exploded={EXPLODED[id]}
      isExploded={exploded} selected={selected} onSelect={onSelect}
      color={id === "cpu" ? cpuColor : id === "ram" ? ramColor : driveColor}
    >
      {children}
    </ExplodablePart>
  );

  return (
    <group>
      {/* Motherboard tray -- purely a backdrop, not clickable, same role as
          HomelabRack's translucent RackFrame. */}
      <mesh position={[0, -0.05, -0.15]}>
        <boxGeometry args={[3.4, 2.6, 0.12]} />
        <meshStandardMaterial color="#061419" metalness={0.4} roughness={0.7} transparent opacity={0.35} />
        <Edges color={CYAN} lineWidth={1} />
      </mesh>

      {part("cpu", (s) => <PulsingChip color={cpuColor} loadPct={cpuPct} selected={s.isSelected} hovered={s.hovered} />)}

      {part("ram", (s) => (
        <group>
          {[-0.22, 0.22].map((x, i) => (
            <mesh key={i} position={[x, 0, 0]}>
              <boxGeometry args={[0.16, 1.1, 0.5]} />
              <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered, ramColor)} />
              <Edges color={ramColor} lineWidth={s.isSelected ? 2 : 1} />
            </mesh>
          ))}
        </group>
      ))}

      {part("drive", (s) => (
        <mesh>
          <boxGeometry args={[1.6, 0.1, 0.4]} />
          <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered, driveColor)} />
          <Edges color={driveColor} lineWidth={s.isSelected ? 2 : 1} />
        </mesh>
      ))}
    </group>
  );
}

/** The CPU die itself pulses faster/brighter the higher its real load is --
 * same emissive-pulse technique as JarvisCore's PulsingCore/HomelabRack's
 * Led, just with speed tied to a live value instead of a fixed rate. */
function PulsingChip({ color, loadPct, selected, hovered }: {
  color: string; loadPct: number; selected: boolean; hovered: boolean;
}) {
  const ref = useRef<Mesh>(null);
  useFrame(({ clock }) => {
    if (!ref.current) return;
    const m = ref.current.material as any;
    const speed = 1.2 + (loadPct / 100) * 3.5;
    const base = selected ? 1.2 : hovered ? 0.95 : 0.7;
    m.emissiveIntensity = base + Math.sin(clock.elapsedTime * speed) * 0.25;
  });
  return (
    <mesh ref={ref}>
      <boxGeometry args={[0.85, 0.12, 0.85]} />
      <meshStandardMaterial color="#052a33" emissive={color} emissiveIntensity={0.8} metalness={0.4} roughness={0.3} />
      <Edges color={color} lineWidth={selected ? 2.5 : 1.4} />
    </mesh>
  );
}
