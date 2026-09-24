#!/usr/bin/env python3
"""Аварийный ручной запуск рекламы из карточки Trello.

Обычный путь (кнопка в вебе) ставит запуск в очередь контура одобрения, и
объявления создаёт фоновый исполнитель. Когда контур стоит — очередь не
разбирается, реклама не выходит, а владелец остаётся без рычага.

Этот скрипт — короткий путь на такой случай. Подготовка та же самая, что у
боевого пути (Trello → staging: медиа, тексты, адсеты по городам, имена
объявлений), но сами объявления создаются прямым вызовом Graph API, без
ожидания одобрения.

Что скрипт НЕ отключает — защиту от дублей: перед созданием читается живой
инвентарь кабинета, и пара (adset_id, имя объявления) второй раз не
создаётся. Это единственная защита, потеря которой стоит денег.

По умолчанию — dry-run: печатает план и выходит. Реальное создание требует
--apply вместе с --confirm-production LAUNCH-NOW.

Примеры:
    # посмотреть план (ничего не создаётся)
    ./venv/bin/python scripts/manual_launch.py --card "Креатор А / Тема"

    # создать объявления в двух городах
    ./venv/bin/python scripts/manual_launch.py --card "Креатор А / Тема" \\
        --cities CityA,CityB --apply --confirm-production LAUNCH-NOW
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.fb_common import API, _throttled_get  # noqa: E402
from integrations.facebook import (  # noqa: E402
    _manifest_creative_payloads,
    _wait_video_ready,
    extract_thumbnail,
)
from integrations.trello import get_card_drive_link, get_done_list_id, get_open_cards  # noqa: E402
from services.action_manifests import build_launch_manifest  # noqa: E402
from services.approval_checker_models import (  # noqa: E402
    ActionOrigin,
    LaunchSourceInput,
    MediaType,
)
from services.fb_token_provider import fb_account, get_fb_account_id, get_fb_token  # noqa: E402
from services.launch_staging import stage_launch  # noqa: E402

CONFIRM_PRODUCTION = "LAUNCH-NOW"
CAMPAIGN_TYPES = ("leadgen", "leadgen_prodb", "website", "mql_online", "prodb_online")
# Порог перехода на чанковый протокол. Простой multipart на больших файлах
# отдаёт голый 413 без тела ошибки — см. историю с видео >90 МБ.
CHUNKED_UPLOAD_THRESHOLD_BYTES = 50 * 1024 * 1024
# Этот путь идёт мимо троттлинга fb_common — держим свой ритм и ретраи,
# иначе выжатая квота обрывает запуск на середине списка.
_POST_MIN_INTERVAL_SECONDS = 2
_POST_MAX_ATTEMPTS = 3
_POST_RETRY_DELAYS = (10, 30)


class ManualLaunchError(RuntimeError):
    """Ошибка ручного запуска — печатается человеку, без стектрейса."""


# --- Trello: поиск карточки ---------------------------------------------


def find_card(query: str) -> dict:
    """Ищет карточку в списке «Готово» по id или части имени.

    Смотрит на ВСЕ открытые карточки колонки, включая уже отмеченные галочкой:
    после частичного запуска карточка отмечена (реклама живёт), а недостающие
    города доливают повторным прогоном — он должен её находить. Существующие
    объявления повторный прогон видит по именам и заново не создаёт.
    """
    cards = get_open_cards(get_done_list_id())
    if not cards:
        raise ManualLaunchError("В списке «Готово» нет карточек")

    exact = [card for card in cards if card.get("id") == query]
    if exact:
        return exact[0]

    needle = query.casefold().strip()
    matches = [card for card in cards if needle in str(card.get("name", "")).casefold()]
    if not matches:
        raise ManualLaunchError(
            f"Карточка «{query}» не найдена. Доступны, например:\n  - "
            + "\n  - ".join(str(card.get("name", "")) for card in cards[:10])
        )
    if len(matches) > 1:
        names = "\n  - ".join(str(card.get("name", "")) for card in matches)
        raise ManualLaunchError(
            f"Под «{query}» подходит несколько карточек — уточните запрос:\n  - {names}"
        )
    return matches[0]


# --- Facebook: прямой транспорт (в обход authorization-гейта) ------------


def _graph_post(url: str, data: dict, files: dict | None = None, timeout: int = 300) -> dict:
    """POST в Graph API: пауза, ретраи на временные отказы, внятная ошибка.

    Пауза перед каждым запросом — потому что этот путь идёт мимо общего
    троттлинга fb_common, а квота кабинета часто уже выжата аналитикой.
    Ретраятся только временные отказы (rate limit, 5xx): на постоянных
    ошибках повтор бесполезен и лишь жжёт квоту.
    """
    last_error = ""
    for attempt in range(_POST_MAX_ATTEMPTS):
        time.sleep(_POST_MIN_INTERVAL_SECONDS)
        try:
            response = requests.post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"обрыв связи: {type(exc).__name__}"
        else:
            if response.status_code == 200:
                return response.json()
            last_error = f"{response.status_code}: {response.text[:300]}"
            if not _is_transient(response):
                break
        if attempt < _POST_MAX_ATTEMPTS - 1:
            delay = _POST_RETRY_DELAYS[attempt]
            print(f"      ⏳ Facebook отбил ({last_error[:80]}) — повтор через {delay}с", flush=True)
            time.sleep(delay)
    raise ManualLaunchError(f"Facebook отказал ({last_error})")


def _is_transient(response: requests.Response) -> bool:
    """Временный отказ: лимит запросов или сбой на стороне FB."""
    if response.status_code >= 500:
        return True
    try:
        code = int((response.json().get("error") or {}).get("code", 0))
    except (ValueError, AttributeError, TypeError):
        return False
    # 4 — app rate limit, 17 — user rate limit, 80004 — ads api throttling.
    return code in {4, 17, 80004}


def _multipart_video_name(path: Path) -> str:
    """Имя видео для multipart-загрузки — с настоящим видео-расширением.

    Staging хранит медиа как <n>-<sha256>.bin, и simple-путь передавал это имя
    в multipart filename. Facebook отвергает такой файл по расширению — 352
    «unsupported format», хотя тот же файл чанковым протоколом принимается
    (там формат определяется по содержимому). Контейнер различаем по ftyp.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return path.stem + ".mp4"
    if head[4:8] == b"ftyp" and head[8:10] == b"qt":
        return path.stem + ".mov"
    return path.stem + ".mp4"


