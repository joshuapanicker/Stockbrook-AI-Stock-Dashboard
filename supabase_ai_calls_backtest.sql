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
