import { useEffect, useRef } from "react";

/**
 * The two interaction effects the landing page still uses.
 *
 * Four others used to live here — GlitchText (chromatic bursts), ScrambleLink
 * (nav labels decoding from random glyphs), SmoothWheel (page-level scroll
 * inertia that intercepted the wheel) and VelocityWarp (skewing the whole
 * page by scroll velocity). They were removed rather than tuned:
 *
 *  - SmoothWheel took over the user's scroll. Nothing makes an interface feel
 *    less trustworthy than a page that doesn't stop when you stop, and it was
 *    fighting the pinned/scrubbed sections it was meant to smooth.
 *  - The glitch and scramble effects simulate a malfunctioning readout. That
 *    is a strange thing to fake in a product whose pitch is that its numbers
 *    can be trusted.
 */

// ── Spotlight card — a pointer-tracked lens in the border ─────────────────

export function SpotlightCard({ children, color, className = "" }: {
  children: React.ReactNode; color?: string; className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  function onMove(e: React.MouseEvent) {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${e.clientX - r.left}px`);
    el.style.setProperty("--my", `${e.clientY - r.top}px`);
  }

  return (
    <div ref={ref} onMouseMove={onMove}
      className={`spot-card ${className}`}
      style={color ? ({ "--spot-color": color } as React.CSSProperties) : undefined}>
      {children}
    </div>
  );
}

// ── Scroll-fill text — outlined giant type that floods with color ──────────
// Two stacked copies of the same line: an outline ghost underneath and a
// filled copy on top, clipped by scroll progress so the fill pours in from
// the left as the line crosses the viewport.

export function ScrollFillText({ text, className = "" }: { text: string; className?: string }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const fillRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const wrap = wrapRef.current;
    const fill = fillRef.current;
    if (!wrap || !fill) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      fill.style.clipPath = "none"; // static: fully filled
      return;
    }

    let rafId = 0;
    let ticking = false;

    function apply() {
      ticking = false;
      const r = wrap!.getBoundingClientRect();
      const vh = window.innerHeight;
      // Fill runs 0→1 while the line travels the middle 70% of the viewport
      const p = Math.max(0, Math.min(1, (vh * 0.88 - r.top) / (vh * 0.7)));
      fill!.style.clipPath = `inset(0 ${(1 - p) * 100}% 0 0)`;
    }

    function onScroll() {
      if (!ticking) { ticking = true; rafId = requestAnimationFrame(apply); }
    }

    window.addEventListener("scroll", onScroll, { passive: true });
    apply();
    return () => {
      window.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(rafId);
    };
  }, []);

  return (
    <div ref={wrapRef} aria-hidden className={`relative select-none whitespace-nowrap ${className}`}>
      <span className="block"
        style={{ WebkitTextStroke: "1.5px rgba(255,255,255,0.14)", color: "transparent" }}>
        {text}
      </span>
      <span ref={fillRef} className="absolute inset-0 block text-white"
        style={{ clipPath: "inset(0 100% 0 0)" }}>
        {text}
      </span>
    </div>
  );
}
