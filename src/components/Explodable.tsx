import { useRef, useState } from "react";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import { Line, Html } from "@react-three/drei";
import type { Group } from "three";

export interface PartState { isSelected: boolean; hovered: boolean }

/**
 * Shared "disassemble to see each component" mechanism -- generalized from
 * HomelabRack.tsx's EdgeGroup (solid-body click/hover/select pattern, kept
 * identical here) plus the one genuinely new piece: an assembled/exploded
 * position pair, smoothly lerped every frame via useFrame (same technique
 * HomelabRack's Led already uses for its own per-frame animation, no new
 * animation library needed inside the Canvas). When exploded, a thin
 * leader line and a floating HTML label appear, both automatically
 * correct as the part moves since they're children of the animated group
 * (local origin [0,0,0] always means "wherever this part currently is").
 *
 * Used by both JarvisCore.tsx and PcInternals.tsx; HomelabRack.tsx could
 * be retrofitted onto this later but isn't touched by this round.
 */
export function ExplodablePart<P extends string>({
  part, label, assembled, exploded, isExploded, selected, onSelect, color = "#00f0ff", children,
}: {
  part: P;
  label: string;
  assembled: [number, number, number];
  exploded: [number, number, number];
  isExploded: boolean;
  selected: P | null;
  onSelect: (part: P) => void;
  color?: string;
  children: (state: PartState) => React.ReactNode;
}) {
  const groupRef = useRef<Group>(null);
  const [hovered, setHovered] = useState(false);
  const isSelected = selected === part;
  const target = isExploded ? exploded : assembled;

  useFrame((_, delta) => {
    const g = groupRef.current;
    if (!g) return;
    const speed = Math.min(1, delta * 4.5);
    g.position.x += (target[0] - g.position.x) * speed;
    g.position.y += (target[1] - g.position.y) * speed;
    g.position.z += (target[2] - g.position.z) * speed;
  });

  // Fixed offset (in the part's own local space) pointing back toward its
  // assembled position -- stays geometrically correct as the group itself
  // moves, no per-frame recompute needed (see module docstring).
  const backToAssembled: [number, number, number] = [
    assembled[0] - exploded[0], assembled[1] - exploded[1], assembled[2] - exploded[2],
  ];

  return (
    <group
      ref={groupRef}
      position={assembled}
      onClick={(e: ThreeEvent<MouseEvent>) => { e.stopPropagation(); onSelect(part); }}
      onPointerOver={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); setHovered(true); document.body.style.cursor = "pointer"; }}
      onPointerOut={() => { setHovered(false); document.body.style.cursor = "auto"; }}
      scale={isSelected ? 1.06 : 1}
    >
      {children({ isSelected, hovered })}
      {isExploded && (
        <>
          <Line points={[[0, 0, 0], backToAssembled]} color={color} lineWidth={1} transparent opacity={0.45} />
          <Html center distanceFactor={9} style={{ pointerEvents: "none" }}>
            <div className="explode-label">{label}</div>
          </Html>
        </>
      )}
    </group>
  );
}
