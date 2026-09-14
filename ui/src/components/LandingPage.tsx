import { useState, useRef } from "react";
import clsx from "clsx";
import {
  TrendingUp, ChevronRight, ArrowRight, Eye, EyeOff, Mail, Lock, Loader2,
  AlertCircle, CheckCircle, Check,
} from "lucide-react";
import { useAuth } from "../context/AuthContext";
import { useInView } from "../hooks/useInView";
import { apiFetch, useMarket } from "../hooks/useApi";
import { TermsOfService, PrivacyPolicy } from "./LegalPages";
import TickerField, { type IgnitedTicker } from "./landing/TickerField";
import VerdictCard from "./landing/VerdictCard";
import PipelineShowcase from "./landing/PipelineShowcase";
import TrackRecordLedger from "./landing/TrackRecordLedger";
import TypeWall from "./landing/TypeWall";
import InstrumentsRail, { INSTRUMENTS } from "./landing/InstrumentsRail";
import AppShowcase from "./landing/AppShowcase";
import { SpotlightCard, ScrollFillText } from "./landing/Effects";
import { ScrollProgress, TickerTape } from "./landing/Atmosphere";

// ── Live SPY/VIX micro-ticker (nav) ───────────────────────────────────────
// The first proof the page is alive: real numbers from /api/market, in the
// mono voice. Renders nothing until data lands.

function MicroTicker({ className = "" }: { className?: string }) {
  const market = useMarket();
  if (!market || market.spy_latest == null) return null;
  const trend = String(market.market_trend ?? "").toLowerCase();
  const up = trend.includes("up");
  const down = trend.includes("down");
  return (
    <div className={clsx("flex items-center gap-3 font-mono text-[11px] tracking-wider text-white/35", className)}>
      <span>
        SPY{" "}
        <span className={up ? "text-green" : down ? "text-red" : "text-white/70"}>
          ${Number(market.spy_latest).toFixed(2)}{up ? " ▲" : down ? " ▼" : ""}
        </span>
      </span>
      <span className="text-white/15">·</span>
      <span>
        VIX <span className="text-white/70">{market.vix != null ? Number(market.vix).toFixed(1) : "—"}</span>
      </span>
    </div>
  );
}

// ── Scroll reveal ─────────────────────────────────────────────────────────
// One motion pattern for the whole page. The old version had nine variants
// (blur / tilt-left / tilt-right / zoom / flip / …) and every section
// arrived a different way, which reads as a demo of the animation library
// rather than as a product. A short rise and a fade, always the same, is
// what makes a page feel composed instead of busy.

function Reveal({ children, delay = 0, className = "" }: {
  children: React.ReactNode; delay?: number; className?: string;
}) {
  const { ref, inView } = useInView(0.12);
  return (
    <div ref={ref} className={className} style={{
      opacity: inView ? 1 : 0,
      transform: inView ? "none" : "translateY(16px)",
      transition: `opacity 0.6s ease ${delay}ms, transform 0.6s cubic-bezier(0.16, 0.9, 0.24, 1) ${delay}ms`,
    }}>
      {children}
    </div>
  );
}

// ── Section heading ───────────────────────────────────────────────────────
// Every section label/title/subtitle pair goes through here, so they cannot
// drift apart in size, spacing, or color the way they had.

function SectionHead({ label, title, accent, sub }: {
  label: string; title: string; accent?: string; sub?: string;
}) {
  return (
    <div className="text-center max-w-2xl mx-auto mb-14">
      <p className="font-mono text-[11px] tracking-[0.24em] text-accent-bright/80 uppercase mb-4">{label}</p>
      <h2 className="font-bold tracking-tight text-3xl md:text-[2.75rem] leading-[1.12] text-white">
        {title}{accent && <span className="text-white/40"> {accent}</span>}
      </h2>
      {sub && <p className="text-muted text-[15px] leading-relaxed mt-4">{sub}</p>}
    </div>
  );
}

// ── Auth form ─────────────────────────────────────────────────────────────

type Mode = "login" | "signup";

/** Supabase's wording varies by version, but a cross-provider collision on
 * signup always mentions the account/user already existing. */
function isAccountExistsError(msg: string): boolean {
  const m = msg.toLowerCase();
  return m.includes("already registered") || m.includes("already exists") || m.includes("user already");
}

