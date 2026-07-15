"""
Chat orchestration — LLM thread runners for conversational agent and APEX panel.

Contains:
  - _build_chat_history()      : converts session messages to LLM history
  - _CHAT_SYSTEM               : system prompt string
  - _classify_intent()         : 'predict' vs 'chat' router
  - _run_chat_agent_thread()   : conversational tool-calling agent (thread)
  - _apex_adk_run()            : ADK-based APEX runner (Claude/Gemini)
  - _apex_direct_run()         : direct LiteLLM runner (Groq + no-tools path)
  - _run_apex_thread()         : picks runner per provider, handles failover
  - _extract_json()            : extract prediction JSON from model output
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path
from queue import Queue

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.chat.tools import _CHAT_TOOLS, _execute_chat_tool


# ── Chat history formatter ────────────────────────────────────────────────────

def _build_chat_history(messages: list[dict], limit: int = 12) -> list[dict]:
    """Convert session messages to LLM-compatible history (last N exchanges)."""
    result = []
    for msg in messages[-limit:]:
        if msg["role"] == "user":
            result.append({"role": "user", "content": msg["content"]})
        elif msg.get("type") == "prediction":
            meta = msg.get("metadata", {})
            summary = (
                f"[APEX Prediction: {meta.get('recommendation','?')} on {meta.get('ticker','?')}, "
                f"composite {meta.get('composite_score','?')}/10, "
                f"confidence {meta.get('confidence','?')}/10] "
                f"{(msg.get('content') or '')[:200]}"
            )
            result.append({"role": "assistant", "content": summary})
        else:
            content = (msg.get("content") or "")[:600]
            if content:
                result.append({"role": "assistant", "content": content})
    return result


# ── System prompt ─────────────────────────────────────────────────────────────

_CHAT_SYSTEM = """\
You are APEX Chat, an AI financial assistant with access to a local investment database \
(holdings, predictions, opportunities) plus live market data, news, and web search.

You can answer questions about:
- **Individual stocks** -- fundamentals, broker research, price history, news, APEX predictions
- **Portfolio** -- current holdings, weights, P&L, APEX recommendations
- **New opportunities** -- trending stocks discovered and analyzed by the pipeline
- **Today's predictions** -- latest APEX signals across all tickers
- **Market news** -- latest financial headlines from Reuters, CNBC, Yahoo Finance, MarketWatch
- **Economy & macro** -- VIX, interest rates, yield curve, Fed policy, inflation, GDP trends
- **IPOs & listings** -- recent IPOs, company debut details, upcoming listings
- **Sectors & indices** -- sector ETF performance, S&P 500, Nasdaq, Dow Jones, Russell 2000
- **General finance** -- bonds, currencies, commodities, crypto market trends, regulatory issues

## Tool Priority (CRITICAL -- follow this exact order)

### 1. LOCAL DATABASE FIRST (always try these before anything else)
These query the local portfolio database and return real pipeline results:

| Question type | Tool to call FIRST |
|---|---|
| "latest opportunities", "trending stocks", "what looks good", "new discoveries", "top picks" | `get_latest_opportunities` |
| "most opportunistic stock", "undervalued", "trading below where it should", "best upside to target", any screen combining a valuation gap with a bullish signal | `get_undervalued_opportunities` |
| "my portfolio", "what do I own", "holdings", "portfolio performance" | `get_portfolio_summary` |
| "today's predictions", "latest signals", "what does APEX say", "all recommendations" | `get_predictions_summary` |
| Specific ticker prediction/recommendation | `get_prediction_history` |
| Specific ticker -- "should I trust this", "how accurate has APEX been", track record | `get_prediction_accuracy` |
| Specific ticker fundamentals | `get_fundamentals` |
| Specific ticker broker research | `get_research` |
| Specific ticker news | `get_ticker_news` |
| Specific ticker price | `get_price_history` |
| Specific ticker momentum/trend/RSI/moving averages/"technical setup" | `get_technical_snapshot` |
| "sector allocation", "diversification", "what sectors am I in" | `get_portfolio_sector_allocation` |
| "am I too concentrated", "correlated holdings", "risk flags", "beta vs the market" | `get_portfolio_risk_flags` |

