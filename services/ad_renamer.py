"""
Переименователь активных объявлений — продуктовый тег-суффикс в имени.

Читает из creative_kb ТОЛЬКО «реально живые» объявления (крутятся сейчас или
недавно крутились — см. plan_renames / _live_scope_sql), строит план
переименований «старое имя → новое имя [ПРОДУКТ]» (через
product_tags.classify_product / format_ad_name) и, только по явному запросу
владельца (mode='active'), переименовывает их в FB батчами через
integrations.facebook.rename_ad.

ВАЖНО (почему не «весь ACTIVE+PAUSED»): поле status в creative_kb у большинства
строк протухшее (synced_at старше 2-4 недель, есть строки с created_at 2018 г.,
по факту реклама давно на паузе через паузу кампании/адсета). Широкий фильтр
`status IN ('ACTIVE','PAUSED')` раздувает план до тысяч мёртвых объявлений —
это тысячи бессмысленных FB-вызовов. Поэтому скоуп сужен до живых+свежих
(effective_status='ACTIVE' ИЛИ свежий синк живого status), исторический хвост
намеренно мимо (телеметрия excluded_stale показывает, сколько отсеяли).

Гейт безопасности (см. docs/specs/ARCH-product-tags.md §8, §10 — T8):
  - mode='dry_run' (ДЕФОЛТ) — ничего не меняет в FB, только строит план.
  - mode='active' — вызывается ТОЛЬКО руками владельца после ручного «да»
    (после просмотра dry-run списка). Крон НЕ регистрируем.
  - Каждое успешное переименование сразу пишется в data/ad_rename_state.json
    (mapping ad_id -> {old, new, at}) — обратимость: rollback_renames()
    возвращает старые имена по этому mapping'у.
  - Батчи по batch_size с паузой между ними (троттлинг) + стоп-гейт при
    _MAX_CONSECUTIVE_ERRORS ошибках подряд (не долбим FB, если что-то сломалось
    массово — например токен истёк).

Переименование name — правка метаданных объявления, НЕ трогает creative/
таргетинг/бюджет и НЕ сбрасывает обучение алгоритма FB.
"""

import html
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from services.product_tags import classify_product, format_ad_name

logger = logging.getLogger(__name__)

# Путь к mapping-файлу старое→новое имя (обратимость). Пишется атомарно
# (tmp + rename), см. паттерн services/ads_watchdog.py::_save_state.
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "ad_rename_state.json"

# Статусы, которые переименовываем. Архив/DELETED/прочие effective_status —
# не трогаем (история метрик там уже не актуальна для владельца).
_RENAMABLE_STATUSES = ("ACTIVE", "PAUSED")

# Окно «свежести» синка (дни): PAUSED/ACTIVE со свежим synced_at считаем живыми
# (недавнюю паузу владелец видит в кабинете и хочет переименовать). Строки с
# протухшим synced_at (status не обновлялся 2-4 недели и дольше) — исторический
# хвост, не трогаем. 14 дней = запас поверх ежедневного синка spend_refresh.
_DEFAULT_FRESH_DAYS = 14


def _live_scope_sql(fresh_days: int) -> tuple[str, list]:
    """SQL-предикат «реально живого» объявления + его параметры (для WHERE).

    Живым считаем строку, если ЛИБО она крутится прямо сейчас
    (effective_status='ACTIVE' — вычисляемый FB-статус, свежести синка НЕ требует),
    ЛИБО владелец держит её ACTIVE/PAUSED И строка синкалась недавно
    (datetime(synced_at) в пределах fresh_days). datetime(synced_at) нормализует
    формат (все записи пишутся datetime('now'), но обёртка страхует от ISO-строк).

    Возвращает (sql_fragment, params) — предикат общий для plan_renames и
    телеметрии, чтобы логика скоупа не разъезжалась в двух местах.
    """
    placeholders = ",".join("?" for _ in _RENAMABLE_STATUSES)
    sql = (
        f"(effective_status = 'ACTIVE' "
        f"OR (status IN ({placeholders}) "
        f"AND datetime(synced_at) >= datetime('now', ?)))"
    )
    # fresh_days приводим к int — интерполируем ТОЛЬКО число в модификатор,
    # который дальше уходит связанным параметром (без риска инъекции).
    params = [*_RENAMABLE_STATUSES, f"-{int(fresh_days)} days"]
    return sql, params

