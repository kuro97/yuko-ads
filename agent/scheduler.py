"""
Планировщик: ежедневный анализ + авто-применение решений.
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from agent.analyzer import analyze_all, save_decision

DATA_DIR = Path(__file__).parent.parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
AUTO_ACTIONS_FILE = DATA_DIR / "auto_actions.json"

logger = logging.getLogger("scheduler")


def load_settings() -> dict:
    from agent.analyzer import DEFAULT_THRESHOLDS
    defaults = {"auto_apply": False, "thresholds": dict(DEFAULT_THRESHOLDS)}
    if SETTINGS_FILE.exists():
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if "thresholds" in data:
            defaults["thresholds"].update(data["thresholds"])
            data_without_th = {k: v for k, v in data.items() if k != "thresholds"}
            defaults.update(data_without_th)
        else:
            defaults.update(data)
    return defaults


def save_settings(settings: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_auto_actions() -> list:
    if AUTO_ACTIONS_FILE.exists():
        return json.loads(AUTO_ACTIONS_FILE.read_text(encoding="utf-8"))
    return []


def _save_auto_actions(actions: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTO_ACTIONS_FILE.write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")


def daily_analysis():
    """Запуск анализа объявлений с авто-паузой.

    DEPRECATED: заменён services/autopilot.run_autopilot; оставлен для совместимости.
    Не удалять — может использоваться внешними вызовами и тестами.
    """
    settings = load_settings()
    auto_apply = settings.get("auto_apply", False)
    try:
        ads = analyze_all(thresholds=settings.get("thresholds"))
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        return {"error": str(e)}

    disable_ads = [ad for ad in ads if ad.get("recommendation") == "ОТКЛЮЧИТЬ"]
    actions_taken = []
    run_scope = str(uuid.uuid4())
    for ad in disable_ads:
        if auto_apply:
            success = False
            action = "ОШИБКА"
            try:
                from services.action_producer_gateway import execute_pause
                from services.approval_checker_models import ActionOrigin
                pause_outcome = execute_pause(
                    ad["id"],
                    origin=ActionOrigin.LEGACY_SCHEDULER,
                    scope=f"legacy-scheduler:{run_scope}:{ad['id']}",
                    reason_code="LEGACY_SCHEDULER",
                )
                action = pause_outcome.action
                # Approval-first: producer отдаёт квитанцию предложения, а не
                # результат исполнения. Созданное предложение — это УСПЕШНО
                # выполненный шаг планировщика (владельцу есть что одобрять), а
                # не отказ safety guard: раньше `confirmed and run is not None`
                # всегда давал False, контур писал в лог ложное «заблокирована
                # safety guard: None» и обрывал весь батч на первом кандидате.
                receipt = getattr(pause_outcome, "receipt", None)
                proposal_id = None if receipt is None else receipt.proposal_id
                legacy_run = getattr(pause_outcome, "run", None)
                success = proposal_id is not None or (
                    bool(getattr(pause_outcome, "confirmed", False))
                    and legacy_run is not None
                )
                if proposal_id is not None:
                    # Решение PAUSED здесь не пишем: отключения ещё не было,
                    # его выполнит execution boundary после одобрения.
                    logger.info(
                        "Для %s создано предложение паузы %s — ждёт одобрения владельца",
                        ad["id"], proposal_id,
                    )
                elif success and legacy_run is not None:
                    save_decision(
                        ad["id"], ad["name"], "PAUSED", ad["reason"],
                        confirmed_by="autopilot",
                        effect_id=f"{legacy_run.operation_id}:decision:PAUSED",
                        projection_kind="AUTO_ACTION",
                        projection_payload={
                            "ad_id": ad["id"], "ad_name": ad["name"],
                            "action": "PAUSED", "reason": ad["reason"],
                            "confirmed_by": "autopilot", "success": True,
                        },
                    )
                elif pause_outcome.action == "REPLACEMENT_ENQUEUED":
                    logger.info(
                        "Для %s поставлена в очередь замена: workflow=%s",
                        ad["id"], pause_outcome.workflow_id,
                    )
                else:
                    logger.error(
                        "PAUSE %s заблокирована safety guard: %s",
                        ad["id"], pause_outcome.reason,
                    )
                    actions_taken.append({"ad_id": ad["id"], "ad_name": ad["name"], "action": action, "reason": ad["reason"], "success": success})
                    break
            except Exception as e:
                logger.error(f"Ошибка при отключении {ad['id']}: {e}")
                actions_taken.append({"ad_id": ad["id"], "ad_name": ad["name"], "action": action, "reason": ad["reason"], "success": success})
                break
            actions_taken.append({"ad_id": ad["id"], "ad_name": ad["name"], "action": action, "reason": ad["reason"], "success": success})
        else:
            actions_taken.append({"ad_id": ad["id"], "ad_name": ad["name"], "action": "РЕКОМЕНДАЦИЯ", "reason": ad["reason"], "success": True})

    _log_batch(actions_taken, auto_apply, len(ads))
    settings["last_run"] = datetime.now().isoformat()
    save_settings(settings)
    return {"total_analyzed": len(ads), "disable_recommended": len(disable_ads), "auto_applied": auto_apply, "actions": actions_taken}


def _log_batch(action_records, auto_applied, total_analyzed):
    actions = load_auto_actions()
    timestamp = datetime.now().isoformat()
    for record in reversed(action_records):
        actions.insert(0, {"timestamp": timestamp, **record})
    actions.insert(0, {"timestamp": timestamp, "ad_id": "", "ad_name": "", "action": "АНАЛИЗ",
                        "reason": f"Проверено {total_analyzed} объявлений, {len(action_records)} к отключению. Авто: {'ВКЛ' if auto_applied else 'ВЫКЛ'}",
                        "success": True})
    _save_auto_actions(actions[:500])
