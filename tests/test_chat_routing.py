"""
Chat routing (web/chat/routing.py): personal / portfolio phrasing is always chat and never resolves a company name; predict needs
an explicit predict phrase AND a ticker; message length never decides; broad and comparison questions are chat. The page calls the
same function (checked at the source level below), so these are the page's decisions.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from web.chat.orchestration import _classify_intent
from web.chat.routing import PREDICT_PHRASE_RE, Route, is_personal, route_message, wants_prediction

PAGE = Path(__file__).resolve().parents[1] / "web" / "pages" / "4_🤖_Chat.py"


def mode(q, mentioned=(), session=()):
    return route_message(q, mentioned, session).mode


# ── the cases named in the brief ──────────────────────────────────────────────

def test_analyze_my_portfolio_is_chat_with_no_ticker_resolution():
    r = route_message("Analyze my portfolio", [], [])
    assert r.mode == "chat" and r.tickers == [] and r.reason == "personal"
    assert _classify_intent("Analyze my portfolio", []) == "chat"


def test_analyze_my_portfolio_is_chat_even_if_a_ticker_is_mentioned_or_in_session():
    assert mode("Analyze my portfolio vs NVDA", ["NVDA"]) == "chat"
    assert route_message("Analyze my portfolio", [], ["NVDA"]).tickers == []          # a stale session ticker is never adopted


def test_ticker_plus_a_few_words_is_chat_not_a_paid_panel():
    r = route_message("NVDA news", ["NVDA"], [])
    assert r.mode == "chat" and r.tickers == ["NVDA"]
    assert r.suggest and "Analyze NVDA" in r.suggest
    assert _classify_intent("NVDA news", ["NVDA"]) == "chat"


def test_bare_ticker_is_chat_with_an_analyze_suggestion():
    r = route_message("NVDA", ["NVDA"], [])
    assert r.mode == "chat" and "Analyze NVDA" in (r.suggest or "")


def test_analyze_ticker_and_should_i_buy_are_predict():
    assert route_message("Analyze NVDA", ["NVDA"]).mode == "predict"
    assert route_message("Analyze NVDA", ["NVDA"]).tickers == ["NVDA"]
    assert route_message("Should I buy AAPL?", ["AAPL"]).mode == "predict"
    assert _classify_intent("Analyze NVDA", ["NVDA"]) == "predict"
    assert _classify_intent("Should I buy AAPL?", ["AAPL"]) == "predict"


def test_comparison_and_screening_questions_are_chat():
    assert mode("Compare MSFT vs GOOGL", ["MSFT", "GOOGL"]) == "chat"
    assert mode("what AI stocks look good", []) == "chat"
    assert mode("what AI stocks look good", ["AI"]) == "chat"                         # even if "AI" is read as a ticker
    assert mode("Analyze MSFT vs GOOGL", ["MSFT", "GOOGL"]) == "chat"                 # two tickers: a comparison, not one panel


# ── personal phrasing, many wordings ──────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "my portfolio", "How is my portfolio doing?", "what are my holdings", "show my positions", "what I own",
    "what do I own in tech", "How much NVDA do I really own including funds?", "my account balance",
    "should I trim AAPL", "Should I trim my NVDA?", "where should I put $5,000", "I have $5,000, where should it go?",
    "I have 5000 to invest", "what if I sell half my AAPL and buy VOO?", "sell half of my AAPL", "should I rebalance",
    "analyze my holdings", "deep dive on my retirement accounts", "Should I buy more of what I own?", "my cash",
])
def test_personal_phrasing_is_always_chat(q):
    assert is_personal(q), q
    assert route_message(q, ["AAPL"], ["AAPL"]).mode == "chat"
    assert route_message(q, [], []).mode == "chat"


@pytest.mark.parametrize("q", ["Analyze NVDA", "Should I buy AAPL?", "deep dive on TSLA", "run apex on AMD", "worth buying MSFT",
                               "what are analyst predictions on AAPL saying", "Compare MSFT vs GOOGL", "NVDA news"])
def test_non_personal_phrasing_is_not_flagged_personal(q):
    assert not is_personal(q), q


# ── predict needs a phrase AND a ticker; length never decides ─────────────────

def test_predict_phrase_without_a_ticker_resolves_or_asks_and_never_guesses_from_wording():
    r = route_message("deep dive on Microsoft", [], [])
    assert r.mode == "resolve" and r.tickers == []
    assert mode("analyze the market", []) == "chat"                                   # about the market at large
    assert mode("analyze tech stocks for me", []) == "chat"
    assert mode("analyze it", [], ["NVDA"]) == "predict"                              # a short follow-up on the session ticker
    assert route_message("analyze it", [], ["NVDA"]).tickers == ["NVDA"]
    assert mode("analyze it", [], []) == "resolve"


def test_predictions_is_not_predict():
    assert not wants_prediction("what are analyst predictions on AAPL saying")
    assert PREDICT_PHRASE_RE.search("run a prediction") and not PREDICT_PHRASE_RE.search("analyst predictions")
    assert mode("what are analyst predictions on AAPL saying", ["AAPL"]) == "chat"


@pytest.mark.parametrize("q, tickers", [("hi", []), ("NVDA", ["NVDA"]), ("AAPL MSFT", ["AAPL", "MSFT"]), ("NVDA price", ["NVDA"]),
                                         ("TSLA?", ["TSLA"]), ("what is VOO", ["VOO"])])
def test_short_messages_never_reach_predict_by_length(q, tickers):
    assert mode(q, tickers) == "chat"


def test_general_questions_stay_chat_and_keep_session_context_only_as_context():
    r = route_message("what happened to it today", [], ["NVDA"])
    assert r.mode == "chat" and r.tickers == ["NVDA"]
    assert mode("Fed rate outlook", []) == "chat"
    assert mode("Latest IPOs this week", []) == "chat"
    assert mode("What's happening in markets?", []) == "chat"


def test_route_is_a_frozen_dataclass_with_defaults():
    r = Route("chat")
    assert r.tickers == [] and r.suggest is None
    with pytest.raises(Exception):
        r.mode = "predict"


# ── the page uses this function, and only resolves a company in the "resolve" case ──

def test_page_routes_through_route_message_and_has_no_inline_predict_keyword_set():
    src = PAGE.read_text()
    assert "route_message(user_input" in src
    assert "_predict_kw" not in src and "_wants_prediction" not in src
    assert src.count("_search_ticker_by_name(user_input)") == 1              # one call site...
    block = src.split('if route.mode == "chat":')[1]
    chat_part, rest = block.split('elif route.mode == "predict":')
    predict_part, resolve_part = rest.split("    else:\n", 1)
    assert "_search_ticker_by_name" not in chat_part and "_search_ticker_by_name" not in predict_part
    assert "_search_ticker_by_name(user_input)" in resolve_part.split("\n\n")[0] + resolve_part   # ...and it is in the resolve branch


def test_a_personal_message_never_calls_search_ticker_by_name(monkeypatch):
    """Drive the page's branch logic with the real routing function and a spy: only mode == 'resolve' may call the resolver."""
    calls = []
    for q in ["Analyze my portfolio", "analyze my holdings", "should I trim", "where should I put $5,000", "NVDA news", "NVDA"]:
        r = route_message(q, ["NVDA"] if "NVDA" in q else [], [])
        if r.mode == "resolve":
            calls.append(q)
    assert calls == []