def _upload_video_simple(path: Path, account_id: str, token: str) -> str:
    name = _multipart_video_name(path)
    with open(path, "rb") as handle:
        payload = _graph_post(
            f"{API}/act_{account_id}/advideos",
            data={"access_token": token, "name": name},
            files={"source": (name, handle)},
        )
    video_id = str(payload.get("id") or "")
    if not video_id:
        raise ManualLaunchError("Facebook не вернул video_id")
    return video_id


def _upload_video_chunked(path: Path, account_id: str, token: str) -> str:
    """Resumable-протокол start/transfer/finish для больших файлов.

    video_id берётся из start-фазы: finish возвращает только success.
    """
    url = f"{API}/act_{account_id}/advideos"
    file_size = path.stat().st_size
    start = _graph_post(
        url,
        data={"access_token": token, "upload_phase": "start", "file_size": file_size},
    )
    session_id = str(start.get("upload_session_id") or "")
    video_id = str(start.get("video_id") or "")
    if not session_id or not video_id:
        raise ManualLaunchError("Facebook не открыл сессию загрузки видео")

    start_offset = int(start.get("start_offset", 0))
    end_offset = int(start.get("end_offset", 0))
    with open(path, "rb") as handle:
        while start_offset < end_offset:
            handle.seek(start_offset)
            chunk = handle.read(end_offset - start_offset)
            transferred = _graph_post(
                url,
                data={
                    "access_token": token,
                    "upload_phase": "transfer",
                    "upload_session_id": session_id,
                    "start_offset": start_offset,
                },
                files={"video_file_chunk": (path.name, chunk)},
            )
            next_start = int(transferred.get("start_offset", end_offset))
            if next_start == start_offset:
                raise ManualLaunchError("Загрузка видео не двигается — обрыв на FB")
            start_offset = next_start
            end_offset = int(transferred.get("end_offset", end_offset))
            print(f"      … {start_offset * 100 // max(file_size, 1)}%", flush=True)

    _graph_post(
        url,
        data={
            "access_token": token,
            "upload_phase": "finish",
            "upload_session_id": session_id,
        },
    )
    return video_id


def _upload_image(path: Path, account_id: str, token: str) -> str:
    with open(path, "rb") as handle:
        payload = _graph_post(
            f"{API}/act_{account_id}/adimages",
            data={"access_token": token},
            files={"filename": handle},
        )
    images = payload.get("images") or {}
    for entry in images.values():
        image_hash = str(entry.get("hash") or "")
        if image_hash:
            return image_hash
    raise ManualLaunchError("Facebook не вернул image_hash")


