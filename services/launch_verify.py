"""
Контроль запуска — проверяет, что сегодняшние авто-запущенные объявления
реально крутятся в Facebook (закрывает три слепые зоны бота).

Слепые зоны ДО этого модуля:
1. Свежие объявления попадают в creative_kb только ночью (backfill) — днём
   бот про них "не знает".
2. PENDING_REVIEW / IN_PROCESS (модерация FB) никак не проверяется.
3. ACTIVE, но 0 показов ("не крутится") — тоже не проверяется.

Источник ad_id: data/auto_launch_state.json, launched_ever[card_id]["ad_ids"] —
плоский список ad_id, добавленных сегодняшним авто-запуском (см. services/auto_launch.py
_execute_launch — там же парсится status["log"] через extract_ad_ids_from_log).

Обычный контроль сегодняшних запусков остаётся read-only. Отдельный replacement
контур может вызвать workflow-bound single-ad cleaner и поставить старое
объявление на PAUSE, но только через orchestrator после exact ACTIVE evidence.
"""

import logging
import hashlib
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from services import state_store

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается) — единое со всеми остальными модулями проекта
_TZ_LOCAL = timezone(timedelta(hours=5))

# Путь к state авто-запуска (источник ad_id сегодняшних запусков)
_AUTO_LAUNCH_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "auto_launch_state.json"

# Путь к state-файлу результата проверки (дайджест читает его за вчера)
_LAUNCH_VERIFY_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "launch_verify_state.json"

# Потолок объявлений за один прогон — не заваливаем FB API большим batch
MAX_ADS_PER_RUN = 20

# Сколько часов даём модерации FB, прежде чем считать PENDING_REVIEW проблемой
# для отчёта (используется только в тексте "в модерации уже N часов" — сам
# факт "уже в модерации" в проблемы попадает всегда, часы — для контекста).
_MODERATION_REPORT_AFTER_HOURS = 0

_REPLACEMENT_VERIFY_LIMIT = 20


@dataclass(frozen=True, slots=True)
class VerificationRun:
    """Итог одного worker-прохода по durable launch watchdogs."""

    checked: int
    verified: int
    pending: int
    failed: int
    reconcile_required: int
    errors: tuple[str, ...]


def _owner_launch_repository():
    from services.launch_repository import OwnerLaunchRepository

    return OwnerLaunchRepository()


