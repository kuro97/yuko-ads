"""Тесты: progress_cb в _wait_video_ready и launch_creative с progress_cb=None.

У `integrations.facebook` больше нет собственной HTTP-сессии: все чтения идут
через общий `_throttled_get` из agent.fb_common, поэтому мокаем именно его.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_wait_video_ready_calls_progress_cb():
    """_wait_video_ready вызывает progress_cb при каждом опросе с растущим waited."""
    from integrations.facebook import _wait_video_ready

    # Первый опрос — "processing", второй — "ready"
    processing_resp = MagicMock()
    processing_resp.ok = True
    processing_resp.json.return_value = {"status": {"video_status": "processing"}}

    ready_resp = MagicMock()
    ready_resp.ok = True
    ready_resp.json.return_value = {"status": {"video_status": "ready"}}

    calls_received = []

    def fake_cb(step, step_pct=None):
        calls_received.append((step, step_pct))

    with patch("integrations.facebook._throttled_get", side_effect=[processing_resp, ready_resp]), \
         patch("integrations.facebook.get_fb_token", return_value="tok"), \
         patch("time.sleep"):  # мокаем sleep чтобы не ждать
        _wait_video_ready("vid123", max_wait=300, progress_cb=fake_cb)

    # Должно быть 2 вызова (по одному на каждый опрос)
    assert len(calls_received) == 2
    # Первый вызов: waited=5
    step0, pct0 = calls_received[0]
    assert "Facebook обрабатывает видео" in step0
    assert "5с" in step0
    assert pct0 == int(5 / 300 * 100)

    # Второй вызов: waited=10 (больше первого — растёт)
    step1, pct1 = calls_received[1]
    assert "10с" in step1
    assert pct1 > pct0


def test_wait_video_ready_no_crash_without_cb():
    """_wait_video_ready с progress_cb=None (дефолт) — не падает."""
    from integrations.facebook import _wait_video_ready

    ready_resp = MagicMock()
    ready_resp.ok = True
    ready_resp.json.return_value = {"status": {"video_status": "ready"}}

    with patch("integrations.facebook._throttled_get", return_value=ready_resp), \
         patch("integrations.facebook.get_fb_token", return_value="tok"), \
         patch("time.sleep"):
        # Не должно бросать исключение
        _wait_video_ready("vid456", max_wait=300)


def test_legacy_launch_creative_valid_proof_blocks_before_authorized_path():
    """Даже старый валидный proof не открывает raw-media provider path."""
    from integrations.facebook import launch_creative
    from services.launch_checker import LaunchCheckBlocked, ProviderLaunchAuthorization

    proof = ProviderLaunchAuthorization("auth-progress", "secret-progress")
    with patch("integrations.facebook._prepare_launch_media") as prepare, patch(
        "integrations.facebook._launch_authorized_creative"
    ) as authorized, pytest.raises(LaunchCheckBlocked) as error:
        launch_creative(
            card_name="Тест",
            adset_type="L2",
            media={"type": "image", "paths": ["/tmp/test.jpg"]},
            body="body",
            campaign_type="leadgen",
            authorization=proof,
        )

    assert error.value.code == "LEGACY_LAUNCH_BYPASS_FORBIDDEN"
    prepare.assert_not_called()
    authorized.assert_not_called()


def test_launch_creative_without_proof_blocks_before_media_or_impl():
    """Legacy default None сохранён только для импорта и всегда fail-closed."""
    from integrations.facebook import launch_creative
    from services.launch_checker import LaunchCheckBlocked

    with patch("integrations.facebook._prepare_launch_media") as prepare, \
         patch("integrations.facebook._launch_creative_impl") as launch_impl:
        with pytest.raises(LaunchCheckBlocked) as exc_info:
            launch_creative("Тест", "L2", {"type": "image", "paths": []}, "body")

    assert exc_info.value.code == "LEGACY_LAUNCH_BYPASS_FORBIDDEN"
    prepare.assert_not_called()
    launch_impl.assert_not_called()


def test_progress_cb_exception_doesnt_break_wait():
    """Исключение в progress_cb не роняет _wait_video_ready."""
    from integrations.facebook import _wait_video_ready

    ready_resp = MagicMock()
    ready_resp.ok = True
    ready_resp.json.return_value = {"status": {"video_status": "ready"}}

    def bad_cb(step, step_pct=None):
        raise RuntimeError("колбэк сломан")

    with patch("integrations.facebook._throttled_get", return_value=ready_resp), \
         patch("integrations.facebook.get_fb_token", return_value="tok"), \
         patch("time.sleep"):
        # Не должно бросать — ошибка колбэка поглощается
        _wait_video_ready("vid789", max_wait=300, progress_cb=bad_cb)
