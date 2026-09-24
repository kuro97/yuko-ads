"""Тесты сезонного сервиса."""
from datetime import date
from unittest.mock import patch

from services.seasons import get_all_seasons, get_season_for_date, get_current_season


class TestGetAllSeasons:
    def test_returns_list_of_five(self):
        seasons = get_all_seasons()
        assert isinstance(seasons, list)
        assert len(seasons) == 5

    def test_each_season_has_required_fields(self):
        required = {"id", "name", "start_month", "end_month", "description", "budget_modifier"}
        for season in get_all_seasons():
            assert required.issubset(season.keys()), f"Сезон {season.get('id')} не содержит {required - season.keys()}"

    def test_returns_copy(self):
        """get_all_seasons() не возвращает оригинальный список."""
        s1 = get_all_seasons()
        s2 = get_all_seasons()
        assert s1 is not s2


class TestGetSeasonForDate:
    def test_main_peak_march(self):
        season = get_season_for_date(date(2026, 3, 15))
        assert season is not None
        assert season["id"] == "main_peak"

    def test_summer_dip_july(self):
        season = get_season_for_date(date(2026, 7, 1))
        assert season is not None
        assert season["id"] == "summer_dip"

    def test_winter_holidays_december(self):
        """Зимние праздники — пересечение границы года (декабрь)."""
        season = get_season_for_date(date(2026, 12, 20))
        assert season is not None
        assert season["id"] == "winter_holidays"

    def test_january_returns_first_matching(self):
        """Январь попадает в main_peak (1-5) — он первый в списке."""
        season = get_season_for_date(date(2026, 1, 10))
        assert season is not None
        assert season["id"] == "main_peak"

    def test_november_returns_none(self):
        """Ноябрь — межсезонье, нет подходящего сезона."""
        season = get_season_for_date(date(2026, 11, 15))
        assert season is None

    def test_autumn_rebound_september(self):
        season = get_season_for_date(date(2026, 9, 1))
        assert season is not None
        assert season["id"] == "autumn_rebound"


class TestGetCurrentSeason:
    @patch("services.seasons.date")
    def test_delegates_to_season_for_date(self, mock_date):
        mock_date.today.return_value = date(2026, 9, 1)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        season = get_current_season()
        assert season is not None
        assert season["id"] == "autumn_rebound"