def upload_assets(manifest, directory: Path, account_id: str, token: str) -> dict:
    """Грузит медиа манифеста и отдаёт их в формате боевого пути."""
    provider_assets: dict[str, dict[str, str]] = {}
    for asset in sorted(manifest.media_assets, key=lambda item: item.order_index):
        path = directory / asset.staged_relative_path
        size_mb = asset.size_bytes / 1024 / 1024
        print(f"   ↑ {asset.media_type.value} {path.name} ({size_mb:.1f} МБ)", flush=True)
        if asset.media_type is MediaType.VIDEO:
            if asset.size_bytes > CHUNKED_UPLOAD_THRESHOLD_BYTES:
                video_id = _upload_video_chunked(path, account_id, token)
            else:
                video_id = _upload_video_simple(path, account_id, token)
            _wait_video_ready(video_id)
            thumbnail = extract_thumbnail(str(path))
            try:
                thumb_hash = _upload_image(Path(thumbnail), account_id, token)
            finally:
                if os.path.exists(thumbnail):
                    os.remove(thumbnail)
            provider_assets[asset.asset_id] = {
                "video_id": video_id,
                "thumb_hash": thumb_hash,
            }
        else:
            provider_assets[asset.asset_id] = {
                "image_hash": _upload_image(path, account_id, token)
            }
    return provider_assets


def create_ad(name: str, adset_id: str, creative: dict, account_id: str, token: str) -> str:
    """Создаёт одно объявление и возвращает ad_id."""
    payload = _graph_post(
        f"{API}/act_{account_id}/ads",
        data={
            "access_token": token,
            "name": name,
            "adset_id": adset_id,
            "creative": json.dumps(creative),
            "status": "ACTIVE",
            "url_tags": (
                "utm_source=facebook&utm_medium=cpc&"
                "utm_content={{ad.id}}&utm_campaign={{campaign.name}}"
            ),
        },
        timeout=120,
    )
    ad_id = str(payload.get("id") or "")
    if not ad_id:
        raise ManualLaunchError(f"Facebook не вернул ad_id для «{name}»")
    return ad_id


# --- Дубли и проверка результата -----------------------------------------


def live_ad_names(destinations: tuple, account_id: str) -> set[tuple[str, str]]:
    """Пары (adset_id, имя) занятых имён — основа защиты от дублей.

    Читаем адсеты поимённо, а не инвентарь кабинета: так дешевле по квоте и,
    главное, видны ВСЕ статусы. Общий get_all_ads отдаёт только ACTIVE/PAUSED,
    а свежесозданное объявление висит в PENDING_REVIEW/IN_PROCESS — по нему
    дубль как раз и проскочил бы при повторном запуске.
    """
    occupied: set[tuple[str, str]] = set()
    with fb_account(_account_context(account_id)):
        token = get_fb_token()
        for destination in destinations:
            after = None
            while True:
                params = {
                    "access_token": token,
                    "fields": "id,name,status,effective_status",
                    "limit": 100,
                }
                if after:
                    params["after"] = after
                response = _throttled_get(f"{API}/{destination.adset_id}/ads", params=params)
                if response.status_code != 200:
                    raise ManualLaunchError(
                        "Не удалось прочитать объявления адсета "
                        f"{destination.adset_id}: {response.text[:200]}"
                    )
                payload = response.json()
                for row in payload.get("data", []):
                    # Архивные слот не занимают и дублем не считаются.
                    if str(row.get("status")) in ("ARCHIVED", "DELETED"):
                        continue
                    occupied.add((destination.adset_id, str(row.get("name") or "")))
                after = (payload.get("paging", {}).get("cursors", {}) or {}).get("after")
                if not payload.get("paging", {}).get("next") or not after:
                    break
    return occupied


def _account_context(account_id: str) -> str | None:
    """Имя thread-контекста FB по account_id (None = дефолтный кабинет)."""
    from config import FB_ACCOUNT_ID, FB_ACCOUNT_ID_ONLINE

    normalized = str(account_id).removeprefix("act_")
    if normalized == str(FB_ACCOUNT_ID_ONLINE).removeprefix("act_"):
        return "online"
    if normalized == str(FB_ACCOUNT_ID).removeprefix("act_"):
        return None
    return f"offline:{normalized}"


def verify_created(ad_ids: list[str], token: str) -> list[dict]:
    """Перечитывает созданные объявления — «создано» только с подтверждением."""
    verified = []
    for ad_id in ad_ids:
        response = _throttled_get(
            f"{API}/{ad_id}",
            params={"access_token": token, "fields": "id,name,adset_id,status,effective_status"},
        )
        if response.status_code != 200:
            verified.append({"id": ad_id, "status": "НЕ ПОДТВЕРЖДЕНО"})
            continue
        verified.append(response.json())
    return verified


