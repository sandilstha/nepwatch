"""
desk_assistant.py — grounded natural-language Q&A over the A/D Radar payload.

The point of difference, stated plainly because it is the whole design:

A browser-sidebar assistant (Gemini reading the page) sees PIXELS. It re-reads
numbers visually and can quote a figure that is not on screen. This module never
shows the model a screen — it hands over the exact JSON the desk already
computed, and forbids any number that is not in it.

It also carries something no generic assistant can know: the A/D score is NOT a
validated predictive signal (out-of-sample t=0.82, p=0.412). The brief states
that, so the answer describes flow structure instead of inventing a forecast.

Scope is deliberately narrow: it answers about the CURRENT window's scan. It
does not fetch, browse, or remember. Every claim is traceable to a number in the
brief, which is what makes the answers checkable.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from django.core.cache import cache

from core_analysis.services import accumulation as ac
from core_analysis.services.gemini_analysis import call_llm, markdown_lite_to_html

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 400
# Rows sent to the model. The full scan is ~270 KB — far too much to send, and
# most of it is irrelevant to any one question. The extremes are what questions
# are actually about, so send both tails plus aggregate context.
ROWS_PER_TAIL = 18
# Per-user daily cap. Every question costs API money, so this is a real spend
# control, not a formality.
DAILY_QUESTION_CAP = 40
_CAP_TTL = 86_400


def _quota_key(user_id) -> str:
    from datetime import date
    return f"desk_ai_quota_{user_id or 'anon'}_{date.today().isoformat()}"


def check_quota(user_id) -> tuple[bool, int]:
    """(allowed, remaining) — read-only, does not consume."""
    used = cache.get(_quota_key(user_id)) or 0
    return used < DAILY_QUESTION_CAP, max(0, DAILY_QUESTION_CAP - used)


def consume_quota(user_id) -> None:
    k = _quota_key(user_id)
    try:
        cache.set(k, (cache.get(k) or 0) + 1, _CAP_TTL)
    except Exception:  # pragma: no cover - cache is best-effort
        logger.warning("desk assistant quota update failed", exc_info=True)


def _row_brief(r: dict[str, Any]) -> dict[str, Any]:
    """One scan row, trimmed to the fields a question can actually be about."""
    return {
        "symbol": r["symbol"],
        "sector": r["sector"],
        "band": r["band_label"],
        "score": r["score"],
        "percentile": r["percentile"],
        "top5_net_pct_of_volume": r["absorb_pct"],
        "largest_single_buyer_pct_of_net_buying": r["top1_pct"],
        "sell_side_concentration_hhi": r["sell_hhi"],
        "brokers_for_half_the_net_buying": r["group_n"],
        "net_buyers": r["buyers_n"],
        "net_sellers": r["sellers_n"],
        "price_change_pct_in_window": r["price_chg_pct"],
        "shares_traded": r["volume"],
    }


def build_brief(scan: dict[str, Any], sector: str = "All") -> dict[str, Any]:
    """Compact, factual brief for the model — both tails plus context."""
    rows = scan.get("rows") or []
    ranked = sorted(rows, key=lambda r: r["score"], reverse=True)
    bt = scan.get("backtest") or {}
    return {
        "what_this_is": (
            "Accumulation/Distribution scan of NEPSE ordinary equities, computed "
            "from broker-tagged floorsheet trades. Scores are cross-sectional "
            "z-scores across the whole market for this window."
        ),
        "window": {
            "sessions": scan.get("sessions"),
            "as_of": str(scan.get("as_of")),
            "equities_scored": scan.get("universe"),
            "sector_filter": sector,
        },
        "field_meanings": {
            "top5_net_pct_of_volume":
                "share of ALL shares traded in the window that ended up in the "
                "5 largest net buyers' hands (the 'absorb' feature)",
            "largest_single_buyer_pct_of_net_buying":
                "how much of the net buying one broker did",
            "sell_side_concentration_hhi":
                "Herfindahl index of the selling side, 0-10000; higher = fewer "
                "sellers doing the selling",
            "price_change_pct_in_window":
                "close-to-close change over the same window",
        },
        "evidence_status": {
            "validated_as_predictive": False,
            "out_of_sample": {
                "spread_pct": bt.get("spread_pct"), "t_stat": bt.get("t_stat"),
                "p_value": bt.get("p_value"), "n": bt.get("n"),
            },
            "meaning": (
                "The score has NO demonstrated ability to predict returns "
                "out-of-sample. It measures observed flow structure only."
            ),
        },
        "known_blind_spot": (
            "Off-market transfers (DP/BOD transfers, pledges, private deals) "
            "never print on the floorsheet, so a campaign done that way is invisible."
        ),
        "top_of_ranking": [_row_brief(r) for r in ranked[:ROWS_PER_TAIL]],
        "bottom_of_ranking": [_row_brief(r) for r in ranked[-ROWS_PER_TAIL:]],
        "absorbing_without_price_markup": [
            _row_brief(r) for r in (scan.get("quiet_accumulation") or [])
        ],
    }


_SYSTEM_PROMPT = """You are an analyst on a NEPSE trading desk, answering a colleague's \
question about an Accumulation/Distribution flow scan. You are given a JSON brief of \
ALREADY-COMPUTED numbers.

