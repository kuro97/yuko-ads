"""
Тесты визуального анализа креативов через Gemini Vision.
"""

import json
from unittest.mock import patch, MagicMock

import pytest


# --- Интеграция: vision.py ---


class TestAnalyzeVideoFrames:
    """Тесты Gemini Vision обёртки."""

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    def test_valid_json_response(self):
        """Gemini возвращает валидный JSON — парсится корректно."""
        import importlib
        import integrations.vision as vis

        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "first_frame_type": "person_talking",
            "has_person": True,
            "has_subtitles": False,
            "emotion": "positive",
            "text_overlay": None,
            "hook_description": "Человек улыбается в камеру",
            "summary": "Тестовый креатив",
        })
        mock_genai.GenerativeModel.return_value = mock_model
        mock_model.generate_content.return_value = mock_response

        with patch.dict("sys.modules", {"google.generativeai": mock_genai}):
            result = vis.analyze_video_frames([b"fake_frame"])

        assert result["first_frame_type"] == "person_talking"
        assert result["has_person"] is True
        assert result["emotion"] == "positive"

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    def test_markdown_wrapped_json(self):
        """Gemini возвращает JSON в markdown-блоке — корректно очищается."""
        import integrations.vision as vis

        wrapped = '```json\n{"first_frame_type":"text_screen","has_person":false,"has_subtitles":true,"emotion":"neutral","text_overlay":"Скидка 50%","hook_description":"Текст","summary":"Промо"}\n```'

        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = wrapped
        mock_genai.GenerativeModel.return_value = mock_model
        mock_model.generate_content.return_value = mock_response

        with patch.dict("sys.modules", {"google.generativeai": mock_genai}):
            result = vis.analyze_video_frames([b"frame"])

        assert result["first_frame_type"] == "text_screen"
        assert result["has_subtitles"] is True
        assert result["text_overlay"] == "Скидка 50%"

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    def test_invalid_json_fallback(self):
        """Gemini возвращает невалидный JSON — fallback."""
        import integrations.vision as vis

        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Я не могу ответить в JSON формате, вот текст..."
        mock_genai.GenerativeModel.return_value = mock_model
        mock_model.generate_content.return_value = mock_response

        with patch.dict("sys.modules", {"google.generativeai": mock_genai}):
            result = vis.analyze_video_frames([b"frame"])

        assert result["first_frame_type"] == "other"
        assert result["has_person"] is False
        assert "Не удалось распарсить" in result["summary"]

    @patch.dict("os.environ", {}, clear=True)
    @patch("config.GEMINI_API_KEY", None)
    def test_no_api_key_raises(self):
        """Без GEMINI_API_KEY — RuntimeError.

        Гнилой тест (A4): analyze_video_frames читает `from config import
        GEMINI_API_KEY` внутри функции (значение из .env, загруженное при импорте
        config), а не только os.environ. Очистки os.environ недостаточно — нужно
        также патчить config.GEMINI_API_KEY, иначе ключ из .env "просачивается"
        и код не бросает RuntimeError."""
        from integrations.vision import analyze_video_frames

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            analyze_video_frames([b"frame"])

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    def test_with_ad_name(self):
        """ad_name включается в промпт."""
        import integrations.vision as vis

        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "first_frame_type": "lifestyle",
            "has_person": True,
            "has_subtitles": False,
            "emotion": "energetic",
            "text_overlay": None,
            "hook_description": "Клиенты пользуются сервисом",
            "summary": "Реклама сервиса",
        })
        mock_genai.GenerativeModel.return_value = mock_model
        mock_model.generate_content.return_value = mock_response

        with patch.dict("sys.modules", {"google.generativeai": mock_genai}):
            result = vis.analyze_video_frames([b"frame"], ad_name="CityA | PRODA оффер")

        assert result["first_frame_type"] == "lifestyle"
        # Проверяем что ad_name был в промпте
        call_args = mock_model.generate_content.call_args[0][0]
        assert "CityA | PRODA оффер" in call_args[0]


# --- Тест analyze_image ---

class TestAnalyzeImage:
    """Тесты analyze_image."""

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    def test_returns_text(self):
        """analyze_image возвращает текст ответа."""
        from integrations.vision import analyze_image

        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "На изображении человек"
        mock_genai.GenerativeModel.return_value = mock_model
        mock_model.generate_content.return_value = mock_response

        with patch.dict("sys.modules", {"google.generativeai": mock_genai}):
            result = analyze_image(b"image_data", "Опиши изображение")

        assert result["text"] == "На изображении человек"

    @patch.dict("os.environ", {}, clear=True)
    @patch("config.GEMINI_API_KEY", None)
    def test_no_api_key_raises(self):
        """Без GEMINI_API_KEY — RuntimeError.

        Гнилой тест (A4): analyze_image читает `from config import GEMINI_API_KEY`
        внутри функции — нужно патчить config.GEMINI_API_KEY, а не только
        os.environ (см. пояснение в TestAnalyzeVideoFrames.test_no_api_key_raises)."""
        from integrations.vision import analyze_image

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            analyze_image(b"data", "prompt")