# --- План и печать --------------------------------------------------------


def build_plan(card: dict, campaign_type: str, cities: tuple[str, ...], as_carousel: bool):
    """Готовит staging и манифест — ровно как боевой путь до одобрения."""
    source = LaunchSourceInput(
        card_id=str(card["id"]),
        campaign_type=campaign_type,
        requested_cities=cities,
        as_carousel=as_carousel,
        origin_reference="manual-cli",
    )
    prepared = stage_launch(source, manifest_id=str(uuid.uuid4()))
    manifest = build_launch_manifest(
        prepared,
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        now=prepared.staged_at,
    )
    return prepared, manifest


def print_plan(destinations: tuple, existing: set[tuple[str, str]]) -> list[tuple]:
    """Печатает план кабинета и возвращает то, что реально надо создать.

    Учитывается ёмкость адсета: у Facebook потолок 50 объявлений на адсет,
    и попытка сверх него отбивается ошибкой про campaign_id. Лишнее в план
    не берём — иначе dry-run обещает больше, чем кабинет примет.
    """
    pending: list[tuple] = []
    duplicates = 0
    no_room = 0
    for destination in destinations:
        room = int(destination.capacity_available)
        print(f"  {destination.city} · adset {destination.adset_id} · свободно слотов: {room}")
        for creative in sorted(destination.creatives, key=lambda item: item.order_index):
            key = (destination.adset_id, creative.ad_name)
            if key in existing:
                duplicates += 1
                print(f"    ✗ {creative.ad_name} — УЖЕ ЕСТЬ в кабинете, пропуск")
                continue
            if room <= 0:
                no_room += 1
                print(f"    ✗ {creative.ad_name} — НЕТ СВОБОДНЫХ СЛОТОВ в адсете")
                continue
            room -= 1
            pending.append((destination, creative))
            print(f"    + {creative.ad_name}")

    tail = f", некуда положить {no_room}" if no_room else ""
    print(f"  → создать {len(pending)}, пропустить как дубли {duplicates}{tail}")
    if no_room:
        print(
            "    ⚠ адсет упёрся в лимит Facebook (50 объявлений). "
            "Освободите слоты архивацией старых — «Чистка» в интерфейсе."
        )
    return pending


def group_by_account(manifest) -> dict[str, tuple]:
    """Группирует города по кабинетам.

    Города маршрутизируются в разные кабинеты (например, CityF — в
    отдельный), а загрузка медиа и создание всегда идут внутри одного
    кабинета. Поэтому запуск исполняется по кабинетам последовательно.
    """
    grouped: dict[str, list] = {}
    for destination in manifest.destinations:
        grouped.setdefault(destination.account_id.removeprefix("act_"), []).append(destination)
    return {account: tuple(items) for account, items in sorted(grouped.items())}


# --- CLI ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ручной запуск рекламы из карточки Trello, минуя очередь одобрения",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--card", required=True, help="ID карточки или часть её имени")
    parser.add_argument(
        "--cities",
        default="",
        help="Города через запятую (пусто = все доступные)",
    )
    parser.add_argument("--type", dest="campaign_type", default="leadgen", choices=CAMPAIGN_TYPES)
    parser.add_argument("--as-carousel", action="store_true", help="Собрать картинки в карусель")
    parser.add_argument("--apply", action="store_true", help="Создать объявления по-настоящему")
    parser.add_argument("--confirm-production", default="", help=f"Требуется: {CONFIRM_PRODUCTION}")
    parser.add_argument("--keep-staging", action="store_true", help="Не удалять скачанные медиа")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cities = tuple(city.strip() for city in args.cities.split(",") if city.strip())

    card = find_card(args.card)
    drive_url = get_card_drive_link(str(card["id"]))
    if not drive_url:
        raise ManualLaunchError("В карточке нет ссылки на Google Drive — нечего запускать")

    print("Готовлю план: качаю медиа и раскладываю по адсетам…", flush=True)
    prepared, manifest = build_plan(card, args.campaign_type, cities, args.as_carousel)
    staging_directory = Path(manifest.staging_directory)

    try:
        grouped = group_by_account(manifest)
        print(f"\nКарточка: {card.get('name')}")
        print(f"Тип кампании: {manifest.campaign_type}")
        print(f"Медиа: {len(manifest.media_assets)} файл(ов)")

        plan: list[tuple[str, list]] = []
        for account_id, destinations in grouped.items():
            print(f"\nКабинет act_{account_id}:")
            existing = live_ad_names(destinations, account_id)
            plan.append((account_id, print_plan(destinations, existing)))

        total = sum(len(pending) for _, pending in plan)
        if not total:
            print("\nСоздавать нечего — всё уже в кабинете.")
            return 0
        if not args.apply:
            print(
                f"\nDry-run: ничего не создано (создалось бы {total}). Для запуска добавьте:\n"
                f"  --apply --confirm-production {CONFIRM_PRODUCTION}"
            )
            return 0
        if args.confirm_production != CONFIRM_PRODUCTION:
            print(f"\nОтказ: для --apply нужен --confirm-production {CONFIRM_PRODUCTION}")
            return 2

        created: list[str] = []
        failures: list[str] = []
        for account_id, pending in plan:
            if not pending:
                continue
            created_here, failed_here = launch_in_account(
                manifest, pending, account_id, staging_directory
            )
            created.extend(created_here)
            failures.extend(failed_here)

        print(f"\nИтог: создано {len(created)}, не удалось {len(failures)}")
        for failure in failures:
            print(f"  ✗ {failure}")
        if created:
            if failures:
                print(
                    "Часть объявлений не создана, но живые уже есть — "
                    "карточку отмечаю запущенной."
                )
            _mark_card_launched(card)
        return 1 if failures else 0
    finally:
        if not args.keep_staging and staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)