def register_executed_launch(
    action_run,
    *,
    now: datetime | None = None,
    scheduler_run_id: str | None = None,
) -> str:
    """Создаёт watchdog из exact EXECUTED owner claims и provider bindings."""

    from services import launch_repository
    from services.owner_action_repository import get_proposal

    registered_at = now or datetime.now(timezone.utc)
    if registered_at.tzinfo is None or registered_at.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    if (
        str(getattr(action_run, "state", "")) not in {"EXECUTED", "FAILED_NO_EFFECT"}
        or bool(getattr(action_run, "reconciliation_required", False))
    ):
        raise ValueError("Watchdog создаётся только из exact EXECUTED owner run")
    proposal_id = str(getattr(action_run, "proposal_id", "") or "")
    proposal = get_proposal(proposal_id)
    if proposal is None or proposal.proposal_kind.value != "LAUNCH":
        raise ValueError("Owner LAUNCH proposal не найден")
    executions = tuple(getattr(action_run, "executions", ()) or ())
    if not executions:
        raise ValueError("Owner run не содержит ни одного исполнения")

    # Watchdog ставится ПО-CLAIM'НО, а не «всё или ничего»: run, доехавший до
    # EXECUTED после ретраев, несёт исполнения только своего тика, и строгая
    # кардинальность роняла регистрацию целиком — ноль watchdog'ов и алерт
    # «Контроль запуска не поставлен» при живых объявлениях. Claim'ы прежних
    # тиков подтверждает reconcile верификатора по фактам кабинета.
    # Пара claim↔execution строится по ordinal exact-манифеста
    # (`<manifest_id>:<ordinal>` — см. agent/launcher._owner_launch_plan).
    executions_by_ordinal: dict[int, object] = {}
    for execution in executions:
        raw_manifest_id = str(getattr(execution, "action_manifest_id", "") or "")
        head, separator, tail = raw_manifest_id.rpartition(":")
        if not separator or not head or not tail.isdigit():
            raise ValueError("Owner execution без ordinal exact-манифеста")
        ordinal = int(tail)
        created = tuple(getattr(execution, "created_ids", ()) or ())
        if len(created) > 1:
            raise ValueError("Каждый owner launch claim должен создать один exact ad")
        if not created:
            # Попытка без созданного ad (ретрай, отклонённый review) — нечего
            # сторожить; падение регистрации здесь оставляло бы без watchdog
            # и реально созданные claim'ы того же run.
            continue
        existing = executions_by_ordinal.get(ordinal)
        if existing is not None:
            existing_created = tuple(getattr(existing, "created_ids", ()) or ())
            if existing_created != created:
                raise ValueError("Противоречивые executions одного claim")
            continue
        executions_by_ordinal[ordinal] = execution

    targets: list[launch_repository.LaunchWatchdogTarget] = []
    for target in proposal.targets:
        execution = executions_by_ordinal.get(target.ordinal)
        if execution is None:
            # Claim исполнялся в прежнем тике или ещё не исполнялся — его
            # подтверждение не задача этого watchdog.
            continue
        created_ids = tuple(getattr(execution, "created_ids", ()) or ())
        if len(created_ids) != 1:
            raise ValueError("Каждый owner launch claim должен создать один exact ad")
        payload = target.intended_payload
        destinations = payload.get("destinations")
        if not isinstance(destinations, tuple) or len(destinations) != 1:
            raise ValueError("Owner launch target manifest должен содержать один adset")
        destination = destinations[0]
        if not isinstance(destination, Mapping):
            raise ValueError("Owner launch destination повреждён")
        creatives = destination.get("creatives")
        if not isinstance(creatives, tuple) or len(creatives) != 1:
            raise ValueError("Owner launch target manifest должен содержать один creative")
        creative = creatives[0]
        if not isinstance(creative, Mapping):
            raise ValueError("Owner launch creative повреждён")
        manifest_id = str(payload.get("manifest_id") or "")
        auth_id = (
            "launch-auth-gateway-"
            + hashlib.sha256(manifest_id.encode("utf-8")).hexdigest()[:32]
        )
        bindings = launch_repository.get_provider_ad_bindings(auth_id)
        matching = [
            binding
            for binding in bindings
            if str(binding.get("ad_id") or "") == str(created_ids[0])
            and str(binding.get("phase") or "") == "VERIFIED"
        ]
        if len(matching) != 1:
            raise ValueError("Exact provider semantic binding не найден")
        binding = matching[0]
        targets.append(
            launch_repository.LaunchWatchdogTarget(
                claim_id=target.claim_id,
                account_id=target.account_id,
                adset_id=str(target.adset_id or ""),
                expected_ad_name=str(creative.get("ad_name") or ""),
                expected_fingerprint=str(binding["expected_fingerprint"]),
                created_ad_id=str(created_ids[0]),
            )
        )
    if not targets:
        raise ValueError("Ни один claim этого run не дал watchdog-цели")
    return _owner_launch_repository().create_watchdog(
        proposal_id=proposal_id,
        decision_id=str(getattr(action_run, "decision_id", "") or ""),
        job_id=str(getattr(action_run, "job_id", "") or ""),
        targets=tuple(targets),
        now=registered_at,
        scheduler_run_id=scheduler_run_id,
    )


