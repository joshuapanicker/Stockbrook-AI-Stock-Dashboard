import clsx from "clsx";
import { useTrackRecord, type TrackRecordCall } from "../../hooks/useApi";
import { useInView } from "../../hooks/useInView";
import TickerLogo from "../TickerLogo";

/**
 * "We grade our own calls" — the trust section. Real data from the public
 * /api/track-record scoreboard: every AI verdict is logged with the price
 * at that moment, then graded against reality (and SPY) at 30/90/180 days.
 * Losses render in red just like wins render in green — honesty is the flex.
 *
 * The scoreboard is new, so the empty state is designed as a feature:
 * an open ledger awaiting its first grades, not a missing section.
 */

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
}

function pctColor(v: number | null | undefined): string {
  if (v == null) return "text-white/30";
  return v >= 0 ? "text-green" : "text-red";
}

function LedgerRow({ call, index }: { call: TrackRecordCall; index: number }) {
  const { ref, inView } = useInView(0.2);
  const buy = call.action === "buy";
  const yes = call.decision === "YES";
  return (
    <div
      ref={ref}
      className="grid grid-cols-[92px_88px_1fr_92px_72px_72px] items-center gap-2 px-4 py-2.5 border-b border-white/[0.05] font-mono text-xs"
      style={{
        opacity: inView ? 1 : 0,
        transform: inView ? "none" : "translateY(10px)",
        transition: `opacity 0.4s ease ${index * 60}ms, transform 0.4s ease ${index * 60}ms`,
      }}
    >
      <span className="flex items-center gap-2 text-white font-semibold">
        <TickerLogo symbol={call.symbol} size={18} />
        {call.symbol}
      </span>
      <span
        className="inline-flex justify-center border rounded px-1.5 py-0.5 text-[9px] tracking-wider font-semibold"
        style={yes && buy
          ? { color: "#2EE6A8", borderColor: "rgba(46,230,168,0.4)", background: "rgba(46,230,168,0.06)" }
          : yes
            ? { color: "#FF5C7A", borderColor: "rgba(255,92,122,0.4)", background: "rgba(255,92,122,0.06)" }
            : { color: "#FFAC26", borderColor: "rgba(255,172,38,0.4)", background: "rgba(255,172,38,0.06)" }}
      >
        {yes ? (buy ? "BUY" : "SELL") : "NO CALL"}
      </span>
      <span className="text-white/40 truncate">
        {call.rules_met != null && call.rules_total != null
          ? `${call.rules_met}/${call.rules_total} rules`
          : ""}
      </span>
      <span className="text-white/60">${call.price_at_call?.toFixed(2)} · {call.call_date?.slice(5)}</span>
      <span className={clsx("text-right", pctColor(call.return_30d))}>{fmtPct(call.return_30d)}</span>
      <span className={clsx("text-right", pctColor(call.alpha_30d))}>
        {call.alpha_30d != null ? `α ${fmtPct(call.alpha_30d)}` : "—"}
      </span>
    </div>
  );
}

