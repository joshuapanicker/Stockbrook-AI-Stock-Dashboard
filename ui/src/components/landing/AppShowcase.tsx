import { useEffect, useRef, useState } from "react";
import { Monitor } from "lucide-react";
import { useIsMobile } from "../../hooks/useIsMobile";

/**
 * "Where the verdicts land" — the terminal section, now a live demo reel
 * instead of a static screenshot. A single 43s screen recording of the real
 * app (Dashboard -> Portfolio -> Market Chat), pinned and scroll-scrubbed.
 *
 * Scrubbing is INERTIAL, not direct: scroll sets a target time, and a
 * continuous rAF loop eases playback toward it (exponential approach), so
 * releasing the wheel lets the footage glide to a stop instead of freezing
 * mid-frame. The loop also refuses to issue a new seek while the previous
 * one is still decoding — hammering currentTime mid-seek is what reads as
 * "low framerate".
 *
 * Lazy-mounted: the ~26MB file isn't fetched until the section is within
 * 600px of the viewport. prefers-reduced-motion: static first frame.
 *
 * Mobile (< md): scroll-scrubbing a <video> via currentTime is unreliable
 * on touch browsers (iOS won't seek smoothly without a gesture, Android
 * stutters), and a 340vh pinned section becomes a dead, static scroll zone.
 * So mobile gets a normal-height section where the reel simply autoplays
 * and loops — the demo actually moves instead of freezing.
 */

// One accent across all three chapters. Giving each its own color made the
// chapter strip look like a legend for three unrelated things rather than a
// progress indicator through one continuous recording.
const CHAPTER_ACCENT = "#2EE6A8";

const CHAPTERS = [
  { label: "Dashboard",   start: 0,     end: 18.04,    color: CHAPTER_ACCENT },
  { label: "Portfolio",   start: 18.05, end: 32.67,    color: CHAPTER_ACCENT },
  { label: "Market Chat", start: 32.68, end: Infinity, color: CHAPTER_ACCENT },
];

const FALLBACK_DURATION = 43.7;

