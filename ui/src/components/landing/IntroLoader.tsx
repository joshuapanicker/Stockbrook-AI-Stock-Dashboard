import { useEffect, useState } from "react";

/**
 * Intro loader — the curtain before the page. Black screen, the Stockbrook
 * mark draws itself on, "STOCKBROOK" staggers in beneath it in the mono
 * badge voice, then the curtain wipes upward and hands the page over.
 *
 * Retinted to the terminal blue that now carries the brand — it used to
 * draw in the red→orange "heat" gradient, which was the same color the
 * interface uses to mean "you lost money".
 *
 * Timeline (ms):
 *    0 — mark begins drawing (stroke-dashoffset)
 *  650 — wordmark letters start staggering in
 * 1550 — curtain wipe starts; onReveal() fires so the page can start
 * 2150 — overlay unmounts
 *
 * prefers-reduced-motion: skips entirely (onReveal immediately, no overlay).
 */

const WORD = "STOCKBROOK";

export default function IntroLoader({ onReveal }: { onReveal: () => void }) {
  const [phase, setPhase] = useState<"draw" | "wipe" | "gone">("draw");

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setPhase("gone");
      onReveal();
      return;
    }
    // Curtain holds while the mark draws, then lifts
    document.documentElement.style.overflow = "hidden";
    const wipe = setTimeout(() => {
      setPhase("wipe");
      onReveal();
      document.documentElement.style.overflow = "";
    }, 1550);
    const gone = setTimeout(() => setPhase("gone"), 2150);
    return () => {
      clearTimeout(wipe);
      clearTimeout(gone);
      document.documentElement.style.overflow = "";
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (phase === "gone") return null;

  return (
    <div
      aria-hidden
      className="fixed inset-0 z-[200] flex flex-col items-center justify-center bg-black"
      style={{
        transform: phase === "wipe" ? "translateY(-100%)" : "translateY(0)",
        transition: "transform 0.6s cubic-bezier(0.7, 0, 0.2, 1)",
        willChange: "transform",
      }}
    >
      {/* The mark — draws on, glowing */}
      <svg
        width="76" height="76" viewBox="0 0 24 24" fill="none"
        style={{ filter: "drop-shadow(0 0 14px rgba(46,230,168,0.5)) drop-shadow(0 0 34px rgba(46,230,168,0.22))" }}
      >
        <defs>
          <linearGradient id="intro-mark" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0%" stopColor="#1FC48D" />
            <stop offset="100%" stopColor="#5BF0C0" />
          </linearGradient>
        </defs>
        <polyline
          points="2 17 8.5 10.5 13.5 15.5 22 7"
          stroke="url(#intro-mark)" strokeWidth="1.8"
          strokeLinecap="round" strokeLinejoin="round"
          className="intro-draw"
          style={{ ["--len" as string]: 32 }}
        />
        <polyline
          points="16 7 22 7 22 13"
          stroke="url(#intro-mark)" strokeWidth="1.8"
          strokeLinecap="round" strokeLinejoin="round"
          className="intro-draw"
          style={{ ["--len" as string]: 13, animationDelay: "0.55s" }}
        />
      </svg>

      {/* Wordmark — the badge voice, letter-staggered */}
      <p className="font-mono text-[12px] tracking-[0.42em] uppercase mt-6 ml-[0.42em]"
        style={{ color: "#5BF0C0", textShadow: "0 0 18px rgba(46,230,168,0.45)" }}>
        {WORD.split("").map((ch, i) => (
          <span key={i} className="intro-char" style={{ ["--ci" as string]: i }}>{ch}</span>
        ))}
      </p>
    </div>
  );
}