def _fetch_watchdog_live_ads(
    targets: Sequence[object],
    repository,
) -> Mapping[str, object]:
    """Читает полный account inventory и добавляет pre-POST fingerprint."""

    from config import FB_ACCOUNT_ID_ONLINE
    from integrations.facebook import fetch_complete_account_ad_inventory
    from services.launch_repository import LaunchTargetObservation

    expected_ids = {
        str(getattr(target, "created_ad_id"))
        for target in targets
    }
    fingerprints = repository.get_semantic_fingerprints(tuple(expected_ids))
    if set(fingerprints) != expected_ids:
        raise RuntimeError("LAUNCH_SEMANTIC_FINGERPRINT_UNAVAILABLE")

    online_account_id = str(FB_ACCOUNT_ID_ONLINE).removeprefix("act_")
    rows_by_id: dict[str, dict[str, object]] = {}
    account_ids = {
        str(getattr(target, "account_id")).removeprefix("act_")
        for target in targets
    }
    for account_id in sorted(account_ids):
        account_kind = "online" if account_id == online_account_id else "offline"
        rows = fetch_complete_account_ad_inventory(account_kind, account_id)
        for row in rows:
            ad_id = str(row.get("id") or "")
            if ad_id in expected_ids:
                if ad_id in rows_by_id:
                    raise RuntimeError("LAUNCH_EXACT_AD_DUPLICATE")
                rows_by_id[ad_id] = row

    return {
        ad_id: LaunchTargetObservation(
            created_ad_id=ad_id,
            account_id=str(row.get("account_id") or ""),
            adset_id=str(row.get("adset_id") or ""),
            ad_name=str(row.get("name") or ""),
            configured_status=str(row.get("status") or ""),
            effective_status=str(row.get("effective_status") or ""),
            fingerprint=fingerprints[ad_id],
        )
        for ad_id, row in rows_by_id.items()
    }


def verify_launch_watchdogs(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 50,
    on_transition=None,
) -> VerificationRun:
    """Проверяет exact created IDs; provider mutation здесь принципиально нет.

    Args:
        on_transition: необязательный колбэк, получает каждый
            LaunchWatchdogTransition. Нужен отчёту (см.
            verify_and_report_launch_watchdogs), чтобы не дублировать обход
            watchdog-ов и не расширять VerificationRun.
    """

    from services.launch_repository import LaunchRepositoryBlocked

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    checked_at = checked_at.astimezone(timezone.utc)
    repository = _owner_launch_repository()
    leases = repository.claim_due(
        worker_id=worker_id,
        now=checked_at,
        limit=limit,
    )
    verified = 0
    pending = 0
    failed = 0
    reconcile_required = 0
    errors: list[str] = []
    for lease in leases:
        fetch_complete = True
        observations: Mapping[str, object] = {}
        try:
            observations = _fetch_watchdog_live_ads(lease.targets, repository)
        except Exception as exc:
            fetch_complete = False
            errors.append(f"{lease.watchdog_id}:{type(exc).__name__}")
            logger.warning(
                "launch watchdog %s: live inventory недоступен — %s",
                lease.watchdog_id,
                exc,
            )
        try:
            transition = repository.record_observation(
                lease,
                observations=observations,
                fetch_complete=fetch_complete,
                now=checked_at,
            )
        except LaunchRepositoryBlocked as exc:
            errors.append(f"{lease.watchdog_id}:{exc.code}")
            logger.warning(
                "launch watchdog %s: durable transition заблокирован — %s",
                lease.watchdog_id,
                exc.code,
            )
            continue
        if on_transition is not None:
            # Сбой отчёта не должен прятать durable результат проверки.
            try:
                on_transition(transition)
            except Exception as exc:
                errors.append(f"{lease.watchdog_id}:REPORT_{type(exc).__name__}")
                logger.warning(
                    "launch watchdog %s: колбэк отчёта упал — %s",
                    lease.watchdog_id,
                    exc,
                )
        if transition.state == "VERIFIED":
            verified += 1
        elif transition.state == "FAILED_VERIFICATION":
            failed += 1
        elif transition.state == "RECONCILE_REQUIRED":
            reconcile_required += 1
        else:
            pending += 1
    return VerificationRun(
        checked=len(leases),
        verified=verified,
        pending=pending,
        failed=failed,
        reconcile_required=reconcile_required,
        errors=tuple(errors),
    )


