"""Точечный recovery missing FB asset без сетевых вызовов."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from services.ad_asset_recovery import (
    PreparedAsset,
    ProductionRecoveryBackend,
    ManifestValidationError,
    RecoveryError,
    RecoveryLedger,
    RecoveryManifest,
    recover_one,
    recover_sequential,
)
from scripts.recover_missing_ad_asset import main as recovery_cli_main


NOW = datetime.fromisoformat("2026-07-20T12:00:00+05:00")
CREATED_AT = "2026-07-20T11:00:00+0500"
LIVE_ADSET_NAMES = {
    "CityA": "Adset | L1 | Tag | Гео CityA | v1.1",
    "CityB": "Adset | L1 | Tag | Гео | CityB v1",
    "CityC": "Adset | L1 | Tag | Гео CityC v2",
}


def _adset_name(city: str, adset_type: str = "L1") -> str:
    live_name = LIVE_ADSET_NAMES.get(city)
    if live_name:
        return live_name.replace(" | L1 | ", f" | {adset_type} | ")
    return f"Adset | {adset_type} | Tag | Гео {city} | test"


def _manifest(
    *,
    city: str = "CityB",
    adset_id: str = "52608012843319",
) -> RecoveryManifest:
    title = "Креаторы / Тема А / Подтема 1, 2 видео"
    return RecoveryManifest.from_dict({
        "account_kind": "offline",
        "account_id": "152882611033373",
        "card_id": "000000000000000000000abc",
        "card_title": title,
        "drive_url": (
            "https://drive.google.com/drive/folders/"
            "1ExampleDriveFileId0000000000000"
        ),
        "media_basename": "тиктокер 2.mp4",
        "city": city,
        "adset_id": adset_id,
        "expected_adset_name": _adset_name(city),
        "adset_type": "L1",
        "expected_ad_name": (
            f"{city} | {title} / тиктокер 2 [PRODB]"
        ),
        "sibling_expected_ad_name": (
            f"{city} | {title} / тиктокер 1 [PRODB]"
        ),
        "product": "PRODB",
        "window_start": "2026-07-20T00:00:00+05:00",
        "window_end": "2026-07-20T23:59:59+05:00",
    })


def _source_manifest(**changes: Any) -> RecoveryManifest:
    values = {
        "drive_url": None,
        "source_ad_id": "53504247540033",
        "source_adset_id": "56528334005133",
        "expected_source_adset_name": _adset_name("CityD"),
    }
    values.update(changes)
    return replace(_manifest(), **values)


def _ad(
    ad_id: str,
    name: str,
    *,
    status: str = "ACTIVE",
    effective_status: str = "ACTIVE",
    created_time: str = CREATED_AT,
) -> dict[str, str]:
    return {
        "id": ad_id,
        "name": name,
        "status": status,
        "effective_status": effective_status,
        "created_time": created_time,
    }


class FakeBackend:
    def __init__(self, manifests: list[RecoveryManifest]) -> None:
        self.account_id = "152882611033373"
        self.inventories = {
            manifest.adset_id: (
                [_ad(f"sibling-{manifest.city}", manifest.sibling_expected_ad_name)]
                if manifest.sibling_expected_ad_name
                else [_ad(f"active-{manifest.city}", "Другой ACTIVE")]
            )
            for manifest in manifests
        }
        self.adset_names = {
            manifest.adset_id: manifest.expected_adset_name for manifest in manifests
        }
        self.inventory_complete = {manifest.adset_id: True for manifest in manifests}
        self.unknown = {manifest.adset_id: [] for manifest in manifests}
        self.prepared: list[str] = []
        self.created: list[str] = []
        self.lock_events: list[tuple[str, str]] = []
        self.create_mode = "success"
        self.source_revalidation_error = False
        self.current_time = NOW

    @contextmanager
    def account_scope(self, account_kind):
        assert account_kind == "offline"
        yield

    @contextmanager
    def adset_lock(self, adset_id):
        self.lock_events.append(("enter", adset_id))
        try:
            yield
        finally:
            self.lock_events.append(("exit", adset_id))

    def current_account_id(self) -> str:
        return self.account_id

    def get_adset_info(self, adset_id: str) -> dict[str, Any]:
        ads = [dict(ad) for ad in self.inventories[adset_id]]
        return {
            "adset_id": adset_id,
            "name": self.adset_names[adset_id],
            "inventory_complete": self.inventory_complete[adset_id],
            "unknown_effective_status_ids": list(self.unknown[adset_id]),
            "ads": ads,
            "ad_count": len(ads),
            "effective_active_count": sum(
                ad["effective_status"] == "ACTIVE" for ad in ads
            ),
        }

    def prepare_asset(self, manifest: RecoveryManifest) -> PreparedAsset:
        self.prepared.append(manifest.city)
        if manifest.source_ad_id:
            return PreparedAsset(
                "existing_creative",
                "",
                creative_id="creative-1",
                source_name=(
                    f"CityD | {manifest.expected_ad_name.split(' | ', 1)[1]}"
                ),
            )
        return PreparedAsset("video", "thumb-hash", "video-id")

    def revalidate_prepared(
        self, manifest: RecoveryManifest, prepared: PreparedAsset
    ) -> None:
        if self.source_revalidation_error:
            raise RecoveryError("source изменился")

    def create_asset(
        self, manifest: RecoveryManifest, prepared: PreparedAsset
    ) -> str:
        self.created.append(manifest.city)
        ad_id = f"target-{manifest.city}"
        if self.create_mode in {"success", "ambiguous_active"}:
            self.inventories[manifest.adset_id].append(
                _ad(ad_id, manifest.expected_ad_name)
            )
        if self.create_mode in {"ambiguous_active", "exception_absent"}:
            raise RuntimeError("неоднозначный ответ FB")
        return ad_id

    def now(self) -> datetime:
        return self.current_time


@pytest.fixture
def ledger(tmp_path: Path) -> RecoveryLedger:
    return RecoveryLedger(tmp_path / "recovery.json")


def test_already_exists_reconciles_without_create(ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])
    backend.inventories[manifest.adset_id].append(
        _ad("existing-target", manifest.expected_ad_name)
    )

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "SUCCEEDED"
    assert result.reason == "already_exists_reconciled"
    assert result.ad_id == "existing-target"
    assert backend.prepared == []
    assert backend.created == []


def test_crash_reconcile_succeeds_after_window_without_second_create(ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])
    ledger.put(manifest, "CREATING", reason="create_started")
    backend.inventories[manifest.adset_id].append(
        _ad("existing-target", manifest.expected_ad_name)
    )
    backend.current_time = datetime.fromisoformat("2026-07-21T01:00:00+05:00")

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "SUCCEEDED"
    assert result.reason == "already_exists_reconciled"
    assert result.ad_id == "existing-target"
    assert backend.created == []


def test_wrong_live_target_adset_name_blocks_before_prepare(ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])
    backend.adset_names[manifest.adset_id] = _adset_name("CityA")

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "BLOCKED"
    assert result.reason == "initial_inventory_failed"
    assert backend.prepared == []
    assert backend.created == []


@pytest.mark.parametrize(
    "expected_adset_name",
    [_adset_name("CityA"), _adset_name("CityB", "L2")],
    ids=["wrong-city", "wrong-type"],
)
def test_manifest_rejects_wrong_target_adset_identity(expected_adset_name):
    with pytest.raises(ManifestValidationError, match="expected_adset_name"):
        replace(_manifest(), expected_adset_name=expected_adset_name).validate()


@pytest.mark.parametrize(
    "city,adset_id,live_name",
    [
        ("CityA", "58029559394919", LIVE_ADSET_NAMES["CityA"]),
        ("CityB", "52608012843319", LIVE_ADSET_NAMES["CityB"]),
        ("CityC", "50001463399805", LIVE_ADSET_NAMES["CityC"]),
    ],
)
def test_manifest_accepts_exact_target_adset_name_variants(city, adset_id, live_name):
    manifest = replace(
        _manifest(city=city, adset_id=adset_id),
        expected_adset_name=live_name,
    )

    manifest.validate()


def test_manifest_city_match_rejects_substring_inside_word():
    manifest = replace(
        _manifest(),
        expected_adset_name="Adset | L1 | Tag | Гео ТестCityB v1",
    )

    with pytest.raises(ManifestValidationError, match="сегмент города"):
        manifest.validate()


@pytest.mark.parametrize(
    "setup,expected_reason",
    [
        (
            lambda backend, manifest: backend.inventory_complete.__setitem__(
                manifest.adset_id, False
            ),
            "initial_inventory_failed",
        ),
        (
            lambda backend, manifest: backend.unknown.__setitem__(
                manifest.adset_id, ["unknown-ad"]
            ),
            "initial_inventory_failed",
        ),
        (
            lambda backend, manifest: backend.inventories.__setitem__(
                manifest.adset_id,
                [
                    _ad(
                        "paused-sibling",
                        manifest.sibling_expected_ad_name,
                        status="PAUSED",
                        effective_status="PAUSED",
                    )
                ],
            ),
            "zero_effective_active",
        ),
        (
            lambda backend, manifest: backend.inventories.__setitem__(
                manifest.adset_id,
                backend.inventories[manifest.adset_id]
                + [
                    _ad(f"filler-{index}", f"Filler {index}")
                    for index in range(49)
                ],
            ),
            "no_free_slot",
        ),
        (
            lambda backend, manifest: backend.inventories.__setitem__(
                manifest.adset_id, [_ad("other", "Другой exact name")]
            ),
            "sibling_exact_count_not_one",
        ),
    ],
    ids=["incomplete", "unknown", "zero-active", "no-slot", "sibling"],
)
def test_preflight_blocks_without_create(setup, expected_reason, ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])
    setup(backend, manifest)

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "BLOCKED"
    assert result.reason == expected_reason
    assert backend.prepared == []
    assert backend.created == []


def test_success_requires_live_exact_active_reconcile(ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "SUCCEEDED"
    assert result.reason == "created_and_reconciled"
    assert result.ad_id == "target-CityB"
    assert backend.prepared == ["CityB"]
    assert backend.created == ["CityB"]
    assert ledger.get(manifest.key)["phase"] == "SUCCEEDED"


def test_ambiguous_create_exception_with_one_active_is_success(ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])
    backend.create_mode = "ambiguous_active"

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "SUCCEEDED"
    assert result.reason == "create_exception_reconciled"
    assert result.ad_id == "target-CityB"


def test_create_exception_without_target_blocks_and_never_retries(ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])
    backend.create_mode = "exception_absent"

    first = recover_one(manifest, backend=backend, ledger=ledger)
    second = recover_one(manifest, backend=backend, ledger=ledger)

    assert first.status == "BLOCKED"
    assert first.reason == "create_exception_target_absent"
    assert second.status == "BLOCKED"
    assert second.reason == "previous_attempt_target_absent"
    assert backend.created == ["CityB"]


def test_duplicate_target_is_blocked(ledger):
    manifest = _manifest()
    backend = FakeBackend([manifest])
    backend.inventories[manifest.adset_id].extend([
        _ad("target-1", manifest.expected_ad_name),
        _ad("target-2", manifest.expected_ad_name),
    ])

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "BLOCKED"
    assert result.reason == "target_exact_count_not_one"
    assert backend.created == []


def test_cities_run_strictly_sequentially(ledger):
    cityb = _manifest()
    cityc = _manifest(city="CityC", adset_id="50001463399805")
    backend = FakeBackend([cityb, cityc])

    results = recover_sequential(
        [cityb, cityc], backend=backend, ledger=ledger
    )

    assert [result.status for result in results] == ["SUCCEEDED", "SUCCEEDED"]
    assert backend.created == ["CityB", "CityC"]
    assert backend.lock_events == [
        ("enter", cityb.adset_id),
        ("exit", cityb.adset_id),
        ("enter", cityc.adset_id),
        ("exit", cityc.adset_id),
    ]


def test_source_ad_mode_reuses_creative_without_drive(ledger):
    manifest = _source_manifest()
    manifest.validate()
    backend = FakeBackend([manifest])

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "SUCCEEDED"
    assert backend.prepared == ["CityB"]
    assert backend.created == ["CityB"]


def test_source_mode_still_checks_nonempty_sibling_name(ledger):
    manifest = _source_manifest()
    backend = FakeBackend([manifest])
    backend.inventories[manifest.adset_id] = [_ad("other", "Другой ACTIVE")]

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "BLOCKED"
    assert result.reason == "sibling_exact_count_not_one"
    assert backend.prepared == []
    assert backend.created == []


def test_source_mode_supports_single_file_card_name_without_sibling(ledger):
    title = "Петров / Тема В / Подтема 3"
    manifest = RecoveryManifest.from_dict({
        "account_kind": "offline",
        "account_id": "152882611033373",
        "card_id": "000000000000000000000def",
        "card_title": title,
        "media_basename": "5.mp4",
        "city": "CityA",
        "adset_id": "58029559394919",
        "expected_adset_name": _adset_name("CityA"),
        "adset_type": "L1",
        "expected_ad_name": f"CityA | {title} [PRODA]",
        "sibling_expected_ad_name": "",
        "product": "PRODA",
        "window_start": "2026-07-20T00:00:00+05:00",
        "window_end": "2026-07-20T23:59:59+05:00",
        "source_ad_id": "56024347287436",
        "source_adset_id": "56528334005133",
        "expected_source_adset_name": _adset_name("CityD"),
        "name_mode": "card_only",
    })
    backend = FakeBackend([manifest])

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "SUCCEEDED"
    assert backend.created == ["CityA"]


def test_source_mode_accepts_legacy_untagged_exact_name():
    title = "Петров / Тема Г / Подтема 4"
    manifest = RecoveryManifest.from_dict({
        "account_kind": "offline",
        "account_id": "152882611033373",
        "card_id": "000000000000000000000def",
        "card_title": title,
        "media_basename": "4.mp4",
        "city": "CityA",
        "adset_id": "58029559394919",
        "expected_adset_name": _adset_name("CityA"),
        "adset_type": "L1",
        "expected_ad_name": f"CityA | {title}",
        "sibling_expected_ad_name": "",
        "product": "PRODA",
        "window_start": "2026-07-20T00:00:00+05:00",
        "window_end": "2026-07-20T23:59:59+05:00",
        "source_ad_id": "52549205039094",
        "source_adset_id": "56528334005133",
        "expected_source_adset_name": _adset_name("CityD"),
        "name_mode": "card_only",
    })

    assert manifest.expected_ad_name == f"CityA | {title}"


def test_source_mode_accepts_requested_product_tag():
    manifest = _source_manifest()

    manifest.validate()


def test_source_mode_rejects_wrong_derived_suffix():
    manifest = _source_manifest(
        expected_ad_name="CityB | Креаторы / другой ролик [PRODB]",
    )

    with pytest.raises(ManifestValidationError, match="city/card/media/product"):
        manifest.validate()


def test_source_mode_rejects_other_known_product_tag():
    manifest = _source_manifest(
        expected_ad_name=(
            "CityB | Креаторы / Тема А / Подтема 1, 2 видео / "
            "тиктокер 2 [PRODA]"
        ),
    )

    with pytest.raises(ManifestValidationError, match="другого продукта"):
        manifest.validate()


def test_drive_mode_still_rejects_legacy_untagged_name():
    manifest = replace(
        _manifest(),
        expected_ad_name=(
            "CityB | Креаторы / Тема А / Подтема 1, 2 видео / "
            "тиктокер 2"
        ),
    )

    with pytest.raises(ManifestValidationError, match="city/card/media/product"):
        manifest.validate()


def test_source_change_on_final_revalidation_blocks_create(ledger):
    manifest = _source_manifest()
    backend = FakeBackend([manifest])
    backend.source_revalidation_error = True

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "BLOCKED"
    assert result.reason == "source_revalidation_failed"
    assert backend.created == []


def test_time_window_is_rechecked_immediately_before_create(ledger):
    manifest = _source_manifest()
    backend = FakeBackend([manifest])
    backend.now = Mock(side_effect=[
        NOW,
        datetime.fromisoformat("2026-07-21T00:00:01+05:00"),
    ])

    result = recover_one(manifest, backend=backend, ledger=ledger)

    assert result.status == "BLOCKED"
    assert result.reason == "outside_manifest_time_window"
    assert backend.created == []


def test_production_source_requires_same_city_stripped_exact_name():
    manifest = _source_manifest()
    source = {
        "ad_id": manifest.source_ad_id,
        "name": (
            "CityD | Креаторы / Тема А / Подтема 1, 2 видео / "
            "другой ролик [PRODB]"
        ),
        "account_id": manifest.account_id,
        "adset_id": manifest.source_adset_id,
        "adset_name": manifest.expected_source_adset_name,
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "creative_id": "123456",
    }

    with patch(
        "integrations.facebook.get_existing_ad_creative_source",
        return_value=source,
    ):
        with pytest.raises(RecoveryError, match="разную exact identity"):
            ProductionRecoveryBackend().prepare_asset(manifest)


def test_fb_source_lookup_requires_effective_active():
    from integrations.facebook import (
        ExistingCreativeSourceError,
        get_existing_ad_creative_source,
    )

    response = Mock(status_code=200)
    response.json.return_value = {
        "53504247540033": {
            "id": "53504247540033",
            "name": "CityD | Exact creative",
            "account_id": "152882611033373",
            "adset": {
                "id": "56528334005133",
                "name": _adset_name("CityD"),
            },
            "status": "ACTIVE",
            "effective_status": "PAUSED",
            "creative": {"id": "123456"},
        }
    }
    with patch("integrations.facebook.get_fb_account_id", return_value="152882611033373"), \
         patch("integrations.facebook.get_fb_token", return_value="token"), \
         patch("integrations.facebook._throttled_get", return_value=response):
        with pytest.raises(ExistingCreativeSourceError):
            get_existing_ad_creative_source(
                "53504247540033",
                "152882611033373",
                "56528334005133",
                _adset_name("CityD"),
                "L1",
            )


def test_fb_source_lookup_returns_exact_live_creative():
    from integrations.facebook import get_existing_ad_creative_source

    response = Mock(status_code=200)
    response.json.return_value = {
        "53504247540033": {
            "id": "53504247540033",
            "name": "CityD | Exact creative",
            "account_id": "152882611033373",
            "adset": {
                "id": "56528334005133",
                "name": _adset_name("CityD"),
            },
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "creative": {"id": "123456"},
        }
    }
    with patch("integrations.facebook.get_fb_account_id", return_value="152882611033373"), \
         patch("integrations.facebook.get_fb_token", return_value="token"), \
         patch("integrations.facebook._throttled_get", return_value=response):
        source = get_existing_ad_creative_source(
            "53504247540033",
            "152882611033373",
            "56528334005133",
            _adset_name("CityD"),
            "L1",
        )

    assert source["creative_id"] == "123456"
    assert source["effective_status"] == "ACTIVE"


@pytest.mark.parametrize(
    "city,live_name",
    list(LIVE_ADSET_NAMES.items()),
)
def test_fb_source_lookup_accepts_adset_city_segment_variants(city, live_name):
    from integrations.facebook import get_existing_ad_creative_source

    response = Mock(status_code=200)
    response.json.return_value = {
        "53504247540033": {
            "id": "53504247540033",
            "name": f"{city} | Exact creative",
            "account_id": "152882611033373",
            "adset": {"id": "56528334005133", "name": live_name},
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "creative": {"id": "123456"},
        }
    }
    with (
        patch(
            "integrations.facebook.get_fb_account_id",
            return_value="152882611033373",
        ),
        patch("integrations.facebook.get_fb_token", return_value="token"),
        patch("integrations.facebook._throttled_get", return_value=response),
    ):
        source = get_existing_ad_creative_source(
            "53504247540033",
            "152882611033373",
            "56528334005133",
            live_name,
            "L1",
        )

    assert source["adset_name"] == live_name


@pytest.mark.parametrize(
    "live_adset_id,live_adset_name",
    [
        ("99999999999999", _adset_name("CityD")),
        ("56528334005133", _adset_name("CityC")),
        ("56528334005133", _adset_name("CityD", "L2")),
    ],
    ids=["wrong-source-adset-id", "wrong-source-adset-name", "wrong-source-type"],
)
def test_fb_source_lookup_rejects_wrong_source_adset_identity(
    live_adset_id,
    live_adset_name,
):
    from integrations.facebook import (
        ExistingCreativeSourceError,
        get_existing_ad_creative_source,
    )

    response = Mock(status_code=200)
    response.json.return_value = {
        "53504247540033": {
            "id": "53504247540033",
            "name": "CityD | Exact creative",
            "account_id": "152882611033373",
            "adset": {"id": live_adset_id, "name": live_adset_name},
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "creative": {"id": "123456"},
        }
    }
    with (
        patch(
            "integrations.facebook.get_fb_account_id",
            return_value="152882611033373",
        ),
        patch("integrations.facebook.get_fb_token", return_value="token"),
        patch("integrations.facebook._throttled_get", return_value=response),
        pytest.raises(ExistingCreativeSourceError),
    ):
        get_existing_ad_creative_source(
            "53504247540033",
            "152882611033373",
            "56528334005133",
            _adset_name("CityD"),
            "L1",
        )


def test_production_existing_creative_create_is_blocked_without_gateway_attempt():
    backend = ProductionRecoveryBackend()
    manifest = _manifest()
    prepared = PreparedAsset(
        media_type="existing_creative",
        image_hash=None,
        creative_id="123456",
        source_name="CityD | Exact creative",
    )

    # CREATE живёт только в execution-транспорте — именно его и сторожим.
    with patch(
        "integrations.facebook_ads_mutation_transport.create_ad"
    ) as provider_create, pytest.raises(
        RecoveryError,
        match="asset_recovery_gateway_id_missing",
    ):
        backend.create_asset(manifest, prepared)

    provider_create.assert_not_called()


def test_manifest_rejects_name_not_derived_from_exact_basename():
    manifest = _manifest()

    with pytest.raises(ValueError, match="expected_ad_name"):
        replace(manifest, expected_ad_name="CityB | Неверное имя").validate()


def test_ledger_fsyncs_parent_directory_after_replace(tmp_path):
    ledger = RecoveryLedger(tmp_path / "state" / "recovery.json")
    real_replace = __import__("os").replace
    real_fsync = __import__("os").fsync
    events: list[str] = []

    def tracked_replace(source, target):
        events.append("replace")
        return real_replace(source, target)

    def tracked_fsync(file_descriptor):
        events.append("fsync")
        return real_fsync(file_descriptor)

    with (
        patch("services.ad_asset_recovery.os.replace", side_effect=tracked_replace),
        patch("services.ad_asset_recovery.os.fsync", side_effect=tracked_fsync),
    ):
        ledger.put(_manifest(), "PREPARED", reason="test")

    assert events == ["fsync", "replace", "fsync"]


def test_production_drive_mode_blocks_before_external_context(ledger):
    backend = ProductionRecoveryBackend()

    with patch.object(backend, "account_scope") as account_scope:
        result = recover_one(_manifest(), backend=backend, ledger=ledger)

    assert result.status == "BLOCKED"
    assert result.reason == "production_recovery_source_only"
    account_scope.assert_not_called()


def test_cli_validation_only_returns_zero(tmp_path, capsys):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(asdict(_manifest()), ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = recovery_cli_main(["--manifest", str(manifest_path)])

    assert exit_code == 0
    assert '"status": "VALIDATED_ONLY"' in capsys.readouterr().out


def test_cli_execute_drive_blocks_without_recovery_calls(tmp_path, capsys):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(asdict(_manifest()), ensure_ascii=False),
        encoding="utf-8",
    )

    with patch("scripts.recover_missing_ad_asset.recover_sequential") as recover:
        exit_code = recovery_cli_main([
            "--manifest",
            str(manifest_path),
            "--execute",
        ])

    assert exit_code == 3
    assert "production_recovery_source_only" in capsys.readouterr().out
    recover.assert_not_called()
