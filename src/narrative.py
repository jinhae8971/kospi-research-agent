"""Weekly narrative synthesis over recent daily reports."""
from __future__ import annotations

import json
from collections import Counter

from .analyzer import _call_claude, _extract_json
from .config import get_settings
from .logging_setup import get_logger
from .models import DailyReport, NarrativeInsight, StockAnalysis

log = get_logger(__name__)


def _load_system_prompt() -> str:
    settings = get_settings()
    return (settings.prompts_dir / "narrative_system.md").read_text(encoding="utf-8")


def _summarize_report(report: DailyReport) -> dict:
    return {
        "date": report.date,
        "gainers": [
            {
                "ticker": g.ticker,
                "name": g.name,
                "change_pct_nd": round(g.change_pct_nd, 2),
            }
            for g in report.gainers
        ],
        "analyses": [
            {
                "ticker": a.ticker,
                "name": a.name,
                "pump_thesis": a.pump_thesis,
                "sector_tags": a.sector_tags,
                "confidence": a.confidence,
            }
            for a in report.analyses
        ],
    }


def _data_driven_fallback(
    today_analyses: list[StockAnalysis],
    has_history: bool,
    exc: Exception,
) -> NarrativeInsight:
    """Generate a minimal data-driven narrative when Claude synthesis fails."""
    tag_counts: Counter = Counter()
    for a in today_analyses:
        for t in (a.sector_tags or []):
            tag_counts[t] += 1
    hot_sectors = [t for t, _ in tag_counts.most_common(3)]

    fallback_narrative = (
        f"오늘 상승 종목 {len(today_analyses)}개의 섹터 태그 분포를 기반으로 한 "
        f"데이터 주도 요약입니다 (Claude 내러티브 합성 실패 시 자동 fallback)."
    )
    return NarrativeInsight(
        current_narrative=fallback_narrative,
        hot_sectors=hot_sectors,
        cooling_sectors=[],
        week_over_week_change="이전 히스토리 비교 불가" if not has_history else "",
        investment_insight=(
            f"Claude 합성 오류: {type(exc).__name__}: {str(exc)[:120]}. "
            f"다음 실행 시 자동 복구 예상."
        ),
    )


def synthesize_narrative(
    today_analyses: list[StockAnalysis],
    prior_reports: list[DailyReport],
) -> NarrativeInsight:
    """Combine today's analyses + recent reports into a narrative insight."""
    today_summary = {
        "date": "today",
        "analyses": [
            {
                "ticker": a.ticker,
                "name": a.name,
                "pump_thesis": a.pump_thesis,
                "sector_tags": a.sector_tags,
                "confidence": a.confidence,
            }
            for a in today_analyses
        ],
    }
    history = [_summarize_report(r) for r in prior_reports]

    user_text = (
        "최근 일별 보고서 이력(최신순)과 오늘의 분석 결과입니다. "
        "시스템 프롬프트의 JSON 스키마에 맞춰 내러티브를 반환해주세요.\n\n"
        "중요: 응답은 반드시 `{`로 시작하는 JSON 객체여야 하며, 그 외 텍스트나 "
        "마크다운 코드 펜스는 포함하지 마세요. 히스토리가 비어있어도 거부하지 말고 "
        "오늘 데이터만으로 내러티브를 생성해주세요.\n\n"
        + json.dumps(
            {"today": today_summary, "history": history},
            ensure_ascii=False,
            indent=2,
        )
    )

    raw = ""
    try:
        raw = _call_claude(_load_system_prompt(), user_text)
        data = _extract_json(raw)
        return NarrativeInsight(
            current_narrative=data.get("current_narrative", ""),
            hot_sectors=list(data.get("hot_sectors") or []),
            cooling_sectors=list(data.get("cooling_sectors") or []),
            week_over_week_change=data.get("week_over_week_change", ""),
            investment_insight=data.get("investment_insight", ""),
        )
    except Exception as exc:  # noqa: BLE001
        if raw:
            log.error(
                "narrative synthesis failed: %s | raw response (first 500 chars): %r",
                exc, raw[:500],
            )
        else:
            log.error("narrative synthesis failed before response: %s", exc)
        return _data_driven_fallback(today_analyses, bool(prior_reports), exc)