# Сколько объявлений переименовываем подряд перед паузой (троттлинг FB API).
_DEFAULT_BATCH_SIZE = 20
# Пауза между батчами, секунд.
_BATCH_SLEEP_SEC = 2.0
# Стоп-гейт: столько ошибок подряд — прекращаем прогон досрочно (не долбим FB).
_MAX_CONSECUTIVE_ERRORS = 5

# Лимит длины сообщения Telegram.
_TELEGRAM_MAX_LEN = 4096


def plan_renames(fresh_days: int = _DEFAULT_FRESH_DAYS) -> list[dict]:
    """Строит план переименований для «реально живых» объявлений из creative_kb.

    Скоуп (см. _live_scope_sql): объявление берём, если оно крутится сейчас
    (effective_status='ACTIVE') ИЛИ владелец держит его ACTIVE/PAUSED и строка
    синкалась не позже fresh_days назад. Исторический хвост (протухший status
    без свежего синка и без живого effective_status) НЕ берём — иначе план
    раздувается до тысяч мёртвых объявлений (тысячи лишних FB-вызовов).

    fresh_days — окно свежести синка в днях (дефолт 14). Расширить сознательно:
    plan_renames(fresh_days=45) захватит более старые паузы.

    Для каждого: продукт = classify_product(name=ad_name, target_product=<из KB>);
    new_name = format_ad_name(ad_name, product). В план попадают ТОЛЬКО те,
    где new_name != old_name (тега ещё нет или он другой, т.е. переименование
    реально что-то меняет).

    Возвращает: [{"ad_id": str, "old_name": str, "new_name": str,
                  "product": str, "status": str}, ...]
    """
    from services.creative_intelligence import _get_connection

    scope_sql, scope_params = _live_scope_sql(fresh_days)
    conn = _get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT ad_id, ad_name, target_product, status
              FROM creative_kb
             WHERE {scope_sql}
            """,
            scope_params,
        ).fetchall()
    finally:
        conn.close()

    plan: list[dict] = []
    for row in rows:
        old_name = row["ad_name"] or ""
        product = classify_product(name=old_name, target_product=row["target_product"])
        new_name = format_ad_name(old_name, product)
        if new_name == old_name:
            # Тег уже стоит и совпадает с актуальным продуктом — нечего менять.
            continue
        plan.append({
            "ad_id": row["ad_id"],
            "old_name": old_name,
            "new_name": new_name,
            "product": product,
            "status": row["status"],
        })
    return plan


def _scope_telemetry(fresh_days: int = _DEFAULT_FRESH_DAYS) -> dict:
    """Диагностика скоупа для владельца: total_in_kb + excluded_stale.

    Помогает понять, ПОЧЕМУ план маленький (это фича сужения, а не «сломалось»):
      - total_in_kb    — всего строк в creative_kb (весь накопленный аккаунт).
      - excluded_stale — строки, которые СТАРЫЙ широкий фильтр
        (status IN ('ACTIVE','PAUSED')) подхватил бы, но новый живой+свежий скоуп
        режет: status числится ACTIVE/PAUSED, а по факту синк протух и
        effective_status не 'ACTIVE' (реклама реально не крутится).

    Отдельные COUNT'ы дёшевы. Предикат живого скоупа общий с plan_renames
    (_live_scope_sql), чтобы excluded_stale считался ровно по той же границе.

    Возвращает {"total_in_kb": int, "excluded_stale": int}.
    """
    from services.creative_intelligence import _get_connection

    placeholders = ",".join("?" for _ in _RENAMABLE_STATUSES)
    scope_sql, scope_params = _live_scope_sql(fresh_days)
    conn = _get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM creative_kb").fetchone()[0]
        excluded = conn.execute(
            f"SELECT COUNT(*) FROM creative_kb "
            f"WHERE status IN ({placeholders}) AND NOT {scope_sql}",
            [*_RENAMABLE_STATUSES, *scope_params],
        ).fetchone()[0]
    finally:
        conn.close()
    return {"total_in_kb": int(total), "excluded_stale": int(excluded)}


def _load_rename_state() -> dict:
    """Читает mapping {ad_id: {"old", "new", "at"}} из data/ad_rename_state.json.
    Файла нет / битый JSON → {} (не бросает)."""
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("ad_renamer: не удалось прочитать state — %s", exc)
        return {}


def _save_rename_state(state: dict) -> None:
    """Атомарно сохраняет mapping (tmp-файл + rename), см. services/ads_watchdog.py."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as exc:
        logger.error("ad_renamer: не удалось сохранить state — %s", exc)


