import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useIsMobile } from "../../hooks/useIsMobile";
import TickerLogo from "../TickerLogo";

/**
 * "How a verdict gets made" — the pipeline, told as a 4-act scroll-scrubbed
 * sequence inside a sticky viewport. Scroll position IS the timeline: every
 * transform and every typed character derives from scroll progress, so
 * scrubbing backwards replays the story in reverse. No timers.
 *
 * Act 1  NOISE      — the universe grid, flickering
 * Act 2  FILTER     — rules stamp, rows die, 5,700 collapses to 23
 * Act 3  REASONING  — Claude reads filings; text streams in serif italic
 * Act 4  VERDICT    — the stamp lands; the ledger remembers
 *
 * Mobile renders the acts as stacked static cards instead (no sticky scrub).
 */

const ACTS = [
  { n: "01", label: "The noise",    sub: "5,700 tickers, every session" },
  { n: "02", label: "Your rules",   sub: "criteria stamp pass or fail" },
  { n: "03", label: "Claude reads", sub: "filings, news, fundamentals" },
  { n: "04", label: "One verdict",  sub: "logged, then graded" },
];

// Act 1/2 grid — static plausible universe slice (symbol, price string)
const GRID: [string, string][] = [
  ["AAPL", "232.40"], ["NVDA", "210.96"], ["MSFT", "428.21"], ["GOOGL", "182.63"],
  ["AMZN", "218.54"], ["META", "585.12"], ["TSLA", "262.91"], ["AVGO", "172.33"],
  ["JPM", "248.72"], ["V", "311.24"], ["UNH", "512.44"], ["XOM", "108.21"],
  ["WMT", "96.84"], ["LLY", "782.51"], ["COST", "918.32"], ["HD", "386.24"],
  ["CAT", "396.53"], ["BA", "178.34"], ["DIS", "96.23"], ["PLTR", "142.63"],
  ["AMD", "121.72"], ["ORCL", "174.92"], ["KO", "71.31"], ["PEP", "152.94"],
  ["MCD", "292.42"], ["NKE", "71.64"], ["GS", "601.43"], ["ABBV", "192.31"],
  ["CVX", "152.63"], ["TMO", "528.13"], ["UPS", "118.22"], ["INTC", "21.53"],
];

const RULES = [
  { label: "Forward P/E under cap",        pass: true },
  { label: "Revenue growth positive",      pass: true },
  { label: "Profit margin above floor",    pass: true },
  { label: "Not overheated vs 52-wk high", pass: false },
  { label: "Market trend acceptable",      pass: true },
];

const REASONING_TEXT =
  "Forward multiple sits well inside the cap while revenue is still compounding — the growth gate clears with room. Margins hold above the floor. The one red flag: price has run 89% off its 52-week low, so the overheat rule stays failed. Four of five gates clear.";

const CITATIONS = [
  { form: "10-K", section: "Risk Factors" },
  { form: "10-Q", section: "MD&A" },
  { form: "NEWS", section: "2 days ago" },
];

function hashN(s: string, mod: number): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h) % mod;
}

/** Sub-progress within [a,b], clamped 0..1 */
function seg(p: number, a: number, b: number): number {
  return Math.max(0, Math.min(1, (p - a) / (b - a)));
}

// ── Act visuals ────────────────────────────────────────────────────────────

