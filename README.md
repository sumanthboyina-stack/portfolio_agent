# APEX

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
- **No options, derivatives, or alternatives.** Coverage is stocks and funds (ETFs, ETNs, mutual funds, target-date funds). Cash, money-market funds and ticker-less funds are held but never forecast.
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
| `OPENAI_API_KEY` | Recommended | GPT-4o, GPT-5 and o3 — the default APEX fallback models (and any challenger), see [Model providers](#model-providers) |
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

### Deploying to Azure App Service

Notes only — no infra is set up by this repo.

- Streamlit's `[auth]` block only ever reads `.streamlit/secrets.toml`, never environment variables. Store the real values as App Service **App Settings** (or Key Vault references), then run `python scripts/write_secrets.py` from the startup command, before Streamlit, to render `secrets.toml` from `AUTH_REDIRECT_URI`, `AUTH_COOKIE_SECRET`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_METADATA_URL`.
- Add the production redirect URI to the app registration: `https://<app-name>.azurewebsites.net/oauth2callback`.
- Enable **WebSockets** on the App Service (Streamlit needs them) and enforce **HTTPS only**.
- Don't also turn on App Service **Easy Auth** — one auth layer only (`st.login`); stacking both breaks the OIDC callback.
- SQLite needs a persistent mount on App Service (the default filesystem is ephemeral across restarts/scale events) — flag this as a follow-up; it isn't auth work.

---

## Holdings taxonomy: what each holding is, and what each account is

**Instruments.** Every security carries an `instrument_type` (`STOCK`, `ETF`, `ETN`, `MUTUAL_FUND`, `MONEY_MARKET`, `TARGET_DATE_FUND`, `BOND`, `CIT`, `CASH`, `OTHER`, `UNKNOWN`), an `asset_class` (`US_EQUITY`, `INTL_EQUITY`, `FIXED_INCOME`, `REAL_ESTATE`, `COMMODITY`, `CASH`, `MULTI_ASSET`, `UNKNOWN`), an optional `sub_asset_class` (e.g. `LARGE_CAP_GROWTH`, `EMERGING_MARKETS`, `TREASURY`, `HIGH_YIELD`), a `leverage_factor` (1.0 normal, 2.0 / 3.0 leveraged, negative = inverse) and, for funds, `expense_ratio` (a fraction), `fund_family` and `category`. The vocabularies are defined once in `portfolio_agent/domain.py`.

- **Automatic classification** (`portfolio_agent/services/instrument_classifier.py`) uses Yahoo Finance's `quoteType` first, then name/category keywords (leveraged/inverse, target-date, money market, ETN). "Short-Term Treasury" and "Ultra-Short Bond" funds are *not* flagged inverse. It runs after an import or a manual add commits — never inside a database transaction — and a failed lookup leaves the instrument as it was (it never raises into an import or the pipeline). Automatic classifications older than 30 days are re-checked.
- **Manual override** (Accounts → *What each holding is*): a classification you set is never overwritten by the automatic check. The instrument row is shared, so an override applies in every account that holds that security. Every change is recorded in the audit log.
- **Backfill** existing holdings once: `python -m scripts.backfill_instrument_classification --dry-run`, then without `--dry-run`. Safe to re-run.
- **Funds with no ticker** (a 401(k) collective investment trust): add them under Accounts → *Add a fund with no ticker*. They are keyed `CIT:<PLAN>-<FUND>`, priced by hand, and never sent to a price provider or the prediction pipeline. An optional **proxy ticker** (e.g. `VOO` for an S&P 500 CIT) is used for analytics only and is never shown as your holding.
- **Portfolio page** shows type, asset class and leverage columns, filters by type / asset class, and groups the allocation chart by sector, asset class or instrument type.

**Accounts.** `account_type` is one of `TAXABLE`, `TRADITIONAL_IRA`, `ROTH_IRA`, `ROLLOVER_IRA`, `SEP_IRA`, `401K`, `ROTH_401K`, `403B`, `457B`, `HSA`, `529`, `CASH`, `OTHER`; every importer maps its output to these (the old `IRA` became `TRADITIONAL_IRA`, and the previous value is kept in `account_type_legacy`). The **tax treatment** shown on each account (taxable / tax-deferred / tax-free) is derived from the type, not stored. Two columns are reserved for later work: `holdings_source` (`snapshot`, today's behaviour, or `ledger`) and `is_paper` — paper accounts are excluded from your holdings, totals, cash and the pipeline universe.

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
4. **Fundamentals** — (funds, money market and cash are left out first — they file nothing with the SEC; see [Instrument routing](#instrument-routing-funds-vs-stocks)) extracts revenue, net margin, FCF, debt/equity from the latest EDGAR filing; fetches the latest 8-K EX-99.1 exhibit and extracts the guidance/outlook section; passes guidance text and financial data together to the LLM, which outputs `guidance_direction`; stores in `fundamentals` table
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

APEX uses one **champion** model for every prediction, so the validation record compares like with like over time. If the champion times out (90 s), hits a provider/quota error, or returns unparseable output, APEX tries each **fallback** model in order. If every model fails, the ticker is skipped — no placeholder is written to the database.

| Setting (`engine_settings`) | Default | Meaning |
|---|---|---|
| `apex_champion_model` | `Claude-Sonnet` | Model used for every prediction |
| `apex_fallback_models` | `["GPT-5", "GPT-4o", "o3"]` | Tried in this order, only when the model before it failed |
| `apex_challenger_model` | none (off) | Optional second model run on the same prompt for comparison |
| `apex_challenger_sample_pct` | `0` | % of tickers (0–100) that also get a challenger run |

No environment variable is needed; the defaults live in `portfolio_agent/tools/apex_dual_run.py`. To change them, call `update_engine_settings(updated_by=..., apex_champion_model=..., apex_fallback_models=[...], apex_challenger_model=..., apex_challenger_sample_pct=...)` from `portfolio_agent.tools.engine_settings_db` (labels must be one of `Claude-Sonnet`, `GPT-4o`, `GPT-5`, `o3`; `apex_challenger_model=None` turns the challenger off).

Every stored prediction records its `prediction_role`:

| Role | Shown to users | Counted in default validation |
|---|---|---|
| `champion` | Yes | Yes |
| `fallback` — champion failed that run | Yes | Yes |
| `challenger` — shadow run on a deterministic sample of tickers | Never | Only on Validation QA → Roles (paired champion-vs-challenger comparison) |
| `legacy_random` — made before champion selection, when each run picked a random model | Yes (history) | Only with **Include legacy predictions** switched on |

Which model ran and why is logged with `event_type` `apex_model_plan` (once per phase), `apex_model_select` (role + reason per attempt), `apex_retry`, `apex_complete`, `apex_exhausted` and `apex_challenger`.

### Batch pipeline phases (fundamentals, research, news)

These phases use a multi-model failover chain. Models are tried in order until one succeeds:

**Gemini → Groq → Cerebras → OpenRouter → GPT-4o-mini → Claude-Haiku → GPT-4o → Claude-Sonnet**

Keys for unused providers can be omitted; those models are skipped. The macro snapshot, dynamic weight computation, short interest trend computation, and validation are fully deterministic — no LLM is involved.

---

## Investor profile, target allocation and emergency reserve

Everything here is **optional** and everyone starts with zero input: a **Moderate** profile and a **$3,000 emergency cash reserve**. Edit it on the **Profile** page. (This is an educational tool, not investment advice.)

- **Three profiles** — Conservative, Moderate, Aggressive — each with a target allocation (US stocks / international stocks / bonds / cash, with a band around each target), concentration limits (position, issuer, sector, share of new cash per candidate), a limit on leveraged/inverse funds, and the instrument types it allows.
- **The quiz** is three optional questions: *when will you need most of this money*, *how would you react to a 20% drop*, *what matters most*. The first sets a **ceiling** (under 3 years → Conservative, 3–10 → Moderate, 10+ → Aggressive); the other two set a **preference** (points 0/1/2 each; 0–1 Conservative, 2–3 Moderate, 4 Aggressive). You get the more careful of the two. A skipped question counts as the middle answer, so skipping the whole quiz is Moderate.
- **Or just pick** a profile. If you answered the time-horizon question and pick something riskier than it suggests, you tick a one-line acknowledgment; that is recorded, audited, and shown as a small flag on your profile.
- **Emergency cash reserve** is one **dollar amount**: yours if you set it, otherwise the global default ($3,000). Only cash in taxable and cash accounts counts toward it — cash inside a 401(k) or IRA isn't available in an emergency without penalties or taxes. It is not a goal and not a number of months. USD only.
- **Custom allocation** (optional) replaces your profile's targets with your own (targets must add up to 100, each band must contain its target). *Use profile default* switches back.
- **Concentration limits** come from your profile (`limits_source = profile`, the default for new users) or from your own numbers on the profile page (`custom`). Everyone who existed before this feature was set to `custom`, so no existing limit changed.
- **Goals** (retirement, house, education, other) are optional notes; nothing reads them yet.

**Where it lives.** No config files: all of it is in the database. The global tables — `risk_quiz_settings` (questions, options, points, cap rule, thresholds), `risk_profile_settings` (one row per profile) and `portfolio_settings` (the default reserve) — are seeded from code constants, versioned (`version`, `updated_at`, `updated_by`), updated with `expected_version`, and can be reset to defaults. They are **admin-only**, enforced in the service (an enabled `auth_users` admin) and not just hidden in the UI; admins edit them under **Profile → Investor settings**. A person's own profile, reserve, custom allocation and goals are private, owner-scoped tables (`investor_profile` keeps history; `target_allocation_custom`; `investment_goals`), written only through `services/investor_service.py` with an audit event per change.

**Effective policy.** `tools/portfolio_policy.py` combines all of the above into one fingerprinted `EffectivePolicy` (the four original limits in the same units, plus profile, source, target bands, leveraged/inverse limit, allowed types, reserve and its source, limits source, and the settings versions used). The fingerprint changes with anything that changes a computation — including the reserve and the settings behind it — and never with your name, employer, notification settings or goals.

**Privacy.** None of this is ever read by the shared market forecasts or the prediction pipeline; a test enforces that boundary.

---

## Fund data and look-through

A fund is not one thing you own — it is a basket. The app opens the baskets so concentration checks count what you really hold.

**Data.** `tools/fund_data_providers.py` defines a `FundDataProvider` interface (profile, holdings, sector weights, asset-class weights, optional country weights). `YFinanceFundProvider` is the one implemented: it exposes only a fund's **top 10 holdings**, so each fund records its `holdings_coverage`; `IssuerCSVProvider` and `NPortProvider` are stubs for fuller data. A failed or missing piece becomes a reason code on the fund, never an exception. Shared tables (market data, no owner): `fund_profiles` (expense ratio for the **exact share class**, family, category, AUM, share class and `share_class_group`, coverage, `as_of`, `source`, `fetched_at`), `fund_holdings`, `fund_sector_weights`, `fund_asset_class_weights`. Refresh (`services/fund_data_service.py`) happens outside any transaction, four funds at a time, guarded by a TTL (ETFs 24 h, mutual funds 168 h by default), and runs in the daily APEX phase.

**Funds with no data** (a 401(k) collective trust, a stable value fund): your own **manual breakdown** (asset-class mix, optional sector mix, from a plan fact sheet) wins, else the data of the instrument's `proxy_ticker` (labeled "via proxy X", analytics only, never a holding), else **UNKNOWN**. A fund's own provider data always comes first. The manual breakdown is private and owner-scoped (`fund_manual_breakdown`, history kept, audited).

**The engine** (`tools/lookthrough.py`, pure and deterministic): per scope (household, each account, each tax treatment; paper accounts excluded) it computes issuer exposure with the funds it came *via*, sector and asset-class weights, international share, coverage %, an overlap report, leveraged/inverse exposure (× `leverage_factor`), and concentration (top 1/5/10, HHI, effective N, normalized by coverage). Whatever a fund's named holdings don't explain stays in a `<FUND>_other` bucket; a fund with no data is `<FUND>_unknown`. Funds inside funds (target-date funds) are opened up to `lookthrough_max_depth` with a cycle guard; missing levels, cycles and depth limits are reason codes.

**In the assessment.** Issuer and sector checks use look-through when it explains at least `lookthrough_min_coverage_pct` (default 80 %) of your money, and each check's `detail` says how the number splits ("87.50% = 75.00% direct + 12.50% via funds"). Below that they use the direct numbers and carry `LOOKTHROUGH_PARTIAL`: over the limit is still reported as breached, but under it is **never** reported as "not breached" — it is UNKNOWN, and sizing is withheld. Two new advisory checks: leveraged/inverse share vs your profile's limit, and whether the instrument type is allowed by your profile. A portfolio with no funds is computed exactly as before. Fund-data fetch times and your manual breakdowns are part of the cache key.

**Staleness.** `tools/price_freshness.py`: a mutual fund prices once a day (NAV posts around 21:00 ET), so yesterday's NAV is current until then; a stock or ETF is judged against the 16:00 close. A missing price date is unknown, never fresh.

**Settings** (all in `portfolio_settings`, admin-only, versioned, reset to defaults — **Profile → Investor settings → Fund data & health**): `etf_data_ttl_hours`, `mutual_fund_data_ttl_hours`, `lookthrough_min_coverage_pct`, `lookthrough_max_depth`, `high_expense_ratio_pct`, `health_snapshot_enabled`.

**Privacy.** The shared fund tables hold market data about the fund only. The module that reads them for the forecast path (`tools/instrument_reference.py`) never imports the private manual-breakdown or proxy logic; a test enforces it. Look-through itself runs on your holdings and is private.

---

## Portfolio Health

A checkup of **your own** portfolio against **your own** profile and limits — the **Health** page (and a card on Today). Educational, not investment, tax or legal advice; it never tells you to trade.

**No single score.** Each check is one of four statuses, and the page shows how many are **ACTION / WATCH / UNKNOWN / GOOD**:

| Status | Meaning |
|---|---|
| **GOOD** | inside your limit or band |
| **WATCH** | close to a limit (default: at 80% of it) or outside a band |
| **ACTION** | over a limit, or far outside a band (default: more than 2 band-widths from the target) |
| **UNKNOWN** | the data to judge it is missing or too partial — **never** shown as GOOD |

**The checks** (household-wide unless noted): *stock / other allocation*, *bonds and international*, *excess cash* (each against your profile's target bands); *emergency reserve*; *concentration* (largest single security, largest issuer and sector **counting what your funds hold**, against your profile's limits); *diversification* (effective number of holdings and average correlation); *leveraged / inverse funds*; *fund fees*; *risk profile* (still on the default → a nudge to take the quiz; above-cap pick acknowledged → WATCH); *restricted list*; *data quality* (unpriced or unclassified holdings, out-of-date prices, stale or missing fund data, CITs / stable value with no breakdown or proxy, partial look-through).

- **Emergency reserve** counts **only** cash and money-market holdings in **taxable and cash accounts** — never a 401(k), IRA, HSA, 529 or paper account. ACTION with the dollar shortfall if below your reserve (your own amount or the default, and it says which); GOOD at or above; UNKNOWN when no cash balance is known (or the only cash is in an account whose type isn't set).
- **Fund fees** show the weighted expense ratio, funds above the "high" line, and the **estimated** annual fund expenses in dollars (labeled as estimated). A cheaper-class note appears only for the **same fund** in another share class we have data for — never a different product, and never a recommendation.
- **Position concentration** looks at single securities; an index fund's own weight is judged through what it holds (issuer / sector look-through). When fund contents are only partly known, a check that would be GOOD becomes UNKNOWN (and a breach is still reported as a breach).
- If too much of the portfolio can't be classified, an "under the band" verdict is UNKNOWN rather than a false alarm (over the band is still over).

**Scopes.** *Household*, each *account*, and each *tax treatment* (taxable / tax-deferred / tax-free). A slice gets the checks that make sense on a slice (concentration, diversification, leveraged / inverse, fees, data quality), judged on its own value against the same limits; targets, the reserve, profile and restricted list belong to the whole household. Paper accounts are excluded everywhere.

**History.** One snapshot per day (taken the first time you open the page or the Today card that day, and by the evening batch), shown as *what changed since the last snapshot* and *since about 30 days ago*. Stored in `health_snapshots` (private, owner-scoped); each new day's snapshot writes an audit event. Turn snapshots off with the `health_snapshot_enabled` setting.

**Notifications.** A check that **becomes ACTION** notifies once — only ACTION, never WATCH, at severity 4, through your own notification settings (channel, minimum severity, quiet hours). The de-duplication key is `health:<check>:<date the run of ACTION began>`, so an ongoing problem doesn't repeat daily; if it clears and comes back it notifies again. (Delivery is the existing structured-log hook.)

**Chat.** The `get_portfolio_health` tool is read-only and owner-scoped: it returns your statuses and summaries, never anyone else's, and takes no snapshot and sends nothing.

**Where the numbers live.** Everything is in the database, none in config files. Your profile, reserve and limits are yours (Profile page). The shared rules — the band multiple, the WATCH fraction, effective-holdings and correlation levels, the unknown-share tolerance, the unpriced and stale-fund-data levels — are columns of `portfolio_settings` (versioned, admin-only, reset to defaults), edited under **Profile → Investor settings → Health thresholds**. All logic is in `tools/portfolio_health.py` (pure checks over gathered inputs); no language model decides any number, label or status. History and notifications go through `services/health_service.py`.

---

## Notifications: channels, contacts, routing and settings

*(Complete: the data model, the outbox, the delivery worker, the email / phone-push senders, the alert sources — market events, Portfolio Health, compliance and the daily digest — and the screens. WhatsApp and SMS are reserved, with no sender.)*

**Channels.** `EMAIL` (Gmail SMTP) and `PUSH` (phone push through [ntfy](https://ntfy.sh)) are the only channels that can ever deliver. `WHATSAPP` and `SMS` exist in the code as stubs and cannot be turned on; `SLACK` is kept only so the old `slack` value still reads. **Alert categories:** `MARKET_EVENT`, `PORTFOLIO_HEALTH`, `COMPLIANCE`, `DAILY_DIGEST`, `SYSTEM`.

**Contacts** (private, per person). Your sign-in email becomes a **verified** email contact automatically (your identity provider already verified it). You may add **one** alternate email, which receives nothing until it is verified by a code (next step). Push gets an unguessable topic, `apex-` plus 32 random characters — on a public ntfy server the topic *is* the password, so treat it like one: it is shown only to you, never logged, and you can replace it if it leaks. Addresses are personal data and never appear raw in logs, audit events or error messages — only as a short tag.

**Routing** (private, per person): a grid of category × channel. A new person gets: market events → push + email; portfolio health → email; compliance → push + email; daily digest → email, **off**; system → every active channel. A channel receives a message only if **all five** hold: it is an active channel and switched on globally (email also needs an SMTP host and a from-address), routing is on for that category, you have a contact on it, that contact is verified, and it is not opted out.

**Your preferences:** content level (`BRIEF` — no amounts, position sizes or portfolio values, safe on a lock screen — or `DETAILED`; push is always brief), what to do with alerts in your quiet hours (`DEFER` until they end, or `SUPPRESS`), and the digest time (default 07:30 in your profile timezone), alongside the existing severity and conviction thresholds.

**Upgrading.** The old single `channel` setting is kept (deprecated, still readable) and was migrated once: `email` → email routing on for exactly what it covered (market events, portfolio health); `none` or `slack` → everything off. Anyone added after this release gets the defaults above. Re-running the migration never changes a choice you made since.

**Global settings** (admin only; versioned, reset-to-defaults; **Profile → Investor settings** gets its editor in a later step): which channels are on, the SMTP server and sender, the ntfy server, rate and retry limits, verification-code rules, the public app URL, and a dry-run switch. WhatsApp / SMS cannot be enabled, and email cannot be enabled without an SMTP host and a from-address.

**Secrets live only in environment variables — never in the database, a file, a log or a screen.** The database stores just the variable *name*; the admin screen shows each as *present* or *missing*, never the value:

| Variable (default name) | Used for |
|---|---|
| `APEX_SMTP_USERNAME`, `APEX_SMTP_PASSWORD` | Gmail SMTP login (an *app password*, not your Google password) |
| `APEX_NTFY_TOKEN` | optional ntfy access token (for a protected or self-hosted server) |
| `APEX_NOTIFY_SIGNING_KEY` | signs the one-click unsubscribe links in emails |

**How delivery works.** A producer calls `notification_service.notify(owner, category, dedup_key=…, title=…, brief=…)`. That applies the person's filters (minimum severity / conviction, quiet hours in *their* timezone), fans out to every deliverable channel and **only writes rows to the outbox** — no network call, and it never raises into the caller. A separate worker (`python -m scripts.send_notifications`) claims due rows, sends them with no database connection open, and records the result. Quiet hours either **defer** the message until they end or **suppress** it, as the person chose. Over `max_messages_per_user_per_hour` a message is **folded into the next digest, not dropped**. A transient failure is retried after `retry_backoff_seconds × 2^(attempt−1)` (capped at a day); after `retry_max_attempts` — or at once for a permanent error such as a bad login — the row is `FAILED` and an administrator can requeue it. The same thing is never enqueued twice for the same person and channel, and an opt-out made *after* a message was queued is honoured at send time. With the global `dry_run` setting on, the worker only logs what it would send.

### Where you manage it
**Profile → Notifications** (everyone): your contacts — the verified sign-in email, **one** alternate email (verified with a 6-digit code), and phone push (your private topic, a QR code, *Send test*, and *I got it* to verify it); the grid of which alert types go to which channel (Email and Phone push are live; WhatsApp and SMS are shown disabled, "Coming later"); brief or detailed messages; quiet hours and whether to hold or drop what arrives in them; your digest time; the severity and conviction thresholds; *Send test* buttons; and your own last 50 notifications. Each part saves with its own button (not "Save profile"). The old single "Channel" dropdown is gone — routing replaces it.

**Profile → Notification settings** (administrators only): the channel switches, SMTP and ntfy servers, sender, limits and a **dry-run** switch; each secret environment variable shown only as *present* or *missing* (never its value); and the outbox — counts by status, and recent failures with the recipient **masked**, the error, and a *Retry* button.

**Unsubscribe link.** Every email ends with a signed link to the **Unsubscribe** page, which needs no login. It asks for one click to confirm (so mail scanners that open links cannot opt anyone out by accident) and stops emails to that one address only; you can opt back in under *Profile → Notifications*.

### What sends a notification

| Category | Trigger | Who is told | Text |
|---|---|---|---|
| `MARKET_EVENT` | an event the detector finds (material news, …) | **every person who holds the ticker in a real (non-paper) account or watches it** — nobody else, each judged by their own routing, severity, quiet hours and content level | "APEX: NVDA - material news (severity 4). Open APEX for details." plus, if their clearance says so, "[BLOCKED: no new trade currently permitted]" — information only, it never stops the alert |
| `PORTFOLIO_HEALTH` | a Portfolio Health check *becomes* ACTION (once per run of ACTION; never WATCH) | that person | the check's name only — no amounts, no account names; the email's detailed text adds the full summary |
| `COMPLIANCE` | a pre-clearance approval recorded or withdrawn · an approval expiring within 24 hours · your restricted list changed for a ticker you **hold** | that person | informational: "…was recorded", "…was withdrawn; no new trade is currently permitted", "…expires within 24 hours" |
| `DAILY_DIGEST` | at each person's digest time, in their timezone | that person, **email only**, **off by default** | one email: the Health checks needing action or a watch, the day's events on tickers they hold or watch, and anything held back by the hourly limit. **Skipped when there is nothing to say**, and at most one per person per day |
| `SYSTEM` | reserved | every active channel | — |

Every notification is **informational** — never phrased as a trade instruction — and the brief text (always used for push) has **no amounts, position sizes or portfolio values**. De-duplication keys: `event:<event id>`, `health:<check>:<date the ACTION run began>` (with the account for an account-level check), `approval:<id>:<event>`, `approval:<id>:expiring:<expiry>`, `restricted:<ticker>:<added|removed>:<time>`, `digest:<local date>`. A notification failure never breaks the detector, a health run, a page, or a compliance record (the approval and restricted-list stores notify best-effort *after* their own commit).

The clock-driven alerts (expiring approvals, digests) are queued by `python -m scripts.send_notifications` (run it from cron every few minutes) and by the evening batch, just before delivery.

### Setting up email (Gmail)
1. Use a Google account with **2-Step Verification** on (Google Account → Security).
2. Create an **app password**: Google Account → Security → 2-Step Verification → *App passwords* → name it "APEX" → copy the 16-character password. (It is *not* your Google password, and it can be revoked any time.)
3. Put the secrets in the environment of whatever runs the pipeline and the web app — a `.env` file next to `main.py` works locally, App Settings on Azure. **Never** in the database or a committed file:
   ```
   APEX_SMTP_USERNAME=you@gmail.com
   APEX_SMTP_PASSWORD=<the 16-character app password>
   APEX_NOTIFY_SIGNING_KEY=<a long random string, e.g. from: python -c "import secrets; print(secrets.token_urlsafe(32))">
   ```
4. In the settings (admin) set `email_from_address` to the **same Gmail address** (Gmail rewrites any other From address) and `app_base_url` to where APEX is reachable (e.g. `https://apex.example.com`) — it is used for "Open in APEX", "Manage notifications" and the unsubscribe link. Gmail's free limit is about 500 messages a day.
5. Turn email on, send yourself a test notification, then switch off `dry_run`.

### Setting up phone push (ntfy)
1. Install the **ntfy** app — iOS (App Store) or Android (Google Play / F-Droid).
2. In APEX, open your notification settings → *Phone push*. APEX generates your private topic (`apex-` plus 32 random characters) and shows it with a QR code.
3. In the ntfy app tap **+**, choose *Subscribe to topic*, and enter the topic (or scan the QR code). If you use a self-hosted server, set the server URL in the app too.
4. Tap *Send test* in APEX, then **I got it** once it appears on the phone — that verifies the contact.
5. Push text is always the brief form: no amounts, position sizes or portfolio values, so it is safe on a lock screen.

On the public server **ntfy.sh the topic is the only thing keeping others out** — anyone who learns it can read your pushes. Keep it private; *Replace topic* generates a new one if it leaks. For stronger protection run your own ntfy server, protect it with access control, set `ntfy_base_url` to it and put its access token in `APEX_NTFY_TOKEN`.

### Scheduling the worker
Run the worker every few minutes — it is safe to run concurrently and at any time:
```cron
*/5 * * * *   /path/to/portfolio-agent/run_notifications.sh
```
The evening batch also delivers whatever is due at its end. `python -m scripts.send_notifications --status` prints the outbox counts by status.

### Enabling WhatsApp or SMS later
Both are stubs today (`WhatsAppSender`, `SmsSender` in `services/notification_senders.py`) and return "skipped: channel not implemented". Each class's `TODO` lists what a real one needs — Twilio or the Meta Cloud API; phone numbers in E.164 form verified by a code; approved WhatsApp message templates; SMS A2P 10DLC registration; STOP handling. To enable one later: implement its `send()`, lift the "cannot be turned on" validation in `notification_settings_db` and the "not available yet" refusal in `notification_contacts_db`, and add the channel to `domain.ACTIVE_CHANNELS`.

---

## Instrument routing: funds vs stocks

What the pipeline does with a ticker depends on its `instrument_type` (see [Holdings taxonomy](#holdings-taxonomy-what-each-holding-is-and-what-each-account-is)). Tickers nobody holds — watchlist, trending, extras — are classified at the start of each phase too.

| Type | EDGAR / 8-K guidance | Fundamentals + DCF valuation | APEX forecast |
|---|---|---|---|
| `STOCK`, `UNKNOWN` (priced), `BOND`, `OTHER` | fetched | run, weighted as before | yes — unchanged path |
| `ETF`, `ETN`, `MUTUAL_FUND`, `TARGET_DATE_FUND` | skipped cleanly (no error noise) | not run; weight 0 | yes — Research, Macro, News only |
| `MONEY_MARKET`, `CASH`, `CIT` (ticker-less), `STABLE_VALUE` | skipped | not run | no — logged as `[skip] … nothing to forecast` |
| `UNKNOWN` with no price | — | — | no — logged as `type unknown and no price available` |

Each stored prediction records the `instrument_type` and `leverage_factor` it was made under (older rows show as *Unclassified* in validation until you run `python -m scripts.backfill_instrument_classification --stamp-predictions`).

**Which horizons each type gets** is a database setting (`engine_settings.instrument_type_horizons_json`, admin-editable under **Profile → Investor settings → Horizons by type**, versioned, reset to defaults). Seeded: stocks / ETFs / ETNs / bonds / other / unknown 5, 21, 63, 250 days; mutual funds and target-date funds 63 and 250 (they price once a day); CITs, stable value, cash and money market none. The daily phase intersects this with the day's scheduled horizons per ticker, and skips a ticker (with a logged reason) when nothing is left.

**Fund context.** A fund's forecast context carries `fund_context`: a compact summary of its top holdings, sector and asset-class mix, expense ratio and the concentration of what is known (top-10 %, HHI, effective N), read from the **shared** fund tables only (see below). With no stored data it is `{"status": "UNKNOWN", "reason_code": "FUND_DATA_UNAVAILABLE"}`.

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
- **Instrument type** — for an ETF, ETN, mutual fund or target-date fund the Fundamentals and Valuation weights are exactly 0 on every horizon and the remaining sources (Research, Macro, News) are renormalised to sum to 1.0, each still subject to the floor. Stocks are unaffected.

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
- Broken down by horizon, ticker segment, model, prediction role (champion / fallback / challenger / legacy) and instrument type (stock vs ETF vs mutual fund …, with leveraged funds counted separately — Validation QA → *Instrument types*)
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

**Funds** — an ETF or fund is forecast from Research, Macro and News only; there is no Fundamental or Valuation score for it (stored as empty, never a 5/10 placeholder) and "no fundamentals" is not counted as a data gap. A leveraged or inverse fund (2x, 3x, −1x…) is flagged in its prompt and record (`leverage_factor`); expect its realised returns to be amplified.

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