export default function TrackRecordLedger() {
  const { data, loading } = useTrackRecord();
  const { ref, inView } = useInView(0.15);

  const buy30 = data?.summary?.buy_30d;
  const hasGrades = (buy30?.count ?? 0) > 0;
  const calls = data?.recent_calls ?? [];
  const total = data?.total_calls_logged ?? 0;

  return (
    <section id="track-record" className="relative z-10 px-6 md:px-8 py-24 max-w-5xl mx-auto">
      <div
        ref={ref}
        style={{
          opacity: inView ? 1 : 0,
          transform: inView ? "none" : "translateY(24px)",
          transition: "opacity 0.6s ease, transform 0.6s ease",
        }}
      >
        <div className="text-center mb-4">
          <p className="font-mono text-[11px] tracking-[0.24em] text-accent-bright/80 uppercase mb-4">Track record</p>
          <h2 className="font-bold tracking-tight text-3xl md:text-[2.75rem] leading-[1.12] text-white">
            The AI grades <span className="text-white/40">its own calls.</span>
          </h2>
          <p className="text-white/55 text-sm md:text-base mt-5 max-w-xl mx-auto leading-relaxed">
            Inside the app, Claude issues buy and sell verdicts on real stocks.
            Every verdict is frozen here the moment it's made — then scored against
            what the market actually did, benchmarked to SPY. Nothing edited, nothing deleted.
          </p>
        </div>

        {/* How the grading works — three steps, so the ledger explains itself */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-8">
          {[
            { n: "01", t: "Verdict issued",  d: "Claude calls BUY or SELL — the stock price is logged at that exact moment" },
            { n: "02", t: "Time passes",     d: "No edits allowed. The call sits in the ledger for 30, 90, then 180 days" },
            { n: "03", t: "Reality grades it", d: "Actual return vs SPY is stamped on — wins in green, losses in red" },
          ].map(s => (
            <div key={s.n} className="flex items-start gap-3 border border-white/[0.07] rounded-xl px-4 py-3.5 bg-white/[0.015]">
              <span className="font-mono text-lg font-bold text-green/40">{s.n}</span>
              <div>
                <p className="font-mono text-[10px] tracking-[0.16em] uppercase text-white/80">{s.t}</p>
                <p className="text-white/45 text-xs leading-relaxed mt-1">{s.d}</p>
              </div>
            </div>
          ))}
        </div>

        {/* Summary tiles */}
        <div className="grid grid-cols-3 gap-3 mt-10 mb-6">
          <div className="glass-card border border-white/10 rounded-xl px-4 py-4 text-center">
            <p className="font-mono text-2xl font-semibold text-white tabular-nums">{loading ? "…" : total}</p>
            <p className="font-mono text-[9px] tracking-[0.2em] text-white/40 uppercase mt-1">Calls logged</p>
          </div>
          <div className="glass-card border border-white/10 rounded-xl px-4 py-4 text-center">
            <p className={clsx("font-mono text-2xl font-semibold tabular-nums",
              hasGrades ? pctColor((buy30?.win_rate ?? 0) - 0.5) : "text-white/35")}>
              {hasGrades && buy30?.win_rate != null ? `${(buy30.win_rate * 100).toFixed(0)}%` : "—"}
            </p>
            <p className="font-mono text-[9px] tracking-[0.2em] text-white/40 uppercase mt-1">Buy win rate · 30d</p>
          </div>
          <div className="glass-card border border-white/10 rounded-xl px-4 py-4 text-center">
            <p className={clsx("font-mono text-2xl font-semibold tabular-nums",
              hasGrades ? pctColor(buy30?.avg_alpha_vs_spy) : "text-white/35")}>
              {hasGrades ? fmtPct(buy30?.avg_alpha_vs_spy) : "—"}
            </p>
            <p className="font-mono text-[9px] tracking-[0.2em] text-white/40 uppercase mt-1">Avg alpha vs SPY · 30d</p>
          </div>
        </div>

        {/* The ledger */}
        <div className="border border-white/10 rounded-2xl overflow-hidden bg-white/[0.015]">
          <div className="grid grid-cols-[92px_88px_1fr_92px_72px_72px] gap-2 px-4 py-2.5 border-b border-white/10 font-mono text-[9px] tracking-[0.18em] text-white/35 uppercase">
            <span>Sym</span><span className="text-center">Call</span><span>Basis</span>
            <span>At call</span><span className="text-right">30d</span><span className="text-right">vs SPY</span>
          </div>

          {calls.length > 0 ? (
            calls.slice(0, 8).map((c, i) => <LedgerRow key={`${c.symbol}-${c.created_at}-${i}`} call={c} index={i} />)
          ) : (
            <div className="px-6 py-12 text-center">
              <p className="font-mono text-xs tracking-[0.2em] text-white/45 uppercase">
                {loading ? "Opening the ledger…" : "Ledger open — awaiting first graded calls"}
              </p>
              {!loading && (
                <p className="text-white/40 text-sm mt-3 max-w-sm mx-auto leading-relaxed">
                  The scoreboard is live — every AI verdict issued in the app lands
                  here, and its first grade arrives 30 days later. Wins and losses both stay.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
