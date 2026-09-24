# Portfolio Agent

A personal investment research assistant that runs a structured multi-analyst pipeline on your portfolio holdings and watchlist tickers. It pulls financial data from multiple public sources, runs a four-role AI analyst panel through a weighted deliberation, stores predictions in a local SQLite database, and measures its own accuracy over time.

---

## What it does

- **Daily pipeline** — fetches fundamentals (EDGAR XBRL + 8-K guidance), sell-side research (Finnhub consensus), news (RSS + Finnhub), short interest (FINRA, twice-monthly refresh), and macroeconomic data (FRED) for every ticker in your portfolio and watchlist
- **APEX predictions** — four analyst roles (Fundamental, Research, Macro, News) each score their domain 1–10; a dynamic weight engine blends the scores into a composite recommendation across multiple time horizons (5d, 21d, 63d, 250d)
- **Event-driven triggers** — material news (severity ≥ 3, detected from the news filter log) fires intraday predictions outside the normal batch schedule. Earnings, SEC 8-K, rating-change, and macro-event detectors are wired into the pipeline but not yet implemented (they return empty lists today)
- **8-K guidance extraction** — each fundamentals run fetches the latest EX-99.1 exhibit from SEC EDGAR, extracts the forward guidance/outlook section, and passes it into the Fundamental analyst's prompt. LLM outputs a `guidance_direction` (raised / maintained / lowered / withdrawn / none); text and direction are stored in the `fundamentals` table
- **Short interest (FINRA)** — settlement dates are computed deterministically (mid-month + end-of-month, with an 8-business-day publication lag). When new data is due, one paginated FINRA API call fetches the full file; rows are filtered to your ticker universe and stored in `short_interest`. Trend direction and days-to-cover are computed in Python and injected into the Research prompt
- **Prediction history** — every recommendation is written to SQLite with the start price; outcome is evaluated when the horizon matures using actual closing prices
- **Validation layer** — nightly job computes directional accuracy, high/low conviction accuracy, Brier score, and log-loss; results are broken down by horizon, ticker, and model
- **Streamlit dashboard** — seven-page browser UI covering database tables, predictions, run schedule, portfolio view, validation metrics, and an AI chat interface

## What it does NOT do

- **No order execution.** The system never connects to a broker, never places trades, and has no access to your brokerage accounts. All output is read-only research.
- **No real-time price feeds.** Prices are fetched from Yahoo Finance on a delayed basis (typically 15–20 minutes during market hours). Intraday precision is not available.
- **No options, derivatives, or alternatives.** Coverage is equities only.
- **No tax advice.** The system has no knowledge of your cost basis, holding periods, wash-sale rules, or tax situation.
- **No guarantee of accuracy.** The validation layer exists precisely because forecasts are unreliable. Past accuracy in the validation database does not predict future accuracy.
- **Not a registered investment adviser.** See the disclaimer at the bottom of this file.

---

## Setup

### Prerequisites

- Python 3.11+
- A virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Environment variables