HARD RULES — these are not style preferences:
- Use ONLY numbers present in the brief. Never invent, estimate, extrapolate or \
recall a price, ratio, date or symbol that is not there. If the brief does not contain \
what is needed, say exactly what is missing and stop.
- The brief holds only the TOP and BOTTOM of the ranking, not every scored stock. If a \
symbol is absent you cannot say where it ranks — say it is not in the supplied extract.
- NEVER present the score as predictive. evidence_status.validated_as_predictive is \
false: out-of-sample it does not predict returns. Describe what the flow DID; never say \
a stock "will" rise or fall, and never issue buy/sell advice.
- Do not infer intent. High absorption does not prove a deliberate campaign — a broker \
with many retail clients and one split institutional order look identical here.
- Mention the off-market blind spot only if the question turns on completeness.

STYLE:
- Answer the actual question first, in one direct sentence.
- Then support it with specific numbers, naming the symbol and field.
- Prefer a short markdown table or bullets when comparing several stocks.
- Be concise: under 250 words unless a table needs more. No preamble, no disclaimers \
about being an AI, no restating the question.
- If a question asks for a prediction or advice, answer the factual part and state \
plainly that this data cannot support a forecast."""


def ask(question: str, range_key: str = "1m", sector: str = "All",
        start=None, end=None) -> dict[str, Any]:
    """Answer one question about the current A/D scan. Never raises."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "Ask a question first."}
    if len(question) > MAX_QUESTION_CHARS:
        return {"ok": False,
                "error": f"Question is too long (max {MAX_QUESTION_CHARS} characters)."}

    scan = ac.accumulation_scan(range_key=range_key, sector=sector, start=start, end=end)
    if not scan.get("ok"):
        return {"ok": False,
                "error": scan.get("reason", "No scan available for this window.")}

    brief = build_brief(scan, sector)
    user_text = (
        "Question from the desk:\n"
        f"{question}\n\n"
        "Answer it using ONLY this brief:\n"
        + json.dumps(brief, default=str, indent=1)
    )
    # Budget must be generous: Gemini 3.x flash models spend part of
    # maxOutputTokens on internal reasoning before emitting a single visible
    # character, so a tight cap truncates the answer mid-sentence rather than
    # producing a shorter one. Length is controlled by the prompt, not this.
    res = call_llm(_SYSTEM_PROMPT, user_text, max_tokens=4000, temperature=0.35)
    if not res.get("text"):
        return {"ok": False, "error": res.get("error", "AI request failed.")}

    return {
        "ok": True,
        "question": question,
        "answer_html": markdown_lite_to_html(res["text"]),
        "answer_text": res["text"],
        "model": res["model"],
        "provider": res["provider"],
        # Shown under the answer so the reader always knows what it saw.
        "grounding": {
            "sessions": scan.get("sessions"),
            "as_of": str(scan.get("as_of")),
            "equities_scored": scan.get("universe"),
            "rows_supplied": len(brief["top_of_ranking"]) + len(brief["bottom_of_ranking"]),
            "sector": sector,
        },
    }
