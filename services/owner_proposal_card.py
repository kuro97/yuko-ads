"""Текст карточки предложения владельцу: цифры решения, а не голый заголовок.

Карточка складывается ОДИН раз — в момент создания предложения — и живёт в
``owner_action_proposals.summary``. Так она информативна независимо от того, кто
подготовил доставку: ``prepare_owner_delivery`` при уже существующей активной
доставке молча возвращает её и игнорирует переданный ``rendered_text``, а
``_ensure_initial_deliveries`` крутится каждые 15 минут и раньше создавал
доставку из голого summary («Поставить на паузу <имя>» — без единой цифры).

Формат — эталон Telegram-сообщений проекта (``autopilot._format_live_pause_block``
и ``_format_pause_report``): деньги через ``fmt_money``, «нет данных» вместо
фиктивного нуля, обрезка по границе слова. Разметки НЕТ: доставка шлёт
``sendMessage`` без ``parse_mode``, поэтому HTML-теги владелец увидел бы как
текст.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from services.formatting import fmt_money, pluralize_leads, truncate_at_word_boundary


# Лимит Telegram — 4096. Держим запас: к карточке при ack дописывается статус
# («⏳ Принято, исполняю») через edit_message_text, и итог обязан влезть.
MAX_CARD_LEN = 3500
# Причина решения читается целиком, но не превращает карточку в простыню.
MAX_REASON_LEN = 300
# Имена объявлений в кабинете бывают очень длинными («Город | Креатив А / 2»).
MAX_NAME_LEN = 90


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Цифры, на которых бот принял решение. Любое поле может быть неизвестно.

    Заполняется вызывающим (autopilot, budget_scaler), у которого эти метрики
    уже посчитаны для отчёта. ``None`` печатается как «нет данных» — фиктивный
    ноль владельцу не показываем (решение владельца).
    """

    spend_usd: float | None = None
    leads: int | None = None
    cpl_usd: float | None = None
    qual_leads: int | None = None
    qual_pct: float | None = None
    payments: int | None = None
    romi_pct: float | None = None
    drr_pct: float | None = None
    days_running: int | None = None
    business_reason: str | None = None

    @property
    def has_numbers(self) -> bool:
        """Есть ли хоть одна метрика: без них карточка деградирует до заголовка."""
        return any(
            value is not None
            for value in (
                self.spend_usd,
                self.leads,
                self.cpl_usd,
                self.qual_leads,
                self.qual_pct,
                self.payments,
                self.romi_pct,
                self.drr_pct,
                self.days_running,
            )
        )


def _clean(value: object | None, limit: int) -> str:
    text = str(value or "").strip()
    return truncate_at_word_boundary(text, limit) if text else ""


def _scope_line(
    city: str | None,
    language: str | None,
    adset_id: str | None,
) -> str | None:
    """«CityA · L2 · adset 120210…» — только известные части, без прочерков."""
    parts = [part for part in (_clean(city, 40), _clean(language, 4)) if part]
    adset = _clean(adset_id, 40)
    if adset:
        parts.append(f"adset {adset}")
    return " · ".join(parts) if parts else None


def _subject_line(prefix: str, name: str | None, fallback_id: str) -> str:
    label = _clean(name, MAX_NAME_LEN)
    if not label:
        return f"{prefix} {fallback_id}"
    return f"{prefix} «{label}»"


def _money_line(decision: DecisionContext) -> str | None:
    """«💸 $500 · 30 лидов · CPL $16.7» с честными «нет данных»."""
    if decision.spend_usd is None and decision.leads is None and decision.cpl_usd is None:
        return None
    spend = (
        fmt_money(decision.spend_usd, "$")
        if decision.spend_usd is not None
        else "расход: нет данных"
    )
    leads = (
        f"{decision.leads} {pluralize_leads(decision.leads)}"
        if decision.leads is not None
        else "лиды: нет данных"
    )
    # CPL=0 при расходе — дыра данных (для 0 лидов CPL не определён).
    cpl = f"CPL {fmt_money(decision.cpl_usd, '$')}" if decision.cpl_usd else "CPL нет данных"
    return f"💸 {spend} · {leads} · {cpl}"


def _quality_line(decision: DecisionContext) -> str | None:
    """«👥 квал 1 (3%) · оплат 0» — количество квалов считаем из процента."""
    if (
        decision.qual_leads is None
        and decision.qual_pct is None
        and decision.payments is None
    ):
        return None
    qual_count = decision.qual_leads
    if qual_count is None and decision.qual_pct is not None and decision.leads:
        qual_count = round(decision.qual_pct / 100 * decision.leads)
    if qual_count is not None and decision.qual_pct is not None:
        qual = f"квал {qual_count} ({decision.qual_pct:.0f}%)"
    elif qual_count is not None:
        qual = f"квал {qual_count}"
    elif decision.qual_pct is not None:
        qual = f"квал {decision.qual_pct:.0f}%"
    else:
        qual = "квал нет данных"
    payments = (
        f"оплат {decision.payments}"
        if decision.payments is not None
        else "оплат нет данных"
    )
    return f"👥 {qual} · {payments}"


