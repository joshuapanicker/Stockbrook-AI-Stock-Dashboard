-- Run in Supabase SQL Editor
-- AI backtest ledger — verdicts the AI produces on RECONSTRUCTED historical
-- dates (core/point_in_time.py), scored against real subsequent price
-- history. A separate table from ai_calls on purpose: ai_calls is the
-- honest, unedited record of real usage ("we log every call we make");
-- this is a much larger, ongoing, synthetically-generated validation
-- sample used to measure verdict quality, and the two must never be
-- presented as the same thing. See core/backtest_ledger.py,
-- scripts/backtest_worker.py, docs/rag-and-model-roadmap.md.

create table if not exists ai_calls_backtest (
  id                 bigserial primary key,
  symbol             text not null,
  action             text not null,             -- 'buy' | 'sell'
  decision           text not null,             -- 'YES' | 'NO'
  price_at_call      numeric,
  spy_at_call        numeric,
  rules_met          int,
  rules_total        int,
  call_date          date not null,             -- the historical date being simulated
  -- Which reconstructed fields were unavailable for this call (e.g.
  -- ["forward_pe", "revenue_growth"]) — so a low rules_met can always be
  -- explained by a real, disclosed data gap rather than hidden.
  data_gaps          text[] not null default '{}',
  filing_form        text,                      -- '10-K' | '10-Q' | null if ungrounded
  filing_date        date,
  -- What the deterministic criteria engine concluded on its own, BEFORE
  -- Claude was asked. Scored against the same forward price history as
  -- the AI's verdict, so the two can be compared directly: the question
  -- isn't only "was the AI right", it's "did asking the AI beat just
  -- following the rules". Without this the track record can't separate
  -- the model's contribution from the screener's.
  criteria_passed    boolean,
  -- Figures in the model's answer that trace back to nothing in its
  -- prompt. Not proof of fabrication (dividing two provided numbers
  -- yields a third that isn't literally present), but the RATE across
  -- many calls measures whether it's reading its inputs or reaching.
  -- NULL means "not audited", which is NOT the same as '{}' ("audited,
  -- nothing flagged") — defaulting to '{}' silently counted every row
  -- written before the audit existed as a clean result.
  unsourced_numbers  text[],
  created_at         timestamptz not null default now(),
  unique (symbol, action, call_date)
);

create index if not exists ai_calls_backtest_created_idx
  on ai_calls_backtest (created_at desc);
create index if not exists ai_calls_backtest_call_date_idx
  on ai_calls_backtest (call_date);

-- Same policy as ai_calls: only the service role writes and reads. Served
-- through our own API, never queried directly by the frontend.
alter table ai_calls_backtest enable row level security;

-- ── Migration, 2026-09-15 ───────────────────────────────────────────────
-- For a table created before criteria_passed / unsourced_numbers existed.
-- Safe to run repeatedly, and safe to run on a fresh table created by the
-- statement above (both are no-ops there). Rows written before this keep
-- NULL/empty and are simply excluded from the comparison arms rather than
-- counted as a passing baseline or a clean audit.
alter table ai_calls_backtest
  add column if not exists criteria_passed boolean;
alter table ai_calls_backtest
  add column if not exists unsourced_numbers text[];

-- ── Migration, 2026-09-16 ───────────────────────────────────────────────
-- Two corrections to the audit column, both found by checking the first
-- real results instead of trusting them.
--
-- 1. It was NOT NULL DEFAULT '{}', so every row written before the audit
--    existed read as "audited, nothing flagged". The denominator was 600+
--    rows that were never actually checked. NULL now means "not audited".
-- 2. The first 8 genuinely-audited rows were checked by a version that
--    compared numbers as strings, so a price correctly rounded to the
--    cent (276.96 against a stored 276.957352118569) was flagged as
--    invented. Those flags are unreliable.
--
-- Both are cleared rather than kept: the verdicts, criteria_passed and
-- returns on these rows are all still valid, only the audit field is
-- untrustworthy, and claiming an audit that didn't happen is worse than
-- admitting the sample starts now.
alter table ai_calls_backtest
  alter column unsourced_numbers drop not null;
alter table ai_calls_backtest
  alter column unsourced_numbers drop default;
update ai_calls_backtest set unsourced_numbers = null;