def finalize_executed_launch(
    action_run,
    *,
    now: datetime | None = None,
    scheduler_run_id: str | None = None,
    send_telegram_fn=None,
) -> dict:
    """Пост-исполнение owner LAUNCH: ставит ACTIVE-гейт, успех НЕ объявляет.

    Вызывается сразу после исполнения одобренного владельцем LAUNCH. Ничего не
    мутирует в Facebook: только создаёт durable watchdog, который затем
    подтверждает по живому effective_status, что объявление реально ACTIVE в
    ЦЕЛЕВОМ адсете (см. verify_and_report_launch_watchdogs).

    Здесь сознательно нет ни одного «✅ запущено»: на этом шаге известно лишь
    то, что Facebook принял CREATE. Отчёт владельцу — «создано, ждём ACTIVE».

    Returns:
        {"registered": bool, "watchdog_id": str | None, "reason": str | None}
    """
    from services.owner_action_repository import get_proposal

    send = send_telegram_fn or _send_launch_verify_telegram
    proposal_id = str(getattr(action_run, "proposal_id", "") or "")
    state = str(getattr(action_run, "state", "") or "")

    created_any = any(
        getattr(execution, "created_ids", ()) or ()
        for execution in (getattr(action_run, "executions", ()) or ())
    )
    if bool(getattr(action_run, "reconciliation_required", False)) or not (
        state == "EXECUTED" or (state == "FAILED_NO_EFFECT" and created_any)
    ):
        # Требует сверки или ничего не создано — watchdog не создаём, успех не рапортуем.
        # FAILED_NO_EFFECT с созданными объявлениями — законный частичный запуск
        # (часть городов пропущена без эффекта), созданное сторожим.
        return {"registered": False, "watchdog_id": None, "reason": f"STATE_{state or 'UNKNOWN'}"}
    try:
        proposal = get_proposal(proposal_id)
    except Exception as exc:
        logger.warning("finalize_executed_launch: proposal %s не прочитан — %s", proposal_id, exc)
        return {"registered": False, "watchdog_id": None, "reason": "PROPOSAL_READ_FAILED"}
    if proposal is None or proposal.proposal_kind.value != "LAUNCH":
        return {"registered": False, "watchdog_id": None, "reason": "NOT_LAUNCH"}

    try:
        watchdog_id = register_executed_launch(
            action_run,
            now=now,
            scheduler_run_id=scheduler_run_id,
        )
    except Exception as exc:
        # Fail-closed по смыслу: без watchdog запуск НИКОГДА не станет VERIFIED,
        # поэтому «запущено» не прозвучит. Ошибку делаем видимой владельцу.
        logger.error(
            "finalize_executed_launch: ACTIVE-гейт не поставлен для %s — %s",
            proposal_id,
            exc,
        )
        _send_launch_critical(
            "Контроль запуска не поставлен",
            f"proposal {proposal_id}: {type(exc).__name__}. "
            "Объявления могли быть созданы, но подтвердить ACTIVE автоматически нельзя — "
            "нужна ручная проверка в Facebook.",
        )
        return {"registered": False, "watchdog_id": None, "reason": "WATCHDOG_REGISTER_FAILED"}

    send(
        "🕒 Создано, ждём ACTIVE\n"
        f"proposal {proposal_id}: Facebook принял создание объявлений.\n"
        "«Запущено» будет объявлено только после живой проверки effective_status=ACTIVE "
        "в целевом адсете."
    )
    return {"registered": True, "watchdog_id": watchdog_id, "reason": None}


def _send_launch_verify_telegram(text: str) -> None:
    """Обычное уведомление контроля запуска; сбой отправки не роняет проверку."""
    try:
        from services.notifications import send_telegram

        send_telegram(text, channel="ads")
    except Exception as exc:
        logger.warning("launch verify: Telegram-уведомление не ушло — %s", type(exc).__name__)


def _send_launch_critical(title: str, detail: str) -> None:
    """Видимая ошибка запуска: критический алерт по всем каналам."""
    try:
        from services.notifications import send_critical_alert

        send_critical_alert(title, detail)
    except Exception as exc:
        logger.warning("launch verify: критический алерт не ушёл — %s", type(exc).__name__)


