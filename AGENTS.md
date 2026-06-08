# AGENTS.md - TradingAgents-AShare

This file is the working guide for AI coding agents in this repository.

## Project Snapshot

TradingAgents-AShare is an A-share intelligent research SaaS product. It runs a
multi-agent research workflow with FastAPI, a React/Vite web UI, SQLite storage,
optional Redis job state, scheduled analysis, portfolio tracking, and report
management.

Core user flow:

1. User submits a natural-language analysis request.
2. The backend resolves symbol, date, horizon, and user context.
3. A background job runs the TradingAgents graph.
4. Agent status and report chunks stream to the frontend through SSE.
5. Results are persisted as reports and can be revisited from the UI.

## Repository Map

| Area | Path | Notes |
| --- | --- | --- |
| FastAPI backend | `api/main.py` | API routes, runtime config, job startup, SSE streaming |
| Job state | `api/job_store.py`, `api/job_store_redis.py` | In-memory/Redis job metadata and event streams |
| Database | `api/database.py` | SQLAlchemy models and migrations-on-startup |
| Reports | `api/services/report_service.py` | Report persistence, recovery, structured extraction |
| Scheduler | `scheduler/main.py` | Independent scheduled-analysis process |
| Board-gold scanner | `api/services/board_gold_service.py`, `frontend/src/pages/GoldBoard.tsx` | Local parquet pattern scanner from board_has_gold |
| Core graph | `tradingagents/graph/` | LangGraph setup, propagation, data collection |
| Analyst agents | `tradingagents/agents/analysts/` | Market, fundamentals, macro, smart money, etc. |
| Data providers | `tradingagents/dataflows/providers/` | AkShare, BaoStock, yfinance, Alpha Vantage |
| Frontend | `frontend/src/` | React/Vite SPA |
| Frontend state | `frontend/src/stores/analysisStore.ts` | Current job, agents, streamed report, chat state |
| Frontend API | `frontend/src/services/api.ts` | REST client and base URL resolution |
| Tests | `tests/` | API smoke, job store, data provider, report recovery |

## Commands

Use `uv` for backend work. The repository may have a local `.venv` managed by
`uv sync`.

```bash
# Install/sync Python dependencies
uv sync

# Run backend
uv run python -m uvicorn api.main:app --port 8000

# Run scheduler
uv run python -m scheduler.main

# Run backend tests
uv run pytest -q

# Run focused tests
uv run pytest -q tests/test_api_smoke.py
uv run pytest -q tests/test_realtime_quote_provider.py tests/test_data_collector.py
uv run pytest -q tests/test_board_gold_service.py tests/test_board_gold_api.py

# Frontend
cd frontend
npm install
npm run build
npm run lint
npm run dev -- --host 127.0.0.1
```

If `uv run` fails in a sandbox because it cannot access `~/.cache/uv`, rerun the
same command with the required approval instead of switching to a different
Python environment.

## Runtime Configuration

Important environment variables:

```bash
TA_API_KEY=
TA_BASE_URL=https://api.openai.com/v1
TA_LLM_PROVIDER=openai
TA_LLM_QUICK=gpt-4o-mini
TA_LLM_DEEP=gpt-4o
TA_APP_SECRET_KEY=
TA_JOB_TIMEOUT=1800
DATABASE_URL=sqlite:///./tradingagents.db
REDIS_URL=
TA_BOARD_GOLD_DATA_DIR=./data/board_gold
TA_BOARD_GOLD_RESULTS_DIR=./board_gold_results
TA_BOARD_GOLD_CACHE_SCRIPTS_DIR=./cache/board_gold/scripts
```

Supported `TA_LLM_PROVIDER` values are:

- `openai`
- `anthropic`
- `google`
- `xai`
- `openrouter`
- `ollama`

OpenAI-compatible vendors such as DeepSeek, DashScope, Moonshot/Kimi, Zhipu, and
SiliconFlow should still use `TA_LLM_PROVIDER=openai` and set `TA_BASE_URL` to
the vendor endpoint.

Never commit `.env`, `.env.local`, personal API keys, database files, or generated
runtime secrets.

## Backend Notes

- Authentication is bearer-token based. Web login uses JWT; API integrations can
  use generated API tokens.
- `POST /v1/analyze` creates a background job and returns `job_id`.
- `GET /v1/jobs/{job_id}` is the polling recovery endpoint.
- `GET /v1/jobs/{job_id}/events` streams named SSE events.
- `GET /v1/jobs/{job_id}/result` only works once the job is completed.
- Chat-driven analysis uses `/v1/chat/completions` and streams the same job
  events after symbol/date intent parsing.
- Completed/failed in-memory jobs have a TTL; reports are the durable record.
- Production deployments must set `TA_APP_SECRET_KEY`; changing it after users
  have saved encrypted model keys can break decryption unless migration logic is
  involved.

## Frontend Notes

- The frontend assumes API routes are served from the same origin unless
  `VITE_API_URL` is set.
- Do not casually change Vercel rewrites for `/v1` and `/uploads`.
- The analysis page has two visible state surfaces:
  - left chat/report stream (`chatMessages`, streamed sections)
  - right workflow visualization (`agents`, `isAnalyzing`, `currentHorizon`)
- Keep those surfaces synchronized through backend job status and SSE events.
- Native `EventSource` cannot send bearer headers. If an authenticated SSE
  reconnect is needed, use fetch-based SSE parsing.
- Frontend build is the most reliable baseline check: `cd frontend && npm run build`.
- Full `npm run lint` may surface existing React compiler rule violations in
  unrelated files; for scoped edits, also lint the files you touched.