### Broad screening questions (no single ticker in mind)
Questions like "what's the most opportunistic stock right now", "what looks undervalued", \
"what should I look at" are a DATABASE SCREEN across many tickers, not a request to deep-dive \
one stock. Call `get_undervalued_opportunities` and/or `get_latest_opportunities` / \
`get_predictions_summary`, reason over the *results*, and answer directly. Never guess a \
ticker out of the wording of a broad question (e.g. do NOT treat "trading lower" as a ticker \
symbol) -- if a tool needs a ticker and none was given, that's a sign the question is a screen, \
not a single-stock lookup.

### Ticker-specific questions
When the user does name a ticker (or a company you resolve via `search_ticker`) and it's \
already in the local database, ground your answer in what's stored before reaching for \
anything else: `get_prediction_history` (past recommendations), `get_prediction_accuracy` \
(has APEX been right about this ticker before?), `get_price_history` (has it actually moved \
the way predicted?), plus `get_fundamentals` / `get_research` / `get_ticker_news` as relevant. \
Only fall back to `web_search` for gaps those don't cover.

### 2. LIVE MARKET DATA (when DB doesn't have what's needed)
- Market headlines → `get_market_news`
- Macro snapshot → `get_macro_snapshot`
- Sector performance → `get_sector_performance`
- IPO info → `get_ipo_info`
- Unknown company → `search_ticker`

### 3. WEB SEARCH (last resort only)
Use `web_search` ONLY when:
- DB and live tools returned empty / "unavailable" / clearly stale data
- Question is about a very recent event (last 1-2 days) not yet in the DB
- Question asks for something no internal tool covers (e.g. regulatory filing details, \
  specific earnings call quotes from today)

**Never fabricate**: If both DB and web search return nothing useful, say so clearly.

## Response discipline (IMPORTANT)
- You have **at most 6 tool calls** per response. Budget them wisely.
- For opportunity/portfolio/prediction questions: call the DB tool, then answer immediately.
- For stock analysis: call 2–3 tools max then answer -- do not over-chain.
- Do not call the same tool twice for the same ticker in one response.
- If `get_latest_opportunities` returns data, present it directly -- do not also call web search.

## Presenting opportunities
When showing results from `get_latest_opportunities`, `get_undervalued_opportunities`, or `get_predictions_summary`:
- Lead with the ticker, recommendation, and composite score
- For `get_undervalued_opportunities`, lead with `upside_to_target_pct` -- that's the point of the screen
- Include news/research/fundamental sub-scores when available
- Add a 1-sentence reasoning summary if the DB returned one
- Suggest "Type **Analyze [TICKER]** for the full 4-analyst APEX panel" for any ticker of interest

## Format rules
- Use **bold** for key figures, bullet lists for multi-point summaries.
- Cite your source: "(from local DB)" or "(web search)" or "(yfinance)".
- For full stock investment analysis, suggest: "Type **Analyze [TICKER]** for the full \
  4-analyst APEX panel."
