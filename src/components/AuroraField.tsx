import { useEffect, useRef } from "react";

interface Blob {
  x: number; y: number; vx: number; vy: number; r: number; hue: number; a: number;
}

const COUNT = 4;
// Cyan-forward palette, staying in the JARVIS hue family rather than
// introducing new brand colors -- narrow variation (170-200) reads as
// "the same light drifting" rather than a rainbow.
const HUES = [178, 188, 196, 172];

/** Slow drifting aurora-style ambient glow behind the grid/ring -- the
 * React-Bits-derived background layer (technique borrowed and hand-built
 * against this canvas, rather than pulling in OGL as a new dependency
 * when this project already leans on plain canvas for ParticleField).
 * Pure atmosphere: no data bound to it, kept low-opacity and slow so it
 * reads as depth behind the ring, never competing with it. */
export function AuroraField() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let width = 0, height = 0;
    const resize = () => {
      width = canvas.width = window.innerWidth * devicePixelRatio;
      height = canvas.height = window.innerHeight * devicePixelRatio;
    };
    resize();
    window.addEventListener("resize", resize);

    const blobs: Blob[] = Array.from({ length: COUNT }, (_, i) => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.045 * devicePixelRatio,
      vy: (Math.random() - 0.5) * 0.045 * devicePixelRatio,
      r: (Math.min(width, height) * (0.28 + Math.random() * 0.14)),
      hue: HUES[i % HUES.length],
      a: 0.05 + Math.random() * 0.04,
    }));

    const drawFrame = () => {
      ctx.clearRect(0, 0, width, height);
      ctx.globalCompositeOperation = "lighter";
      for (const b of blobs) {
        const grad = ctx.createRadialGradient(b.x, b.y, 0, b.x, b.y, b.r);
        grad.addColorStop(0, `hsla(${b.hue}, 100%, 55%, ${b.a})`);
        grad.addColorStop(1, `hsla(${b.hue}, 100%, 55%, 0)`);
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalCompositeOperation = "source-over";
    };

    if (reduced) {
      drawFrame();
      return () => window.removeEventListener("resize", resize);
    }

    let raf: number;
    const draw = () => {
      for (const b of blobs) {
        b.x += b.vx; b.y += b.vy;
        if (b.x < -b.r) b.x = width + b.r; else if (b.x > width + b.r) b.x = -b.r;
        if (b.y < -b.r) b.y = height + b.r; else if (b.y > height + b.r) b.y = -b.r;
      }
      drawFrame();
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);

    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", resize); };
  }, []);

  return <canvas ref={canvasRef} className="hud-aurora" aria-hidden />;
}