function AuthForm({ onOpenTerms, onOpenPrivacy }: { onOpenTerms: () => void; onOpenPrivacy: () => void }) {
  const { signIn, signUp, signInWithGoogle } = useAuth();
  const [mode, setMode] = useState<Mode>("signup");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [loading, setLoading] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null); setSuccess(null);
    if (!email.trim() || !password.trim()) { setError("Email and password required."); return; }
    if (mode === "signup" && password.length < 8) {
      setError("Password must be at least 8 characters."); return;
    }
    setLoading(true);
    if (mode === "login") {
      const { error } = await signIn(email, password);
      if (error) setError(error);
    } else {
      // Catch typo'd/fabricated domains before creating the account —
      // doesn't require sending any email.
      try {
        const check = await apiFetch<{ valid: boolean; reason: string | null }>("/auth/validate-email", {
          method: "POST",
          body: JSON.stringify({ email: email.trim() }),
        });
        if (!check.valid) {
          setError(check.reason ?? "That email address looks invalid.");
          setLoading(false);
          return;
        }
      } catch {
        // Validation service unreachable — fail open rather than block signup
      }

      const { error } = await signUp(email, password);
      if (error) {
        setError(isAccountExistsError(error)
          ? "An account with this email already exists. Sign in below, then link Google from Settings → Security if you'd like to use it too."
          : error);
      } else { setSuccess("Account created! You're signed in."); setMode("login"); }
    }
    setLoading(false);
  }

  async function handleGoogle() {
    setError(null); setSuccess(null); setGoogleLoading(true);
    const { error } = await signInWithGoogle();
    if (error) {
      setError(isAccountExistsError(error)
        ? "An account with this email already exists. Sign in with your password, then link Google from Settings → Security."
        : error);
      setGoogleLoading(false);
    }
    // On success the page redirects to Google, so no need to clear loading here.
  }

  return (
    <div className="bg-card border border-border/60 rounded-2xl p-6 shadow-2xl w-full max-w-sm">
      <div className="flex gap-1 bg-card2 rounded-lg p-1 mb-5">
        {(["signup", "login"] as Mode[]).map(m => (
          <button key={m} onClick={() => { setMode(m); setError(null); setSuccess(null); }}
            className={clsx("flex-1 py-2 rounded-md text-sm font-medium transition-colors",
              mode === m ? "bg-accent text-bg" : "text-muted hover:text-white")}>
            {m === "signup" ? "Get Started" : "Sign In"}
          </button>
        ))}
      </div>

      <button type="button" onClick={handleGoogle} disabled={googleLoading || loading}
        className="w-full flex items-center justify-center gap-2.5 bg-white hover:bg-white/90 disabled:opacity-60 text-[#1f1f1f] rounded-lg py-2.5 text-sm font-semibold transition-colors mb-4">
        {googleLoading ? <Loader2 size={16} className="animate-spin" /> : (
          <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">
            <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.9 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.1 8 3l5.7-5.7C34.6 6 29.6 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z"/>
            <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 15.9 18.9 13 24 13c3.1 0 5.8 1.1 8 3l5.7-5.7C34.6 6 29.6 4 24 4c-7.7 0-14.3 4.4-17.7 10.7z"/>
            <path fill="#4CAF50" d="M24 44c5.5 0 10.4-1.9 14.3-5.1l-6.6-5.6C29.6 34.9 26.9 36 24 36c-5.3 0-9.7-3.1-11.3-7.6l-6.6 5.1C9.6 39.5 16.2 44 24 44z"/>
            <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.3 4.2-4.2 5.6l6.6 5.6C41.5 36 44 30.5 44 24c0-1.3-.1-2.7-.4-3.5z"/>
          </svg>
        )}
        {mode === "signup" ? "Sign up with Google" : "Continue with Google"}
      </button>

      <div className="flex items-center gap-3 mb-4">
        <div className="flex-1 h-px bg-border" />
        <span className="text-[10px] text-muted uppercase tracking-wider">or</span>
        <div className="flex-1 h-px bg-border" />
      </div>

      <form onSubmit={handleSubmit} className="space-y-3">
        <div className="flex items-center gap-2 bg-card2 border border-border rounded-lg px-3 py-2.5 focus-within:border-accent/60 transition-colors">
          <Mail size={13} className="text-muted flex-shrink-0" />
          <input type="email" value={email} onChange={e => setEmail(e.target.value)}
            placeholder="your@email.com" autoComplete="email"
            className="flex-1 bg-transparent text-sm text-white placeholder-muted focus:outline-none" />
        </div>
        <div className="flex items-center gap-2 bg-card2 border border-border rounded-lg px-3 py-2.5 focus-within:border-accent/60 transition-colors">
          <Lock size={13} className="text-muted flex-shrink-0" />
          <input type={showPw ? "text" : "password"} value={password} onChange={e => setPassword(e.target.value)}
            placeholder={mode === "signup" ? "Min 8 characters" : "Password"}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            className="flex-1 bg-transparent text-sm text-white placeholder-muted focus:outline-none" />
          <button type="button" onClick={() => setShowPw(v => !v)} className="text-muted hover:text-white transition-colors">
            {showPw ? <EyeOff size={13} /> : <Eye size={13} />}
          </button>
        </div>
        {error && (
          <div className="flex items-start gap-2 bg-red/10 border border-red/20 rounded-lg px-3 py-2">
            <AlertCircle size={13} className="text-red flex-shrink-0 mt-0.5" />
            <p className="text-red text-xs leading-relaxed">{error}</p>
          </div>
        )}
        {success && (
          <div className="flex items-start gap-2 bg-green/10 border border-green/20 rounded-lg px-3 py-2">
            <CheckCircle size={13} className="text-green flex-shrink-0 mt-0.5" />
            <p className="text-green text-xs leading-relaxed">{success}</p>
          </div>
        )}
        <button type="submit" disabled={loading}
          className="w-full bg-accent hover:bg-accent-dim disabled:opacity-50 text-bg rounded-lg py-2.5 text-sm font-semibold transition-colors flex items-center justify-center gap-2">
          {loading
            ? <><Loader2 size={14} className="animate-spin" />{mode === "login" ? "Signing in..." : "Creating account..."}</>
            : mode === "login" ? "Sign In" : "Create free account"}
        </button>
      </form>
      <p className="text-center text-[11px] text-muted mt-3">
        {mode === "signup" ? "Already have an account? " : "No account yet? "}
        <button onClick={() => { setMode(mode === "signup" ? "login" : "signup"); setError(null); }}
          className="text-accent-bright hover:underline">
          {mode === "signup" ? "Sign in" : "Sign up free"}
        </button>
      </p>
      {mode === "signup" && (
        <p className="text-center text-[10px] text-muted/70 mt-3 leading-relaxed">
          By creating an account you agree to our{" "}
          <button type="button" onClick={onOpenTerms} className="text-muted hover:text-white underline">Terms</button>
          {" "}and{" "}
          <button type="button" onClick={onOpenPrivacy} className="text-muted hover:text-white underline">Privacy Policy</button>.
          Stockbrook does not provide financial advice.
        </p>
      )}
    </div>
  );
}