"""


# ── Intent classifier ─────────────────────────────────────────────────────────

_PREDICT_PHRASE_RE = re.compile(
    r"\b("
    r"analyz\w*|analysis|predict|prediction|"
    r"should i buy|should i sell|buy or sell|worth buying|"
    r"investment thesis|investment recommendation|"
    r"full report|deep dive|apex analysis|"
    r"give me a report|run apex|run analysis"
    r")\b"
)


def _classify_intent(query: str, tickers: list[str]) -> str:
    """
    Classify the query as 'predict' (full APEX panel) or 'chat' (conversational).

    'predict' triggers only when the user names a specific ticker AND clearly
    wants a fresh investment recommendation for it. Broad/exploratory questions
    (screening, comparisons, "what looks good") always route to the
    conversational tool-calling agent, which can pull DB records (predictions,
    research, price history, validation outcomes) and web search as needed --
    even for questions that happen to mention a ticker.
    """
    if not tickers:
        return "chat"
    q = query.lower()
    # Word-boundary match -- a plain substring check would match "predict"/
    # "prediction" inside "predictions", turning "analyst predictions are
    # strong" into a false "predict" classification.
    if _PREDICT_PHRASE_RE.search(q):
        return "predict"
    if len(query.split()) <= 3:
        return "predict"
    return "chat"


# ── Conversational chat agent thread ─────────────────────────────────────────

def _run_chat_agent_thread(
    tickers: list[str], query: str, history: list[dict], q: Queue
) -> None:
    """
    LLM-orchestrated conversational agent with tool-calling.
    Runs in a daemon thread; pushes events to *q*.
    """
    async def _inner():
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
        import litellm
        from portfolio_agent._models import FAILOVER_CHAINS, _is_failover_error

        chat_chain = FAILOVER_CHAINS.get("flash", [])
        ticker_hint = (
            f"[Tickers in question: {', '.join(tickers)}]\n\n" if tickers else ""
        )
        user_content = ticker_hint + query

        messages = [{"role": "system", "content": _CHAT_SYSTEM}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        for model_id, provider, label in chat_chain:
            q.put(("info", f"💬 {label} ({provider})"))
            try:
                for _round in range(8):
                    resp = await litellm.acompletion(
                        model=model_id,
                        messages=messages,
                        tools=_CHAT_TOOLS,
                        tool_choice="auto",
                        temperature=0.2,
                        max_tokens=2500,
                    )
                    msg = resp.choices[0].message

                    if msg.tool_calls:
                        tool_results = []
                        for tc in msg.tool_calls:
                            fn_name = tc.function.name
                            fn_args = json.loads(tc.function.arguments or "{}")
                            q.put(("tool", f"{fn_name}({json.dumps(fn_args)})"))
                            result_str = _execute_chat_tool(fn_name, fn_args)
                            q.put(("result", f"{fn_name} → {result_str[:250]}"))
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result_str,
                            })
                        messages.append({
                            "role": "assistant",
                            "content": msg.content or "",
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                                for tc in msg.tool_calls
                            ],
                        })
                        messages.extend(tool_results)
                    else:
                        text = msg.content or "_No response generated._"
                        q.put(("text", text))
                        q.put(("model_used", (label, provider)))
                        q.put(("done", "chat"))
                        return

                q.put(("text", "_Agent did not produce a final answer within tool limits._"))
                q.put(("done", "chat"))
                return

            except Exception as exc:
                if _is_failover_error(exc) and (model_id, provider, label) != chat_chain[-1]:
                    q.put(("warn", f"⚠ {label} failed -- trying next model…"))
                    continue
                q.put(("error", str(exc)))
                q.put(("done", ""))
                return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_inner())
    finally:
        loop.close()


# ── APEX streaming runners ────────────────────────────────────────────────────

_FAILOVER_ERRORS = (
    "429", "rate limit", "quota", "resource_exhausted",
    "credit", "billing", "529", "overloaded",
    "not_found", "is not found for api",
    "tool_use_failed", "failed_generation", "bad_request",
    "invalid_request_error",
)


def _is_apex_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _FAILOVER_ERRORS)


async def _apex_adk_run(ticker: str, query: str, model_id: str, label: str,
                        provider: str, chain_pos: str, q: Queue) -> bool:
    """Run APEX via ADK Runner (Claude / Gemini). Returns True on success."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from portfolio_agent._models import _lite
    from portfolio_agent.specialists.reasoning import make_reasoning_agent

    agent  = make_reasoning_agent(model=_lite(model_id))
    svc    = InMemorySessionService()
    runner = Runner(agent=agent, app_name="apex_chat", session_service=svc)
    sess   = await svc.create_session(
        app_name="apex_chat", user_id="chat_user",
        state={"current_ticker": ticker.upper(), "user_query": query},
    )
    msg = types.Content(role="user",
                        parts=[types.Part(text=f"Analyze {ticker.upper()}: {query}")])

    q.put(("info", f"🤖 Reasoning with {label} ({provider})  [{chain_pos}]"))
    async for ev in runner.run_async(user_id="chat_user",
                                     session_id=sess.id, new_message=msg):
        if not (hasattr(ev, "content") and ev.content):
            continue
        for part in (ev.content.parts or []):
            if hasattr(part, "text") and part.text:
                q.put(("text", part.text))
            elif hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                q.put(("tool", f"{fc.name}({json.dumps(fc.args) if fc.args else ''})"))
            elif hasattr(part, "function_response") and part.function_response:
                fr = part.function_response
                q.put(("result", f"{fr.name} → {str(fr.response)[:300]}"))

    final = await svc.get_session(app_name="apex_chat", user_id="chat_user",
                                   session_id=sess.id)
    state = dict(final.state) if final else {}
    q.put(("model_used", (label, provider)))
    q.put(("done", state.get("apex_prediction", "")))
    return True


