"""
Автовердикт гипотез Фазы 4 (Аналитик-Гипотезник).

Раз в сутки крон (web/app.py._cron_hypothesis_verdict) вызывает
run_hypothesis_verdict(): берёт открытые гипотезы возрастом 7-14 дней,
сверяет их ИЗМЕРИМОЕ ожидание (hypothesis_journal.build_expectation) с
ФАКТОМ продаж по ad_ids (creative_kb), выносит вердикт confirmed/refuted/
inconclusive и пишет по каждой закрытой гипотезе человекочитаемый урок в
learnings (source='hypothesis').

КРИТИЧНО про None-семантику (миграция 010, B1): payments/qual_leads IS NULL
означает «сверка с AMO ещё не проводилась», а НЕ «0 оплат». Поэтому факт
считается только по строкам creative_kb, где outcomes_matched_at IS NOT NULL
(matched_rows). Если matched_rows == 0 — данных для вердикта нет, это
inconclusive, а не refuted (иначе бот сделает ложный вывод «не конвертит»
там, где просто ещё не пришла сверка из AMO).

См. docs/specs/ARCH-phase4-hypothesist.md §6.2, §8.
"""

import json
import logging
from datetime import datetime

from services.creative_intelligence import _get_connection
from services.hypothesis_journal import _local_naive, get_open_hypotheses

logger = logging.getLogger(__name__)

# Минимальный расход для вынесения вердикта (как pattern_engine.MIN_SPEND_FOR_EVAL,
# §8 спеки: HYP_MIN_SPEND) — ниже этого порога данных мало, вердикт откладываем.
HYP_MIN_SPEND = 15.0

# Возрастное окно открытых гипотез, которые вообще рассматриваем на этом проходе.
# Верхняя граница символическая (999 дней) — реальное принудительное закрытие
# делает MAX_AGE_DAYS ниже, отдельно от диапазона выборки.
_MIN_AGE_DAYS = 7
_MAX_AGE_DAYS_FETCH = 999

# Возраст, после которого гипотеза закрывается принудительно (inconclusive),
# даже если данных так и не накопилось — не должна висеть вечно (§AC4).
MAX_AGE_DAYS = 14

# Минимум лидов для вердикта по CPL (иначе одна случайная заявка решает вердикт)
_MIN_LEADS_FOR_CPL = 2

# SQLite лимит переменных на запрос — батчим IN(...) с запасом
_BATCH_SIZE = 900


