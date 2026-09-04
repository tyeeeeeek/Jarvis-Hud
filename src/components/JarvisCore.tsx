import { useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, Edges } from "@react-three/drei";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import type { Mesh } from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { ExplodablePart, type PartState } from "./Explodable";

export type CorePart = "housing" | "tickDial" | "core" | "projectors" | "base";

const CYAN = "#00f0ff";
const BODY_COLOR = "#0b2530";

const PART_LABELS: Record<CorePart, string> = {
  housing: "OUTER HOUSING", tickDial: "TICK-DIAL ASSEMBLY", core: "ENERGY CORE",
  projectors: "PROJECTOR ARRAY", base: "BASE MOUNT",
};

// Assembled position is always the origin (everything nests together when
// closed) -- only the exploded destination differs per part, one axis of
// "burst apart" each so it reads as a real disassembly, not everything
// sliding the same direction.
const ASSEMBLED: [number, number, number] = [0, 0, 0];
const EXPLODED: Record<CorePart, [number, number, number]> = {
  housing: [0, 2.3, 0],
  tickDial: [0, 1.1, 0],
  core: [0, 0, 1.6],
  projectors: [0, -1.1, 0],
  base: [0, -2.4, 0],
};

function bodyProps(isSelected: boolean, hovered: boolean) {
  return {
    color: BODY_COLOR, metalness: 0.55, roughness: 0.4,
    emissive: isSelected ? "#00c4e6" : hovered ? "#036575" : "#01181e",
    emissiveIntensity: isSelected ? 0.85 : hovered ? 0.5 : 0.3,
  };
}

/** A real, solid 3D model in the arc reactor's own visual language (no
 * literal Iron Man hardware exists to reference, so this is an original
 * design) -- ring housing, a tick-dial assembly echoing the 2D TickRing
 * from the HUD's boot ring, a pulsing energy core, a 4-node projector
 * array, and a base mount. Same construction as HomelabRack.tsx (solid
 * meshStandardMaterial, cyan Edges, click/hover/select) plus Explodable's
 * assembled/exploded animation on every part. Purely decorative/live-in-
 * the-HUD -- no external data feed, unlike PcInternals.tsx, and (unlike
 * HomelabRack, which needs its parent HomelabWidget for a data side-panel)
 * fully self-contained: owns its own selected-part state since nothing
 * outside this component needs to react to it. */
export function JarvisCore() {
  const [exploded, setExploded] = useState(false);
  const [turned, setTurned] = useState(false);
  const [selected, setSelected] = useState<CorePart | null>(null);
  const onSelect = (part: CorePart) => setSelected(prev => (prev === part ? null : part));
  const controlsRef = useRef<OrbitControlsImpl | null>(null);
  const resetView = () => controlsRef.current?.reset();

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
        <Canvas camera={{ position: [4.2, 1.8, 6.5], fov: 32 }} onPointerDown={() => setTurned(true)}>
          <ambientLight intensity={0.5} />
          <directionalLight position={[6, 9, 6]} intensity={1.3} />
          <directionalLight position={[-6, -3, -4]} intensity={0.35} />
          <pointLight position={[0, 0, 0]} intensity={1.1} color={CYAN} />
          <CoreScene exploded={exploded} selected={selected} onSelect={onSelect} />
          <OrbitControls
            ref={controlsRef}
            enablePan={false}
            minDistance={4}
            maxDistance={14}
            autoRotate={!turned}
            autoRotateSpeed={0.6}
            target={[0, 0, 0]}
          />
          <EffectComposer>
            <Bloom intensity={0.9} luminanceThreshold={0.25} luminanceSmoothing={0.3} mipmapBlur />
          </EffectComposer>
        </Canvas>
      </div>
    </div>
  );
}

function CoreScene({ exploded, selected, onSelect }: {
  exploded: boolean; selected: CorePart | null; onSelect: (p: CorePart) => void;
}) {
  const part = (id: CorePart, children: (s: PartState) => React.ReactNode) => (
    <ExplodablePart
      part={id} label={PART_LABELS[id]} assembled={ASSEMBLED} exploded={EXPLODED[id]}
      isExploded={exploded} selected={selected} onSelect={onSelect}
    >
      {children}
    </ExplodablePart>
  );

  return (
    <group>
      {part("housing", (s) => (
        <mesh rotation={[Math.PI / 2, 0, 0]}>
          <torusGeometry args={[1.5, 0.13, 16, 56]} />
          <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
          <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
        </mesh>
      ))}

      {part("tickDial", (s) => (
        <group>
          <mesh rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[1.15, 0.04, 8, 48]} />
            <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
            <Edges color={CYAN} lineWidth={s.isSelected ? 2 : 1} />
          </mesh>
          {Array.from({ length: 32 }).map((_, i) => {
            const a = (i / 32) * Math.PI * 2;
            const major = i % 4 === 0;
            const r = 1.15;
            return (
              <mesh key={i} position={[Math.cos(a) * r, Math.sin(a) * r, 0]} rotation={[0, 0, a]}>
                <boxGeometry args={[major ? 0.1 : 0.05, 0.18, 0.05]} />
                <meshStandardMaterial color="#04323d" emissive={CYAN} emissiveIntensity={major ? 0.7 : 0.4} />
              </mesh>
            );
          })}
        </group>
      ))}

      {part("core", (s) => <PulsingCore selected={s.isSelected} hovered={s.hovered} />)}

      {part("projectors", (s) => (
        <group>
          {[0, 1, 2, 3].map(i => {
            const a = (i / 4) * Math.PI * 2 + Math.PI / 4;
            const r = 0.95;
            return (
              <mesh key={i} position={[Math.cos(a) * r, 0, Math.sin(a) * r]}>
                <coneGeometry args={[0.16, 0.4, 12]} />
                <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
                <Edges color={CYAN} lineWidth={s.isSelected ? 2 : 1} />
              </mesh>
            );
          })}
        </group>
      ))}

      {part("base", (s) => (
        <mesh>
          <cylinderGeometry args={[1.05, 1.25, 0.45, 32]} />
          <meshStandardMaterial {...bodyProps(s.isSelected, s.hovered)} />
          <Edges color={CYAN} lineWidth={s.isSelected ? 2.5 : 1.2} />
        </mesh>
      ))}
    </group>
  );
}

/** The one part that visibly breathes even when the model is idle/assembled
 * -- same emissive-pulse technique HomelabRack's Led uses, just on a bigger
 * centerpiece shape instead of a tiny sphere. */
function PulsingCore({ selected, hovered }: { selected: boolean; hovered: boolean }) {
  const ref = useRef<Mesh>(null);
  useFrame(({ clock }) => {
    if (!ref.current) return;
    const m = ref.current.material as any;
    const base = selected ? 1.3 : hovered ? 1.0 : 0.75;
    m.emissiveIntensity = base + Math.sin(clock.elapsedTime * 2) * 0.3;
  });
  return (
    <mesh ref={ref}>
      <icosahedronGeometry args={[0.42, 1]} />
      <meshStandardMaterial color="#052a33" emissive={CYAN} emissiveIntensity={0.8} metalness={0.3} roughness={0.3} />
      <Edges color={CYAN} lineWidth={selected ? 2.5 : 1.4} />
    </mesh>
  );
}
