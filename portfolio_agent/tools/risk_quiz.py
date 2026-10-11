"""
Risk quiz scoring — pure functions, no database, no LLM. The questions, options, point values, cap rule and
preference thresholds all come from the settings dict passed in (investor_settings_db.get_quiz_settings()); nothing is
hard-coded here, so an administrator editing the quiz changes the scoring with no code change.

The rule (with the seeded quiz):
  * the "when will you need it" question sets a CAP — what the person's situation can handle;
  * the other questions are summed into a PREFERENCE (what they say they'd tolerate);
  * the result is the MORE CONSERVATIVE of the cap and the preference;
  * a question left unanswered counts as its middle ("default_option") answer. Skipping the whole quiz therefore
    scores as MODERATE, and a quiz is never required.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from portfolio_agent.domain import RISK_PROFILES


def profile_rank(profile: str) -> int:
    """0 = most conservative … 2 = most aggressive."""
    return RISK_PROFILES.index(profile)


def more_conservative(a: str, b: str) -> str:
    return a if profile_rank(a) <= profile_rank(b) else b


def is_above_cap(picked: str, cap_profile: str | None) -> bool:
    """True when *picked* is riskier than the cap. No cap (the cap question was not answered) -> never above."""
    return cap_profile is not None and profile_rank(picked) > profile_rank(cap_profile)


@dataclass(frozen=True)
class QuizResult:
    profile: str                       # the more conservative of cap and preference
    cap_profile: str                   # the cap actually applied (the middle answer's cap if the cap question was skipped)
    cap_answered: bool                 # False: cap_profile is only the default — don't treat it as the person's answer
    preference_profile: str
    preference_points: float
    unanswered: tuple = field(default_factory=tuple)   # question ids that were left blank (scored as the middle answer)
    quiz_version: int | None = None


def _option(question: dict, key: str) -> dict:
    for o in question["options"]:
        if o["key"] == key:
            return o
    raise ValueError(f"question {question['id']!r} has no option {key!r}")


def score_quiz(answers: dict, settings: dict) -> QuizResult:
    """
    answers: {question_id: option_key or None}. Missing / None / "" = unanswered. An unknown question id or option
    key raises ValueError (a typo must never silently score as the middle answer).
    """
    questions = {q["id"]: q for q in settings["questions"]}
    unknown = sorted(set(answers) - set(questions))
    if unknown:
        raise ValueError(f"unknown quiz question(s): {unknown}")

    def chosen(qid: str) -> tuple[dict, bool]:
        q = questions[qid]
        key = answers.get(qid)
        answered = key not in (None, "")
        return _option(q, key if answered else q["default_option"]), answered

    unanswered = [qid for qid in questions if answers.get(qid) in (None, "")]

    cap_q = settings["cap_rules"]["question_id"]
    cap_opt, cap_answered = chosen(cap_q)
    cap_profile = settings["cap_rules"]["cap_by_option"][cap_opt["key"]]

    pref_ids = settings["preference_thresholds"]["question_ids"]
    points = sum(chosen(qid)[0]["points"] for qid in pref_ids)
    preference = next((b["profile"] for b in settings["preference_thresholds"]["bands"] if points <= b["max_points"]),
                      settings["preference_thresholds"]["bands"][-1]["profile"])

    return QuizResult(
        profile=more_conservative(cap_profile, preference), cap_profile=cap_profile, cap_answered=cap_answered,
        preference_profile=preference, preference_points=points, unanswered=tuple(unanswered),
        quiz_version=settings.get("quiz_version"),
    )