def _economics_line(decision: DecisionContext) -> str | None:
    """«📈 ROMI 240% · ДРР 18% · крутится 6 дн.» — только известные части."""
    parts: list[str] = []
    if decision.romi_pct is not None:
        parts.append(f"ROMI {decision.romi_pct:.0f}%")
    if decision.drr_pct is not None:
        parts.append(f"ДРР {decision.drr_pct:.0f}%")
    if decision.days_running is not None:
        parts.append(f"крутится {decision.days_running} дн.")
    return f"📈 {' · '.join(parts)}" if parts else None


def _reason_line(decision: DecisionContext) -> str | None:
    reason = _clean(decision.business_reason, MAX_REASON_LEN)
    return f"📉 Причина: {reason}" if reason else None


def _remaining_line(remaining_active: int | None, *, paused: bool) -> str:
    """Остаток активных реклам в адсете. Неизвестно — говорим об этом честно."""
    if remaining_active is None:
        return "ℹ️ Сколько реклам останется активными — не знаю (FB не ответил)"
    verb = "После паузы в адсете останется" if paused else "После возврата в адсете будет"
    return f"✅ {verb} {remaining_active} активных"


def _assemble(lines: list[str | None]) -> str:
    text = "\n".join(line for line in lines if line)
    return truncate_at_word_boundary(text, MAX_CARD_LEN)


def render_pause_card(
    *,
    ad_id: str,
    ad_name: str | None,
    adset_id: str | None,
    city: str | None,
    language: str | None,
    remaining_active: int | None,
    decision: DecisionContext | None = None,
) -> str:
    """Карточка PAUSE. Без контекста решения деградирует до заголовка и адреса."""
    context = decision or DecisionContext()
    return _assemble(
        [
            "⏸ Предлагаю паузу",
            _scope_line(city, language, adset_id),
            _subject_line("Реклама", ad_name, ad_id),
            _money_line(context),
            _quality_line(context),
            _economics_line(context),
            _reason_line(context),
            _remaining_line(remaining_active, paused=True),
        ]
    )


def render_unpause_card(
    *,
    ad_id: str,
    ad_name: str | None,
    adset_id: str | None,
    city: str | None,
    language: str | None,
    active_after: int | None,
    decision: DecisionContext | None = None,
) -> str:
    """Карточка UNPAUSE: та же структура, обратное действие."""
    context = decision or DecisionContext()
    return _assemble(
        [
            "▶️ Предлагаю вернуть рекламу",
            _scope_line(city, language, adset_id),
            _subject_line("Реклама", ad_name, ad_id),
            _money_line(context),
            _quality_line(context),
            _economics_line(context),
            _reason_line(context),
            _remaining_line(active_after, paused=False),
        ]
    )


def _budget(value: object) -> str:
    if isinstance(value, Decimal):
        value = float(value)
    return fmt_money(value, "$") if isinstance(value, (int, float)) else str(value)


def render_scale_card(
    *,
    adset_id: str,
    adset_name: str | None,
    ad_name: str | None,
    city: str | None,
    language: str | None,
    current_budget_usd: object,
    target_budget_usd: object,
    decision: DecisionContext | None = None,
) -> str:
    """Карточка SCALE: бюджет «было → станет» плюс экономика победителя."""
    context = decision or DecisionContext()
    scope = _scope_line(city, language, adset_id)
    name = _clean(adset_name, MAX_NAME_LEN)
    return _assemble(
        [
            "📈 Предлагаю поднять бюджет",
            scope,
            f"Адсет «{name}»" if name else None,
            _subject_line("Победитель", ad_name, adset_id) if ad_name else None,
            f"💰 Бюджет {_budget(current_budget_usd)} → {_budget(target_budget_usd)} в день",
            _money_line(context),
            _quality_line(context),
            _economics_line(context),
            _reason_line(context),
        ]
    )


def render_recovery_card(
    *,
    subject_id: str,
    adset_id: str | None,
    city: str | None,
    language: str | None,
    reason: str | None = None,
) -> str:
    """Карточка ASSET_RECOVERY: что восстанавливаем и где."""
    return _assemble(
        [
            "🛠 Предлагаю восстановить рекламный ассет",
            _scope_line(city, language, adset_id),
            f"Объект {subject_id}",
            f"📉 Причина: {_clean(reason, MAX_REASON_LEN)}" if reason else None,
        ]
    )
