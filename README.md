# Portfolio Agent

A personal investment research assistant that runs a structured multi-analyst pipeline on your portfolio holdings and watchlist tickers. It pulls financial data from multiple public sources, runs a panel of four AI specialists through a weighted deliberation, stores predictions in a local SQLite database, and measures its own accuracy over time.

---

## What it does

- **Daily pipeline** — fetches fundamentals (EDGAR filings), sell-side research (Finnhub consensus), news (RSS + Finnhub), and macroeconomic data (FRED) for every ticker in your portfolio and watchlist
- **APEX predictions** — four analyst roles (Fundamental, Research, Macro, News) score their domain 1–10; a dynamic weight engine blends the scores into a composite recommendation across multiple time horizons (5d, 21d, 63d, 250d)
- **Event-driven triggers** — severity-3 events (earnings releases, SEC 8-K filings, major rating changes, material news) fire intraday predictions outside the normal batch schedule
- **Prediction history** — every recommendation is written to SQLite with the start price; outcome is evaluated when the horizon matures using actual closing prices
- **Validation layer** — nightly job computes directional accuracy, high/low conviction accuracy, Brier score, and log-loss; results are broken down by horizon, ticker, and model
- **Streamlit dashboard** — browser UI for browsing all database tables, reading predictions, viewing the run schedule, chatting with the agent, and monitoring validation metrics

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
| `OPENAI_API_KEY` | Recommended | GPT-4o fallback if Claude times out; APEX randomly selects between the two |
| `FRED_API_KEY` | Recommended | Macroeconomic data (rates, inflation, labor, credit). Free at [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) |
| `FINNHUB_API_KEY` | Recommended | Sell-side consensus, price targets, news, SEC filings. Free tier available at [finnhub.io](https://finnhub.io) |
| `EDGAR_USER_AGENT` | Optional | SEC EDGAR filing pull (`Firstname Lastname email@domain.com` format) |
| `GROQ_API_KEY` | Optional | Additional model provider via LiteLLM |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Optional | Gemini models via LiteLLM |
| `CEREBRAS_API_KEY` | Optional | Additional model provider via LiteLLM |
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

### Run the dashboard

```bash
./run_web.sh
# opens at http://localhost:8501
```

### Run the daily pipeline manually

```bash
./run_daily.sh          # full pipeline: EDGAR check + fundamentals + research + news + APEX
python main.py --daily  # same, inline
```

### Schedule automated runs (macOS launchd or cron)

The pipeline is designed to run on a daily cadence. Suggested cron schedule (CST):

```cron
30  6  * * 1-5   /path/to/portfolio-agent/run_daily.sh        # morning batch
0   17 * * 1-5   /path/to/portfolio-agent/run_validate.sh     # after-close validation
```

The web UI Schedule page can also trigger manual runs directly from the browser.

---

## Data sources

| Source | What it provides | Notes |
|---|---|---|
| **Yahoo Finance** (`yfinance`) | VIX, S&P 500, real-time/delayed quotes, historical prices for validation | 15–20 min delayed during market hours |
| **FRED** (St. Louis Fed) | Fed funds rate, Treasury yields (10Y/2Y/3M), CPI, Core CPI, PCE, PPI, NFP, unemployment, jobless claims, HY credit spread (ICE BofA), DXY | 1-hour in-process cache; free API key required |
| **Finnhub** | Sell-side consensus ratings (1=Strong Buy … 5=Strong Sell), analyst price targets, SEC 8-K/10-K/10-Q filings, news headlines, economic calendar | Free tier has rate limits; some features require paid plan |
| **EDGAR** (SEC.gov) | 10-K and 10-Q filing detection and financial data extraction | Requires `EDGAR_USER_AGENT` in `.env` |
| **RSS feeds** | Supplemental news headlines per ticker | No API key required |
| **Computed / hard-coded** | FOMC meeting dates (2026), NFP/CPI/PCE estimated release dates | Deterministic; updated manually each year |

All data is stored locally in `data/portfolio.db` (SQLite). Nothing is sent to external servers except the API calls listed above.

---

## Pipeline phases

Each daily run executes in sequence:

1. **EDGAR check** — detects new 10-K/10-Q filings; triggers a fundamentals refresh if a new filing is found
2. **Fundamentals** — extracts revenue, net margin, FCF, debt/equity from the latest filing; stores in `fundamentals` table
3. **Research** — fetches Finnhub analyst consensus, price targets, and recent rating changes; stores in `research` table
4. **News** — pulls and triages headlines from RSS + Finnhub; runs an LLM news-scoring pass on material items; stores in `news_daily` table
5. **APEX** — runs the 4-analyst panel for each portfolio ticker on today's scheduled horizons; stores recommendations in `predictions` table

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

**Event-driven overrides** — severity-3 events (major earnings beat/miss, material 8-K, significant rating change, macro surprise) fire an immediate intraday APEX run regardless of the schedule. Watchlist tickers are promoted to portfolio treatment for that run.

Batch times (CST): Morning 06:30 · Intraday checks 11:00, 13:00, 15:00 · Evening 17:30.

---

## Model providers

APEX predictions use a randomly selected primary model with automatic fallback:

| Role | Primary | Fallback |
|---|---|---|
| APEX prediction | Claude Sonnet (Anthropic) | GPT-4o (OpenAI) |

Selection is random per run; if the chosen model times out (90 s) or errors, the other model is tried. If both fail, the ticker is skipped for that run — no placeholder is written to the database.

Other pipeline phases (fundamentals scoring, news scoring, research summarization) use lighter models configured via `.env`. The macro snapshot, weight computation, and validation are fully deterministic — no LLM is involved.

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
