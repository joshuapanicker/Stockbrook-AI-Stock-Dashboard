import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../../hooks/useApi";
import TickerLogo from "../TickerLogo";

/**
 * The page-wide ambient layer, and what's left of it.
 *
 *  - ScrollProgress: a hairline at the very top showing read position.
 *  - TickerTape: infinite price marquee (hydrates with real universe prices)
 *    used as a section divider — the market runs through the page.
 *
 * Three others were removed. DataConstellation (a full-viewport canvas of
 * drifting particles joined by lines), CursorGlow (a gradient lens trailing
 * the pointer) and AmbientWashes (radial washes whose hue was scrubbed by
 * scroll position) all ran at once, behind every section, on top of the
 * hero's own ticker canvas. Four simultaneous decorative layers is what made
 * the page feel restless no matter how the content above them was arranged —
 * and the particle field alone held a 70-node O(n^2) neighbor loop on every
 * frame for the entire length of the page.
 */

// ── Scroll progress — a hairline at the very top ──────────────────────────

export function ScrollProgress() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onScroll() {
      const el = ref.current;
      if (!el) return;
      const max = document.documentElement.scrollHeight - window.innerHeight;
      const p = max > 0 ? window.scrollY / max : 0;
      el.style.transform = `scaleX(${p})`;
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className="fixed top-0 left-0 right-0 h-[2px] z-50 pointer-events-none">
      <div
        ref={ref}
        className="h-full w-full origin-left"
        style={{
          transform: "scaleX(0)",
          background: "#2EE6A8",
          willChange: "transform",
        }}
      />
    </div>
  );
}

// ── Ticker tape — the market runs through the page ────────────────────────

const TAPE_SEED: [string, number, number][] = [
  ["AAPL", 232.4, 0.81], ["NVDA", 211.0, -2.39], ["MSFT", 428.2, 0.44],
  ["GOOGL", 182.6, 1.02], ["AMZN", 218.5, 0.36], ["META", 585.1, -0.21],
  ["TSLA", 262.9, -1.81], ["JPM", 248.7, -0.44], ["V", 311.2, 0.19],
  ["UNH", 512.4, -2.03], ["XOM", 108.2, 2.56], ["WMT", 96.8, -0.3],
  ["LLY", 782.5, 2.59], ["COST", 918.3, 0.67], ["HD", 386.2, -0.33],
  ["CAT", 396.5, 1.4], ["DIS", 96.2, 0.85], ["PLTR", 142.6, 3.21],
  ["AMD", 121.7, -0.6], ["KO", 71.3, 0.12], ["GS", 601.4, 1.56],
  ["CVX", 152.6, -0.51], ["INTC", 21.5, 0.91], ["BA", 178.3, -1.1],
];

export function TickerTape({ hot = false, logos = false }: { hot?: boolean; logos?: boolean } = {}) {
  const [rows, setRows] = useState(TAPE_SEED);

  useEffect(() => {
    let live = true;
    fetch(`${API_BASE}/universe/signals?limit=30`)
      .then(r => (r.ok ? r.json() : null))
      .then((data: any[] | null) => {
        if (!data || !live) return;
        const next: [string, number, number][] = [];
        for (const row of data) {
          const p = row?.metrics?.close_price;
          const lo = row?.metrics?.low_52_week;
          if (row?.symbol && typeof p === "number") {
            // No intraday change in this payload — derive a stable pseudo
            // move per symbol so the tape stays consistent between loops
            let h = 0;
            for (const ch of row.symbol) h = (h * 31 + ch.charCodeAt(0)) | 0;
            const chg = typeof lo === "number" && lo > 0
              ? ((Math.abs(h) % 500) - 250) / 100
              : 0;
            next.push([row.symbol, p, chg]);
          }
        }
        if (next.length >= 12) setRows(next);
      })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  const items = [...rows, ...rows]; // doubled for the -50% loop

  if (hot) {
    // The editorial band — solid heat, black type, unapologetic
    return (
      <div className="ticker-tape ticker-tape-hot relative z-10 overflow-hidden py-3 select-none" aria-hidden>
        <div className="ticker-tape-track">
          {items.map(([sym, price, chg], i) => (
            <span key={`${sym}-${i}`} className="inline-flex items-baseline gap-2 px-6 font-mono text-xs font-bold tracking-wider text-black">
              <span>{sym}</span>
              <span className="opacity-60">${price.toFixed(2)}</span>
              <span>{chg >= 0 ? "▲" : "▼"} {Math.abs(chg).toFixed(2)}%</span>
            </span>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="ticker-tape relative z-10 border-y border-white/[0.06] bg-white/[0.015] overflow-hidden py-2.5 select-none" aria-hidden>
      <div className="ticker-tape-track">
        {items.map(([sym, price, chg], i) => (
          <span key={`${sym}-${i}`} className="inline-flex items-center gap-2 px-6 font-mono text-[11px] tracking-wider">
            {logos && <TickerLogo symbol={sym} size={16} className="opacity-90" />}
            <span className="text-white/70 font-semibold">{sym}</span>
            <span className="text-white/40">${price.toFixed(2)}</span>
            <span className={chg >= 0 ? "text-green/80" : "text-red/80"}>
              {chg >= 0 ? "▲" : "▼"} {Math.abs(chg).toFixed(2)}%
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}