# --- Сервис: vision_analysis.py ---


class TestAnalysisStorage:
    """Тесты хранения анализов в JSON-файле."""

    def test_get_analysis_empty(self, tmp_path):
        """Пустое хранилище возвращает None."""
        import services.vision_analysis as svc

        with patch.object(svc, "ANALYSIS_FILE", tmp_path / "empty.json"):
            assert svc.get_analysis("12345") is None

    def test_save_and_load(self, tmp_path):
        """Сохранение и загрузка анализа."""
        import services.vision_analysis as svc

        analysis_file = tmp_path / "vision_analysis.json"
        with patch.object(svc, "ANALYSIS_FILE", analysis_file), \
             patch.object(svc, "DATA_DIR", tmp_path):
            svc._save_analyses({"123": {"first_frame_type": "person_talking"}})
            result = svc._load_analyses()

        assert result["123"]["first_frame_type"] == "person_talking"

    def test_get_all_analyses(self, tmp_path):
        """Получение всех анализов."""
        import services.vision_analysis as svc

        analysis_file = tmp_path / "vision_analysis.json"
        analysis_file.write_text(json.dumps({
            "111": {"first_frame_type": "person_talking"},
            "222": {"first_frame_type": "text_screen"},
        }))

        with patch.object(svc, "ANALYSIS_FILE", analysis_file):
            result = svc.get_all_analyses()

        assert len(result) == 2


class TestAnalyzeCreative:
    """Тесты анализа креатива."""

    def test_returns_cached(self, tmp_path):
        """Если анализ уже есть — возвращает из кэша."""
        import services.vision_analysis as svc

        cached_data = {"123": {"first_frame_type": "product", "ad_id": "123"}}
        analysis_file = tmp_path / "vision_analysis.json"
        analysis_file.write_text(json.dumps(cached_data))

        with patch.object(svc, "ANALYSIS_FILE", analysis_file):
            result = svc.analyze_creative("123")

        assert result["first_frame_type"] == "product"

    @patch("services.vision_analysis.analyze_video_frames")
    @patch("services.vision_analysis.extract_frames_from_video")
    def test_with_video_url(self, mock_extract, mock_analyze, tmp_path):
        """Анализ по прямому URL видео."""
        import services.vision_analysis as svc

        mock_extract.return_value = [b"frame1", b"frame2"]
        mock_analyze.return_value = {
            "first_frame_type": "person_talking",
            "has_person": True,
            "has_subtitles": False,
            "emotion": "positive",
            "text_overlay": None,
            "hook_description": "Человек в кадре",
            "summary": "Тестовый анализ",
        }

        analysis_file = tmp_path / "vision_analysis.json"
        with patch.object(svc, "ANALYSIS_FILE", analysis_file), \
             patch.object(svc, "DATA_DIR", tmp_path):
            result = svc.analyze_creative(
                "456", ad_name="Тест", video_url="https://example.com/video.mp4"
            )

        assert result["first_frame_type"] == "person_talking"
        assert result["ad_id"] == "456"
        assert result["frames_count"] == 2

    def test_no_video_url_raises(self, tmp_path):
        """Нет video_url и FB не вернул — ValueError."""
        import services.vision_analysis as svc

        analysis_file = tmp_path / "empty.json"

        with patch.object(svc, "ANALYSIS_FILE", analysis_file), \
             patch("services.vision_analysis.get_video_url_from_fb", return_value=None):
            with pytest.raises(ValueError, match="Видео не найдено"):
                svc.analyze_creative("789")

    @patch("services.vision_analysis.analyze_video_frames")
    @patch("services.vision_analysis.extract_frames_from_video")
    def test_no_frames_raises(self, mock_extract, mock_analyze, tmp_path):
        """Нет кадров — RuntimeError."""
        import services.vision_analysis as svc

        mock_extract.return_value = []
        analysis_file = tmp_path / "empty.json"

        with patch.object(svc, "ANALYSIS_FILE", analysis_file):
            with pytest.raises(RuntimeError, match="Не удалось извлечь кадры"):
                svc.analyze_creative("999", video_url="https://example.com/v.mp4")


