import { useEffect, useRef, useState } from "react";
import type { IgnitedTicker } from "./TickerField";
import TickerLogo from "../TickerLogo";

/**
 * The glass verdict card — where an ignited ticker becomes a verdict.
 * Sequence per ticker: header stamps → seven rule squares stamp in →
 * Claude's reasoning types itself out (serif italic, violet caret — the
 * app's actual streaming aesthetic) → the verdict chip lands with a thunk.
 *
 * Honesty note: this is a pipeline demo with deterministic rule outcomes
 * per symbol — labeled as such in the card header. Reasoning lines speak
 * only about the screening rules, never invented fundamentals.
 */

const RULE_COUNT = 7;

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

interface Script {
  rules: boolean[];
  met: number;
  buy: boolean;
  reasoning: string;
}

function buildScript(symbol: string): Script {
  const h = hash(symbol);
  // 4..7 rules pass, deterministic per symbol
  const met = 4 + (h % 4);
  const rules = Array.from({ length: RULE_COUNT }, (_, i) => {
    if (met === RULE_COUNT) return true;
    // distribute failures pseudo-randomly but stably
    return (h >> (i + 2)) % RULE_COUNT < met;
  });
  // normalize to exactly `met` passes
  let passes = rules.filter(Boolean).length;
  for (let i = 0; i < RULE_COUNT && passes !== met; i++) {
    if (passes < met && !rules[i]) { rules[i] = true; passes++; }
    else if (passes > met && rules[i]) { rules[i] = false; passes--; }
  }
  const buy = met >= 6;
  const reasoning = buy
    ? `Valuation, growth, and margin gates all clear at today's close. ${met} of ${RULE_COUNT} rules pass — this one earns a closer look.`
    : `${met} of ${RULE_COUNT} rules pass, but a gate this strategy treats as non-negotiable stays red. Discipline says wait.`;
  return { rules, met, buy, reasoning };
}

type Phase = "idle" | "header" | "rules" | "reasoning" | "verdict";