def _mark_card_launched(card: dict) -> None:
    """Ставит карточке зелёный чекбокс — как боевой путь после запуска.

    Без отметки карточка остаётся «незапущенной» для get_unlaunched_cards,
    и список «Готово» продолжает предлагать её к запуску. Отказ Trello
    запуск не отменяет — объявления уже созданы, поэтому только
    предупреждение: отметить можно руками.

    Отмечаем при ЛЮБОМ числе созданных объявлений, а не только при нуле
    отказов: галочка читается людьми как «реклама живёт», и частичный
    запуск (город без слотов) её не отменяет. Недостающие города доливают
    повторным прогоном — он видит существующие имена и создаёт только их.
    """
    from integrations.trello import mark_card_done, redact_trello_secrets

    if card.get("dueComplete") is True:
        print("Карточка в Trello уже отмечена запущенной — долив.")
        return
    try:
        mark_card_done(str(card["id"]))
        print("Карточка в Trello отмечена запущенной (dueComplete).")
    except Exception as exc:  # noqa: BLE001 — отметка вторична к созданию
        # mark_card_done ходит мимо safe_request: сырое исключение requests несёт
        # URL с key/token — в stdout Run-скрипта только редактированный текст.
        print(f"⚠ Не удалось отметить карточку в Trello: {redact_trello_secrets(exc)}")


def launch_in_account(
    manifest, pending: list[tuple], account_id: str, staging_directory: Path
) -> tuple[list[str], list[str]]:
    """Грузит медиа и создаёт объявления внутри одного кабинета.

    Медиа привязаны к кабинету, поэтому для каждого кабинета загрузка своя.
    """
    created: list[str] = []
    failures: list[str] = []
    with fb_account(_account_context(account_id)):
        if str(get_fb_account_id()).removeprefix("act_") != account_id:
            raise ManualLaunchError("Активный кабинет FB не совпал с планом — остановка")
        token = get_fb_token()

        print(f"\n[act_{account_id}] Загружаю медиа в Facebook…", flush=True)
        provider_assets = upload_assets(manifest, staging_directory, account_id, token)
        payloads = _manifest_creative_payloads(manifest, staging_directory, provider_assets)

        print(f"[act_{account_id}] Создаю объявления…", flush=True)
        for destination, creative in pending:
            key = (destination.adset_id, creative.ad_name)
            try:
                ad_id = create_ad(
                    creative.ad_name, destination.adset_id, payloads[key], account_id, token
                )
            except ManualLaunchError as exc:
                failures.append(f"{creative.ad_name}: {exc}")
                print(f"   ✗ {creative.ad_name} — {exc}", flush=True)
                continue
            created.append(ad_id)
            print(f"   ✓ {creative.ad_name} → {ad_id}", flush=True)

        print(f"[act_{account_id}] Проверяю созданное в кабинете…", flush=True)
        for row in verify_created(created, token):
            print(
                f"   {row.get('id')} · {row.get('status', '?')}"
                f" · {row.get('effective_status', '?')} · {row.get('name', '')}"
            )
    return created, failures


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ManualLaunchError as error:
        print(f"\nОшибка: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        sys.exit(130)