def _apply_renames_batch(items: list[dict], batch_size: int, on_success) -> dict:
    """Общий цикл переименования: батчи по batch_size, троттлинг между батчами,
    стоп-гейт при _MAX_CONSECUTIVE_ERRORS ошибках подряд. Используется и
    rename_active_ads(mode='active'), и rollback_renames() — разница только в
    направлении (item['new_name'] — куда переименовываем) и в on_success-колбэке
    (запись/удаление mapping'а).

    items: [{"ad_id","old_name","new_name",...}, ...] — переименовываем в new_name.
    on_success(item) — вызывается после успешного FB rename_ad (обновляет state).

    Граница FB — integrations.facebook.rename_ad — импортируется лениво (внутри
    функции), чтобы модуль ad_renamer оставался импортируемым независимо от
    того, добавлена ли уже функция в integrations/facebook.py (см. T7 в
    docs/specs/ARCH-product-tags.md — rename_ad там же, отдельная волна).

    Возвращает {"succeeded": [...], "failed": [...], "error": str | None}.
    """
    from integrations.facebook import rename_ad

    succeeded: list[dict] = []
    failed: list[dict] = []
    consecutive_errors = 0

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        for item in batch:
            try:
                ok = rename_ad(item["ad_id"], item["new_name"])
            except Exception as exc:
                logger.error("ad_renamer: rename_ad(%s) упал — %s", item["ad_id"], exc)
                ok = False

            if ok:
                consecutive_errors = 0
                succeeded.append(item)
                on_success(item)
                continue

            consecutive_errors += 1
            failed.append({**item, "error": "FB rename_ad вернул False"})
            logger.error(
                "ad_renamer: не удалось переименовать %s (%d ошибок подряд)",
                item["ad_id"], consecutive_errors,
            )
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                logger.error(
                    "ad_renamer: %d ошибок подряд — прогон остановлен досрочно",
                    _MAX_CONSECUTIVE_ERRORS,
                )
                return {
                    "succeeded": succeeded,
                    "failed": failed,
                    "error": f"остановлено после {_MAX_CONSECUTIVE_ERRORS} ошибок подряд",
                }

        # Троттлинг: пауза перед следующим батчем (если он есть).
        if i + batch_size < len(items):
            time.sleep(_BATCH_SLEEP_SEC)

    return {"succeeded": succeeded, "failed": failed, "error": None}