## Data Provider Notes

Default A-share data routing uses AkShare/BaoStock/yfinance fallback chains.
Trace logs are controlled by `TA_TRACE`.

Smart-money related tools:

- `get_individual_fund_flow` -> AkShare `stock_individual_fund_flow`, Eastmoney
  host `push2his.eastmoney.com`.
- `get_lhb_detail` -> AkShare `stock_lhb_detail_em(start_date, end_date)` and
  then filter by stock code. Some AkShare versions no longer accept a `symbol`
  keyword here.
- `get_board_fund_flow` -> AkShare `stock_board_industry_fund_flow_em`.

Eastmoney anti-ban rules:

- Treat Eastmoney fund-flow endpoints as rate-sensitive. Do not add tight loops,
  broad symbol sweeps, aggressive retries, or concurrent live probes against
  `push2his.eastmoney.com` / `push2.eastmoney.com`.
- Prefer unit tests with mocked Eastmoney responses. Live checks should be rare:
  use one representative symbol, make one request, inspect the result, and stop.
- Keep fund-flow caching and cooldown safeguards intact. Current knobs are
  `TA_FUND_FLOW_CACHE_TTL`, `TA_FUND_FLOW_FAILURE_COOLDOWN`, and
  `TA_FUND_FLOW_MIN_INTERVAL`.
- On live failures such as `ProxyError`, DNS errors, `curl: (52) Empty reply from
  server`, or Eastmoney empty JSON, do not immediately retry in a loop. Record the
  failure, respect the cooldown, and verify proxy/network state separately.
- If changing provider code, preserve the AkShare path plus the conservative
  Eastmoney direct fallback, and keep `--noproxy '*'` for the direct curl path so a
  broken local proxy does not poison the fallback.

When fund-flow or LHB data is unavailable, prefer an explicit data-quality note
and use price/volume feedback from the same `DataCollector` pool as weak
evidence. Do not let an LLM silently infer institutional flow from a single VWMA
value.

Board-gold scanner notes:

- `api/services/board_gold_service.py` ports the standalone `board_has_gold`
  strategy scanner into this product. Keep it local-cache based: it reads
  parquet files under `TA_BOARD_GOLD_DATA_DIR` and must not fan out live AkShare
  requests during scans.
- Cache refresh is exposed as a one-click automatic task. Keep provider routing
  behind the backend: legacy scripts run from `TA_BOARD_GOLD_CACHE_SCRIPTS_DIR`,
  while the internal `baostock_daily` task writes BaoStock daily data into the
  same `stock_daily/` and `stock_daily_raw/` parquet cache. The frontend should
  not ask users to choose provider scripts or stock-count limits for normal
  refreshes. Preserve conservative sleeps, single-worker execution, consecutive
  failure stops, and detailed logs.
- The frontend route is `/gold-board`; keep the UI aligned with the existing
  dense slate workbench style rather than the old Flask/Jinja templates.
- Do not commit `data/board_gold/` or `board_gold_results/`; both are local
  runtime artifacts.

## Testing Guidance

Pick tests based on the changed surface:

| Change | Tests |
| --- | --- |
| API routes or job recovery | `uv run pytest -q tests/test_api_smoke.py tests/test_job_store.py` |
| Redis job store | `uv run pytest -q tests/test_job_store_redis.py` |
| Report recovery/persistence | `uv run pytest -q tests/test_report_recovery.py` |
| Data provider logic | `uv run pytest -q tests/test_realtime_quote_provider.py tests/test_data_collector.py` |
| Board-gold scanner | `uv run pytest -q tests/test_board_gold_service.py tests/test_board_gold_api.py` |
| Scheduler | `uv run pytest -q tests/test_scheduled_queue.py tests/test_watchlist_scheduled.py` |
| Frontend state/UI | `cd frontend && npm run build` plus scoped eslint |

For live AkShare/Eastmoney checks, distinguish code bugs from environment
network/proxy failures. A `ProxyError` or DNS failure against Eastmoney is an
environment connectivity issue; an unexpected keyword argument is a code/API
compatibility issue.

For fund-flow live checks specifically, do not run repeated probes just to make a
green result appear. The anti-ban posture is part of correctness for this
project.

## Coding Conventions

- Keep changes scoped to the requested behavior.
- Prefer existing helpers and local patterns over new abstractions.
- Use structured parsers/APIs for data handling instead of ad hoc string slicing
  when a better option exists.
- Do not revert unrelated dirty work in the tree.
- Avoid committing generated build output, caches, `.venv`, or local databases.
- When editing frontend UI, preserve the dense, work-focused product style.
- When changing agent prompts or data fallbacks, keep uncertainty explicit and
  avoid overstating investment conclusions.

## Common Pitfalls

- Do not treat a persisted chat message as proof that a job is still running.
  Always reconcile with `/v1/jobs/{job_id}` when recovering after refresh.
- Do not drop `job_id` during streaming startup; refresh recovery depends on it.
- Do not mark a job completed before final post-processing and report persistence
  have finished, or the SSE stream may close before `job.completed` reaches the UI.
- Do not assume every AkShare function signature is stable across versions.
- Do not submit `.env.local` or personal credentials.
- Do not make production deployments without `TA_APP_SECRET_KEY`.

## Licensing And Product Notes

The original TradingAgents core lineage is Apache 2.0. Newer product modules and
major rewrites in this repository are PolyForm Noncommercial 1.0.0. Keep license
boundaries in mind when moving code between core and product surfaces.

The live product is referenced in the README as <https://app.510168.xyz>.