def _format_watchdog_transition(transition) -> tuple[str, str, str] | None:
    """Переводит terminal-состояние watchdog в (уровень, заголовок, детали).

    Возвращает None для нетерминальных состояний (VERIFYING/EXECUTED): проверка
    ещё идёт, и рапортовать о ней нечего — ни успеха, ни провала.
    """
    state = str(getattr(transition, "state", "") or "")
    verified = int(getattr(transition, "verified_count", 0) or 0)
    expected = int(getattr(transition, "expected_count", 0) or 0)
    proposal_id = str(getattr(transition, "proposal_id", "") or "")
    reason_code = str(getattr(transition, "reason_code", "") or "")

    if state == "VERIFIED":
        return (
            "ok",
            "✅ Запущено",
            f"proposal {proposal_id}: {verified} из {expected} объявлений подтверждены "
            "живым effective_status=ACTIVE в целевом адсете.",
        )
    if state == "FAILED_VERIFICATION":
        return (
            "critical",
            "❌ Запуск не подтверждён",
            f"proposal {proposal_id}: ACTIVE подтверждено только у {verified} из {expected} "
            f"объявлений, причина {reason_code}. «Запущено» не рапортуем — нужна ручная "
            "проверка в Facebook.",
        )
    if state == "RECONCILE_REQUIRED":
        return (
            "critical",
            "🚨 Запуск требует сверки",
            f"proposal {proposal_id}: живой инвентарь противоречит ожиданиям "
            f"({verified} из {expected}), причина {reason_code}. Автоматика остановлена.",
        )
    return None


def verify_and_report_launch_watchdogs(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 50,
) -> dict:
    """Гоняет ACTIVE-гейт запусков и честно рапортует результат владельцу.

    Проверку делает verify_launch_watchdogs (живой GET effective_status созданных
    ad в целевом адсете, никаких мутаций) — здесь только отчёт:

    * VERIFIED → «✅ Запущено» (единственное место, где это вообще звучит);
    * FAILED_VERIFICATION → критический алерт «не подтверждён» с деталями;
    * RECONCILE_REQUIRED → критический алерт «требует сверки»;
    * VERIFYING → молча, проверка продолжается до дедлайна WATCHDOG_VERIFY_TTL
      (24 ч). Застрявшую модерацию отдельно показывает дневной
      verify_todays_launches, поэтому здесь не спамим каждые 30 минут.

    Записи в owner_action_events и переходы lifecycle делает сам
    repository.record_observation — здесь они не дублируются.
    """
    reported: list[dict] = []

    def _report(transition) -> None:
        formatted = _format_watchdog_transition(transition)
        if formatted is None:
            return
        level, title, detail = formatted
        if level == "critical":
            _send_launch_critical(title, detail)
        else:
            _send_launch_verify_telegram(f"{title}\n{detail}")
        reported.append(
            {
                "watchdog_id": str(getattr(transition, "watchdog_id", "") or ""),
                "state": str(getattr(transition, "state", "") or ""),
                "level": level,
            }
        )

    run = verify_launch_watchdogs(
        worker_id=worker_id,
        now=now,
        limit=limit,
        on_transition=_report,
    )
    return {
        "checked": run.checked,
        "verified": run.verified,
        "pending": run.pending,
        "failed": run.failed,
        "reconcile_required": run.reconcile_required,
        "errors": list(run.errors),
        "reported": reported,
    }


def _load_auto_launch_state() -> dict:
    """Читает state авто-запуска (обёртка над state_store для мокирования в тестах)."""
    return state_store.load_json_state(_AUTO_LAUNCH_STATE_FILE)


def _get_today_str() -> str:
    """Сегодняшняя дата по локальному времени как строка 'YYYY-MM-DD'."""
    return datetime.now(_TZ_LOCAL).date().isoformat()


def get_todays_launched_ads() -> list[dict]:
    """Возвращает ad_id, запущенные СЕГОДНЯ через авто-запуск.

    Источник: data/auto_launch_state.json → launched_ever[card_id].
    Запись содержит "at" (когда запущена карточка) и "ad_ids" (плоский список
    ad_id, добавлен в auto_launch._execute_launch). Старые записи (до этой
    фичи) не имеют "ad_ids" — пропускаются молча, это не ошибка, а нормальный
    переходный период (следующий запуск уже будет с ad_ids).

    Returns:
        [{"ad_id": str, "card_name": str}, ...] — только за сегодня.
    """
    state = _load_auto_launch_state()
    launched_ever = state.get("launched_ever", {})
    today = _get_today_str()

    result: list[dict] = []
    for card_id, entry in launched_ever.items():
        if not isinstance(entry, dict):
            continue  # старый формат (просто строка) — нет ad_ids, пропускаем
        at_iso = entry.get("at", "")
        if not at_iso.startswith(today):
            continue
        ad_ids = entry.get("ad_ids") or []
        card_name = entry.get("name", card_id)
        for ad_id in ad_ids:
            result.append({"ad_id": ad_id, "card_name": card_name})

    return result