def rename_active_ads(
    mode: str = "dry_run", batch_size: int = 20, fresh_days: int = _DEFAULT_FRESH_DAYS
) -> dict:
    """mode='dry_run' (ДЕФОЛТ): только план, FB НЕ трогаем.
    mode='active': реально переименовывает через integrations.facebook.rename_ad,
    батчами по batch_size, сохраняя mapping {ad_id: {old, new, at}} в
    data/ad_rename_state.json (обратимость — см. rollback_renames).

    fresh_days — окно свежести синка для скоупа plan_renames (дефолт 14).
    Расширить сознательно: rename_active_ads(fresh_days=45) захватит старые паузы.

    mode='active' — организационный гейт: запускается ТОЛЬКО руками владельца
    после явного «да» на dry-run список (см. §8/§10 ARCH-product-tags.md, T8).
    Крон НЕ регистрируем.

    Возвращает:
      {"mode": str, "planned": int, "renamed": list[dict], "failed": list[dict],
       "error": str | None,
       "telemetry": {"total_in_kb": int, "excluded_stale": int, "planned": int}}
    telemetry.excluded_stale — сколько мёртвого хвоста отсеял свежестный гейт.
    """
    if mode == "active":
        from integrations.facebook_ads_mutation_transport import ForbiddenMutation

        raise ForbiddenMutation("RENAME_OPERATION_FORBIDDEN")

    try:
        plan = plan_renames(fresh_days=fresh_days)
        telemetry = _scope_telemetry(fresh_days=fresh_days)
    except Exception as exc:
        logger.error("ad_renamer: не удалось построить план переименований — %s", exc)
        return {
            "mode": mode, "planned": 0, "renamed": [], "failed": [], "error": str(exc),
            "telemetry": {"total_in_kb": 0, "excluded_stale": 0, "planned": 0},
        }

    telemetry["planned"] = len(plan)

    if mode != "active":
        return {
            "mode": "dry_run", "planned": len(plan), "renamed": [], "failed": [],
            "error": None, "telemetry": telemetry,
        }

    state = _load_rename_state()

    def _record(item: dict) -> None:
        state[item["ad_id"]] = {
            "old": item["old_name"],
            "new": item["new_name"],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _save_rename_state(state)

    items = [
        {"ad_id": p["ad_id"], "old_name": p["old_name"], "new_name": p["new_name"], "product": p["product"]}
        for p in plan
    ]
    result = _apply_renames_batch(items, batch_size, on_success=_record)

    return {
        "mode": "active",
        "planned": len(plan),
        "renamed": result["succeeded"],
        "failed": result["failed"],
        "error": result["error"],
        "telemetry": telemetry,
    }


def rollback_renames(batch_size: int = _DEFAULT_BATCH_SIZE) -> dict:
    """Откат переименований: возвращает объявлениям старые имена из mapping'а
    data/ad_rename_state.json (записан rename_active_ads(mode='active')).

    Батчи/троттлинг/стоп-гейт — как в rename_active_ads. Успешно откаченные
    записи убираются из mapping'а (частичный откат безопасно повторить —
    повторный вызов найдёт только оставшиеся записи).

    Пустой/отсутствующий mapping → no-op, FB не трогаем.

    Возвращает {"planned": int, "rolled_back": list[dict], "failed": list[dict],
                 "error": str | None}.
    """
    del batch_size
    from integrations.facebook_ads_mutation_transport import ForbiddenMutation

    raise ForbiddenMutation("RENAME_ROLLBACK_OPERATION_FORBIDDEN")


def format_rename_dry_run(plan: list[dict]) -> str:
    """HTML-строка dry-run списка «старое → новое» для Telegram (≤4096)."""
    if not plan:
        return (
            "🏷 <b>Переименование объявлений</b>\n\n"
            "переименовывать нечего — все ACTIVE/PAUSED объявления уже с тегом продукта"
        )

    lines = [f"🏷 <b>Переименование объявлений</b> (план: {len(plan)})", ""]
    for item in plan:
        old_name = html.escape(str(item.get("old_name", "")))
        new_name = html.escape(str(item.get("new_name", "")))
        lines.append(f"• {old_name} → {new_name}")

    text = "\n".join(lines)
    if len(text) <= _TELEGRAM_MAX_LEN:
        return text

    suffix = "\n…(полный список — data/ad_rename_state.json после запуска)"
    cut = _TELEGRAM_MAX_LEN - len(suffix)
    return text[:cut] + suffix