class TestVisionPatterns:
    """Тесты поиска паттернов."""

    def test_empty_patterns(self, tmp_path):
        """Нет анализов — пустые паттерны."""
        import services.vision_analysis as svc

        with patch.object(svc, "ANALYSIS_FILE", tmp_path / "empty.json"):
            result = svc.get_vision_patterns()

        assert result["total_analyzed"] == 0
        assert result["patterns"] == []

    def test_patterns_with_data(self, tmp_path):
        """Паттерны считаются корректно."""
        import services.vision_analysis as svc

        analyses = {
            "1": {"first_frame_type": "person_talking", "has_person": True},
            "2": {"first_frame_type": "person_talking", "has_person": True},
            "3": {"first_frame_type": "text_screen", "has_person": False},
        }
        analysis_file = tmp_path / "vision_analysis.json"
        analysis_file.write_text(json.dumps(analyses))

        learner_data = {
            "creative_table": [
                {"ad_id": "1", "cpl": 20.0},
                {"ad_id": "2", "cpl": 30.0},
                {"ad_id": "3", "cpl": 50.0},
            ]
        }
        learner_file = tmp_path / "learner_results.json"
        learner_file.write_text(json.dumps(learner_data))

        with patch.object(svc, "ANALYSIS_FILE", analysis_file), \
             patch.object(svc, "DATA_DIR", tmp_path):
            result = svc.get_vision_patterns()

        assert result["total_analyzed"] == 3
        assert len(result["patterns"]) == 2

        pt = next(p for p in result["patterns"] if p["type"] == "person_talking")
        assert pt["count"] == 2
        assert pt["avg_cpl"] == 25.0
        assert pt["person_pct"] == 100.0

        ts = next(p for p in result["patterns"] if p["type"] == "text_screen")
        assert ts["count"] == 1
        assert ts["avg_cpl"] == 50.0

    def test_type_description(self):
        """Описания типов корректны."""
        from services.vision_analysis import _type_description

        assert "Человек" in _type_description("person_talking")
        assert "Текст" in _type_description("text_screen")
        assert _type_description("unknown") == "unknown"


# --- API тесты ---


class TestVisionAPI:
    """Тесты FastAPI эндпоинтов."""

    @patch("web.app.get_vision_analysis", return_value=None)
    def test_get_analysis_not_found(self, mock_get):
        """GET /api/creative/{ad_id}/analysis — 404 если нет анализа."""
        from fastapi.testclient import TestClient
        from web.app import app

        client = TestClient(app)
        resp = client.get("/api/creative/99999/analysis")
        assert resp.status_code == 404

    @patch("web.app.get_vision_analysis")
    def test_get_analysis_found(self, mock_get):
        """GET /api/creative/{ad_id}/analysis — возвращает анализ."""
        from fastapi.testclient import TestClient
        from web.app import app

        mock_get.return_value = {
            "ad_id": "123",
            "first_frame_type": "person_talking",
            "has_person": True,
        }

        client = TestClient(app)
        resp = client.get("/api/creative/123/analysis")
        assert resp.status_code == 200
        assert resp.json()["first_frame_type"] == "person_talking"

    @patch("web.app.get_vision_patterns")
    def test_get_patterns(self, mock_patterns):
        """GET /api/vision/patterns — возвращает паттерны."""
        from fastapi.testclient import TestClient
        from web.app import app

        mock_patterns.return_value = {"total_analyzed": 5, "patterns": []}

        client = TestClient(app)
        resp = client.get("/api/vision/patterns")
        assert resp.status_code == 200
        assert resp.json()["total_analyzed"] == 5

    @patch("web.app.get_all_vision_analyses")
    def test_list_all_analyses(self, mock_all):
        """GET /api/vision/analyses — список всех анализов."""
        from fastapi.testclient import TestClient
        from web.app import app

        mock_all.return_value = {
            "111": {"ad_id": "111", "first_frame_type": "product"},
            "222": {"ad_id": "222", "first_frame_type": "lifestyle"},
        }

        client = TestClient(app)
        resp = client.get("/api/vision/analyses")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    @patch("web.app.analyze_creative")
    def test_run_analysis(self, mock_analyze):
        """POST /api/creative/{ad_id}/analysis — запуск анализа."""
        from fastapi.testclient import TestClient
        from web.app import app

        mock_analyze.return_value = {
            "ad_id": "456",
            "first_frame_type": "text_screen",
            "has_person": False,
        }

        client = TestClient(app)
        resp = client.post(
            "/api/creative/456/analysis",
            json={"video_url": "https://example.com/v.mp4", "ad_name": "Тест"},
        )
        assert resp.status_code == 200
        assert resp.json()["first_frame_type"] == "text_screen"