export default function VerdictCard({ ticker, className = "" }: {
  ticker: IgnitedTicker | null;
  className?: string;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [stampedRules, setStampedRules] = useState(0);
  const [typedChars, setTypedChars] = useState(0);
  const timers = useRef<number[]>([]);
  const reduced = useRef(
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  const script = ticker ? buildScript(ticker.symbol) : null;

  useEffect(() => {
    if (!ticker) return;
    timers.current.forEach(clearTimeout);
    timers.current = [];
    const later = (fn: () => void, ms: number) =>
      timers.current.push(window.setTimeout(fn, ms));

    if (reduced.current) {
      // No choreography: everything lands at once
      setPhase("verdict");
      setStampedRules(RULE_COUNT);
      setTypedChars(9999);
      return;
    }

    const s = buildScript(ticker.symbol);
    setPhase("header");
    setStampedRules(0);
    setTypedChars(0);

    later(() => setPhase("rules"), 500);
    for (let i = 1; i <= RULE_COUNT; i++) {
      later(() => setStampedRules(i), 500 + i * 130);
    }
    const reasonStart = 500 + RULE_COUNT * 130 + 250;
    later(() => setPhase("reasoning"), reasonStart);
    const chars = s.reasoning.length;
    for (let c = 1; c <= chars; c++) {
      later(() => setTypedChars(c), reasonStart + c * 26);
    }
    later(() => setPhase("verdict"), reasonStart + chars * 26 + 300);

    return () => { timers.current.forEach(clearTimeout); timers.current = []; };
  }, [ticker?.symbol]); // eslint-disable-line react-hooks/exhaustive-deps

  // Near-opaque surface: the flickering field behind must never bleed
  // through the card's text.
  const surface: React.CSSProperties = {
    background: "rgba(9, 12, 18, 0.94)",
    backdropFilter: "blur(18px)",
    WebkitBackdropFilter: "blur(18px)",
  };

  if (!ticker || !script) {
    return (
      <div className={className}>
        <div className="border border-white/10 rounded-2xl px-5 py-4 w-[340px]" style={surface}>
          <p className="font-mono text-[10px] tracking-[0.2em] text-white/30 uppercase">
            Stockbrook · pipeline demo
          </p>
          <p className="font-mono text-xs text-white/40 mt-3 flex items-center gap-2">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-green animate-pulse motion-reduce:animate-none" />
            SCANNING UNIVERSE…
          </p>
        </div>
      </div>
    );
  }

  const up = ticker.changePct >= 0;
  const verdictLanded = phase === "verdict";

  return (
    <div className={className}>
      <div
        className="border border-white/10 rounded-2xl px-5 py-4 w-[340px] transition-shadow duration-500"
        style={{
          ...surface,
          ...(verdictLanded && script.buy
            ? { boxShadow: "0 0 42px rgba(46,230,168,0.12), 0 18px 40px rgba(0,0,0,0.5)" }
            : { boxShadow: "0 18px 40px rgba(0,0,0,0.5)" }),
        }}
      >
        {/* Honesty label */}
        <p className="font-mono text-[10px] tracking-[0.2em] text-white/30 uppercase mb-3">
          Stockbrook · pipeline demo
        </p>

        {/* Header — the ignited ticker */}
        <div className="flex items-center justify-between anim-fade-in">
          <span className="flex items-center gap-2.5 font-mono font-semibold text-white text-lg tracking-wide">
            <TickerLogo symbol={ticker.symbol} size={26} />
            {ticker.symbol}
          </span>
          <span className="font-mono text-sm text-white/70">
            ${ticker.price.toFixed(2)}{" "}
            <span className={up ? "text-green" : "text-red"}>
              {up ? "+" : ""}{ticker.changePct.toFixed(2)}%
            </span>
          </span>
        </div>

        {/* Rule stamps */}
        <div className="flex items-center gap-2 mt-3 h-5">
          <div className="flex gap-1">
            {script.rules.map((pass, i) => (
              <span
                key={i}
                className="inline-block w-3 h-3 rounded-[3px] border transition-all duration-200"
                style={i < stampedRules ? {
                  borderColor: pass ? "rgba(46,230,168,0.7)" : "rgba(255,92,122,0.7)",
                  background: pass ? "rgba(46,230,168,0.25)" : "rgba(255,92,122,0.18)",
                  transform: "scale(1)",
                } : {
                  borderColor: "rgba(255,255,255,0.12)",
                  background: "transparent",
                  transform: "scale(0.8)",
                }}
              />
            ))}
          </div>
          {stampedRules === RULE_COUNT && (
            <span className="font-mono text-[10px] tracking-wider text-white/55 anim-fade-in">
              {script.met}/{RULE_COUNT} RULES MET
            </span>
          )}
        </div>

        {/* Claude reasoning — plain English (the product's promise), violet
            caret, typed */}
        <div className="mt-3 min-h-[72px]">
          {(phase === "reasoning" || phase === "verdict") && (
            <p className="text-[13.5px] leading-relaxed text-white/80">
              {script.reasoning.slice(0, typedChars)}
              {phase === "reasoning" && (
                <span className="inline-block w-[7px] h-[15px] bg-accent ml-0.5 align-middle animate-pulse motion-reduce:animate-none" />
              )}
            </p>
          )}
        </div>
        {(phase === "reasoning" || phase === "verdict") && (
          <p className="font-mono text-[9px] tracking-[0.18em] text-accent-bright/70 uppercase mt-1 text-right">
            — Claude · streamed
          </p>
        )}

        {/* Verdict stamp */}
        <div className="mt-3 h-8">
          {verdictLanded && (
            <span
              className="inline-block font-mono text-xs font-semibold tracking-[0.14em] border rounded-lg px-3 py-1.5 anim-stamp"
              style={script.buy ? {
                color: "#2EE6A8", borderColor: "rgba(46,230,168,0.5)",
                background: "rgba(46,230,168,0.08)",
              } : {
                color: "#FFAC26", borderColor: "rgba(255,172,38,0.5)",
                background: "rgba(255,172,38,0.08)",
              }}
            >
              VERDICT: {script.buy ? "BUY" : "HOLD OFF"} · {script.met}/{RULE_COUNT}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