// ── Main landing page ─────────────────────────────────────────────────────

export default function LandingPage() {
  const authRef = useRef<HTMLDivElement>(null);
  const [termsOpen, setTermsOpen] = useState(false);
  const [privacyOpen, setPrivacyOpen] = useState(false);
  // The hero's canvas field hands ignited tickers to the verdict card
  const [ignited, setIgnited] = useState<IgnitedTicker | null>(null);

  function scrollToAuth() {
    authRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  return (
    <div className="min-h-screen text-white" style={{ background: "#0B0D12" }}>
      <style>{`body { overflow-x: hidden; }`}</style>

      {/* The only page-wide ambient layer left. The cursor lens, the drifting
          particle constellation, and the scroll-driven color washes are gone:
          three canvases and a pointer-tracked gradient competing behind the
          content is what made every screen feel restless. */}
      <ScrollProgress />

      {/* ── NAV ──
          Three columns in normal flow. The market pulse used to be absolutely
          positioned at left-1/2, which sat it directly on top of the "Pipeline"
          link at common widths. */}
      <nav className="sticky top-0 z-30 flex items-center justify-between gap-6 px-6 md:px-8 py-3.5 border-b border-white/[0.06] bg-[#0B0D12]/85 backdrop-blur-md">
        <div className="flex items-center gap-2.5 flex-shrink-0">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center bg-accent/15 border border-accent/30">
            <TrendingUp size={15} className="text-accent-bright" />
          </div>
          <span className="text-white font-semibold text-[15px] tracking-tight">Stockbrook</span>
        </div>

        <MicroTicker className="hidden xl:flex" />

        <div className="flex items-center gap-6 flex-shrink-0">
          <div className="hidden md:flex items-center gap-6 text-[13px] text-muted">
            <a href="#product" className="hover:text-white transition-colors">Product</a>
            <a href="#pipeline" className="hover:text-white transition-colors">How it works</a>
            <a href="#track-record" className="hover:text-white transition-colors">Track record</a>
            <a href="#pricing" className="hover:text-white transition-colors">Pricing</a>
          </div>
          <button onClick={scrollToAuth}
            className="bg-accent hover:bg-accent-dim text-bg rounded-lg px-4 py-2 text-[13px] font-semibold transition-colors">
            Get started
          </button>
        </div>
      </nav>

      {/* ── HERO ── */}
      <section className="relative flex flex-col overflow-hidden" style={{ minHeight: "calc(100vh - 57px)" }}>

        {/* The live ticker field stays — it is the one decorative layer that
            is actually the product's subject matter. Dialed back so it reads
            as texture behind the type instead of competing with it. */}
        <div aria-hidden className="absolute inset-0 pointer-events-none opacity-[0.5]">
          <div className="absolute inset-0 landing-grid-texture" />
          <div className="absolute inset-0" style={{ background: "radial-gradient(ellipse 60% 45% at 50% 8%, rgba(46,230,168,0.09), transparent 62%)" }} />
          <TickerField onIgnite={setIgnited} className="absolute inset-0 w-full h-full" />
        </div>
        {/* Scrim. The field is the subject matter, but it was running directly
            under the body copy at full strength — the line about Claude
            reasoning had ticker rows crossing it. The type sits in a calm
            pocket; the field stays legible everywhere else. */}
        <div aria-hidden className="absolute inset-0 pointer-events-none"
          style={{ background: "radial-gradient(ellipse 46% 42% at 50% 46%, rgba(11,13,18,0.92) 35%, rgba(11,13,18,0) 100%)" }} />
        <div aria-hidden className="absolute inset-x-0 bottom-0 h-32" style={{ background: "linear-gradient(to bottom, transparent, #0B0D12)" }} />

        <div className="hidden xl:block absolute right-6 bottom-20 z-10 opacity-60 hover:opacity-100 transition-opacity duration-300"
          style={{ transform: "scale(0.78)", transformOrigin: "bottom right" }}>
          <VerdictCard ticker={ignited} />
        </div>

        <div className="relative flex-1 flex flex-col items-center justify-center text-center px-6 pt-16 pb-12 max-w-3xl mx-auto w-full">

          <div className="inline-flex items-center gap-2.5 border border-white/10 bg-white/[0.03] rounded-full px-3.5 py-1.5 font-mono text-[10px] tracking-[0.16em] text-white/55 uppercase mb-10">
            <span className="relative flex h-1.5 w-1.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green opacity-60" />
              <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-green" />
            </span>
            Live · reading 5,700 tickers
          </div>

          {/* Two voices, no serif: the market states its size in mono, the
              product answers in one heavy line of Inter. */}
          <h1 className="mb-7">
            <span className="block font-mono text-sm md:text-base tracking-[0.22em] text-white/45 uppercase mb-5">
              5,700 stocks.
            </span>
            <span className="block font-bold tracking-[-0.03em] text-[3.25rem] sm:text-7xl md:text-[5.5rem] leading-[0.95] text-white">
              One verdict<span className="text-accent">.</span>
            </span>
          </h1>

          <p className="text-white/55 text-base md:text-[17px] leading-relaxed max-w-xl mb-10">
            Live market data, your rules, and Claude reasoning over every position —
            in plain English, as it streams.
          </p>

          <div className="flex items-center justify-center gap-4 flex-wrap">
            <button onClick={scrollToAuth}
              className="flex items-center gap-2 bg-accent hover:bg-accent-dim text-bg rounded-lg px-6 py-3 text-sm font-semibold transition-colors">
              Start for free <ArrowRight size={15} />
            </button>
            <button onClick={() => document.getElementById("product")?.scrollIntoView({ behavior: "smooth" })}
              className="flex items-center gap-1.5 text-sm text-white/60 hover:text-white transition-colors px-2 py-3">
              See it in action <ChevronRight size={14} />
            </button>
          </div>
        </div>

        {/* Proof strip — three true numbers, mono voice */}
        <div className="relative flex flex-wrap items-center justify-center gap-x-8 gap-y-2 px-6 pb-10 font-mono text-[11px] tracking-[0.1em] text-white/35 uppercase">
          <span><span className="text-white/70">5,700+</span> US listings scanned</span>
          <span className="hidden md:inline text-white/15">·</span>
          <span><span className="text-white/70">200K</span> free AI tokens / mo</span>
          <span className="hidden md:inline text-white/15">·</span>
          <span>Every verdict graded at <span className="text-white/70">30/90/180d</span></span>
        </div>
      </section>

      <TickerTape logos />

      {/* ── THE PRODUCT — the demo reel, promoted to right after the hero.
             It is the most credible thing on the page; it was buried below
             two full scroll sequences. ── */}
      <div id="product">
        <AppShowcase />
      </div>

      {/* ── HOW IT WORKS — scroll-scrubbed 4-act sequence ── */}
      <section id="pipeline" className="relative">
        <PipelineShowcase />
      </section>

      {/* ── THE VERDICT WALL ── */}
      <TypeWall />

      <div className="relative overflow-hidden py-10 md:py-14">
        <ScrollFillText
          text="GRADED IN PUBLIC"
          className="text-center text-[7vw] leading-none tracking-[-0.02em] font-bold"
        />
      </div>

      {/* ── TRACK RECORD ── */}
      <TrackRecordLedger />

      {/* ── INSTRUMENTS ── */}
      <div className="hidden lg:block">
        <InstrumentsRail />
      </div>
      <section className="relative px-6 py-20 max-w-7xl mx-auto lg:hidden">
        <Reveal>
          <SectionHead label="Instruments" title="Six instruments," accent="one terminal." />
        </Reveal>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {INSTRUMENTS.map(({ icon: Icon, title, desc }, i) => (
            <Reveal key={title} delay={(i % 2) * 70}>
              <SpotlightCard className="glass-card border border-white/[0.07] rounded-xl p-5 h-full">
                <div className="w-9 h-9 rounded-lg border border-accent/25 bg-accent/10 text-accent-bright flex items-center justify-center mb-4">
                  <Icon size={17} />
                </div>
                <p className="font-mono text-[11px] tracking-[0.1em] uppercase text-white mb-2">{title}</p>
                <p className="text-muted text-[13px] leading-relaxed">{desc}</p>
              </SpotlightCard>
            </Reveal>
          ))}
        </div>
      </section>

      {/* ── PRICING ── */}
      <section id="pricing" className="relative px-6 py-24 max-w-7xl mx-auto">
        <Reveal>
          <SectionHead label="Pricing" title="Free while it earns" accent="your trust." />
        </Reveal>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-5 max-w-3xl mx-auto items-start">
          <Reveal>
            <div className="bg-card border border-accent/30 rounded-xl p-6 h-full relative">
              <div className="absolute -top-2.5 left-6 bg-accent text-bg text-[10px] font-semibold px-2 py-0.5 rounded">
                Current plan
              </div>
              <p className="text-white font-semibold mb-1 mt-1">Free</p>
              <p className="text-4xl font-mono font-bold text-white mb-1">$0<span className="text-muted text-sm font-normal">/mo</span></p>
              <p className="font-mono text-[10px] tracking-wider text-muted uppercase mb-6">200K AI tokens/mo · own key = unlimited</p>
              <div className="space-y-2.5 mb-7">
                {["Stock Search Engine (5,700+ US stocks)", "AI criteria builder + presets", "Portfolio tracker with P&L", "Live market data", "Volume Profile charts", "AI analysis + chat"].map(f => (
                  <div key={f} className="flex items-start gap-2.5 text-[13px] text-white/70">
                    <Check size={14} className="text-green flex-shrink-0 mt-0.5" />{f}
                  </div>
                ))}
              </div>
              <button onClick={scrollToAuth}
                className="w-full bg-accent hover:bg-accent-dim text-bg rounded-lg py-2.5 text-sm font-semibold transition-colors">
                Get started free
              </button>
            </div>
          </Reveal>
          <Reveal delay={90}>
            <div className="bg-card border border-border/50 rounded-xl p-6 h-full">
              <div className="flex items-center justify-between mb-1 mt-1">
                <p className="text-white font-semibold">Pro</p>
                <span className="text-muted text-[10px] font-medium px-2 py-0.5 rounded border border-border">Coming soon</span>
              </div>
              <p className="text-4xl font-mono font-bold text-white/50 mb-1">$12<span className="text-muted text-sm font-normal">/mo</span></p>
              <p className="font-mono text-[10px] tracking-wider text-muted uppercase mb-6">For serious investors</p>
              <div className="space-y-2.5 mb-7">
                {["Everything in Free", "Price & criteria alerts (email)", "Backtesting engine", "News & earnings injection", "Priority AI analysis"].map(f => (
                  <div key={f} className="flex items-start gap-2.5 text-[13px] text-white/45">
                    <Check size={14} className="text-muted flex-shrink-0 mt-0.5" />{f}
                  </div>
                ))}
              </div>
              <button disabled className="w-full bg-white/5 text-muted rounded-lg py-2.5 text-sm font-semibold cursor-not-allowed">
                Notify me
              </button>
            </div>
          </Reveal>
        </div>
      </section>

      {/* ── CTA + AUTH ── */}
      <section ref={authRef} className="relative px-6 py-24 max-w-7xl mx-auto">
        <Reveal>
          <div className="relative overflow-hidden rounded-2xl border border-white/[0.08] px-8 py-16"
            style={{ background: "linear-gradient(160deg, rgba(46,230,168,0.06), rgba(11,13,18,0) 55%)" }}>
            <div aria-hidden className="absolute inset-0 landing-grid-texture opacity-40 pointer-events-none" />
            <div className="relative flex flex-col items-center">
              <h2 className="font-bold tracking-tight text-3xl md:text-[2.75rem] leading-[1.12] text-white mb-4 text-center max-w-2xl">
                The market never stops talking.
                <span className="block text-white/40">Hear what matters.</span>
              </h2>
              <p className="text-muted mb-10 max-w-md mx-auto text-[15px] text-center">
                Free account, 200K AI tokens a month, every verdict graded in public.
              </p>
              <AuthForm onOpenTerms={() => setTermsOpen(true)} onOpenPrivacy={() => setPrivacyOpen(true)} />
            </div>
          </div>
        </Reveal>
      </section>

      <TickerTape />

      {/* ── FOOTER ── */}
      <footer className="relative border-t border-white/[0.06] px-6 md:px-8 py-8">
        <div className="max-w-7xl mx-auto flex items-center justify-between flex-wrap gap-5">
          <div className="flex items-center gap-5">
            <div className="flex items-center gap-2">
              <div className="w-6 h-6 rounded-md flex items-center justify-center bg-accent/15 border border-accent/25">
                <TrendingUp size={12} className="text-accent-bright" />
              </div>
              <span className="text-white font-semibold text-sm">Stockbrook</span>
            </div>
            <MicroTicker className="hidden sm:flex" />
          </div>
          <p className="text-muted text-[11px] max-w-xl leading-relaxed">
            © 2026 Stockbrook · Not financial, investment, or tax advice. For informational purposes only.
            Past performance does not guarantee future results.
          </p>
          <div className="flex gap-6 text-xs text-muted">
            <button onClick={() => setPrivacyOpen(true)} className="hover:text-white transition-colors">Privacy</button>
            <button onClick={() => setTermsOpen(true)} className="hover:text-white transition-colors">Terms</button>
          </div>
        </div>
      </footer>

      <TermsOfService open={termsOpen} onClose={() => setTermsOpen(false)} />
      <PrivacyPolicy open={privacyOpen} onClose={() => setPrivacyOpen(false)} />
    </div>
  );
}