async def _apex_direct_run(ticker: str, query: str, model_id: str, label: str,
                           provider: str, chain_pos: str, q: Queue) -> bool:
    """
    Groq / no-tools fallback: pre-fetch context in Python, embed in prompt,
    call model directly via LiteLLM without ADK tool use.
    """
    import litellm
    from portfolio_agent.tools.reasoning_tools import get_full_analysis_context

    q.put(("info", f"🤖 Reasoning with {label} ({provider}) [no-tools]  [{chain_pos}]"))
    q.put(("tool", "get_full_analysis_context -- pre-fetching context…"))

    ctx_json = get_full_analysis_context(ticker.upper())
    q.put(("result", f"get_full_analysis_context → {str(ctx_json)[:200]}…"))

    try:
        ctx_obj = json.loads(ctx_json)
        _caps = ctx_obj.get("dynamic_weights", {}).get("data_caps", {})
        _gaps = ctx_obj.get("data_gaps", [])
    except Exception:
        _caps, _gaps = {}, []

    _cap_lines = []
    for _domain, _cap in _caps.items():
        if _cap < 10:
            _cap_lines.append(f"  • {_domain} score ≤ {_cap} -- no data in DB")
    _cap_block = (
        "\nSCORE CAPS (mandatory -- exceeding these is an error):\n" + "\n".join(_cap_lines) + "\n"
        if _cap_lines else ""
    )

    from portfolio_agent.tools.prediction_db import get_scheduled_horizons as _get_horizons
    _today_date = date.today()
    _sched_horizons = _get_horizons(_today_date) or [5]
    _hz_guidance = {
        5:  "5d  (~1 week)  -- focus on news flow, momentum, short-term catalysts",
        21: "21d (~1 month) -- focus on earnings drift, monthly themes, near catalysts",
        63: "63d (~1 quarter) -- focus on fundamentals, valuation, full earnings cycle",
    }
    _hz_lines = "\n".join(f"  • {_hz_guidance.get(h, f'{h}d')}" for h in sorted(_sched_horizons))

    _hz_example_items = []
    for _h in sorted(_sched_horizons):
        _hz_example_items.append(
            f'    {{"horizon_days": {_h}, "predicted_direction": "UP|DOWN|FLAT", '
            f'"predicted_return_low": 1.5, "predicted_return_high": 4.0, '
            f'"conviction_score": 7, "reasoning_text": "..."}}'
        )
    _hz_example = "[\n" + ",\n".join(_hz_example_items) + "\n  ]"

    _caps_f = _caps.get("fundamentals", 10)
    _caps_r = _caps.get("research", 10)
    _caps_m = _caps.get("macro", 10)
    _caps_n = _caps.get("news", 10)

    prompt = f"""You are APEX (Adaptive Portfolio EXpert), a multi-perspective investment reasoning system.

Ticker  : {ticker.upper()}
Question: {query}

=== FULL ANALYSIS CONTEXT (pre-loaded) ===
{ctx_json}
=== END CONTEXT ===
{_cap_block}
Using the context above (fundamentals, research, news, macro, dynamic_weights, prediction_history):

1. State the weight regime and dynamic weights (from dynamic_weights in context).
2. Have each analyst score their domain -- respect any SCORE CAPS above: if the domain has no data the analyst must state "No <domain> data available" and assign a score ≤ that cap.
   FUNDAMENTAL ANALYST, RESEARCH ANALYST, MACRO ANALYST, NEWS ANALYST.
3. Cross-examine if scores diverge > 3 pts.
4. Compare to prior prediction if one exists.
5. Compute composite = fund_w×fund_score + res_w×res_score + mac_w×mac_score + news_w×news_score.
6. Map composite to STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL.
7. For EACH scheduled horizon below, provide direction, return range, and per-horizon conviction:
{_hz_lines}

End your response with EXACTLY this JSON block (no text after):

```json
{{
  "ticker": "{ticker.upper()}",
  "prediction": "BULLISH|BEARISH|NEUTRAL",
  "recommendation": "STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL",
  "confidence": 7,
  "target_price": null,
  "fundamental_score": 7,
  "research_score": 7,
  "macro_score": 6,
  "news_score": 6,
  "composite_score": 6.5,
  "score_caps_applied": {{"fundamentals": {_caps_f}, "research": {_caps_r}, "macro": {_caps_m}, "news": {_caps_n}}},
  "weight_regime": "QUIET_DAY",
  "weights_used": {{"fundamentals": 0.35, "research": 0.30, "macro": 0.20, "news": 0.15}},
  "panel_summary": {{
    "chen_verdict": "BULLISH (7/10) -- Fundamental Analyst one sentence",
    "webb_verdict": "BULLISH (7/10) -- Research Analyst one sentence",
    "varga_verdict": "NEUTRAL (6/10) -- Macro Analyst one sentence",
    "park_verdict":  "NEUTRAL (6/10) -- News Analyst one sentence",
    "key_debate": "Panel consensus or main disagreement"
  }},
  "weight_rationale": {{
    "fundamentals": "why this weight",
    "research": "why this weight",
    "macro": "why this weight",
    "news": "why this weight"
  }},
  "reasoning": "3-5 sentence narrative",
  "changed_from_previous": false,
  "previous_recommendation": null,
  "horizons": {_hz_example}
}}
```"""

    response = await litellm.acompletion(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=4096,
    )
    text = response.choices[0].message.content or ""
    q.put(("text", text))
    q.put(("model_used", (label, provider)))
    q.put(("done", text))
    return True


def _run_apex_thread(ticker: str, query: str, q: Queue) -> None:
    async def _inner():
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
        from portfolio_agent._models import FAILOVER_CHAINS

        reasoning_chain = FAILOVER_CHAINS.get("reasoning", [])

        for i, (model_id, provider, label) in enumerate(reasoning_chain):
            chain_pos = f"{i+1}/{len(reasoning_chain)}"
            use_direct = (provider == "groq")
            try:
                if use_direct:
                    await _apex_direct_run(ticker, query, model_id, label,
                                           provider, chain_pos, q)
                else:
                    await _apex_adk_run(ticker, query, model_id, label,
                                        provider, chain_pos, q)
                return

            except Exception as exc:
                if _is_apex_transient(exc) and i < len(reasoning_chain) - 1:
                    next_label = reasoning_chain[i + 1][2]
                    q.put(("warn", f"⚠ {label} failed -- falling back to {next_label}…"))
                    continue
                q.put(("error", str(exc)))
                q.put(("done", ""))
                return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_inner())
    finally:
        loop.close()


# ── JSON extractor ────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    for pattern in [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'(\{[^{}]*"recommendation"[^{}]*\})',
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return {}
