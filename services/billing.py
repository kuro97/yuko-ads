"""
Биллинг — Stripe Subscriptions.
Планы, checkout, webhooks, статус подписки.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
SUBSCRIPTIONS_FILE = DATA_DIR / "subscriptions.json"

PLANS = {
    "start": {
        "name": "Старт", "price": 49,
        "features": ["1 рекламный аккаунт", "Аналитика", "Автопилот"],
    },
    "growth": {
        "name": "Рост", "price": 99,
        "features": ["3 рекламных аккаунта", "Аналитика", "Автопилот", "Обучение креативов", "Скоринг"],
    },
    "agency": {
        "name": "Агентство", "price": 199,
        "features": ["10 рекламных аккаунтов", "Всё из Рост", "Приоритетная поддержка", "API доступ"],
    },
}


def _get_stripe():
    import stripe
    from config import STRIPE_SECRET_KEY
    if STRIPE_SECRET_KEY:
        stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def _get_price_ids():
    from config import STRIPE_PRICE_START, STRIPE_PRICE_GROWTH, STRIPE_PRICE_AGENCY
    return {"start": STRIPE_PRICE_START, "growth": STRIPE_PRICE_GROWTH, "agency": STRIPE_PRICE_AGENCY}


def get_plans() -> list[dict]:
    price_ids = _get_price_ids()
    return [
        {"id": pid, "name": info["name"], "price": info["price"], "currency": "usd",
         "interval": "month", "price_id": price_ids.get(pid), "features": info["features"]}
        for pid, info in PLANS.items()
    ]


def create_checkout_session(plan: str, success_url: str, cancel_url: str) -> str:
    if plan not in PLANS:
        raise ValueError(f"Неверный план: {plan}")
    price_id = _get_price_ids().get(plan)
    if not price_id:
        raise ValueError(f"Price ID не задан для плана {plan}")
    stripe = _get_stripe()
    session = stripe.checkout.Session.create(
        payment_method_types=["card"], mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=cancel_url,
        subscription_data={"metadata": {"plan": plan}},
    )
    return session.url


def create_portal_session(customer_id: str, return_url: str) -> str:
    if not customer_id:
        raise ValueError("customer_id обязателен")
    stripe = _get_stripe()
    session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return session.url


def load_subscriptions() -> dict:
    if not SUBSCRIPTIONS_FILE.exists():
        return {}
    try:
        return json.loads(SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_subscriptions(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_subscription_status(customer_id: str | None = None) -> dict:
    subs = load_subscriptions()
    if not subs:
        return {"status": "none"}
    if customer_id and customer_id in subs:
        return subs[customer_id]
    if not customer_id and subs:
        return next(iter(subs.values()))
    return {"status": "none"}


def is_subscription_active(customer_id: str | None = None) -> bool:
    return get_subscription_status(customer_id).get("status") in ("active", "trialing")


def handle_checkout_completed(session: dict) -> None:
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    plan = (session.get("metadata") or {}).get("plan", "start")
    subs = load_subscriptions()
    subs[customer_id] = {
        "status": "active", "plan": plan, "plan_name": PLANS.get(plan, {}).get("name", plan),
        "customer_id": customer_id, "subscription_id": subscription_id,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_subscriptions(subs)
    logger.info(f"Подписка активирована: {customer_id}, план: {plan}")


def handle_payment_succeeded(invoice: dict) -> None:
    customer_id = invoice.get("customer")
    subs = load_subscriptions()
    if customer_id in subs:
        subs[customer_id]["status"] = "active"
        subs[customer_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        lines = invoice.get("lines", {}).get("data", [])
        if lines:
            subs[customer_id]["current_period_end"] = lines[0].get("period", {}).get("end")
        save_subscriptions(subs)


def handle_subscription_deleted(subscription: dict) -> None:
    customer_id = subscription.get("customer")
    subs = load_subscriptions()
    if customer_id in subs:
        subs[customer_id]["status"] = "canceled"
        subs[customer_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_subscriptions(subs)


def handle_payment_failed(invoice: dict) -> None:
    customer_id = invoice.get("customer")
    subs = load_subscriptions()
    if customer_id in subs:
        subs[customer_id]["status"] = "past_due"
        subs[customer_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_subscriptions(subs)
