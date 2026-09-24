"""Тесты языкового сервиса."""
from services.language import detect_language, get_language_config, get_analytics_by_language


class TestDetectLanguage:
    def test_l2_tag_in_desc_returns_l2(self):
        assert detect_language("", "[L2] Текст объявления") == "L2"

    def test_plain_text_returns_l1(self):
        assert detect_language("Тест", "Обычный текст") == "L1"

    def test_empty_text_returns_l1(self):
        assert detect_language("", "") == "L1"

    def test_l2_marker_in_name_only(self):
        assert detect_language("Тема А / l2", "Обычный текст без маркера") == "L2"

    def test_l2_tag_case_insensitive_inside_text(self):
        """Тег [l2] в любом регистре внутри описания → L2."""
        assert detect_language("Тест", "Текст [l2] внутри описания") == "L2"

    def test_marker_inside_word_is_not_marker(self):
        """«l2» внутри слова — не маркер: токен должен совпасть целиком."""
        assert detect_language("Тема model2l2x", "") == "L1"

    def test_bare_marker_in_desc_is_not_marker(self):
        """В описании считается только явный тег [L2], голое слово — нет."""
        assert detect_language("Тема", "обсуждали l2 кэш") == "L1"

    def test_custom_markers(self):
        """Набор маркеров настраивается: свой маркер ловится, дефолтный набор его не знает."""
        assert detect_language("Тема / en", "", markers=["EN"]) == "L2"
        assert detect_language("Тема / en", "") == "L1"


class TestGetLanguageConfig:
    def test_returns_l2_and_l1(self):
        config = get_language_config()
        assert "L2" in config
        assert "L1" in config

    def test_each_language_has_required_fields(self):
        required = {"id", "display_name", "display_name_ru", "ad_body", "form_id"}
        config = get_language_config()
        for lang_id, lang_data in config.items():
            assert required.issubset(lang_data.keys()), f"{lang_id} не содержит {required - lang_data.keys()}"

    def test_display_names_are_neutral_placeholders(self):
        """Имена языков — нейтральные плейсхолдеры, без названия конкретного языка."""
        config = get_language_config()
        assert config["L1"]["display_name"] == "Language 1"
        assert config["L1"]["display_name_ru"] == "Основной язык"
        assert config["L2"]["display_name"] == "Language 2"
        assert config["L2"]["display_name_ru"] == "Второй язык"

    def test_ad_body_not_empty(self):
        config = get_language_config()
        assert config["L2"]["ad_body"] != ""
        assert config["L1"]["ad_body"] != ""


class TestAnalyticsByLanguage:
    def test_with_data(self):
        ads = [
            {"adset_type": "L2", "spend": 100.0, "leads": 10, "cpl": 10.0},
            {"adset_type": "L2", "spend": 50.0, "leads": 5, "cpl": 10.0},
            {"adset_type": "L1", "spend": 200.0, "leads": 20, "cpl": 10.0},
        ]
        result = get_analytics_by_language(ads)
        assert result["L2"]["count"] == 2
        assert result["L2"]["spend"] == 150.0
        assert result["L2"]["leads"] == 15
        assert result["L2"]["avg_cpl"] == 10.0
        assert result["L1"]["count"] == 1
        assert result["L1"]["spend"] == 200.0
        assert result["L1"]["leads"] == 20
        assert result["L1"]["avg_cpl"] == 10.0

    def test_empty_list(self):
        result = get_analytics_by_language([])
        assert result["L2"]["count"] == 0
        assert result["L2"]["spend"] == 0.0
        assert result["L2"]["leads"] == 0
        assert result["L2"]["avg_cpl"] == 0.0
        assert result["L1"]["count"] == 0

    def test_zero_leads_avg_cpl_zero(self):
        ads = [{"adset_type": "L2", "spend": 100.0, "leads": 0, "cpl": 0}]
        result = get_analytics_by_language(ads)
        assert result["L2"]["avg_cpl"] == 0.0

    def test_missing_adset_type_skipped(self):
        ads = [
            {"spend": 100.0, "leads": 10},
            {"adset_type": "L2", "spend": 50.0, "leads": 5, "cpl": 10.0},
        ]
        result = get_analytics_by_language(ads)
        assert result["L2"]["count"] == 1
