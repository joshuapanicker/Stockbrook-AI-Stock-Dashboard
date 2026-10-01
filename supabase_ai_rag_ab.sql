-- Run in Supabase SQL Editor
-- Paired A/B test of RAG grounding: the same scenario asked twice, once
-- with the SEC filing excerpt in the prompt and once without.
--
-- Why a separate table from ai_calls_backtest: there, one row is one
-- call. Here one row is one SCENARIO with two verdicts, which is the
-- whole point — each scenario is its own control, so the comparison
-- carries no confound from date, company size or filing availability.
--
-- The backtest could only compare grounded calls against ungrounded
-- ones, and grounding isn't randomly assigned: a call is ungrounded when
-- no filing existed for that date and company, which skews older (SEC's
-- recent-filings window runs out) and toward smaller and foreign firms.
-- That comparison can't attribute anything to RAG. This one can.
--
-- Outcomes belong to the scenario, not the arm — same stock, same date —
-- so one set of returns scores both verdicts.

create table if not exists ai_rag_ab (
  id                     bigserial primary key,
  symbol                 text not null,
  action                 text not null,          -- 'buy' | 'sell'
  call_date              date not null,          -- the historical date simulated
  universe_tier          text,                   -- 'core' | 'broad'

  -- Identical across both arms: only the filing block differs.
  rules_met              int,
  rules_total            int,
  criteria_passed        boolean,
  price_at_call          numeric,
  spy_at_call            numeric,
  criteria_inputs        jsonb,
  filing_form            text,                   -- what was withheld from arm B
  filing_date            date,

  -- Arm A: prompt included the filing excerpt.
  decision_grounded      text,
  response_grounded      text,
  unsourced_grounded     text[],
  -- Arm B: identical prompt, filing excerpt removed.
  decision_ungrounded    text,
  response_ungrounded    text,
  unsourced_ungrounded   text[],

  -- Realised outcome of the scenario, scored once for both arms.
  return_30d             numeric,
  alpha_30d              numeric,
  return_90d             numeric,
  alpha_90d              numeric,
  return_180d            numeric,
  alpha_180d             numeric,
  resolved_at            timestamptz,
  resolve_attempts       int not null default 0,

  created_at             timestamptz not null default now(),
  unique (symbol, action, call_date)
);

create index if not exists ai_rag_ab_unresolved_idx
  on ai_rag_ab (resolved_at) where resolved_at is null;

-- Same policy as the other AI tables: service role only.
alter table ai_rag_ab enable row level security;