export default function AppShowcase() {
  const sectionRef = useRef<HTMLElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const barRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [mounted, setMounted] = useState(false);
  const [ready, setReady] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const isMobile = useIsMobile();

  // Lazy-mount the <video> only once the section nears the viewport
  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;
    const io = new IntersectionObserver(
      entries => { if (entries[0]?.isIntersecting) { setMounted(true); io.disconnect(); } },
      { rootMargin: "600px" },
    );
    io.observe(section);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!mounted || isMobile) return;
    const section = sectionRef.current;
    const video = videoRef.current;
    if (!section || !video) return;

    let duration = FALLBACK_DURATION;
    let rafId = 0;
    let disposed = false;
    let visible = true;
    let smooth = 0;       // eased playback position (seconds)
    let target = 0;       // where scroll wants playback to be
    let lastActive = -1;

    function onMeta() {
      if (video && isFinite(video.duration) && video.duration > 0) duration = video.duration;
      setReady(true);
    }
    function onCanPlay() { setReady(true); }
    video.addEventListener("loadedmetadata", onMeta);
    video.addEventListener("canplay", onCanPlay);

    const vio = new IntersectionObserver(
      entries => { visible = entries[0]?.isIntersecting ?? true; },
      { rootMargin: "100px" },
    );
    vio.observe(section);

    function tick() {
      if (disposed) return;
      rafId = requestAnimationFrame(tick);
      if (!visible || document.hidden) return;

      // Scroll -> target time (reading rects each frame keeps the math
      // honest through resizes and layout shifts)
      const r = section!.getBoundingClientRect();
      const vh = window.innerHeight;
      const span = r.height - vh;
      const p = span > 0 ? Math.max(0, Math.min(1, -r.top / span)) : 0;
      target = p * duration;

      // Light internal ease — page scroll itself is inertial (SmoothWheel),
      // so this mostly decimates seeks rather than adding a second lag
      smooth += (target - smooth) * 0.16;
      if (Math.abs(target - smooth) < 0.004) smooth = target;

      // One seek in flight at a time — re-seeking mid-decode causes the
      // steppy "low framerate" look
      if (video!.readyState >= 2 && !video!.seeking &&
          Math.abs(video!.currentTime - smooth) > 0.02) {
        video!.currentTime = smooth;
      }

      // Chapter label + segmented bars follow the eased time, so they
      // decelerate with the footage
      const idx = CHAPTERS.findIndex(c => smooth >= c.start && smooth < c.end);
      if (idx !== -1 && idx !== lastActive) { lastActive = idx; setActiveIdx(idx); }

      for (let i = 0; i < CHAPTERS.length; i++) {
        const el = barRefs.current[i];
        if (!el) continue;
        const c = CHAPTERS[i];
        const end = Math.min(duration, c.end);
        const local = end > c.start
          ? Math.max(0, Math.min(1, (smooth - c.start) / (end - c.start)))
          : 0;
        el.style.transform = `scaleX(${local})`;
      }
    }

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return () => {
        video.removeEventListener("loadedmetadata", onMeta);
        video.removeEventListener("canplay", onCanPlay);
        vio.disconnect();
      };
    }

    rafId = requestAnimationFrame(tick);
    return () => {
      disposed = true;
      cancelAnimationFrame(rafId);
      video.removeEventListener("loadedmetadata", onMeta);
      video.removeEventListener("canplay", onCanPlay);
      vio.disconnect();
    };
  }, [mounted, isMobile]);

  // Shared browser-chrome bar so mobile and desktop read identically
  const chromeBar = (
    <div className="absolute inset-x-0 top-0 z-10 h-9 bg-[#171C29]/95 border-b border-white/10 flex items-center gap-2 px-4">
      <div className="flex gap-1.5">
        <div className="w-2.5 h-2.5 rounded-full bg-red/60" />
        <div className="w-2.5 h-2.5 rounded-full bg-orange/60" />
        <div className="w-2.5 h-2.5 rounded-full bg-green/60" />
      </div>
      <div className="flex-1 max-w-xs mx-auto bg-card border border-border/40 rounded-md px-3 py-0.5 flex items-center justify-center gap-2">
        <Monitor size={10} className="text-muted" />
        <span className="text-[10px] text-muted">stockbrook.com</span>
      </div>
      <div className="w-16" aria-hidden />
    </div>
  );

  // ── Mobile: normal-height section, the reel autoplays and loops ──
  if (isMobile) {
    const reduce = typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    return (
      <section ref={sectionRef} className="relative z-10 px-6 py-16">
        <div className="text-center mb-7 max-w-2xl mx-auto">
          <p className="font-mono text-[11px] tracking-[0.24em] text-accent-bright/80 uppercase mb-4">The terminal</p>
          <h2 className="font-bold tracking-tight text-3xl text-white leading-[1.12]">
            Where the verdicts <span className="text-white/40">land.</span>
          </h2>
        </div>

        {/* Chapter labels — static on mobile (no scroll scrub to drive them) */}
        <div className="flex items-center justify-center gap-4 mb-5 font-mono text-[10px] tracking-[0.12em] uppercase">
          {CHAPTERS.map(c => (
            <span key={c.label} style={{ color: c.color }}>{c.label}</span>
          ))}
        </div>

        <div className="relative w-full rounded-2xl overflow-hidden border border-white/10 shadow-2xl mx-auto"
          style={{ aspectRatio: "16/9", background: "#10131A" }}>
          {chromeBar}
          {mounted && (
            <video
              ref={videoRef}
              src="/videos/app-demo.mp4"
              muted
              loop
              playsInline
              autoPlay={!reduce}
              controls={reduce}
              preload="metadata"
              className="absolute inset-0 w-full h-full object-cover pt-9"
            />
          )}
        </div>
      </section>
    );
  }

  return (
    <section ref={sectionRef} className="relative z-10" style={{ height: "340vh" }}>
      <div className="sticky top-0 h-screen flex flex-col items-center justify-center px-6 pt-14">
        <div className="text-center mb-7 max-w-2xl">
          <p className="font-mono text-[11px] tracking-[0.24em] text-accent-bright/80 uppercase mb-4">
            The terminal
          </p>
          <h2 className="font-bold tracking-tight text-3xl md:text-[2.75rem] leading-[1.12] text-white">
            Where the verdicts <span className="text-white/40">land.</span>
          </h2>
        </div>

        {/* Chapter labels — the active one lights up in its own voice */}
        <div className="flex items-center gap-6 mb-5 font-mono text-xs tracking-[0.14em] uppercase">
          {CHAPTERS.map((c, i) => (
            <span key={c.label} className="transition-colors duration-300"
              style={{ color: activeIdx === i ? c.color : "rgba(255,255,255,0.32)" }}>
              {c.label}
            </span>
          ))}
        </div>

        {/* The reel, framed in the app's own browser chrome */}
        <div className="relative w-full max-w-5xl rounded-2xl overflow-hidden border border-white/10 shadow-2xl"
          style={{ aspectRatio: "16/9", background: "#10131A" }}>
          {chromeBar}
          {mounted && (
            <video
              ref={videoRef}
              src="/videos/app-demo.mp4"
              muted
              playsInline
              preload="auto"
              className="absolute inset-0 w-full h-full object-cover pt-9 transition-opacity duration-500"
              style={{ opacity: ready ? 1 : 0 }}
            />
          )}
          {!ready && <div className="absolute inset-0 top-9 chart-skeleton" />}
        </div>

        {/* Segmented scrub bar — one colored fill per chapter */}
        <div className="flex gap-1 w-full max-w-5xl mt-4" aria-hidden>
          {CHAPTERS.map(c => (
            <div key={c.label} className="h-[3px] flex-1 rounded-full overflow-hidden bg-white/10">
              <div ref={el => { barRefs.current[CHAPTERS.indexOf(c)] = el; }} className="h-full origin-left"
                style={{ transform: "scaleX(0)", background: c.color }} />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