def _fetch_facts(ad_ids: list[str]) -> dict:
    """Факт по ad_ids из creative_kb (продажи — главный сигнал).

    ВАЖНО про None-семантику (миграция 010): payments/qual_leads IS NULL =
    «не сверялись», НЕ 0. Считаем:
      payments = SUM(COALESCE(payments,0)) ТОЛЬКО по строкам с
                 outcomes_matched_at IS NOT NULL
      matched_rows = число строк где outcomes_matched_at IS NOT NULL
      spend = SUM(spend) по ВСЕМ строкам (расход был всегда, сверка ни при чём)
      leads = SUM(leads) по ВСЕМ строкам
      qual_pct = SUM(qual_leads)/SUM(leads)*100 по matched-строкам (или None)
      cpl = SUM(spend)/SUM(leads) если leads>0 иначе None

    Возвращает {"payments": int, "matched_rows": int, "spend": float,
                "leads": int, "qual_pct": float|None, "cpl": float|None}.
    Если matched_rows==0 — данных для вердикта нет (обрабатывается в _evaluate).
    """
    empty_facts = {
        "payments": 0,
        "matched_rows": 0,
        "spend": 0.0,
        "leads": 0,
        "qual_pct": None,
        "cpl": None,
    }
    if not ad_ids:
        return empty_facts

    total_spend = 0.0
    total_leads = 0
    total_payments_matched = 0
    total_qual_leads_matched = 0
    total_leads_matched = 0
    matched_rows = 0

    conn = _get_connection()
    try:
        for batch_start in range(0, len(ad_ids), _BATCH_SIZE):
            batch = ad_ids[batch_start : batch_start + _BATCH_SIZE]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""
                SELECT spend, leads, payments, qual_leads, outcomes_matched_at
                FROM creative_kb
                WHERE ad_id IN ({placeholders})
                """,
                tuple(batch),
            ).fetchall()

            for row in rows:
                total_spend += float(row["spend"] or 0)
                total_leads += int(row["leads"] or 0)

                if row["outcomes_matched_at"] is None:
                    continue

                matched_rows += 1
                total_payments_matched += int(row["payments"] or 0)
                total_leads_matched += int(row["leads"] or 0)
                total_qual_leads_matched += int(row["qual_leads"] or 0)
    finally:
        conn.close()

    qual_pct = None
    if matched_rows > 0 and total_leads_matched > 0:
        qual_pct = round(total_qual_leads_matched / total_leads_matched * 100, 1)

    cpl = None
    if total_leads > 0:
        cpl = round(total_spend / total_leads, 2)

    return {
        "payments": total_payments_matched,
        "matched_rows": matched_rows,
        "spend": round(total_spend, 2),
        "leads": total_leads,
        "qual_pct": qual_pct,
        "cpl": cpl,
    }


def _format_label(ad_format: str, angle: str) -> str:
    """Собирает читаемое '{формат} / {угол}' без мусорного слэша, если формат пуст."""
    ad_format = (ad_format or "").strip()
    angle = (angle or "").strip()
    if ad_format:
        return f"{ad_format} / {angle}"
    return angle


def _evaluate(hyp: dict, facts: dict) -> tuple[str, str]:
    """Выносит вердикт по гипотезе. Возвращает (verdict, lesson_text).

    verdict ∈ {"confirmed","refuted","inconclusive"}. lesson_text — русский,
    без кода/SQL/ad_id (ad_id идут отдельно в evidence). Логика по §8 спеки.
    """
    exp = hyp.get("expectation") or {}
    city = hyp.get("city") or ""
    label = _format_label(hyp.get("ad_format", ""), hyp.get("angle", ""))
    age_days = hyp.get("age_days", 0)
    launches = len(hyp.get("ad_ids") or [])
    spend = facts.get("spend", 0.0)

    # Нет данных вообще: сверка не пришла или расход слишком мал.
    if facts.get("matched_rows", 0) == 0 or spend < HYP_MIN_SPEND:
        return (
            "inconclusive",
            f"Мало данных: расход ${spend:.2f}, сверка AMO не пришла — вердикт отложен",
        )

    metric = exp.get("metric")
    threshold = exp.get("threshold")

    if metric == "payments":
        payments = facts.get("payments", 0)
        if payments >= threshold:
            verdict = "confirmed"
            lesson = (
                f"{label} в {city}: подтвердилось — {payments} оплат "
                f"за {age_days} дней ({launches} запусков)"
            )
        else:
            verdict = "refuted"
            lesson = (
                f"{label} в {city} не конвертит: {launches} запусков, "
                f"{payments} оплат за {age_days} дней"
            )
        return verdict, lesson

    if metric == "cpl":
        cpl = facts.get("cpl")
        leads = facts.get("leads", 0)
        if cpl is None or leads < _MIN_LEADS_FOR_CPL:
            return (
                "inconclusive",
                f"Мало лидов ({leads}) для оценки CPL в {city} — вердикт отложен",
            )
        if cpl <= threshold:
            verdict = "confirmed"
            outcome = "подтвердилось"
        else:
            verdict = "refuted"
            outcome = "дороже ожидания"
        lesson = f"{label} в {city}: CPL ${cpl} vs ожидание ${threshold} — {outcome}"
        return verdict, lesson

    if metric == "qual_pct":
        qual_pct = facts.get("qual_pct")
        if qual_pct is None:
            return (
                "inconclusive",
                f"Нет сверенных лидов для оценки квала в {city} — вердикт отложен",
            )
        if qual_pct >= threshold:
            verdict = "confirmed"
            outcome = "подтвердилось"
        else:
            verdict = "refuted"
            outcome = "ниже порога"
        lesson = f"{label} в {city}: квал {qual_pct}% vs порог {threshold}% — {outcome}"
        return verdict, lesson

    # Неизвестная метрика (защита от битого expectation_json) — не делаем ложных выводов.
    return (
        "inconclusive",
        f"Неизвестная метрика ожидания для {label} в {city} — вердикт отложен",
    )


def _write_learning(hyp: dict, verdict: str, lesson: str) -> int:
    """Пишет урок в learnings (source='hypothesis'). Возвращает learning_id.

    evidence_ad_ids — ad_id гипотезы (доказательство в БД, не в тексте урока).
    confidence: confirmed → 'confirmed', refuted → 'confirmed' (тоже факт, просто
    отрицательный), inconclusive уроков в learnings НЕ пишем вовсе (нет вывода).
    """
    ad_ids = hyp.get("ad_ids") or []
    tags = f"hypothesis,verdict:{verdict},city:{hyp.get('city','')}"

    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO learnings
                (statement, evidence_ad_ids, confidence, source, tags, created_at)
            VALUES (?, ?, 'confirmed', 'hypothesis', ?, datetime('now'))
            """,
            (lesson, json.dumps(ad_ids, ensure_ascii=False), tags),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _close_hypothesis(
    hyp_id: int, verdict: str, lesson: str, facts: dict, now_iso: str, learning_id: int | None
) -> None:
    """Закрывает гипотезу: пишет status/verdict_at/lesson/facts_json/learning_id."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            UPDATE hypotheses
            SET status = ?, verdict_at = ?, lesson = ?, facts_json = ?, learning_id = ?
            WHERE id = ?
            """,
            (
                verdict,
                now_iso,
                lesson,
                json.dumps(facts, ensure_ascii=False),
                learning_id,
                hyp_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def run_hypothesis_verdict(now: datetime | None = None) -> dict:
    """Главная функция крона вердикта.

    1. hyps = get_open_hypotheses(min=7, max=999) — младше 7 дней уже
       отфильтрованы на уровне SQL (get_open_hypotheses).
    2. Для каждой: facts = _fetch_facts(ad_ids); verdict, lesson = _evaluate(...).
       - Если verdict == 'inconclusive' И age_days <= MAX_AGE_DAYS (14) —
         гипотезу НЕ закрываем (даём данным дособраться), still_open++.
       - Иначе (verdict != 'inconclusive' ИЛИ age_days > MAX_AGE_DAYS) — закрываем.
    3. Закрытие: для confirmed/refuted пишем урок в learnings и связываем
       learning_id; для принудительного inconclusive (>14 дней) урок тоже
       пишем — это тоже вывод («данных не накопилось»), полезно для отчёта.
    Возвращает {"evaluated": n, "confirmed": n, "refuted": n, "inconclusive": n,
                "still_open": n, "learnings_written": n}.

    Не бросает наружу ничего, КРОМЕ RuntimeError 'KB не инициализирована'
    (пробрасывается из _get_connection/get_open_hypotheses — крон в web/app.py
    ловит его сам и пишет warning).
    """
    # Крон передаёт aware локальное время, а hypotheses хранит naive-локальные
    # строки — приводим один раз на входе, иначе verdict_at уедет в другую
    # конвенцию, чем created_at (см. hypothesis_journal._local_naive).
    now = _local_naive(now or datetime.now())
    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")

    hyps = get_open_hypotheses(min_age_days=_MIN_AGE_DAYS, max_age_days=_MAX_AGE_DAYS_FETCH, now=now)

    stats = {
        "evaluated": 0,
        "confirmed": 0,
        "refuted": 0,
        "inconclusive": 0,
        "still_open": 0,
        "learnings_written": 0,
    }

    for hyp in hyps:
        stats["evaluated"] += 1
        ad_ids = hyp.get("ad_ids") or []
        facts = _fetch_facts(ad_ids)
        verdict, lesson = _evaluate(hyp, facts)
        age_days = hyp.get("age_days", 0)

        force_close_age = age_days > MAX_AGE_DAYS
        if verdict == "inconclusive" and not force_close_age:
            # Данных пока мало, но возраст ещё не критичный — ждём следующего прохода.
            stats["still_open"] += 1
            continue

        if verdict == "inconclusive" and force_close_age:
            # Принудительное закрытие: за 14 дней данных не накопилось (§8).
            lesson = (
                f"{_format_label(hyp.get('ad_format', ''), hyp.get('angle', ''))} "
                f"в {hyp.get('city', '')}: за 14 дней данных не накопилось "
                f"(расход ${facts.get('spend', 0.0):.2f}) — вывод не сделан"
            )

        try:
            learning_id = _write_learning(hyp, verdict, lesson)
            stats["learnings_written"] += 1
        except Exception:
            # Запись урока некритична для целостности вердикта — гипотезу всё
            # равно закрываем, но лог не глотаем молча.
            logger.exception(
                "hypothesis_verdict: не удалось записать урок для гипотезы id=%s", hyp.get("id")
            )
            learning_id = None

        _close_hypothesis(hyp["id"], verdict, lesson, facts, now_iso, learning_id)
        stats[verdict] += 1

    logger.info(
        "hypothesis_verdict: evaluated=%d confirmed=%d refuted=%d inconclusive=%d "
        "still_open=%d learnings_written=%d",
        stats["evaluated"], stats["confirmed"], stats["refuted"], stats["inconclusive"],
        stats["still_open"], stats["learnings_written"],
    )
    return stats


def run_verdict_pass(now: datetime | None = None) -> dict:
    """Never-throw обёртка для крона (web/app.py._cron_hypothesis_verdict).

    Ловит и логирует ЛЮБОЕ исключение (включая RuntimeError 'KB не
    инициализирована') — крон никогда не должен падать молча. Возвращает
    статистику run_hypothesis_verdict() либо словарь с error при сбое.
    """
    try:
        return run_hypothesis_verdict(now=now)
    except Exception as exc:
        logger.warning("hypothesis_verdict: сбой прохода вердикта: %s", exc)
        return {
            "evaluated": 0,
            "confirmed": 0,
            "refuted": 0,
            "inconclusive": 0,
            "still_open": 0,
            "learnings_written": 0,
            "error": str(exc),
        }