function NoiseGrid({ deathProgress = 0 }: { deathProgress?: number }) {
  // deathProgress 0 → all alive; 1 → only survivors remain lit
  return (
    <div className="grid grid-cols-4 gap-2">
      {GRID.map(([sym, price], i) => {
        const survives = hashN(sym, 32) < 5; // a handful make it
        const dieAt = hashN(sym + "d", 100) / 100 * 0.85; // when this row dies
        const dead = !survives && deathProgress > dieAt;
        const stamped = deathProgress > dieAt;
        return (
          <div
            key={sym}
            className="relative rounded-lg border px-2.5 py-2 font-mono transition-all duration-300"
            style={{
              borderColor: dead ? "rgba(255,255,255,0.03)"
                : stamped && survives ? "rgba(46,230,168,0.4)"
                : "rgba(255,255,255,0.08)",
              background: stamped && survives && deathProgress > 0
                ? "rgba(46,230,168,0.05)" : "rgba(255,255,255,0.015)",
              opacity: dead ? 0.14 : 1,
            }}
          >
            <div className="flex items-center justify-between">
              <span
                className={clsx("flex items-center gap-1.5 text-[11px] font-semibold", dead ? "line-through" : "")}
                style={{ color: dead ? "rgba(255,255,255,0.35)" : "rgba(242,245,249,0.85)" }}
              >
                <TickerLogo symbol={sym} size={14}
                  className={dead ? "grayscale opacity-40" : ""} />
                {sym}
              </span>
              {stamped && (
                <span
                  className="text-[8px] font-semibold tracking-wider"
                  style={{ color: survives ? "#2EE6A8" : "#FF5C7A" }}
                >
                  {survives ? "PASS" : "FAIL"}
                </span>
              )}
            </div>
            <span
              className={clsx(
                "text-[10px]",
                deathProgress === 0 && hashN(sym + "f", 3) === 0 ? "pipeline-flicker" : "",
              )}
              style={{ color: "rgba(242,245,249,0.4)" }}
            >
              ${price}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function FilterOverlay({ p }: { p: number }) {
  // Survivor counter: 5700 → 23 with an exponential ease (fast at first)
  const eased = 1 - Math.pow(1 - Math.min(p * 1.15, 1), 3);
  const count = Math.round(5700 - (5700 - 23) * eased);
  return (
    <>
      {/* Criteria checklist — stamps sequentially with progress */}
      <div className="absolute -left-3 top-6 glass-card border border-white/10 rounded-xl px-4 py-3 shadow-2xl">
        <p className="font-mono text-[9px] tracking-[0.2em] text-white/40 uppercase mb-2">Your criteria</p>
        <div className="space-y-1.5">
          {RULES.map((r, i) => {
            const stamped = p > 0.12 + i * 0.14;
            return (
              <div key={r.label} className="flex items-center gap-2">
                <span
                  className="inline-flex items-center justify-center w-3.5 h-3.5 rounded-[3px] border font-mono text-[8px] transition-all duration-200"
                  style={stamped ? {
                    borderColor: r.pass ? "rgba(46,230,168,0.7)" : "rgba(255,92,122,0.7)",
                    background: r.pass ? "rgba(46,230,168,0.2)" : "rgba(255,92,122,0.15)",
                    color: r.pass ? "#2EE6A8" : "#FF5C7A",
                    transform: "scale(1)",
                  } : {
                    borderColor: "rgba(255,255,255,0.15)",
                    color: "transparent",
                    transform: "scale(0.75)",
                  }}
                >
                  {r.pass ? "✓" : "✕"}
                </span>
                <span
                  className="font-mono text-[10px] transition-colors duration-200"
                  style={{ color: stamped ? "rgba(242,245,249,0.75)" : "rgba(242,245,249,0.3)" }}
                >
                  {r.label}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Survivor counter */}
      <div className="absolute -right-3 bottom-6 glass-card border border-white/10 rounded-xl px-5 py-3.5 shadow-2xl text-right">
        <p className="font-mono text-3xl font-semibold text-white tabular-nums">
          {count.toLocaleString()}
        </p>
        <p className="font-mono text-[9px] tracking-[0.22em] uppercase mt-0.5"
          style={{ color: count <= 23 ? "#2EE6A8" : "rgba(242,245,249,0.4)" }}>
          {count <= 23 ? "Survivors" : "Being screened…"}
        </p>
      </div>
    </>
  );
}

function ReasoningCard({ p }: { p: number }) {
  const chars = Math.floor(seg(p, 0.18, 0.92) * REASONING_TEXT.length);
  const typing = chars > 0 && chars < REASONING_TEXT.length;
  return (
    <div className="max-w-lg mx-auto">
      <div className="glass-card border border-white/10 rounded-2xl px-6 py-5 shadow-2xl"
        style={{ transform: `translateY(${(1 - seg(p, 0, 0.15)) * 24}px)`, opacity: seg(p, 0, 0.12) }}>
        <div className="flex items-baseline justify-between mb-1">
          <span className="font-mono font-semibold text-white text-xl tracking-wide">NVDA</span>
          <span className="font-mono text-sm text-white/70">$210.96 <span className="text-green">+1.24%</span></span>
        </div>
        <div className="flex gap-4 font-mono text-[10px] text-white/45 mb-4">
          <span>FWD P/E <span className="text-white/80">16.5</span></span>
          <span>REV GROWTH <span className="text-white/80">+85%</span></span>
          <span>MARGIN <span className="text-white/80">63%</span></span>
        </div>

        {/* Citation chips slide in */}
        <div className="flex gap-2 mb-4">
          {CITATIONS.map((c, i) => {
            const on = p > 0.1 + i * 0.07;
            return (
              <span key={c.form}
                className="inline-flex items-center gap-1.5 border border-accent/30 bg-accent/10 rounded-md px-2 py-1 font-mono text-[9px] tracking-wider text-accent-bright transition-all duration-300"
                style={{ opacity: on ? 1 : 0, transform: on ? "none" : "translateX(-10px)" }}>
                {c.form} · {c.section.toUpperCase()}
              </span>
            );
          })}
        </div>

        {/* Streaming reasoning — scrubbed by scroll */}
        <p className="text-[15px] leading-relaxed text-white/85 min-h-[110px]">
          {REASONING_TEXT.slice(0, chars)}
          {typing && <span className="inline-block w-[8px] h-[17px] bg-accent ml-0.5 align-middle" />}
        </p>
        <p className="font-mono text-[9px] tracking-[0.18em] text-accent-bright/70 uppercase mt-2 text-right">
          — Claude · grounded in SEC filings
        </p>
      </div>
    </div>
  );
}

function VerdictAct({ p, onSeeLedger }: { p: number; onSeeLedger: () => void }) {
  const stampIn = seg(p, 0.05, 0.22);
  const lineIn = seg(p, 0.3, 0.5);
  return (
    <div className="flex flex-col items-center justify-center text-center h-full">
      <div
        className="font-mono border-2 rounded-xl px-8 py-5 select-none"
        style={{
          borderColor: "rgba(46,230,168,0.6)",
          color: "#2EE6A8",
          background: "rgba(46,230,168,0.05)",
          boxShadow: `0 0 ${40 * stampIn}px rgba(46,230,168,0.15)`,
          transform: `rotate(-3deg) scale(${1.8 - 0.8 * stampIn})`,
          opacity: stampIn,
        }}
      >
        <span className="block text-3xl md:text-4xl font-bold tracking-[0.1em]">BUY</span>
        <span className="block text-[11px] tracking-[0.28em] mt-1.5 text-green/80">4/5 RULES · LOGGED</span>
      </div>

      <p
        className="font-display font-medium text-2xl md:text-3xl text-white/85 mt-10 max-w-md"
        style={{ opacity: lineIn, transform: `translateY(${(1 - lineIn) * 16}px)` }}
      >
        …and we remember every call we make.
      </p>

      <button
        onClick={onSeeLedger}
        className="mt-8 font-mono text-[11px] tracking-[0.22em] uppercase text-muted hover:text-green transition-colors"
        style={{ opacity: seg(p, 0.45, 0.6) }}
      >
        See the ledger ↓
      </button>
    </div>
  );
}

// ── Mobile fallback: stacked static acts ──────────────────────────────────

function MobileActs({ onSeeLedger }: { onSeeLedger: () => void }) {
  return (
    <div className="space-y-10 px-6">
      {ACTS.map((act, i) => (
        <div key={act.n}>
          <div className="flex items-baseline gap-3 mb-4">
            <span className="font-mono text-3xl font-bold text-green/25">{act.n}</span>
            <div>
              <p className="text-white font-semibold">{act.label}</p>
              <p className="font-mono text-[10px] tracking-wider text-white/40 uppercase">{act.sub}</p>
            </div>
          </div>
          {i === 0 && <NoiseGrid />}
          {i === 1 && <div className="relative pt-2 pb-2"><NoiseGrid deathProgress={1} /><FilterOverlay p={1} /></div>}
          {i === 2 && <ReasoningCard p={1} />}
          {i === 3 && <div className="h-72"><VerdictAct p={1} onSeeLedger={onSeeLedger} /></div>}
        </div>
      ))}
    </div>
  );
}

// ── Main showcase ──────────────────────────────────────────────────────────

export default function PipelineShowcase() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [progress, setProgress] = useState(0);
  const isMobile = useIsMobile();

  useEffect(() => {
    if (isMobile) return;
    function onScroll() {
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const scrollable = el.offsetHeight - window.innerHeight;
      if (scrollable <= 0) return;
      setProgress(Math.max(0, Math.min(1, -rect.top / scrollable)));
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, [isMobile]);

  function seeLedger() {
    document.getElementById("track-record")?.scrollIntoView({ behavior: "smooth" });
  }

  const header = (
    <div className="text-center mb-4">
      <p className="font-mono text-[11px] tracking-[0.24em] text-accent-bright/80 uppercase mb-4">The pipeline</p>
      <h2 className="font-bold tracking-tight text-3xl md:text-[2.75rem] leading-[1.12] text-white">
        How a verdict <span className="text-white/40">gets made.</span>
      </h2>
    </div>
  );

  if (isMobile) {
    return (
      <div className="py-16">
        <div className="mb-10">{header}</div>
        <MobileActs onSeeLedger={seeLedger} />
      </div>
    );
  }

  const total = ACTS.length;
  const scaled = progress * total;
  const activeIndex = Math.min(total - 1, Math.floor(scaled));
  const actP = scaled - activeIndex; // 0..1 within active act

  // Act crossfade: fade in over first 10%, out over last 10% (except edges)
  function actOpacity(i: number): number {
    if (i !== activeIndex) return 0;
    const fadeIn = i === 0 ? 1 : seg(actP, 0, 0.1);
    const fadeOut = i === total - 1 ? 1 : 1 - seg(actP, 0.9, 1);
    return Math.min(fadeIn, fadeOut);
  }

  return (
    <div ref={containerRef} style={{ height: `${total * 110}vh` }} className="relative w-full">
      <div style={{ position: "sticky", top: 0, height: "100vh", background: "#06080D" }}>
        <div className="h-full flex flex-col justify-center px-8 max-w-7xl mx-auto">
          <div className="flex-shrink-0 pt-6">{header}</div>

          <div className="flex gap-12 items-center flex-1 min-h-0">
            {/* Left rail — acts + per-act progress */}
            <div className="w-64 flex-shrink-0 space-y-1.5">
              {ACTS.map((act, i) => {
                const active = i === activeIndex;
                return (
                  <div key={act.n}
                    className={clsx(
                      "px-4 py-3 rounded-xl border transition-all duration-300",
                      active ? "border-green/25 bg-green/5" : "border-transparent opacity-35",
                    )}>
                    <div className="flex items-center gap-3">
                      <span className={clsx("font-mono text-xl font-bold", active ? "text-green/60" : "text-white/30")}>
                        {act.n}
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className={clsx("text-sm font-medium", active ? "text-white" : "text-muted")}>{act.label}</p>
                        <p className="font-mono text-[9px] tracking-wider text-white/35 uppercase">{act.sub}</p>
                      </div>
                    </div>
                    <div className="h-0.5 rounded-full bg-white/5 mt-2 overflow-hidden">
                      <div className="h-full bg-green rounded-full"
                        style={{ width: i < activeIndex ? "100%" : active ? `${actP * 100}%` : "0%" }} />
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Right — the stage */}
            <div className="flex-1 min-w-0 relative" style={{ height: "min(560px, calc(100vh - 260px))" }}>
              {/* Act 1: noise */}
              <div className="absolute inset-0 flex items-center justify-center transition-opacity duration-200"
                style={{ opacity: actOpacity(0), pointerEvents: "none" }}>
                <div className="w-full max-w-xl"><NoiseGrid /></div>
              </div>

              {/* Act 2: filter */}
              <div className="absolute inset-0 flex items-center justify-center transition-opacity duration-200"
                style={{ opacity: actOpacity(1), pointerEvents: "none" }}>
                <div className="w-full max-w-xl relative">
                  <NoiseGrid deathProgress={activeIndex === 1 ? actP : activeIndex > 1 ? 1 : 0} />
                  <FilterOverlay p={activeIndex === 1 ? actP : activeIndex > 1 ? 1 : 0} />
                </div>
              </div>

              {/* Act 3: reasoning */}
              <div className="absolute inset-0 flex items-center justify-center transition-opacity duration-200"
                style={{ opacity: actOpacity(2), pointerEvents: "none" }}>
                <div className="w-full">
                  <ReasoningCard p={activeIndex === 2 ? actP : activeIndex > 2 ? 1 : 0} />
                </div>
              </div>

              {/* Act 4: verdict */}
              <div className="absolute inset-0 transition-opacity duration-200"
                style={{ opacity: actOpacity(3), pointerEvents: actOpacity(3) > 0.5 ? "auto" : "none" }}>
                <VerdictAct p={activeIndex === 3 ? actP : 0} onSeeLedger={seeLedger} />
              </div>
            </div>
          </div>

          {/* Scroll hint at the start */}
          {activeIndex === 0 && actP < 0.12 && (
            <div className="absolute bottom-7 left-1/2 -translate-x-1/2 flex flex-col items-center gap-1 text-muted font-mono text-[9px] tracking-[0.2em] uppercase anim-fade-up">
              <div className="w-4 h-7 border border-muted/40 rounded-full flex items-start justify-center pt-1">
                <div className="w-1 h-2 bg-muted/60 rounded-full animate-bounce motion-reduce:animate-none" />
              </div>
              Scroll to run the pipeline
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
