import { useEffect, useRef } from "react";

interface Particle {
  x: number; y: number; vx: number; vy: number; r: number; a: number;
}

const COUNT = 60;

/** Slow-drifting ambient dust/energy particles behind the HUD -- pure
 * atmosphere, no data bound to it. Density is deliberately low and motion
 * slow so it reads as depth, not noise. */
export function ParticleField() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let width = 0, height = 0;
    const resize = () => {
      width = canvas.width = window.innerWidth * devicePixelRatio;
      height = canvas.height = window.innerHeight * devicePixelRatio;
    };
    resize();
    window.addEventListener("resize", resize);

    const particles: Particle[] = Array.from({ length: COUNT }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.06 * devicePixelRatio,
      vy: (Math.random() - 0.5) * 0.06 * devicePixelRatio,
      r: (Math.random() * 1.1 + 0.3) * devicePixelRatio,
      a: Math.random() * 0.35 + 0.05,
    }));

    let raf: number;
    const draw = () => {
      ctx.clearRect(0, 0, width, height);
      for (const p of particles) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0) p.x = width; else if (p.x > width) p.x = 0;
        if (p.y < 0) p.y = height; else if (p.y > height) p.y = 0;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(0,240,255,${p.a})`;
        ctx.fill();
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);

    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", resize); };
  }, []);

  return <canvas ref={canvasRef} className="hud-particles" aria-hidden />;
}