Copy `.env.example` to `.env` and fill in the values you want to use. Keys marked **required** will cause the pipeline to fail without them; optional keys enable additional data sources or model providers.

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | Claude models (Sonnet) for APEX predictions |
| `OPENAI_API_KEY` | Recommended | GPT-4o for APEX predictions; APEX randomly selects between Claude Sonnet and GPT-4o each run |
| `FRED_API_KEY` | Recommended | Macroeconomic data (rates, inflation, labor, credit). Free at [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) |
| `FINNHUB_API_KEY` | Recommended | Sell-side consensus, price targets, news, SEC filings. Free tier available at [finnhub.io](https://finnhub.io) |
| `FINRA_API_KEY` | Recommended | Short interest data (FINRA Datasets API). Bearer token; twice-monthly pulls |
| `EDGAR_USER_AGENT` | Optional | SEC EDGAR filing pull (`Firstname Lastname email@domain.com` format) |
| `GROQ_API_KEY` | Optional | Additional model provider in the multi-model failover chain |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Optional | Gemini models; first in the failover chain |
| `CEREBRAS_API_KEY` | Optional | Additional model provider in the failover chain |
| `OPENROUTER_API_KEY` | Optional | Multi-provider routing via LiteLLM |

### Configure your portfolio and watchlist

Edit `config/portfolio.yaml` — add your holdings with ticker, share count, and optionally average cost and broker:

```yaml
holdings:
  - ticker: AAPL
    shares: 50
    avg_cost: 145.00
    account: Fidelity
    broker: fidelity
```

Edit `config/watchlist.yaml` to add tickers you want tracked but don't hold. Group them by category if you want organized filtering in the UI.

### Web login (Entra ID)

The web dashboard requires signing in with Entra ID (`st.login("entra")`) plus an allow-list check — nobody can reach a page without both a real login and an invited, enabled row.

1. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in `cookie_secret` (any long random string), `client_id`, `client_secret`, and `server_metadata_url` from your Entra app registration. `.streamlit/secrets.toml` is gitignored — never commit it.
2. Add `http://localhost:8501/oauth2callback` as a redirect URI on that app registration for local runs.
3. Seed yourself as the first admin — **`--owner` must be `portfolio_agent.domain.LOCAL_OWNER`** (currently `sumanth_b`), not a placeholder string, or your session's identity won't match any of your existing data:
   ```bash
   python main.py --auth-invite you@example.com --owner sumanth_b --role admin
   ```
4. `./run_web.sh`, sign in, then invite/disable further users from the Users section on the Profile page (admin only).

`--auth-list` and `--auth-disable EMAIL` manage users from the CLI the same way.

### Run the dashboard

```bash
./run_web.sh
# opens at http://localhost:8501
```

### Run the daily pipeline manually

```bash
./run_daily.sh          # full morning batch
python main.py --daily  # same, inline
```

### Schedule automated runs (macOS launchd or cron)

The pipeline is designed to run on a daily cadence. Suggested cron schedule (CST):

```cron
30  6  * * 1-5   /path/to/portfolio-agent/run_daily.sh        # morning batch
0   17 * * 1-5   /path/to/portfolio-agent/run_validate.sh     # evening batch (validation + calibration + snapshot)
```

The web UI Schedule page can also trigger manual runs directly from the browser.

---

## Data sources

| Source | What it provides | Notes |
|---|---|---|
| **Yahoo Finance** (`yfinance`) | VIX, S&P 500, real-time/delayed quotes, historical prices for validation, short interest fallback (`sharesShort`, `shortRatio`) | 15–20 min delayed during market hours |
| **FRED** (St. Louis Fed) | Fed funds rate, Treasury yields (10Y/2Y/3M), CPI, Core CPI, PCE, PPI, NFP, unemployment, jobless claims, HY credit spread (ICE BofA), DXY | 1-hour in-process cache; free API key required |
| **Finnhub** | Sell-side consensus ratings (1=Strong Buy … 5=Strong Sell), analyst price targets, news headlines, economic calendar | Free tier has rate limits; some features require paid plan |
| **EDGAR** (SEC.gov) | 10-K / 10-Q XBRL financial data; latest 8-K EX-99.1 exhibit for forward guidance extraction | Requires `EDGAR_USER_AGENT` in `.env` |
| **FINRA Datasets API** | Short interest by settlement date: shares short, previous shares, % change, avg daily volume, days-to-cover | Bearer token (`FINRA_API_KEY`); ~twice-monthly refresh; yfinance used as fallback |
| **RSS feeds** | Supplemental news headlines per ticker | No API key required |
| **Computed / hard-coded** | FOMC meeting dates (2026), NFP/CPI/PCE estimated release dates, FINRA settlement date calendar | Deterministic; updated manually each year |

All data is stored locally in `data/portfolio.db` (SQLite). Nothing is sent to external servers except the API calls listed above.

---

## Pipeline phases

Each morning run executes in sequence:

1. **News** — pulls and triages headlines from RSS + Finnhub; runs a batched LLM news-scoring pass on material items; stores in `news_daily` table
2. **Short Interest Check** — computes the current FINRA settlement date (deterministic, no network); if new data is due, pulls it in one paginated batch and writes to `short_interest` table; otherwise skips (cheap single DB query)
3. **Research** — fetches Finnhub analyst consensus, price targets, and recent rating changes; injects pre-computed short interest trend summary per ticker; stores in `research` table
4. **Fundamentals** — extracts revenue, net margin, FCF, debt/equity from the latest EDGAR filing; fetches the latest 8-K EX-99.1 exhibit and extracts the guidance/outlook section; passes guidance text and financial data together to the LLM, which outputs `guidance_direction`; stores in `fundamentals` table
5. **APEX** — runs the 4-analyst panel for each portfolio ticker on today's scheduled horizons; reads macro snapshot live at prediction time; stores recommendations in `predictions` table

**Event detection** runs at the start of the APEX phase. `detect_material_news` reads the news filter log and fires event-driven predictions for high-severity items. `detect_earnings`, `detect_sec_8k`, `detect_rating_changes`, and `detect_macro_events` are implemented as stubs and return no events today.

The **Macro snapshot** is pulled live at APEX time (not cached to disk) and injected into each ticker's context alongside the stored fundamentals/research/news.

---

## Prediction schedule and horizons

Predictions are generated on a cadence keyed to the horizon length, not every day:

| Horizon | Label | Scheduled on |
|---|---|---|
| 5 days | ~1 week | Every Monday |
| 21 days | ~1 month | Every Monday |
| 63 days | ~1 quarter | First trading day of each month |
| 250 days | ~1 year | First trading day of each quarter |

**Event-driven overrides** — material news events above the severity threshold fire an immediate intraday APEX run regardless of the schedule. Watchlist tickers are promoted to portfolio treatment for that run.

Batch times (CST): Morning 06:30 · Intraday checks 11:00, 13:00, 15:00 · Evening 17:30.

---

## Model providers

### APEX predictions

APEX selects a primary model randomly each run and falls back to the other on timeout (90 s) or error. If both fail, the ticker is skipped — no placeholder is written to the database.

| Role | Primary | Fallback |
|---|---|---|
| APEX prediction | Claude Sonnet (Anthropic) | GPT-4o (OpenAI) |

### Batch pipeline phases (fundamentals, research, news)

These phases use a multi-model failover chain. Models are tried in order until one succeeds:

**Gemini → Groq → Cerebras → OpenRouter → GPT-4o-mini → Claude-Haiku → GPT-4o → Claude-Sonnet**

Keys for unused providers can be omitted; those models are skipped. The macro snapshot, dynamic weight computation, short interest trend computation, and validation are fully deterministic — no LLM is involved.

---

## Short interest (FINRA)

FINRA publishes settlement-date short interest data twice per month: approximately the 15th and the last business day of each month. Data is available ~8 business days after the settlement date.

**How it works:**

1. At the start of each pipeline run, `_latest_settlement_date()` computes the most recently published settlement date using only calendar math — no network call
2. If that date is already in the local DB (`short_interest_meta`), the step is skipped in milliseconds
3. If new data is due, one paginated FINRA API call downloads the full file (~8,000 rows); rows are filtered to your ticker universe in Python and written to `short_interest`
4. Trend direction (rising / stable / falling) and squeeze pressure (high / elevated / low) are computed from the last 2–3 settlement records entirely in Python — no LLM arithmetic
5. The compact summary is injected into each ticker's Research prompt block; the Research analyst scores short interest as a signal alongside consensus and price targets
6. **Fallback**: if the DB has no history for a ticker (e.g. first run before a refresh cycle fires), `yfinance` `Ticker.info` provides `sharesShort`, `shortRatio`, and `shortPercentOfFloat` with no new dependencies

---

## 8-K guidance extraction (EDGAR)

Each fundamentals run calls `get_earnings_guidance(ticker)` in `tools/edgar.py`:

1. Fetches the latest 8-K accession number from `data.sec.gov/submissions/{CIK}.json`
2. Parses the filing folder HTML at `www.sec.gov/Archives/edgar/data/` to find the EX-99.1 exhibit filename
3. Downloads the exhibit, strips HTML tags, decodes entities, and extracts the forward guidance/outlook section using priority-ordered regex patterns (most specific phrases matched first)
4. The guidance text (capped at 2,500 characters) is appended to the fundamentals data block and passed to the LLM
5. The LLM outputs `guidance_direction`: `"raised"` / `"maintained"` / `"lowered"` / `"withdrawn"` / `"none"`. A raise can add 1–2 points to `fundamental_score`; a lowered/withdrawn guidance subtracts 1–2 points from what trailing numbers alone would justify
6. `guidance_text`, `guidance_date`, and `guidance_direction` are stored in the `fundamentals` table

Note: some companies (e.g. Apple since 2020) do not provide quantitative quarterly guidance. In those cases the LLM sets `guidance_direction: "none"` and scores purely on trailing financials.

---

## Dynamic weight engine

The four analyst scores are not combined with fixed weights. The weight engine adjusts based on market conditions at prediction time:

- **Horizon** — shorter horizons (5d) weight News and Research heavily; longer horizons (63d+) shift weight to Fundamentals and Macro
- **VIX level** — elevated volatility boosts the Macro analyst's weight
- **Yield curve** — an inverted curve shifts additional weight to Macro
- **S&P trend** — strong 1-month momentum tilts toward News and Research
- **Event type** — earnings releases boost Fundamental and Research weights; macro surprises boost Macro weight

The actual weights used for each prediction are stored in the database alongside the recommendation and are visible in the Predictions page.

---

## Validation methodology

Every prediction is evaluated when its horizon matures:

**Layer 1 — Outcome assignment** (nightly, no LLM)
- Fetches the actual closing price on the maturity date from Yahoo Finance
- Computes actual return: `(end_price − start_price) / start_price`
- Assigns a directional outcome: correct if the predicted direction matches the sign of the actual return
- Maps actual return to one of five buckets: strong\_down (<−5%), moderate\_down (−5% to −1%), flat (−1% to +1%), moderate\_up (+1% to +5%), strong\_up (>+5%)
- Computes Brier score and log-loss from the probability distribution the model provided at prediction time

**Layer 2 — Rolling metrics** (nightly, incremental)
- Aggregates L1 outcomes into directional accuracy, high/low conviction accuracy, Brier score, and log-loss
- Broken down by horizon, ticker segment, and model (Claude vs GPT-4o)
- Baseline for Brier score is 0.25 (coin-flip); baseline for log-loss is ln(2) ≈ 0.693

**Layer 3 — Pattern analysis** (weekly, LLM-assisted)
- Identifies systematic biases: which horizons are weakest, which conditions the model over- or under-weights

Validation results are visible in the Validation tab of the dashboard. The Brier score and accuracy numbers are the only objective measure of how well the system is performing — check them before acting on any recommendation.

---

## How to interpret recommendations

The system outputs one of five labels: **STRONG\_BUY**, **BUY**, **HOLD**, **SELL**, **STRONG\_SELL**. These map from the composite score:

| Composite (1–10) | Label |
|---|---|
| ≥ 7.5 | STRONG\_BUY |
| 6.0 – 7.4 | BUY |
| 4.0 – 5.9 | HOLD |
| 2.5 – 3.9 | SELL |
| < 2.5 | STRONG\_SELL |

**Confidence** (1–10) reflects analyst agreement, not the strength of the signal. A high-confidence HOLD is not a buy signal. A low-confidence STRONG\_BUY means the analysts disagreed significantly.

**Conviction score** per horizon reflects how decisive the model was within that time window. Low conviction with a spread probability distribution means the model is uncertain — treat those predictions as noise.

**Probability distribution** — each prediction includes a 5-bucket probability distribution over return ranges. A distribution that is spread across all five buckets (e.g., 20/20/20/20/20) means the model has essentially no view. A distribution concentrated in one or two buckets (e.g., 5/10/15/45/25) is a stronger signal, all else equal.

**Data gaps** — if fundamentals or research data is missing for a ticker, the analyst for that domain is score-capped (typically at 5/10) and states "No data." A recommendation generated with two or more data gaps is substantially less reliable.

**Useful questions to ask before acting:**
- Is the prediction from the last day or week, or is it stale?
- What is the validation accuracy for this horizon over the last 60 days?
- Do all four analysts agree, or is one sharply diverging?
- Are there upcoming FOMC decisions, earnings, or macro releases that the model may not yet have priced in?
- What is the macro regime? A RISK\_OFF or HIGH\_VOLATILITY regime degrades the reliability of bullish signals significantly.

---

## Financial disclaimer

**This software is for informational and educational purposes only. It is not investment advice, and nothing generated by this system should be interpreted as a recommendation to buy, sell, or hold any security.**

- This tool is not registered with the SEC, FINRA, or any other regulatory body.
- The authors of this software are not registered investment advisers, broker-dealers, or financial planners.
- Outputs are generated by large language models that can hallucinate, misinterpret data, or produce confidently wrong conclusions.
- Historical validation accuracy does not guarantee future performance. Financial markets are non-stationary; past patterns break without warning.
- This system has no knowledge of your personal financial situation, risk tolerance, tax obligations, investment horizon, or liquidity needs.
- You are solely responsible for any investment decisions you make. Consult a licensed financial adviser before acting on any output from this system.

**Use at your own risk.**