def _fetch_fb_status_and_impressions(ad_ids: list[str]) -> dict:
    """Живой FB-запрос: effective_status, name + показы за сегодня по каждому ad_id.

    Один запрос на chunk (≤MAX_ADS_PER_RUN), поле insights через field expansion —
    дешевле, чем отдельный запрос insights на каждое объявление (паттерн
    services/autopilot.py _fetch_candidate_fb_info, но с добавлением insights).

    Возвращает {ad_id: {"name": str, "effective_status": str, "impressions": int}}.
    При ошибке FB для конкретного chunk — эти ad_id просто отсутствуют в
    результате (вызывающий код должен это учитывать), исключение не пробрасывается
    наружу (fail-soft — контроль запуска не должен ронять остальные кроны).
    """
    if not ad_ids:
        return {}

    from agent.fb_common import _throttled_get, API
    from services.fb_token_provider import get_fb_token

    result: dict = {}
    ids_param = ",".join(ad_ids)
    try:
        resp = _throttled_get(
            f"{API}",
            params={
                "access_token": get_fb_token(),
                "ids": ids_param,
                "fields": "id,name,effective_status,insights.date_preset(today){impressions}",
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "launch_verify: FB вернул %d для %d объявлений",
                resp.status_code, len(ad_ids),
            )
            return result

        data = resp.json()
        for ad_id, info in data.items():
            insights_rows = (info.get("insights") or {}).get("data", [])
            impressions = int(insights_rows[0].get("impressions", 0)) if insights_rows else 0
            result[ad_id] = {
                "name": info.get("name", ""),
                "effective_status": info.get("effective_status", "UNKNOWN"),
                "impressions": impressions,
            }
    except Exception as exc:
        logger.warning("launch_verify: ошибка FB-запроса для %d объявлений — %s", len(ad_ids), exc)

    return result


# Статусы, означающие отказ модерации
_DISAPPROVED_STATUSES = {"DISAPPROVED", "WITH_ISSUES"}

# Статусы, означающие "ещё на модерации"
_PENDING_STATUSES = {"PENDING_REVIEW", "IN_PROCESS"}


def _classify_ad(effective_status: str, impressions: int) -> str:
    """Классифицирует объявление по effective_status + показам.

    Returns: "disapproved" | "pending" | "not_delivering" | "ok"
    """
    status = (effective_status or "").upper()
    if status in _DISAPPROVED_STATUSES:
        return "disapproved"
    if status in _PENDING_STATUSES:
        return "pending"
    if status == "ACTIVE" and impressions == 0:
        return "not_delivering"
    return "ok"


def verify_replacement_workflows(limit: int = _REPLACEMENT_VERIFY_LIMIT) -> dict:
    """Освобождает bound slot и завершает замену только после exact ACTIVE.

    Unbound ``WAITING_SLOT`` никогда не передаётся cleaner. Финальный verifier
    сам проверяет ordered IDs/names/adset/status и только затем вызывает общий
    pause guard старого объявления.
    """
    from services.autopilot import get_autopilot_config

    config = get_autopilot_config()
    replacement = config.get("replacement", {})
    if not isinstance(replacement, dict):
        return {"ran": False, "skipped_reason": "invalid_replacement_config"}
    enabled = replacement.get("enabled", False)
    if type(enabled) is not bool:
        return {"ran": False, "skipped_reason": "invalid_replacement_enabled"}
    if not enabled:
        return {"ran": False, "skipped_reason": "replacement_disabled"}
    kill_switch = config.get("kill_switch", False)
    if type(kill_switch) is not bool or kill_switch:
        return {"ran": False, "skipped_reason": "kill_switch"}

    from services.cleanup_repository import get_cleanup_status
    from services.replacement_orchestrator import (
        ensure_slot_for_workflow,
        verify_and_complete_replacements,
    )
    from services.replacement_workflow import get_replacement_launch

    slot_results: list[dict] = []
    slot_errors: list[str] = []
    try:
        status = get_cleanup_status()
        workflows = status.get("replacement_workflows")
        if not isinstance(workflows, list):
            raise ValueError("replacement_workflows_invalid")
        waiting_slot_rows = [
            row
            for row in workflows
            if isinstance(row, dict) and row.get("phase") == "WAITING_SLOT"
        ][:limit]
        for row in waiting_slot_rows:
            workflow_id = str(row.get("workflow_id") or "")
            if not workflow_id or not isinstance(get_replacement_launch(workflow_id), dict):
                continue
            try:
                outcome = ensure_slot_for_workflow(workflow_id)
                slot_results.append(asdict(outcome))
            except Exception as exc:
                logger.warning(
                    "launch_verify: slot workflow %s не продвинут — %s",
                    workflow_id,
                    type(exc).__name__,
                )
                slot_errors.append(f"{workflow_id}:slot_error:{type(exc).__name__}")
    except Exception as exc:
        logger.warning(
            "launch_verify: список replacement workflows недоступен — %s",
            type(exc).__name__,
        )
        slot_errors.append(f"workflow_list_error:{type(exc).__name__}")

    completion = verify_and_complete_replacements(limit=limit)
    if not is_dataclass(completion):
        return {
            "ran": False,
            "skipped_reason": "invalid_orchestrator_result",
            "slot_results": slot_results,
            "errors": slot_errors,
        }
    completion_result = asdict(completion)
    completion_errors = list(completion_result.pop("errors", ()))
    return {
        "skipped_reason": None,
        **completion_result,
        "slot_results": slot_results,
        "errors": [*slot_errors, *completion_errors],
    }


def _hours_since(at_iso: str, now: datetime) -> float:
    """Сколько часов прошло с момента запуска карточки (для "в модерации уже N часов")."""
    try:
        launched_at = datetime.fromisoformat(at_iso)
        if launched_at.tzinfo is None:
            launched_at = launched_at.replace(tzinfo=_TZ_LOCAL)
        return max((now - launched_at).total_seconds() / 3600.0, 0.0)
    except (ValueError, TypeError):
        return 0.0


def _build_ad_to_launch_at() -> dict[str, str]:
    """{ad_id: at_iso} — момент запуска карточки, для расчёта часов в модерации."""
    state = _load_auto_launch_state()
    launched_ever = state.get("launched_ever", {})
    today = _get_today_str()

    result: dict[str, str] = {}
    for entry in launched_ever.values():
        if not isinstance(entry, dict):
            continue
        at_iso = entry.get("at", "")
        if not at_iso.startswith(today):
            continue
        for ad_id in entry.get("ad_ids") or []:
            result[ad_id] = at_iso
    return result


def verify_todays_launches() -> dict:
    """Проверяет, что сегодняшние авто-запуски реально крутятся в FB.

    Алгоритм:
    1. Берёт ad_id сегодняшних запусков (get_todays_launched_ads).
    2. Если запусков сегодня не было — тихий скип (result launched=0).
    3. Иначе живой FB-запрос по ≤MAX_ADS_PER_RUN объявлениям (потолок,
       остальные — в следующий прогон крона).
    4. Классифицирует каждое: disapproved / pending / not_delivering / ok.
    5. Отправляет Telegram ТОЛЬКО если есть проблемы (нет проблем — просто лог).
    6. Сохраняет результат в data/launch_verify_state.json (дата + summary) —
       читает services/morning_digest.py для строки "Запуски вчера: крутятся X/Y".

    Returns:
        {"launched": int, "running": int, "problems": [...]}
        problems: [{"ad_id", "card_name", "kind", "city", "hours_pending"}]
    """
    launched_ads = get_todays_launched_ads()

    if not launched_ads:
        logger.info("launch_verify: сегодня авто-запусков не было — скип")
        return {"launched": 0, "running": 0, "problems": []}

    # Потолок за один прогон — если запусков сегодня много, добираем в след. тик
    ad_ids = [item["ad_id"] for item in launched_ads[:MAX_ADS_PER_RUN]]
    if len(launched_ads) > MAX_ADS_PER_RUN:
        logger.info(
            "launch_verify: сегодня %d объявлений, проверяю первые %d (потолок за прогон)",
            len(launched_ads), MAX_ADS_PER_RUN,
        )

    card_name_by_ad = {item["ad_id"]: item["card_name"] for item in launched_ads}
    fb_info = _fetch_fb_status_and_impressions(ad_ids)
    launch_at_by_ad = _build_ad_to_launch_at()
    now = datetime.now(_TZ_LOCAL)

    from services.creative_briefs import extract_city

    running = 0
    problems: list[dict] = []

    for ad_id in ad_ids:
        info = fb_info.get(ad_id)
        if info is None:
            # FB не ответил по этому ad_id (chunk упал целиком, либо объявление
            # ещё не проиндексировано FB) — не считаем проблемой, просто пропускаем.
            continue

        kind = _classify_ad(info["effective_status"], info["impressions"])
        ad_name = info.get("name") or ""
        city = extract_city(ad_name) if ad_name else "?"

        if kind == "ok":
            running += 1
            continue

        hours_pending = _hours_since(launch_at_by_ad.get(ad_id, ""), now)
        problems.append({
            "ad_id": ad_id,
            "card_name": card_name_by_ad.get(ad_id, ad_id),
            "city": city,
            "kind": kind,
            "hours_pending": round(hours_pending, 1),
        })

    result = {
        "launched": len(ad_ids),
        "running": running,
        "problems": problems,
    }

    if problems:
        _send_problems_telegram(problems)
    else:
        logger.info(
            "launch_verify: все %d объявлений крутятся нормально",
            len(ad_ids),
        )

    _save_verify_state(result)

    return result


_KIND_LABELS = {
    "disapproved": "🔴 отклонено модерацией",
    "pending": "🟡 в модерации",
    "not_delivering": "🟠 не крутится (0 показов)",
}


def _format_problem_line(problem: dict) -> str:
    """Форматирует одну строку проблемы для Telegram (город жирным)."""
    import html

    city = html.escape(problem["city"])
    card_name = html.escape(problem["card_name"])
    kind = problem["kind"]
    label = _KIND_LABELS.get(kind, kind)

    if kind == "pending":
        label = f"{label} уже {problem['hours_pending']:.0f} ч"

    return f"• <b>{city}</b> | {card_name}\n  {label} (ad_id {problem['ad_id']})"


def _send_problems_telegram(problems: list[dict]) -> None:
    """Отправляет Telegram-сообщение о проблемах контроля запуска (channel='ads')."""
    lines = [f"🚨 <b>Контроль запуска: {len(problems)} проблем</b>\n"]
    lines.extend(_format_problem_line(p) for p in problems)
    text = "\n\n".join(lines)

    try:
        from services.notifications import send_telegram
        send_telegram(text, channel="ads")
    except Exception as exc:
        logger.warning("launch_verify: ошибка отправки Telegram — %s", exc)


def _save_verify_state(result: dict) -> None:
    """Сохраняет результат проверки в data/launch_verify_state.json (дата + summary).

    Формат: {"date": "YYYY-MM-DD", "launched": N, "running": X, "problems_count": N}.
    Дайджест читает именно этот файл за ВЧЕРА (см. services/morning_digest.py).
    """
    state = {
        "date": _get_today_str(),
        "launched": result["launched"],
        "running": result["running"],
        "problems_count": len(result["problems"]),
    }
    try:
        state_store.save_json_state(_LAUNCH_VERIFY_STATE_FILE, state)
    except Exception as exc:
        logger.warning("launch_verify: не удалось сохранить state — %s", exc)


def load_verify_state() -> dict:
    """Читает последний сохранённый результат проверки (для дайджеста)."""
    return state_store.load_json_state(_LAUNCH_VERIFY_STATE_FILE)
