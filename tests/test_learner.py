"""Тесты анализа креативов: бизнес-классификация, 2×2 матрица, усталость, рейтинги, гипотезы."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.learner import (
    classify_creative, classify_business, detect_fatigue,
    build_rankings, generate_hypotheses, build_creative_table,
)


# --- classify_creative (2×2 матрица) ---

def test_classify_winner():
    """Hook высокий + Hold высокий = Winner."""
    assert classify_creative(35.0, 20.0) == "Winner"
    assert classify_creative(30.0, 15.0) == "Winner"


def test_classify_clickbait():
    """Hook высокий + Hold низкий = Clickbait."""
    assert classify_creative(35.0, 10.0) == "Clickbait"
    assert classify_creative(50.0, 5.0) == "Clickbait"


def test_classify_hidden_gem():
    """Hook низкий + Hold высокий = Hidden Gem."""
    assert classify_creative(10.0, 20.0) == "Hidden Gem"
    assert classify_creative(25.0, 15.0) == "Hidden Gem"


def test_classify_dead():
    """Hook низкий + Hold низкий = Dead."""
    assert classify_creative(10.0, 5.0) == "Dead"
    assert classify_creative(0, 0) == "Dead"


def test_classify_boundary():
    """Граничные значения: ровно на пороге = Winner."""
    assert classify_creative(30.0, 15.0) == "Winner"


# --- detect_fatigue ---

def _make_ad(**kwargs):
    """Создаёт объявление с дефолтами для тестов."""
    defaults = {
        "ad_id": "123", "ad_name": "Test Ad", "city": "CityA",
        "adset_type": "L2", "status": "ACTIVE", "days_running": 10,
        "spend": 50, "leads": 2, "cpl": 25, "ctr": 1.5, "cpm": 10,
        "impressions": 3000, "clicks": 45, "frequency": 1.5,
        "video_views_3s": 900, "thruplay": 150,
        "video_p25": 700, "video_p50": 400, "video_p75": 200, "video_p100": 100,
        "hook_rate": 30.0, "hold_rate": 16.7, "creative_class": "Winner",
        # AMO данные
        "qual_pct": None, "romi": None, "payments": None,
        "business_class": "Нет данных",
    }
    defaults.update(kwargs)
    return defaults


# --- classify_business (ROMI + квал) ---

def test_business_profitable():
    """ROMI >= 200% = Прибыльный."""
    assert classify_business({"romi": 300, "qual_pct": 25, "payments": 3}) == "Прибыльный"
    assert classify_business({"romi": 200, "qual_pct": 20, "payments": 1}) == "Прибыльный"


def test_business_breakeven():
    """ROMI 100-200% = Окупается."""
    assert classify_business({"romi": 150, "qual_pct": 20, "payments": 1}) == "Окупается"


def test_business_unprofitable():
    """ROMI < 100% = Убыточный."""
    assert classify_business({"romi": 50, "qual_pct": 10, "payments": 0}) == "Убыточный"


def test_business_promising():
    """Квал >= 20%, нет оплат, нет ROMI = Перспективный."""
    assert classify_business({"romi": None, "qual_pct": 25, "payments": 0}) == "Перспективный"
    assert classify_business({"romi": None, "qual_pct": 30, "payments": None}) == "Перспективный"


def test_business_low_qual():
    """Квал < 20% = Низкая квал."""
    assert classify_business({"romi": None, "qual_pct": 10, "payments": None}) == "Низкая квал"


def test_business_no_data():
    """Нет AMO данных = Нет данных."""
    assert classify_business({"romi": None, "qual_pct": None, "payments": None}) == "Нет данных"
    assert classify_business({}) == "Нет данных"


def test_fatigue_high_frequency():
    """Частота >2.5 — признак усталости."""
    ads = [_make_ad(frequency=3.0)]
    result = detect_fatigue(ads)
    assert len(result) == 1
    assert any("Частота" in r for r in result[0]["reasons"])


def test_fatigue_low_ctr():
    """CTR <0.5% при >1000 показов — усталость."""
    ads = [_make_ad(ctr=0.3, impressions=2000)]
    result = detect_fatigue(ads)
    assert len(result) == 1
    assert any("CTR" in r for r in result[0]["reasons"])


def test_fatigue_no_issues():
    """Нормальные метрики — нет усталости."""
    ads = [_make_ad(frequency=1.5, ctr=1.5, impressions=3000)]
    result = detect_fatigue(ads)
    assert len(result) == 0


def test_fatigue_skip_low_impressions():
    """Мало показов (<500) — не проверяем усталость."""
    ads = [_make_ad(frequency=5.0, impressions=100)]
    result = detect_fatigue(ads)
    assert len(result) == 0


# --- build_rankings ---

def test_rankings_empty():
    """Пустой список — пустые рейтинги."""
    result = build_rankings([])
    assert result["by_city"] == []
    assert result["by_adset_type"] == []
    assert result["by_creative_class"] == []


def test_rankings_aggregation():
    """Агрегация по городам: суммирование spend/leads, расчёт CPL."""
    ads = [
        _make_ad(city="CityA", spend=100, leads=10, impressions=5000,
                 video_views_3s=1500, thruplay=300),
        _make_ad(city="CityA", spend=50, leads=5, impressions=2500,
                 video_views_3s=750, thruplay=150),
    ]
    result = build_rankings(ads)
    cities = result["by_city"]
    assert len(cities) == 1
    assert cities[0]["name"] == "CityA"
    assert cities[0]["spend"] == 150
    assert cities[0]["leads"] == 15
    assert cities[0]["cpl"] == 10.0
    # Hook Rate = (1500+750) / (5000+2500) * 100 = 30.0%
    assert cities[0]["hook_rate"] == 30.0


def test_rankings_sorting():
    """Сортировка: с лидами по CPL (дешевле=лучше), потом без лидов."""
    ads = [
        _make_ad(city="A", spend=100, leads=5, impressions=1000,
                 video_views_3s=300, thruplay=50),
        _make_ad(city="B", spend=50, leads=5, impressions=1000,
                 video_views_3s=300, thruplay=50),
        _make_ad(city="C", spend=200, leads=0, impressions=1000,
                 video_views_3s=300, thruplay=50),
    ]
    result = build_rankings(ads)
    cities = result["by_city"]
    assert cities[0]["name"] == "B"  # CPL 10
    assert cities[1]["name"] == "A"  # CPL 20
    assert cities[2]["name"] == "C"  # без лидов


# --- generate_hypotheses ---

def test_hypotheses_winner_scale():
    """Гипотеза масштабирования для Winner с лидами."""
    ads = [_make_ad(creative_class="Winner", leads=5, cpl=15,
                    hook_rate=35, hold_rate=20)]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    assert any(h["type"] == "scale" for h in hypotheses)


def test_hypotheses_hidden_gem():
    """Гипотеза эксперимента для Hidden Gem."""
    ads = [_make_ad(creative_class="Hidden Gem", hold_rate=20,
                    hook_rate=10, spend=30)]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    assert any(h["type"] == "experiment" for h in hypotheses)


def test_hypotheses_dead_costly():
    """Гипотеза отключения для Dead с большим расходом."""
    ads = [_make_ad(creative_class="Dead", spend=50, leads=0,
                    hook_rate=5, hold_rate=3)]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    assert any(h["type"] == "reduce" for h in hypotheses)


def test_hypotheses_fatigued():
    """Гипотеза сокращения для усталого креатива."""
    ads = [_make_ad()]
    fatigued = [{"ad_id": "123", "ad_name": "Test", "city": "CityA",
                 "reasons": ["Частота 3.0"], "frequency": 3.0,
                 "ctr": 0.5, "cpl": 25, "spend": 100}]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, fatigued)
    assert any(h["type"] == "reduce" and "Усталость" in h["title"] for h in hypotheses)


def test_hypotheses_profitable_scale():
    """Гипотеза масштабирования для прибыльного объявления (ROMI)."""
    ads = [_make_ad(business_class="Прибыльный", romi=300, qual_pct=25,
                    payments=3, leads=5, cpl=15)]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    assert any(h["type"] == "scale" and "ROMI" in h["title"] for h in hypotheses)


def test_hypotheses_unprofitable_reduce():
    """Гипотеза сокращения для убыточного объявления (ROMI)."""
    ads = [_make_ad(business_class="Убыточный", romi=50, qual_pct=10,
                    spend=30, leads=5)]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    assert any(h["type"] == "reduce" and "убыточный" in h["title"].lower() for h in hypotheses)


def test_hypotheses_no_amo_reminder():
    """Напоминание заполнить AMO данные."""
    ads = [_make_ad(business_class="Нет данных", leads=5)]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    assert any("AMO" in h.get("description", "") for h in hypotheses)


def test_hypotheses_empty():
    """Пустые данные — нет гипотез."""
    hypotheses = generate_hypotheses(
        {"by_city": [], "by_adset_type": [], "by_creative_class": []}, [], [])
    assert hypotheses == []


# --- build_creative_table ---

def test_creative_table_sorted_by_spend():
    """Таблица отсортирована по расходу (больше=первый)."""
    ads = [
        _make_ad(ad_id="1", spend=50),
        _make_ad(ad_id="2", spend=200),
        _make_ad(ad_id="3", spend=100),
    ]
    table = build_creative_table(ads)
    assert table[0]["ad_id"] == "2"
    assert table[1]["ad_id"] == "3"
    assert table[2]["ad_id"] == "1"


def test_creative_table_fields():
    """Таблица содержит все нужные поля (бизнес + видео)."""
    ads = [_make_ad(romi=200, qual_pct=25, payments=2)]
    table = build_creative_table(ads)
    row = table[0]
    # Бизнес-поля
    assert "romi" in row
    assert "qual_pct" in row
    assert "payments" in row
    assert "business_class" in row
    # Видео-поля
    assert "hook_rate" in row
    assert "hold_rate" in row
    assert "creative_class" in row
    assert "video_views_3s" in row
    assert "thruplay" in row


# --- Smart hypotheses (winner vs loser pairs) ---

def test_hypotheses_smart_pairs():
    """Умные гипотезы: пары winner vs loser с конкретными числами."""
    ads = [
        _make_ad(ad_id="w1", ad_name="Winner Ad 1", spend=100, leads=10, cpl=10,
                 hook_rate=35, hold_rate=20, creative_class="Winner"),
        _make_ad(ad_id="w2", ad_name="Winner Ad 2", spend=80, leads=8, cpl=10,
                 hook_rate=32, hold_rate=18, creative_class="Winner"),
        _make_ad(ad_id="l1", ad_name="Loser Ad 1", spend=50, leads=1, cpl=50,
                 hook_rate=10, hold_rate=5, creative_class="Dead"),
        _make_ad(ad_id="l2", ad_name="Loser Ad 2", spend=30, leads=1, cpl=30,
                 hook_rate=15, hold_rate=8, creative_class="Dead"),
    ]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    smart = [h for h in hypotheses if h["type"] == "smart"]
    assert len(smart) >= 1
    assert "winner_id" in smart[0]
    assert "loser_id" in smart[0]
    assert "priority" in smart[0]


def test_hypotheses_smart_priority_high():
    """HIGH приоритет при разнице CPL > 3x."""
    ads = [
        _make_ad(ad_id="w1", spend=50, leads=10, cpl=5,
                 hook_rate=35, hold_rate=20, creative_class="Winner"),
        _make_ad(ad_id="l1", spend=50, leads=2, cpl=25,
                 hook_rate=10, hold_rate=5, creative_class="Dead"),
    ]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    smart = [h for h in hypotheses if h["type"] == "smart"]
    assert len(smart) >= 1
    assert smart[0]["priority"] == "HIGH"


def test_hypotheses_all_have_priority():
    """Все гипотезы имеют поле priority."""
    ads = [
        _make_ad(business_class="Прибыльный", romi=300, qual_pct=25,
                 payments=3, leads=5, cpl=15, hook_rate=35, hold_rate=20,
                 creative_class="Winner"),
    ]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    for h in hypotheses:
        assert "priority" in h, f"Гипотеза без priority: {h['title']}"
        assert h["priority"] in ("HIGH", "MED", "LOW")


def test_hypotheses_no_smart_without_enough_data():
    """Нет smart гипотез если мало объявлений с лидами."""
    ads = [_make_ad(leads=0, spend=10, cpl=0)]
    rankings = build_rankings(ads)
    hypotheses = generate_hypotheses(rankings, ads, [])
    smart = [h for h in hypotheses if h["type"] == "smart"]
    assert len(smart) == 0
