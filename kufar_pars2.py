#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kufar_monitor.py
=================
Мониторинг новых объявлений аренды квартир (помесячно) на kufar.by
и отправка их в Telegram-бота: ссылка, телефон (если удастся получить),
фото, описание.

ВАЖНО ПРО НАДЁЖНОСТЬ
---------------------
Это НЕофициальный API kufar.by (обратная инженерия сетевых запросов сайта).
Такие эндпоинты могут:
  - менять формат ответа без предупреждения;
  - банить IP / ставить капчу при частых запросах;
  - скрывать номер телефона за отдельным защищённым запросом.
Скрипт написан максимально защитно (try/except, логирование сырого ответа),
но перед постоянным использованием обязательно прогоните DEBUG-режим
(см. ниже) и при необходимости поправьте имена полей под то, что реально
приходит в JSON на момент запуска — Kufar периодически меняет структуру.

УСТАНОВКА
---------
    pip install requests

НАСТРОЙКА
---------
1. Создайте бота через @BotFather в Telegram, получите TELEGRAM_BOT_TOKEN.
2. Узнайте свой chat_id (например, напишите боту @userinfobot, либо
   отправьте своему боту любое сообщение и откройте
   https://api.telegram.org/bot<ТОКЕН>/getUpdates — там будет chat.id).
3. Впишите токен и chat_id ниже (или задайте через переменные окружения
   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
4. При необходимости поменяйте SEARCH_PARAMS (регион, цена, комнаты).
5. Запустите:  python3 kufar_monitor.py
   Для проверки структуры ответа перед боевым запуском:
       python3 kufar_monitor.py --debug

Скрипт работает бесконечным циклом, опрашивая Kufar каждые POLL_INTERVAL
секунд, и присылает в Telegram только те объявления, которых раньше не
видел (id сохраняются в файле seen_ads.json рядом со скриптом).
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

# ============================== НАСТРОЙКИ ===================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8786490627:AAEZxCa2NgIpm8aNBpE1-RP1VVTHQoNuzm0")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7480929965")

# Официального публичного описания API нет — используется тот же эндпоинт,
# который дергает сайт re.kufar.by при поиске.
KUFAR_SEARCH_URL = "https://cre-api.kufar.by/ads-search/v1/engine/v1/search/rendered-paginated"

# Параметры поиска. cat=1010 — «Аренда квартир» (проверьте актуальность
# на сайте: откройте нужный раздел, DevTools → Network → найдите похожий
# запрос rendered-paginated и сверьте query-параметры).
SEARCH_PARAMS = {
    "size": "30",
    "sort": "lst.d",          # сортировка по дате публикации, свежие сверху
    "typ": "let",              # let = аренда (помесячная)
    "cat": "1010",              # категория "Квартиры" (аренда)
    "gtsy": "country-belarus~province-minsk~locality-minsk",  # только ГОРОД Минск
    "cur": "USD",
    "lang": "ru",
    # Примеры доп. фильтров, которые можно добавить при желании:
    # "pmn": "200",     # цена от
    # "pmx": "500",     # цена до
    # "rms": "2",       # количество комнат
}

# Доп. подстраховка на стороне скрипта: если Kufar вдруг вернёт объявление
# из пригорода/района, а не из самого города Минска, оно будет отброшено.
# Список не претендует на полноту — при необходимости дополните под себя.
MINSK_ONLY_STRICT_FILTER = True
SUBURB_MARKERS = (
    "минский р-н", "минский район", "боровляны", "ждановичи", "новый двор",
    "колодищи", "мачулищи", "сеница", "лесной", "щомыслица", "дражня",
    "цнянка", "озерцо", "малиновка-2",
)

# Куда сохраняем уже отправленные id объявлений, чтобы не дублировать.
# На Railway/аналогах примонтируйте volume и укажите его путь в переменной
# окружения SEEN_FILE_PATH (например: /data/seen_ads.json), иначе список
# "уже виденных" объявлений будет теряться при каждом передеплое.
SEEN_FILE = Path(os.environ.get("SEEN_FILE_PATH", str(Path(__file__).with_name("seen_ads.json"))))

# Интервал опроса, секунды. Не ставьте слишком маленьким — риск бана/капчи.
POLL_INTERVAL = 70

# Сколько фото максимум прикладывать к одному объявлению (Telegram
# ограничивает альбом 10 фотографиями)
MAX_PHOTOS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://re.kufar.by/",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kufar_monitor")


# ============================== ХРАНИЛИЩЕ ID =================================

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            log.warning("Не удалось прочитать %s, начинаю с пустого списка", SEEN_FILE)
    return set()


def save_seen(seen: set) -> None:
    # ограничиваем размер файла, чтобы не рос бесконечно
    trimmed = list(seen)[-5000:]
    SEEN_FILE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


# ============================== ЗАПРОС К KUFAR ================================

def fetch_listings() -> list:
    """Возвращает список объявлений (сырые dict'ы из ответа API)."""
    resp = requests.get(
        KUFAR_SEARCH_URL, params=SEARCH_PARAMS, headers=HEADERS, timeout=20
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("ads", [])


def dump_raw_sample():
    """Режим отладки: печатает «сырой» JSON первого объявления, чтобы можно
    было свериться с реальными именами полей и поправить парсер ниже."""
    ads = fetch_listings()
    if not ads:
        print("Объявления не найдены — проверьте SEARCH_PARAMS")
        return
    print(json.dumps(ads[0], ensure_ascii=False, indent=2))


# ============================== ПАРСИНГ ОБЪЯВЛЕНИЯ =============================

def _get_param_value(ad: dict, param_name: str):
    """Kufar хранит доп. параметры (адрес, комнаты, телефон и т.п.) в списке
    ad_parameters вида [{"p": "address", "v": "...", "vl": "..."}]."""
    for p in ad.get("ad_parameters", []):
        if p.get("p") == param_name:
            return p.get("vl") or p.get("v")
    return None


def extract_phone(ad: dict):
    """Пытается достать телефон прямо из объявления (для недвижимости он
    часто присутствует открыто, в отличие от других категорий).
    Если тут пусто — используйте get_phone_via_owner_api() как fallback."""
    for key in ("phone", "phones"):
        val = _get_param_value(ad, key)
        if val:
            return val
    # иногда телефон лежит в отдельном поле верхнего уровня
    if ad.get("phone"):
        return ad["phone"]
    return None


def get_phone_via_owner_api(account_id):
    """Fallback: инфо о владельце объявления. Не всегда содержит телефон
    в открытом виде — Kufar мог закрыть это через капчу/токен. Если метод
    не работает, просто оставляем ссылку на объявление — телефон будет
    виден при открытии в браузере/приложении."""
    if not account_id:
        return None
    try:
        url = f"https://www.kufar.by/item/api/aduserinfo/{account_id}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # структура тут нестабильна — проверьте вручную через --debug-owner
            return data.get("phone") or data.get("phones")
    except Exception as e:
        log.debug("get_phone_via_owner_api error: %s", e)
    return None


def _url_is_valid_image(url: str) -> bool:
    """Проверяет, что по ссылке реально отдаётся картинка, прежде чем
    отправлять её в Telegram. Без этой проверки Telegram может ответить
    ошибкой WEBPAGE_MEDIA_EMPTY, если ссылка битая/404, и завалить всю
    отправку альбома."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image"):
            return True
        # некоторые CDN не отдают корректный HEAD — пробуем GET
        r = requests.get(url, headers=HEADERS, timeout=8, stream=True)
        ok = r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image")
        r.close()
        return ok
    except Exception:
        return False


def extract_photos(ad: dict) -> list:
    candidates = []
    for img in ad.get("images", []):
        image_id = img.get("id") or img.get("path")
        if not image_id:
            continue
        str_id = str(image_id).split(".")[0]  # на случай если уже с расширением
        # ссылка на полноразмерное фото (см. reverse-engineering в kufar_api)
        url = f"https://yams.kufar.by/api/v1/kufar-ads/images/{str_id[:2]}/{str_id}.jpg?rule=gallery"
        candidates.append(url)
        if len(candidates) >= MAX_PHOTOS:
            break

    urls = [u for u in candidates if _url_is_valid_image(u)]
    return urls


def extract_price(ad: dict) -> str:
    price_byn = ad.get("price_byn")
    price_usd = ad.get("price_usd")
    if price_usd:
        try:
            return f"{int(price_usd) / 100:.0f} $"
        except (TypeError, ValueError):
            pass
    if price_byn:
        try:
            return f"{int(price_byn) / 100:.0f} BYN"
        except (TypeError, ValueError):
            pass
    return "цена не указана"


def is_in_minsk_city(ad: dict) -> bool:
    """Доп. проверка на стороне клиента (см. MINSK_ONLY_STRICT_FILTER).
    Смотрит на текстовый адрес/район объявления и отбрасывает явные
    пригороды Минского района. Это эвристика, а не гарантия — основной
    фильтр всё равно задаётся параметром gtsy в SEARCH_PARAMS."""
    if not MINSK_ONLY_STRICT_FILTER:
        return True
    text = " ".join(
        str(x).lower()
        for x in (
            _get_param_value(ad, "address"),
            ad.get("area"),
            ad.get("subject"),
        )
        if x
    )
    return not any(marker in text for marker in SUBURB_MARKERS)


def build_ad_summary(ad: dict) -> dict:
    ad_id = ad.get("ad_id") or ad.get("id")
    subject = ad.get("subject") or "Без названия"
    body = (ad.get("body") or "").strip()
    address = _get_param_value(ad, "address") or ad.get("area") or ""
    link = ad.get("ad_link") or f"https://re.kufar.by/vi/{ad_id}"
    phone = extract_phone(ad)
    account_id = ad.get("account_id") or ad.get("aid")
    if not phone:
        phone = get_phone_via_owner_api(account_id)
    photos = extract_photos(ad)
    price = extract_price(ad)

    return {
        "id": ad_id,
        "subject": subject,
        "body": body,
        "address": address,
        "link": link,
        "phone": phone,
        "photos": photos,
        "price": price,
    }


# ============================== ОТПРАВКА В TELEGRAM ============================

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def build_caption(item: dict, max_len: int = 3500) -> str:
    body_short = item["body"][:max_len]
    if len(item["body"]) > max_len:
        body_short += "…"
    phone_line = item["phone"] if item["phone"] else "не указан в объявлении (см. ссылку)"
    parts = [
        f"🏠 <b>{escape_html(item['subject'])}</b>",
        f"💰 {item['price']}",
    ]
    if item["address"]:
        parts.append(f"📍 {escape_html(item['address'])}")
    if body_short:
        parts.append("")
        parts.append(escape_html(body_short))
    parts.append("")
    parts.append(f"📞 {escape_html(str(phone_line))}")
    parts.append(f"🔗 {escape_html(item['link'])}")
    return "\n".join(parts)


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _check_telegram_response(resp, context: str) -> bool:
    """Логирует и возвращает False, если Telegram API вернул ошибку.
    Без этой проверки скрипт раньше тихо проглатывал ошибки вроде
    'chat not found', 'bot was blocked', слишком длинный caption и т.д."""
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if not resp.ok or not data.get("ok", True):
        log.error(
            "Telegram API вернул ошибку (%s), статус %s: %s",
            context, resp.status_code, data,
        )
        return False
    return True


def send_to_telegram(item: dict) -> None:
    # У фото caption ограничен 1024 символами, у обычного текстового
    # сообщения — 4096. Берём короткую версию для caption, полную — для
    # текстового сообщения без фото.
    caption_for_photo = build_caption(item, max_len=600)
    caption_full = build_caption(item, max_len=3500)
    photos = item["photos"]

    try:
        if photos:
            if len(photos) == 1:
                resp = requests.post(
                    f"{TELEGRAM_API}/sendPhoto",
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "caption": caption_for_photo,
                        "parse_mode": "HTML",
                        "photo": photos[0],
                    },
                    timeout=30,
                )
                _check_telegram_response(resp, "sendPhoto")
            else:
                media = [{"type": "photo", "media": url} for url in photos]
                media[0]["caption"] = caption_for_photo
                media[0]["parse_mode"] = "HTML"
                resp = requests.post(
                    f"{TELEGRAM_API}/sendMediaGroup",
                    json={"chat_id": TELEGRAM_CHAT_ID, "media": media},
                    timeout=30,
                )
                ok = _check_telegram_response(resp, "sendMediaGroup")
                if not ok:
                    # fallback: пробуем отправить хотя бы текстом, если
                    # проблема была именно в фото (битые ссылки на картинки)
                    resp2 = requests.post(
                        f"{TELEGRAM_API}/sendMessage",
                        data={
                            "chat_id": TELEGRAM_CHAT_ID,
                            "text": caption_full,
                            "parse_mode": "HTML",
                        },
                        timeout=30,
                    )
                    _check_telegram_response(resp2, "sendMessage fallback")
        else:
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": caption_full,
                    "parse_mode": "HTML",
                },
                timeout=30,
            )
            _check_telegram_response(resp, "sendMessage")
    except Exception as e:
        log.exception("Не удалось отправить объявление %s в Telegram: %s", item["id"], e)


# ==================================== MAIN =====================================

def check_config():
    if "ВАШ_ТОКЕН" in TELEGRAM_BOT_TOKEN or "ВАШ_CHAT_ID" in str(TELEGRAM_CHAT_ID):
        log.error(
            "Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (в начале файла "
            "или через переменные окружения) перед запуском."
        )
        sys.exit(1)


def run_once(seen: set) -> set:
    try:
        ads = fetch_listings()
    except Exception as e:
        log.error("Ошибка запроса к Kufar: %s", e)
        return seen

    new_ads = [
        a for a in ads
        if str(a.get("ad_id") or a.get("id")) not in seen and is_in_minsk_city(a)
    ]
    if not new_ads:
        log.info("Новых объявлений нет (всего в выдаче: %d)", len(ads))
        return seen

    log.info("Найдено новых объявлений: %d", len(new_ads))
    # переворачиваем, чтобы отправлять в хронологическом порядке (старые -> новые)
    for ad in reversed(new_ads):
        item = build_ad_summary(ad)
        send_to_telegram(item)
        seen.add(str(item["id"]))
        save_seen(seen)
        time.sleep(1.5)  # не спамим Telegram API

    return seen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug", action="store_true",
        help="Разово вывести сырой JSON первого объявления и выйти (для проверки полей)"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Выполнить один цикл проверки и выйти (без бесконечного polling)"
    )
    args = parser.parse_args()

    if args.debug:
        dump_raw_sample()
        return

    check_config()
    seen = load_seen()
    log.info("Старт мониторинга. Уже известно объявлений: %d", len(seen))

    if args.once:
        run_once(seen)
        return

    while True:
        seen = run_once(seen)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
