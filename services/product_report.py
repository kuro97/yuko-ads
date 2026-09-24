"""
Отчёт «какого продукта больше» (PRODA/PRODB/СТАРТ/ОБЩАЯ).

Источник данных — колонка creative_kb.target_product (канон продукта после
бэкфилла, см. services/creative_intelligence.backfill_target_product), а НЕ
парсинг имён объявлений: «имя — для человека, поле — для машины» (заказ
владельца, см. docs/specs/ARCH-product-tags.md §1, §8).

Используется и командой Telegram /products (services/telegram_console.py),
и секцией недельного отчёта (services/weekly_learning_report.py).
"""

import html
import logging

from services.formatting import fmt_money
from services.product_tags import PRODUCTS

logger = logging.getLogger(__name__)

# Медали для топ-3 продуктов по числу активных объявлений (см. §7 ARCH-product-tags.md)
_MEDALS = ("🥇", "🥈", "🥉")


def build_product_breakdown(days: int = 7) -> list[dict]:
    """Разбивка активных объявлений по продуктам из creative_kb.

    Читает target_product (уже канон после бэкфилла); строки со status='ACTIVE'.
    spend — колонка spend из creative_kb (последний синк за окно). days пока
    информативен (creative_kb хранит агрегат синка, не по дням) — используется
    в заголовке отчёта; фактическая фильтрация по дням не делается (документируем).

    Строки с target_product вне реестра продуктов (NULL/пустое/старый неканонический
    код до бэкфилла) НЕ попадают ни в один из 4 продуктов — это «без разметки»,
    сознательно исключаем их из знаменателя доли, чтобы не искажать долю PRODA/PRODB
    непроклассифицированными данными (см. docs/specs/ARCH-product-tags.md §5).

    Возвращает список dict, ОТСОРТИРОВАННЫЙ по ads_count DESC:
      [{"product": str, "ads_count": int, "spend": float, "share_pct": float}, ...]
    share_pct считается от суммы ads_count всех продуктов (доля по штукам).
    """
    from services.creative_intelligence import _get_connection

    products = list(PRODUCTS.keys())
    placeholders = ",".join("?" for _ in products)

    conn = _get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT target_product AS product,
                   COUNT(*) AS ads_count,
                   COALESCE(SUM(spend), 0) AS spend
              FROM creative_kb
             WHERE status = 'ACTIVE' AND target_product IN ({placeholders})
             GROUP BY target_product
            """,
            tuple(products),
        ).fetchall()
    finally:
        conn.close()

    stats_by_product = {row["product"]: row for row in rows}
    total_ads = sum(row["ads_count"] for row in rows)

    result = []
    for product in products:
        row = stats_by_product.get(product)
        ads_count = int(row["ads_count"]) if row else 0
        spend = float(row["spend"]) if row else 0.0
        share_pct = round(ads_count / total_ads * 100, 1) if total_ads else 0.0
        result.append({
            "product": product,
            "ads_count": ads_count,
            "spend": spend,
            "share_pct": share_pct,
        })

    # Стабильная сортировка: порядок PRODUCTS сохраняется при равенстве ads_count
    result.sort(key=lambda item: item["ads_count"], reverse=True)
    return result


def format_product_report(rows: list[dict]) -> str:
    """HTML-строка для Telegram (≤4096). Чистая функция.

    Формат — см. docs/specs/ARCH-product-tags.md §7:
      🥇 PRODB — 28 объявл. · $412 · 42%
      🥈 PRODA — 22 объявл. · $301 · 33%
      🥉 ОБЩАЯ — 12 объявл. · $88 · 18%
         СТАРТ — 5 объявл. · $30 · 7%
    """
    lines = ["📦 <b>Какого продукта больше</b> (активные, расход 7д)", ""]

    total_ads = sum(row.get("ads_count", 0) for row in rows)
    if total_ads == 0:
        lines.append("активных объявлений с разметкой продукта пока нет")
        return "\n".join(lines)

    for idx, row in enumerate(rows):
        prefix = _MEDALS[idx] if idx < len(_MEDALS) else "  "
        product = html.escape(str(row.get("product", "")))
        ads_count = row.get("ads_count", 0)
        spend = fmt_money(row.get("spend", 0))
        share_pct = row.get("share_pct", 0)
        lines.append(f"{prefix} {product} — {ads_count} объявл. · {spend} · {share_pct:.0f}%")

    lines.append("")
    lines.append(f"Всего активных: {total_ads}")
    return "\n".join(lines)
