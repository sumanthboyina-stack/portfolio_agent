"""
Chat routing — one pure function decides what a message becomes.

    chat      the conversational tool-calling agent (cheap, read-only)
    predict   the full APEX panel on named ticker(s) (paid; saves a private prediction)
    resolve   a full analysis was asked for but no ticker is named: try to resolve a company from the message and confirm it,
              else ask which stock

Rules (see openspec/changes/chat-portfolio-assistant/specs/chat-routing):
  * Personal / portfolio phrasing ("my portfolio", "what I own", "should I trim", "where should it go") is ALWAYS chat. It never
    resolves a company name and never inherits a ticker from earlier in the session.
  * Predict needs an explicit predict phrase AND a ticker (named in this message, or — for a short follow-up such as "should I buy?"
    when the message is not personal or about the market at large — the one the session is already discussing). Message length
    never decides it: a bare "NVDA" or "NVDA news" is chat, with a hint to type "Analyze NVDA".
  * Broad, comparison and screening questions are chat even when they mention tickers.

No Streamlit, no database, no network: the page and the tests call the same code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# An explicit request for a fresh, full investment analysis.
PREDICT_PHRASE_RE = re.compile(
    r"\b("
    r"analyz\w*|analysis|predict|prediction|"
    r"should i buy|should i sell|buy or sell|worth buying|"
    r"investment thesis|investment recommendation|"
    r"full report|deep dive|apex analysis|"
    r"give me a report|run apex|run analysis"
    r")\b"
)

# The message is about the user's own money, holdings or what they might do with them.
PERSONAL_RE = re.compile(
    r"\b("
    r"my\s+(portfolio|holdings?|positions?|accounts?|stocks?|investments?|funds?|etfs?|cash|shares|allocation|retirement|"
    r"401\s?k|ira|roth|brokerage|money|net worth|watchlist|lots?|gains?|taxes)|"
    r"what\s+(do\s+)?i\s+(own|hold|have)|what\s+i\s+(own|hold)|how\s+much\s+\w+\s+do\s+i\s+(really\s+)?own|"
    r"i\s+(own|hold)\b|i\s+have\s+\$?\d+|"
    r"should\s+i\s+(trim|add|rebalance|reduce|cut|diversify|top\s+up|put|move|invest)|"
    r"where\s+(should|do|would)\s+(i|it|that|this|the\s+money|the\s+cash)\s+(put|go|invest|allocate)|"
    r"where\s+should\s+(it|that|this|the\s+money|the\s+cash)\s+go|"
    r"what\s+if\s+i\s+(sell|buy|trim|add|move|invest|put)|"
    r"(sell|buy|trim|add|reduce|move)\s+(half|some|all|part|a\s+third|a\s+quarter|\d+%?)\s+of\s+my|"
    r"rebalanc\w*"
    r")\b"
)

# Questions about markets, sectors or the economy at large rather than one company.
BROAD_RE = re.compile(
    r"\b(market|markets|sector|sectors|industry|industries|economy|economic|macro|stocks|etfs|index|indices|rates|inflation|"
    r"ipos?|trend|trends|opportunit\w+|screen|compare|comparison|versus|vs)\b"
)

# A short reference to the stock under discussion ("analyze it", "deep dive on this one").
_REFERENCE_RE = re.compile(r"\b(it|this|that|this one|that one|the stock|the company|them)\b")


@dataclass(frozen=True)
class Route:
    mode: str                                   # "chat" | "predict" | "resolve"
    tickers: list[str] = field(default_factory=list)   # tickers the chosen mode works on
    suggest: str | None = None                  # a hint to show under a chat answer (e.g. 'Analyze NVDA')
    reason: str = ""                            # why (for logs and tests)


def is_personal(query: str) -> bool:
    return bool(PERSONAL_RE.search((query or "").lower()))


def wants_prediction(query: str) -> bool:
    return bool(PREDICT_PHRASE_RE.search((query or "").lower()))


def route_message(query: str, mentioned_tickers: list[str] | tuple[str, ...] = (),
                  session_tickers: list[str] | tuple[str, ...] = ()) -> Route:
    """
    query: the user's message. mentioned_tickers: tickers found IN this message. session_tickers: tickers the conversation has
    already been about (used only as chat context, or for a non-personal predict follow-up with no ticker of its own).
    """
    q = (query or "").lower().strip()
    mentioned = list(dict.fromkeys(mentioned_tickers))
    session = list(dict.fromkeys(session_tickers))

    if is_personal(q):
        return Route("chat", mentioned, None, "personal")

    if wants_prediction(q):
        if mentioned:
            if BROAD_RE.search(q) and len(mentioned) > 1:
                return Route("chat", mentioned, None, "comparison")         # "analyze MSFT vs GOOGL": a comparison, not one panel
            return Route("predict", mentioned, None, "predict phrase + ticker")
        if BROAD_RE.search(q):
            return Route("chat", session, None, "predict phrase about the market")
        if session and (_REFERENCE_RE.search(q) or len(q.split()) <= 4):
            return Route("predict", session[:1], None, "follow-up on the session ticker")
        return Route("resolve", [], None, "predict phrase, no ticker")

    if mentioned:
        suggest = None
        if len(q.split()) <= 3 and not BROAD_RE.search(q):
            suggest = f"Type **Analyze {mentioned[0]}** for the full 4-analyst APEX panel."
        return Route("chat", mentioned, suggest, "ticker without a predict phrase")

    return Route("chat", session, None, "general question")
