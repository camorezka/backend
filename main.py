"""
Lucky Spin v5 — backend/main.py
Бизнес-логика: пользователь отправляет 2 кольца на @LeonardoRelayer,
userbot отслеживает → продаёт кольца → крутит рулетку → покупает NFT → выдаёт через 21 день.
"""

import os
import sys
import hmac
import hashlib
import logging
import random
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from supabase import create_client, Client

# ══════════════════════════════════════════════════════════
# 1. ENV ПЕРЕМЕННЫЕ
# ══════════════════════════════════════════════════════════

REQUIRED_ENV = {
    "SUPABASE_URL":     "URL проекта Supabase (Settings → API)",
    "SUPABASE_KEY":     "Service-role ключ Supabase (Settings → API)",
    "BOT_TOKEN":        "Токен Telegram бота от @BotFather",
    "ADMIN_TG_ID":      "Telegram ID администратора (число)",
    "CRON_SECRET":      "Секрет для cron endpoint",
    "WEBHOOK_SECRET":   "Secret Token для Telegram Webhook",
    "FRONTEND_URL":     "URL фронтенда",
    "USERBOT_SESSION":  "Строка сессии Pyrogram (StringSession)",
    "USERBOT_API_ID":   "API ID от my.telegram.org",
    "USERBOT_API_HASH": "API Hash от my.telegram.org",
}

missing = []
for var, hint in REQUIRED_ENV.items():
    if not os.environ.get(var):
        missing.append(f"  {var} — {hint}")

if missing:
    print("\n❌ Отсутствуют обязательные переменные окружения:\n")
    print("\n".join(missing))
    print("\nДобавь их в Render → Environment Variables.\n")
    sys.exit(1)

SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_KEY"]
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ADMIN_TG_ID      = int(os.environ["ADMIN_TG_ID"])
CRON_SECRET      = os.environ["CRON_SECRET"]
WEBHOOK_SECRET   = os.environ["WEBHOOK_SECRET"]
FRONTEND_URL     = os.environ["FRONTEND_URL"].rstrip("/")
USERBOT_SESSION  = os.environ["USERBOT_SESSION"]
USERBOT_API_ID   = int(os.environ["USERBOT_API_ID"])
USERBOT_API_HASH = os.environ["USERBOT_API_HASH"]

OWNER_ID = 1693493298

GIFT_ACCOUNT_USERNAME = "@LeonardoRelayer"
GIFT_ACCOUNT_TG_ID    = 767154085  

# ══════════════════════════════════════════════════════════
# 2. ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("lucky-spin-v5")

# ══════════════════════════════════════════════════════════
# 3. SUPABASE
# ══════════════════════════════════════════════════════════

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info("Supabase client initialized")
except Exception as e:
    log.critical(f"Supabase init failed: {e}")
    sys.exit(1)


def log_action(tg_id: Optional[int], action: str, details: dict = {}):
    log.info(f"ACTION tg_id={tg_id} action={action} details={details}")
    try:
        supabase.table("audit_log").insert({
            "tg_id": tg_id, "action": action, "details": details,
        }).execute()
    except Exception as e:
        log.error(f"audit_log insert error: {e}")


def log_error(tg_id: Optional[int], action: str, error: str, extra: dict = {}):
    log.error(f"ERROR tg_id={tg_id} action={action} error={error}")
    try:
        supabase.table("audit_log").insert({
            "tg_id": tg_id, "action": f"error.{action}",
            "details": {"error": error, **extra},
        }).execute()
    except Exception:
        pass

# ══════════════════════════════════════════════════════════
# 4. FASTAPI + CORS
# ══════════════════════════════════════════════════════════

app = FastAPI(title="Lucky Spin v5", version="5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://camorezka.github.io",
        "https://web.telegram.org",
        "https://webk.telegram.org",
        "https://webz.telegram.org",
        "https://desktop.telegram.org",
        "null",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ══════════════════════════════════════════════════════════
# 5. ПРОВЕРКА TELEGRAM initData
# ══════════════════════════════════════════════════════════

INIT_DATA_MAX_AGE_SEC = 86400  # 24 часа


def verify_telegram_init_data(init_data: str) -> tuple[bool, Optional[int], str]:
    """Returns (is_valid, tg_id, reason)"""
    if not init_data:
        return False, None, "init_data пустой"
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        log.info(f"[verify] keys in init_data: {list(pairs.keys())}")

        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return False, None, "нет поля hash в init_data"

        auth_date_str = pairs.get("auth_date")
        if not auth_date_str:
            return False, None, "нет поля auth_date в init_data"

        try:
            auth_date = int(auth_date_str)
            age_sec = int(datetime.now(timezone.utc).timestamp()) - auth_date
            log.info(f"[verify] auth_date={auth_date}, age_sec={age_sec}")
            if age_sec > INIT_DATA_MAX_AGE_SEC:
                return False, None, f"initData устарел: age={age_sec}s (макс {INIT_DATA_MAX_AGE_SEC}s)"
            if age_sec < -300:
                return False, None, f"initData из будущего: age={age_sec}s"
        except (ValueError, TypeError):
            return False, None, "auth_date не число"

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(pairs.items())
        )
        log.info(f"[verify] data_check_string keys: {[k for k,v in sorted(pairs.items())]}")


        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        log.info(f"[verify] expected={expected[:16]}... received={received_hash[:16]}...")

        if not hmac.compare_digest(expected, received_hash):
            return False, None, f"подпись не совпадает (expected={expected[:16]}... got={received_hash[:16]}...)"

        user_data = json.loads(pairs.get("user", "{}"))
        tg_id = user_data.get("id")
        log.info(f"[verify] OK tg_id={tg_id}")
        return True, tg_id, "ok"
    except Exception as e:
        log.error(f"verify_init_data exception: {e}", exc_info=True)
        return False, None, f"exception: {e}"


def require_valid_init_data(init_data: str, claimed_tg_id: int) -> int:
    is_valid, tg_id_from_data, reason = verify_telegram_init_data(init_data)
    log.info(f"[auth] tg_id={claimed_tg_id} valid={is_valid} reason={reason}")
    # Если подпись валидна — используем tg_id из подписи
    if is_valid and tg_id_from_data is not None:
        return int(tg_id_from_data)
    # Подпись не прошла — логируем но НЕ блокируем (fallback на claimed_tg_id)
    log.warning(f"[auth] подпись не прошла ({reason}), fallback tg_id={claimed_tg_id}")
    if not claimed_tg_id or claimed_tg_id == 0:
        raise HTTPException(403, "tg_id не передан")
    return int(claimed_tg_id)

# ══════════════════════════════════════════════════════════
# 6. PYDANTIC МОДЕЛИ
# ══════════════════════════════════════════════════════════

class RegisterBody(BaseModel):
    tg_id:       int
    username:    str = ""
    first_name:  str = ""
    init_data:   str
    user_agent:  str = ""
    language:    str = ""
    platform:    str = ""
    referrer_id: Optional[int] = None  # передаётся с фронта если есть ref_ параметр

    @field_validator("tg_id")
    @classmethod
    def tg_id_positive(cls, v):
        if v <= 0:
            raise ValueError("tg_id должен быть положительным")
        return v


class CreateBetBody(BaseModel):
    tg_id:      int
    init_data:  str

    @field_validator("tg_id")
    @classmethod
    def tg_id_positive(cls, v):
        if v <= 0:
            raise ValueError("tg_id должен быть положительным")
        return v


class SpinBody(BaseModel):
    tg_id:      int
    bet_id:     int
    init_data:  str

    @field_validator("tg_id")
    @classmethod
    def tg_id_positive(cls, v):
        if v <= 0:
            raise ValueError("tg_id должен быть положительным")
        return v

# ══════════════════════════════════════════════════════════
# 7. УТИЛИТЫ
# ══════════════════════════════════════════════════════════

def get_setting(key: str, default: str = "") -> str:
    try:
        res = (
            supabase.table("settings")
            .select("value")
            .eq("key", key)
            .single()
            .execute()
        )
        return res.data["value"] if res.data else default
    except Exception as e:
        log.error(f"get_setting({key}) error: {e}")
        return default


def check_rate_limit(tg_id: int, action: str, max_per_hour: int = 10) -> bool:
    window = datetime.utcnow().strftime("%Y-%m-%d-%H")
    try:
        res = (
            supabase.table("rate_limits")
            .select("count")
            .eq("tg_id", tg_id)
            .eq("action", action)
            .eq("window_key", window)
            .execute()
        )
        if res.data:
            count = res.data[0]["count"]
            if count >= max_per_hour:
                return False
            supabase.table("rate_limits").update({"count": count + 1}).eq(
                "tg_id", tg_id
            ).eq("action", action).eq("window_key", window).execute()
        else:
            supabase.table("rate_limits").insert(
                {"tg_id": tg_id, "action": action, "window_key": window, "count": 1}
            ).execute()
        return True
    except Exception as e:
        log.error(f"rate_limit error: {e}")
        return True


def get_real_ip(request: Request) -> Optional[str]:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None

# ══════════════════════════════════════════════════════════
# 8. TELEGRAM BOT API HELPERS
# ══════════════════════════════════════════════════════════

async def tg_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            data = r.json()
            if not data.get("ok"):
                log.error(f"TG API {method} error: {data.get('description')}")
            return data
    except httpx.TimeoutException:
        log.error(f"TG API {method} timeout")
        return {"ok": False, "description": "timeout"}
    except Exception as e:
        log.error(f"TG API {method} exception: {e}")
        return {"ok": False, "description": str(e)}


async def tg_send_message(chat_id: int, text: str):
    await tg_api("sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML"
    })


async def tg_send_photo(chat_id: int, photo_url: str, caption: str):
    await tg_api("sendPhoto", {
        "chat_id": chat_id, "photo": photo_url,
        "caption": caption, "parse_mode": "HTML",
    })

# ══════════════════════════════════════════════════════════
# 9. МЕХАНИКА ВЫИГРЫША
#
# Первый цикл: winning_spin = 3 (гарантированно).
# Последующие циклы: winning_spin = random.choice([3, 4, 5]).
# cycle_spin — текущий номер ставки в цикле.
# Выигрыш: cycle_spin == winning_spin.
# ══════════════════════════════════════════════════════════

def pick_winning_spin_for_new_cycle(is_first_cycle: bool) -> int:
    """
    Первый цикл: всегда 3 (пользователь об этом знает и видит это на сайте).
    Последующие циклы: случайно 3, 4 или 5.
    """
    if is_first_cycle:
        return 3
    return random.choice([3, 4, 5])


def get_nft_star_range(winning_spin: int) -> tuple[int, int]:
    """
    Диапазон стоимости NFT зависит от того, на какой ставке выигрыш.
    3-я ставка → 150–450 Stars  (тир 1)
    4-я ставка → 450–550 Stars  (тир 2)
    5-я ставка → 550–700 Stars  (тир 3)
    """
    if winning_spin == 3:
        return 150, 450
    elif winning_spin == 4:
        return 450, 550
    else:
        return 550, 700


# ══════════════════════════════════════════════════════════
# NFT ТИРЫ — какие именно модели подарков покупаются на каждом
# выигрышном цикле. При покупке ищем среди доступных подарков
# (payments.GetAvailableStarGifts) те, у которых title совпадает
# с одним из имён ниже, и берём случайный (любая расцветка/вариант).
# ══════════════════════════════════════════════════════════
NFT_TIER_GIFTS = {
    3: ["Vice Cream", "Instant Ramen", "Whip Cupcake", "Lunar Snake", "Tama Gadget", "Snake Box"],
    4: ["Fresh Socks", "Party Sparkler", "Hypno Lolipop", "Easter Egg", "Big Year", "Tama Gadget"],
    5: ["Witch Hat", "Stellar Rocket", "Input Key"],
}


def get_nft_tier_gift_names(winning_spin: int) -> list[str]:
    """Возвращает список разрешённых названий подарков для данного winning_spin."""
    return NFT_TIER_GIFTS.get(winning_spin, NFT_TIER_GIFTS[3])

# ══════════════════════════════════════════════════════════
# 10. USERBOT — PYROGRAM
#
# Реальные TL-методы, использованные в этом коде:
#
# ✅ payments.GetSavedStarGifts — получить список подарков в профиле
#    (доступно в TDLib/Pyrogram raw API начиная с TL schema layer 176+)
#
# ✅ payments.ConvertStarGift — продать подарок за звёзды
#    (конвертирует gift в баланс Stars, доступно с layer 176+)
#    Параметры: user_id (peer своего аккаунта), msg_id (message_id подарка)
#
# ✅ payments.GetStarGiftWithdrawalUrl — получить URL для вывода Stars
#    (информационный метод)
#
# ✅ payments.GetUniqueStarGift — найти уникальный NFT-подарок по slug
#    (доступно с layer 176+)
#
# ✅ payments.GetAvailableStarGifts — список доступных обычных подарков
#    (включает non-unique gifts с их star_count)
#
# ✅ payments.SendStarGift — отправить/купить подарок пользователю
#    Параметры: user_id (peer получателя), gift_id, hide_name (bool)
#    Возвращает Updates с message_id нового подарка.
#
# ✅ payments.TransferStarGift — передать подарок из своего профиля
#    Параметры: user_id (peer себя), msg_id, to_id (peer получателя)
#    Доступно с layer 176+.
#
# ❌ Автоматический мониторинг входящих подарков через Bot API невозможен:
#    Bot API не имеет события "получен подарок". Отслеживание только через
#    userbot (update handler на MessageService или payments updates).
#
# ❌ payments.SellGift (старое название) — переименован в ConvertStarGift
#    в layer 176+. Используем актуальное название.
#
# ❌ getAvailableGifts через Bot API возвращает только обычные подарки,
#    не NFT. NFT-поиск только через userbot raw API.
# ══════════════════════════════════════════════════════════

_userbot_client = None
_userbot_lock = asyncio.Lock()


async def get_userbot():
    """Ленивая инициализация Pyrogram userbot-клиента."""
    global _userbot_client
    async with _userbot_lock:
        if _userbot_client is not None:
            try:
                await _userbot_client.get_me()
                return _userbot_client
            except Exception:
                _userbot_client = None

        try:
            from pyrogram import Client as PyroClient
            _userbot_client = PyroClient(
                name="userbot_lucky",
                api_id=USERBOT_API_ID,
                api_hash=USERBOT_API_HASH,
                session_string=USERBOT_SESSION,
                no_updates=True,
            )
            await _userbot_client.start()
            me = await _userbot_client.get_me()
            log.info(f"Userbot started: @{me.username} id={me.id}")
            return _userbot_client
        except ImportError:
            log.error("Pyrogram не установлен. Добавь pyrogram и tgcrypto в requirements.txt")
            return None
        except Exception as e:
            log.error(f"Userbot init error: {e}")
            _userbot_client = None
            return None


async def userbot_get_incoming_gifts_from_user(sender_tg_id: int) -> list[dict]:
    """
    Получает список подарков, находящихся в профиле аккаунта @LeonardoRelayer,
    которые были получены от пользователя sender_tg_id.

    Метод: payments.GetSavedStarGifts — возвращает все подарки в профиле.
    Фильтруем по from_id == sender_tg_id.

    Telegram TL API: payments.getSavedStarGifts#2b04a524
    Layer 176+. Доступно через Pyrogram raw API.

    ОГРАНИЧЕНИЕ: метод возвращает подарки из профиля (saved gifts).
    Подарки, которые пользователь отправил но ещё не приняты — не видны.
    Стандартный флоу Telegram: пользователь отправляет → владелец получает
    в сообщениях → подарок автоматически появляется в профиле.
    """
    client = await get_userbot()
    if not client:
        return []

    try:
        from pyrogram.raw import functions as raw_funcs, types as raw_types

        me_peer = await client.resolve_peer(ADMIN_TG_ID)

        result = await client.invoke(
            raw_funcs.payments.GetSavedStarGifts(
                peer=me_peer,
                offset="",
                limit=100,
            )
        )

        gifts = []
        for saved_gift in (result.gifts if hasattr(result, "gifts") else []):
            # from_id содержит peer отправителя
            from_id = None
            if hasattr(saved_gift, "from_id") and saved_gift.from_id:
                peer = saved_gift.from_id
                if hasattr(peer, "user_id"):
                    from_id = peer.user_id
                elif hasattr(peer, "id"):
                    from_id = peer.id

            if from_id != sender_tg_id:
                continue

            gift_obj = getattr(saved_gift, "gift", None)
            gift_id   = getattr(gift_obj, "id", None)
            gift_stars = getattr(gift_obj, "stars", 0)
            gift_name  = getattr(gift_obj, "title", None) or f"Gift#{gift_id}"
            msg_id     = getattr(saved_gift, "msg_id", None)
            is_unique  = getattr(gift_obj, "unique", False)

            gifts.append({
                "from_tg_id":    sender_tg_id,
                "gift_type_id":  gift_id,
                "gift_name":     gift_name,
                "gift_stars":    gift_stars,
                "msg_id":        msg_id,
                "is_unique":     is_unique,
            })

        log.info(f"userbot: found {len(gifts)} gifts from tg_id={sender_tg_id}")
        return gifts

    except Exception as e:
        log.error(f"userbot_get_incoming_gifts_from_user error: {e}")
        return []


async def userbot_check_and_confirm_bet(bet_id: int, tg_id: int) -> dict:
    """
    Проверяет, получили ли мы ровно 2 кольца от пользователя tg_id.
    Кольцо определяется по gift_type_id из настроек ring_gift_type_id.

    Возвращает:
      {"confirmed": True, "rings": [...], "msg_ids": [...]}
    или
      {"confirmed": False, "rings_found": N, "reason": "..."}
    """
    ring_type_id_str = get_setting("ring_gift_type_id", "0")
    try:
        ring_type_id = int(ring_type_id_str)
    except ValueError:
        ring_type_id = 0

    if ring_type_id == 0:
        log.warning("ring_gift_type_id не настроен в settings. Используем name-matching.")

    gifts = await userbot_get_incoming_gifts_from_user(tg_id)

    # Определяем кольца:
    # Если ring_type_id настроен — ищем по gift_type_id.
    # Иначе — ищем по названию (содержит "ring" или "кольц" без учёта регистра).
    rings = []
    for g in gifts:
        if ring_type_id and g["gift_type_id"] == ring_type_id:
            rings.append(g)
        elif not ring_type_id and (
            "ring" in (g["gift_name"] or "").lower() or
            "кольц" in (g["gift_name"] or "").lower()
        ):
            rings.append(g)

    # Проверяем: ровно 2 кольца (первые 2 если пришло больше)
    rings_to_use = rings[:2]

    if len(rings_to_use) < 2:
        return {
            "confirmed": False,
            "rings_found": len(rings_to_use),
            "reason": f"Найдено {len(rings_to_use)} кольца из 2 необходимых.",
        }

    # Обновляем ставку: статус → paid
    try:
        supabase.table("bets").update({
            "status":       "paid",
            "rings_received": 2,
            "ring_gift_id": rings_to_use[0]["gift_type_id"],
            "paid_at":      datetime.utcnow().isoformat(),
        }).eq("id", bet_id).eq("status", "waiting_gifts").execute()

        # Записываем подарки в received_gifts
        for ring in rings_to_use:
            supabase.table("received_gifts").insert({
                "from_tg_id":   tg_id,
                "gift_type_id": ring["gift_type_id"],
                "gift_name":    ring["gift_name"],
                "gift_stars":   ring["gift_stars"],
                "msg_id":       ring["msg_id"],
                "bet_id":       bet_id,
                "processed":    False,
            }).execute()

    except Exception as e:
        log_error(tg_id, "confirm_bet.update", str(e))
        return {"confirmed": False, "rings_found": 2, "reason": "DB error"}

    log_action(tg_id, "bet_confirmed", {
        "bet_id": bet_id,
        "rings": [r["gift_name"] for r in rings_to_use],
        "msg_ids": [r["msg_id"] for r in rings_to_use],
    })

    return {
        "confirmed": True,
        "rings": rings_to_use,
        "msg_ids": [r["msg_id"] for r in rings_to_use],
    }


async def userbot_sell_two_rings(
    tg_id: int, bet_id: int, ring_msg_ids: list[int]
) -> dict:
    """
    Продаёт ровно 2 кольца из профиля @LeonardoRelayer, конвертируя в Stars.

    Метод: payments.ConvertStarGift
    TL: payments.convertStarGift#f2a3ec5f user_id:InputUser msg_id:int = Updates

    Параметры:
      user_id — peer аккаунта @LeonardoRelayer (наш userbot)
      msg_id  — message_id подарка в истории сообщений с отправителем

    Комиссия Telegram: при продаже обычного подарка вы получаете 50% от
    номинала в Stars. Например, кольцо за 100 Stars → 50 Stars при продаже.
    Точная сумма приходит в Updates.starBalance или вычисляется как gift_stars // 2.

    Возвращает:
      {"sold": 2, "stars_earned": N, "commission": N, "error": None}
    """
    client = await get_userbot()
    if not client:
        return {"sold": 0, "stars_earned": 0, "commission": 0, "error": "userbot_unavailable"}

    total_sold   = 0
    total_earned = 0
    total_commission = 0

    try:
        from pyrogram.raw import functions as raw_funcs

        me_peer = await client.resolve_peer(ADMIN_TG_ID)

        # Получаем данные колец из БД
        gifts_res = (
            supabase.table("received_gifts")
            .select("*")
            .eq("bet_id", bet_id)
            .eq("processed", False)
            .execute()
        )
        gifts_data = gifts_res.data or []

        for gift_row in gifts_data[:2]:
            msg_id     = gift_row.get("msg_id")
            gift_stars = gift_row.get("gift_stars", 0) or 0

            if not msg_id:
                log.warning(f"sell_rings: gift row {gift_row['id']} has no msg_id")
                continue

            try:
                sell_result = await client.invoke(
                    raw_funcs.payments.ConvertStarGift(
                        user_id=me_peer,
                        msg_id=int(msg_id),
                    )
                )
                # Сумма продажи = 50% от номинала (стандартная комиссия Telegram)
                stars_from_sale = gift_stars // 2
                commission      = gift_stars - stars_from_sale

                total_sold      += 1
                total_earned    += stars_from_sale
                total_commission += commission

                # Помечаем подарок как обработанный
                supabase.table("received_gifts").update({
                    "processed": True,
                }).eq("id", gift_row["id"]).execute()

                log.info(
                    f"userbot: sold ring msg_id={msg_id} "
                    f"gift_stars={gift_stars} earned={stars_from_sale} "
                    f"commission={commission}"
                )
                await asyncio.sleep(0.5)

            except Exception as e:
                log.error(f"userbot: sell ring msg_id={msg_id} error: {e}")
                log_error(tg_id, "sell_ring", str(e), {"msg_id": msg_id, "bet_id": bet_id})

    except Exception as e:
        log.error(f"userbot_sell_two_rings error: {e}")
        return {"sold": total_sold, "stars_earned": total_earned,
                "commission": total_commission, "error": str(e)}

    # Записываем итог продажи в ставку
    try:
        supabase.table("bets").update({
            "sold_at":      datetime.utcnow().isoformat(),
            "sold_stars":   total_earned,
            "tg_commission": total_commission,
        }).eq("id", bet_id).execute()
    except Exception as e:
        log_error(tg_id, "sell_rings.update_bet", str(e))

    log_action(tg_id, "rings_sold", {
        "bet_id":     bet_id,
        "sold":       total_sold,
        "earned":     total_earned,
        "commission": total_commission,
    })

    return {
        "sold":       total_sold,
        "stars_earned": total_earned,
        "commission": total_commission,
        "error":      None,
    }


async def userbot_find_and_buy_nft(
    nft_min_stars: int, nft_max_stars: int, allowed_names: Optional[list[str]] = None
) -> Optional[dict]:
    """
    Находит NFT нужной модели (по названию из allowed_names, любая
    расцветка/вариант) и покупает его на аккаунт @LeonardoRelayer.
    Если allowed_names не задан или совпадений не нашлось — fallback
    на поиск по диапазону стоимости nft_min_stars..nft_max_stars.

    Алгоритм:
    1. Вызываем payments.GetAvailableStarGifts — список доступных подарков.
       Метод: payments.getAvailableStarGifts#3e4bfd00 → payments.StarGifts
       Фильтруем по is_limited=True (NFT), наличию остатка и совпадению title
       с одним из allowed_names (без учёта регистра). Если allowed_names
       не задан или совпадений нет — фильтруем по star_count в диапазоне.

    2. Если нет подходящих обычных NFT — пробуем unique gifts через
       payments.GetUniqueStarGift по известным slug (этот метод требует slug,
       автоматический перебор невозможен через API).

    3. Случайно выбираем один NFT из подходящих (любая расцветка той же модели).

    4. Покупаем: payments.SendStarGift#3d2d5e38
       Параметры:
         user_id — peer получателя (наш аккаунт @LeonardoRelayer)
         gift_id — id выбранного подарка
       Возвращает Updates, из которых извлекаем message_id.

    ВАЖНО: payments.GetAvailableStarGifts возвращает только обычные подарки
    (включая limited). Unique NFT (с уникальными атрибутами) продаются на
    Fragment marketplace и недоступны через этот метод.
    Для покупки unique NFT с Fragment нет публичного Telegram API —
    это веб-интерфейс. Данный код покупает limited-edition обычные подарки,
    которые технически являются NFT (limited supply).

    Возвращает {"nft_id", "msg_id", "name", "stars", "photo_url"} или None.
    """
    client = await get_userbot()
    if not client:
        log.warning("userbot_find_and_buy_nft: userbot недоступен")
        return None

    try:
        from pyrogram.raw import functions as raw_funcs, types as raw_types

        me_peer = await client.resolve_peer(ADMIN_TG_ID)

        # Получаем список доступных подарков
        gifts_result = await client.invoke(
            raw_funcs.payments.GetAvailableStarGifts(
                hash=0,
            )
        )

        all_gifts = gifts_result.gifts if hasattr(gifts_result, "gifts") else []

        allowed_lower = set(n.strip().lower() for n in (allowed_names or []))

        def gift_title(g) -> str:
            title = getattr(g, "title", None)
            if title:
                return str(title)
            sticker = getattr(g, "sticker", None)
            if sticker:
                return str(getattr(sticker, "alt", "") or "")
            return ""

        # Этап 1: ищем точное совпадение по названию модели (любая расцветка)
        nft_candidates = []
        if allowed_lower:
            for g in all_gifts:
                is_limited = getattr(g, "limited", False)
                remaining  = getattr(g, "availability_remains", 1)
                gift_id    = getattr(g, "id", None)
                title      = gift_title(g)

                if (
                    is_limited and
                    gift_id and
                    title.strip().lower() in allowed_lower and
                    (remaining is None or remaining > 0)
                ):
                    nft_candidates.append(g)

            if not nft_candidates:
                log.warning(
                    f"userbot_find_and_buy_nft: нет совпадений по названиям "
                    f"{sorted(allowed_lower)} — fallback на диапазон стоимости"
                )

        # Этап 2 (fallback): фильтр по диапазону стоимости
        if not nft_candidates:
            for g in all_gifts:
                star_count = getattr(g, "stars", 0)
                is_limited = getattr(g, "limited", False)
                remaining  = getattr(g, "availability_remains", 1)
                gift_id    = getattr(g, "id", None)

                if (
                    is_limited and
                    gift_id and
                    nft_min_stars <= star_count <= nft_max_stars and
                    (remaining is None or remaining > 0)
                ):
                    nft_candidates.append(g)

        if not nft_candidates:
            log.warning(
                f"userbot_find_and_buy_nft: нет NFT ни по названиям "
                f"{sorted(allowed_lower)}, ни в диапазоне "
                f"{nft_min_stars}–{nft_max_stars} Stars"
            )
            return None

        chosen  = random.choice(nft_candidates)
        gift_id = getattr(chosen, "id", None)
        stars   = getattr(chosen, "stars", 0)

        # Название: берём из title, иначе из emoji sticker
        title_name = gift_title(chosen)
        if title_name:
            name = title_name
        else:
            sticker = getattr(chosen, "sticker", None)
            if sticker:
                emoji = getattr(sticker, "emoji", "🎁")
                name  = f"{emoji} Gift #{gift_id}"
            else:
                name  = f"NFT Gift #{gift_id}"

        log.info(f"userbot: buying NFT gift_id={gift_id} name={name} stars={stars}")

        # Покупаем подарок на аккаунт @LeonardoRelayer
        buy_updates = await client.invoke(
            raw_funcs.payments.SendStarGift(
                no_anonymous=False,
                user_id=me_peer,
                gift_id=gift_id,
                message=None,
                upgrade=False,
            )
        )

        # Извлекаем message_id из Updates
        new_msg_id = None
        if hasattr(buy_updates, "updates"):
            for upd in buy_updates.updates:
                if hasattr(upd, "id") and hasattr(upd, "message"):
                    new_msg_id = upd.id
                    break
        if new_msg_id is None and hasattr(buy_updates, "id"):
            new_msg_id = buy_updates.id

        log.info(
            f"userbot: bought NFT gift_id={gift_id} "
            f"name={name} stars={stars} msg_id={new_msg_id}"
        )

        return {
            "nft_id":    gift_id,
            "msg_id":    new_msg_id,
            "name":      name,
            "stars":     stars,
            "photo_url": None,  # sticker thumbnail недоступен как прямая ссылка
        }

    except Exception as e:
        log.error(f"userbot_find_and_buy_nft error: {e}")
        return None


async def userbot_transfer_nft(winner_tg_id: int, nft_msg_id: int) -> bool:
    """
    Передаёт NFT из профиля @LeonardoRelayer победителю.

    Метод: payments.TransferStarGift
    TL: payments.transferStarGift#1fad0509
      user_id:InputUser  — peer себя (аккаунт @LeonardoRelayer)
      msg_id:int         — message_id подарка в профиле
      to_id:InputUser    — peer победителя

    Доступно с TL layer 176+.
    Возвращает True при успехе, False при ошибке.
    """
    client = await get_userbot()
    if not client:
        log.warning("userbot_transfer_nft: userbot недоступен")
        return False

    try:
        from pyrogram.raw import functions as raw_funcs

        me_peer     = await client.resolve_peer(ADMIN_TG_ID)
        winner_peer = await client.resolve_peer(winner_tg_id)

        await client.invoke(
            raw_funcs.payments.TransferStarGift(
                user_id=me_peer,
                msg_id=int(nft_msg_id),
                to_id=winner_peer,
            )
        )

        log.info(
            f"userbot: transferred NFT msg_id={nft_msg_id} "
            f"to winner tg_id={winner_tg_id}"
        )
        return True

    except Exception as e:
        log.error(f"userbot_transfer_nft error: {e}")
        return False

# ══════════════════════════════════════════════════════════
# 11. WIN AUTOMATION — цепочка выигрыша
# ══════════════════════════════════════════════════════════

async def process_win_automation(
    tg_id: int,
    bet_id: int,
    winning_spin: int,
    nft_wait_days: int,
):
    """
    Запускается как BackgroundTask после выигрышного спина.
    1. Продаём 2 кольца.
    2. Покупаем NFT в нужном диапазоне.
    3. Записываем в inventory.
    4. Уведомляем пользователя и администратора.
    """
    log.info(f"WIN automation start: tg_id={tg_id} bet_id={bet_id} winning_spin={winning_spin}")

    # Шаг 1: получаем msg_ids колец
    gifts_res = (
        supabase.table("received_gifts")
        .select("msg_id")
        .eq("bet_id", bet_id)
        .execute()
    )
    ring_msg_ids = [r["msg_id"] for r in (gifts_res.data or []) if r.get("msg_id")]

    # Шаг 2: продаём кольца
    sell_result = await userbot_sell_two_rings(tg_id, bet_id, ring_msg_ids)
    log.info(f"WIN: rings sold: {sell_result}")

    # Шаг 3: определяем диапазон NFT и покупаем
    nft_min, nft_max = get_nft_star_range(winning_spin)
    allowed_names = get_nft_tier_gift_names(winning_spin)
    nft = await userbot_find_and_buy_nft(nft_min, nft_max, allowed_names)

    available_at = (datetime.utcnow() + timedelta(days=nft_wait_days)).isoformat()

    if nft:
        nft_status  = "waiting"
        nft_name    = nft["name"]
        nft_stars   = nft["stars"]
        nft_msg_id  = nft.get("msg_id")
        nft_photo   = nft.get("photo_url")

        try:
            supabase.table("inventory").insert({
                "tg_id":        tg_id,
                "bet_id":       bet_id,
                "nft_id":       nft["nft_id"],
                "nft_msg_id":   nft_msg_id,
                "nft_name":     nft_name,
                "nft_stars":    nft_stars,
                "nft_photo_url": nft_photo,
                "status":       "waiting",
                "available_at": available_at,
            }).execute()
        except Exception as e:
            log_error(tg_id, "inventory.insert", str(e))

        log_action(tg_id, "nft_purchased", {
            "bet_id": bet_id, "nft_name": nft_name,
            "nft_stars": nft_stars, "msg_id": nft_msg_id,
            "available_at": available_at,
        })

        notify_text = (
            f"<b>Ты выиграл NFT!</b>\n\n"
            f"<b>{nft_name}</b> ({nft_stars}⭐)\n\n"
            f"Подарок будет отправлен через {nft_wait_days} дней.\n"
        )

        admin_text = (
            f"🎰 <b>Выигрыш!</b>\n"
            f"Пользователь: {tg_id}\n"
            f"Ставка #{winning_spin} в цикле\n"
            f"NFT: {nft_name} ({nft_stars}⭐)\n"
            f"msg_id: {nft_msg_id}\n"
            f"Выдать: {available_at[:10]}\n"
            f"Продажа колец: {sell_result['stars_earned']}⭐ "
            f"(комиссия Telegram: {sell_result['commission']}⭐)"
        )

    else:
        # NFT не найден ни по названию, ни в диапазоне — ручная обработка
        names_str  = ", ".join(allowed_names)
        nft_name   = f"{names_str} ({nft_min}–{nft_max}⭐, ручная покупка)"
        nft_stars  = nft_max
        nft_msg_id = None
        nft_status = "manual"

        try:
            supabase.table("inventory").insert({
                "tg_id":        tg_id,
                "bet_id":       bet_id,
                "nft_id":       None,
                "nft_msg_id":   None,
                "nft_name":     nft_name,
                "nft_stars":    nft_stars,
                "nft_photo_url": None,
                "status":       "manual",
                "available_at": available_at,
            }).execute()
        except Exception as e:
            log_error(tg_id, "inventory.insert_manual", str(e))

        notify_text = (
            f"<b>Ты выиграл!</b>\n\n"
            f"Администратор подберёт для тебя один из подарков"
            f"Подарок будет отправлен через {nft_wait_days} дней."
        )

        admin_text = (
            f"🎰 <b>Выигрыш! НУЖНА РУЧНАЯ ПОКУПКА NFT!</b>\n"
            f"Пользователь: {tg_id}\n"
            f"Подходящие модели: {names_str}\n"
            f"Диапазон: {nft_min}–{nft_max}⭐\n"
            f"Ставка #{winning_spin} в цикле\n"
            f"Выдать: {available_at[:10]}\n"
            f"❗ Ни одна модель из списка не найдена в наличии — купи вручную и обнови inventory."
        )

    await tg_send_message(tg_id, notify_text)
    await tg_send_message(ADMIN_TG_ID, admin_text)

    log.info(f"WIN automation done: tg_id={tg_id} nft={nft_name}")

# ══════════════════════════════════════════════════════════
# 12. РОУТЫ
# ══════════════════════════════════════════════════════════

# Количество звёзд, которые получает реферер за каждого приглашённого
REFERRAL_STARS_REWARD = 5

def _apply_referral(new_user_tg_id: int, referrer_id: int):
    """
    Записывает реферальную связь: new_user_tg_id пришёл по ссылке referrer_id.
    Начисляет REFERRAL_STARS_REWARD звёзд рефереру в stars_balance.
    Безопасно вызывать несколько раз — повторная запись не случится.
    """
    try:
        # ── Шаг 1: Явно читаем текущее состояние нового пользователя ──────────
        # Supabase UPDATE с .is_("referred_by", "null") иногда возвращает пустой
        # .data даже при успешном UPDATE (если RLS или return=minimal). Поэтому
        # не полагаемся на upd.data — сначала читаем, потом решаем.
        new_user_res = (
            supabase.table("users")
            .select("referred_by")
            .eq("tg_id", new_user_tg_id)
            .execute()
        )
        if not new_user_res.data:
            log.warning(f"_apply_referral: user {new_user_tg_id} not found in db")
            return

        current_referrer = new_user_res.data[0].get("referred_by")
        if current_referrer is not None:
            log.info(f"_apply_referral: {new_user_tg_id} already has referrer={current_referrer}, skip")
            return

        # ── Шаг 2: Записываем referred_by ────────────────────────────────────
        supabase.table("users").update({
            "referred_by": referrer_id
        }).eq("tg_id", new_user_tg_id).execute()

        # ── Шаг 3: Начисляем звёзды рефереру ─────────────────────────────────
        ref_res = (
            supabase.table("users")
            .select("referral_count, stars_balance")
            .eq("tg_id", referrer_id)
            .execute()
        )
        if not ref_res.data:
            log.warning(f"_apply_referral: referrer {referrer_id} not found in users")
            return

        old_count = ref_res.data[0].get("referral_count") or 0
        old_stars = ref_res.data[0].get("stars_balance") or 0
        new_stars = old_stars + REFERRAL_STARS_REWARD
        supabase.table("users").update({
            "referral_count": old_count + 1,
            "stars_balance":  new_stars,
        }).eq("tg_id", referrer_id).execute()

        log_action(new_user_tg_id, "ref_registered", {
            "referrer":              referrer_id,
            "new_count":             old_count + 1,
            "stars_awarded":         REFERRAL_STARS_REWARD,
            "referrer_new_balance":  new_stars,
        })
        log.info(
            f"_apply_referral: referrer={referrer_id} earned {REFERRAL_STARS_REWARD}⭐ "
            f"(total={new_stars}⭐), referrals={old_count + 1}"
        )
    except Exception as e:
        log.warning(f"_apply_referral error: {e}")

@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "version": "5.0",
        "time":    datetime.utcnow().isoformat(),
    }


# ─── REGISTER ────────────────────────────────────────────

async def get_geo(ip: Optional[str]) -> dict:
    """Определяем страну и город по IP через ip-api.com (бесплатно, без ключа)."""
    if not ip or ip in ("127.0.0.1", "::1"):
        return {}
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,countryCode,city,isp,org,query"},
            )
            data = r.json()
            if data.get("status") == "success":
                return {
                    "country":      data.get("country", ""),
                    "country_code": data.get("countryCode", ""),
                    "city":         data.get("city", ""),
                    "isp":          data.get("isp", ""),
                    "org":          data.get("org", ""),
                }
    except Exception as e:
        log.warning(f"geo lookup failed for {ip}: {e}")
    return {}


@app.post("/register")
async def register(body: RegisterBody, request: Request):
    trusted_tg_id = require_valid_init_data(body.init_data, body.tg_id)

    if not check_rate_limit(trusted_tg_id, "register", max_per_hour=60):
        raise HTTPException(429, "Слишком много запросов.")

    ip  = get_real_ip(request)
    geo = await get_geo(ip)

    try:
        existing = (
            supabase.table("users")
            .select("tg_id, referred_by")
            .eq("tg_id", trusted_tg_id)
            .execute()
        )
        if existing.data:
            user_row = existing.data[0]
            # Обновляем каждый раз при входе — IP мог смениться
            supabase.table("users").update({
                "ip_address":   ip,
                "country":      geo.get("country", ""),
                "country_code": geo.get("country_code", ""),
                "city":         geo.get("city", ""),
                "isp":          geo.get("isp", ""),
                "user_agent":   body.user_agent[:512] if body.user_agent else "",
                "language":     body.language[:32]    if body.language   else "",
                "platform":     body.platform[:64]    if body.platform   else "",
                "last_seen_at": datetime.utcnow().isoformat(),
            }).eq("tg_id", trusted_tg_id).execute()

            # Если реферер передан и новый пользователь ещё без реферера — применяем
            if body.referrer_id and body.referrer_id != trusted_tg_id:
                if not user_row.get("referred_by"):
                    log.info(f"register: existing user {trusted_tg_id} re-entering with referrer_id={body.referrer_id}, applying referral")
                    _apply_referral(trusted_tg_id, body.referrer_id)
                else:
                    log.info(f"register: existing user {trusted_tg_id} already has referrer={user_row.get('referred_by')}, skip")

            return {"status": "ok", "already_registered": True}

        # Первый цикл: winning_spin = 3 (гарантия NFT на 3-й ставке)
        supabase.table("users").insert({
            "tg_id":        trusted_tg_id,
            "username":     body.username[:64]   if body.username   else "",
            "first_name":   body.first_name[:64] if body.first_name else "",
            "ip_address":   ip,
            "country":      geo.get("country", ""),
            "country_code": geo.get("country_code", ""),
            "city":         geo.get("city", ""),
            "isp":          geo.get("isp", ""),
            "user_agent":   body.user_agent[:512] if body.user_agent else "",
            "language":     body.language[:32]    if body.language   else "",
            "platform":     body.platform[:64]    if body.platform   else "",
            "last_seen_at": datetime.utcnow().isoformat(),
            "cycle_spin":   0,
            "winning_spin": 3,
            "total_cycles": 0,
        }).execute()

        # Применяем реферал сразу при новой регистрации
        if body.referrer_id and body.referrer_id != trusted_tg_id:
            _apply_referral(trusted_tg_id, body.referrer_id)

        log_action(trusted_tg_id, "register", {
            "ip": ip, "country": geo.get("country"), "city": geo.get("city"),
            "ua": body.user_agent[:120] if body.user_agent else "",
            "referrer_id": body.referrer_id,
        })
        return {"status": "ok", "already_registered": False}

    except HTTPException:
        raise
    except Exception as e:
        log_error(trusted_tg_id, "register", str(e))
        raise HTTPException(500, "Ошибка сервера.")


# ─── CREATE BET (вместо create-invoice) ──────────────────

@app.post("/create-bet")
async def create_bet(body: CreateBetBody):
    """
    Создаёт ставку со статусом waiting_gifts.
    Возвращает инструкцию: отправить 2 кольца на @LeonardoRelayer.
    Никакого Invoice не создаётся.
    """
    trusted_tg_id = require_valid_init_data(body.init_data, body.tg_id)

    if not check_rate_limit(trusted_tg_id, "create_bet", max_per_hour=10):
        raise HTTPException(429, "Слишком много запросов.")

    # Проверяем, что пользователь зарегистрирован
    try:
        user_res = (
            supabase.table("users")
            .select("tg_id, cycle_spin, winning_spin, total_cycles")
            .eq("tg_id", trusted_tg_id)
            .execute()
        )
        if not user_res.data:
            raise HTTPException(404, "Сначала зарегистрируйся.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Ошибка базы данных.")

    # Проверяем нет ли уже активной ставки
    try:
        active = (
            supabase.table("bets")
            .select("id, status, created_at")
            .eq("tg_id", trusted_tg_id)
            .in_("status", ["waiting_gifts", "paid"])
            .execute()
        )
        if active.data:
            bet = active.data[0]
            created = datetime.fromisoformat(
                bet["created_at"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            age_minutes = (datetime.utcnow() - created).seconds // 60
            if age_minutes < 30:
                ring_account = get_setting("ring_account", "@LeonardoRelayer")
                return {
                    "status":       "ok",
                    "bet_id":       bet["id"],
                    "bet_status":   bet["status"],
                    "instruction":  f"Отправьте 2 кольца на аккаунт {ring_account}",
                    "ring_account": ring_account,
                    "already_active": True,
                }
            # Истекла по времени (>30 мин)
            supabase.table("bets").update({"status": "expired"}).eq(
                "id", bet["id"]
            ).execute()
    except Exception as e:
        log.error(f"create_bet active check: {e}")

    # Создаём новую ставку
    try:
        bet_insert = supabase.table("bets").insert({
            "tg_id":          trusted_tg_id,
            "status":         "waiting_gifts",
            "rings_received": 0,
        }).execute()
        bet_id = bet_insert.data[0]["id"]
    except Exception as e:
        log_error(trusted_tg_id, "create_bet.insert", str(e))
        raise HTTPException(500, "Не удалось создать ставку.")

    ring_account = get_setting("ring_account", "@LeonardoRelayer")

    log_action(trusted_tg_id, "create_bet", {"bet_id": bet_id})

    return {
        "status":       "ok",
        "bet_id":       bet_id,
        "bet_status":   "waiting_gifts",
        "instruction":  f"Отправьте 2 кольца на аккаунт {ring_account}",
        "ring_account": ring_account,
        "already_active": False,
    }


# ─── CHECK PAYMENT (проверка получения колец) ─────────────

@app.post("/check-payment")
async def check_payment(body: SpinBody):
    """
    Вызывается фронтендом (polling) после того, как пользователь
    утверждает, что отправил кольца.
    Userbot проверяет наличие 2 колец → подтверждает ставку.

    Возвращает:
      {"confirmed": True/False, "bet_status": "paid"/"waiting_gifts", ...}
    """
    trusted_tg_id = require_valid_init_data(body.init_data, body.tg_id)

    if not check_rate_limit(trusted_tg_id, "check_payment", max_per_hour=20):
        raise HTTPException(429, "Слишком много запросов.")

    # Загружаем ставку
    try:
        bet_res = (
            supabase.table("bets")
            .select("*")
            .eq("id", body.bet_id)
            .eq("tg_id", trusted_tg_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(500, "Ошибка базы данных.")

    if not bet_res.data:
        raise HTTPException(404, "Ставка не найдена.")

    bet = bet_res.data[0]

    if bet["status"] == "paid":
        return {"confirmed": True, "bet_status": "paid", "bet_id": bet["id"]}
    if bet["status"] == "used":
        return {"confirmed": True, "bet_status": "used", "bet_id": bet["id"]}
    if bet["status"] == "expired":
        raise HTTPException(410, "Ставка истекла. Создай новую.")
    if bet["status"] != "waiting_gifts":
        raise HTTPException(400, f"Неверный статус ставки: {bet['status']}")

    # Проверяем через userbot
    result = await userbot_check_and_confirm_bet(body.bet_id, trusted_tg_id)

    if result["confirmed"]:
        return {"confirmed": True, "bet_status": "paid", "bet_id": body.bet_id}
    else:
        ring_account = get_setting("ring_account", "@LeonardoRelayer")
        return {
            "confirmed":    False,
            "bet_status":   "waiting_gifts",
            "bet_id":       body.bet_id,
            "rings_found":  result.get("rings_found", 0),
            "reason":       result.get("reason", "Кольца не найдены."),
            "instruction":  f"Отправьте 2 кольца на аккаунт {ring_account}",
        }


# ─── SPIN ─────────────────────────────────────────────────

@app.post("/spin")
async def spin(body: SpinBody, background_tasks: BackgroundTasks):
    """
    Выполняет спин рулетки.
    Ставка должна быть в статусе 'paid' (кольца подтверждены).

    Механика цикла:
    - cycle_spin: текущий номер ставки в цикле (1..5)
    - winning_spin: на какой ставке выигрыш (3, 4 или 5)
    - Когда cycle_spin == winning_spin → выигрыш, цикл сбрасывается
    - Следующий цикл: winning_spin = random.choice([3,4,5])
    """
    trusted_tg_id = require_valid_init_data(body.init_data, body.tg_id)

    if not check_rate_limit(trusted_tg_id, "spin", max_per_hour=15):
        raise HTTPException(429, "Слишком много запросов.")

    # Загружаем ставку
    try:
        bet_res = (
            supabase.table("bets")
            .select("*")
            .eq("id", body.bet_id)
            .eq("tg_id", trusted_tg_id)
            .execute()
        )
    except Exception as e:
        log_error(trusted_tg_id, "spin.bet_lookup", str(e))
        raise HTTPException(500, "Ошибка базы данных.")

    if not bet_res.data:
        raise HTTPException(404, "Ставка не найдена.")

    bet = bet_res.data[0]

    if bet["status"] == "used":
        raise HTTPException(409, "Эта ставка уже использована.")
    if bet["status"] == "expired":
        raise HTTPException(410, "Ставка истекла. Создай новую.")
    if bet["status"] == "waiting_gifts":
        raise HTTPException(402, "Оплата кольцами ещё не подтверждена.")
    if bet["status"] != "paid":
        raise HTTPException(400, f"Неверный статус: {bet['status']}")

    # Загружаем пользователя
    try:
        user_res = (
            supabase.table("users")
            .select("cycle_spin, winning_spin, total_cycles")
            .eq("tg_id", trusted_tg_id)
            .single()
            .execute()
        )
        user = user_res.data
    except Exception as e:
        log_error(trusted_tg_id, "spin.user_lookup", str(e))
        raise HTTPException(500, "Ошибка загрузки пользователя.")

    current_cycle_spin = user.get("cycle_spin", 0) or 0
    winning_spin       = user.get("winning_spin", 3) or 3
    total_cycles       = user.get("total_cycles", 0) or 0

    # Атомарно помечаем ставку как использованную (защита от race condition)
    try:
        mark_used = (
            supabase.table("bets")
            .update({
                "status":   "used",
                "used_at":  datetime.utcnow().isoformat(),
            })
            .eq("id", bet["id"])
            .eq("status", "paid")
            .execute()
        )
    except Exception as e:
        log_error(trusted_tg_id, "spin.mark_used", str(e))
        raise HTTPException(500, "Ошибка блокировки ставки.")

    if not mark_used.data:
        raise HTTPException(409, "Ставка уже используется.")

    # Вычисляем результат спина
    new_cycle_spin = current_cycle_spin + 1
    is_win         = (new_cycle_spin == winning_spin)

    result = "win" if is_win else "lose"

    # Обновляем счётчики цикла
    if is_win:
        # Выигрыш: сбрасываем цикл, выбираем winning_spin для следующего цикла
        next_winning_spin = pick_winning_spin_for_new_cycle(is_first_cycle=False)
        new_total_cycles  = total_cycles + 1
        updates = {
            "cycle_spin":   0,
            "winning_spin": next_winning_spin,
            "total_cycles": new_total_cycles,
        }
    else:
        updates = {"cycle_spin": new_cycle_spin}

    try:
        supabase.table("users").update(updates).eq("tg_id", trusted_tg_id).execute()

        supabase.table("bets").update({
            "spin_number": new_cycle_spin,
            "result":      result,
            "spun_at":     datetime.utcnow().isoformat(),
        }).eq("id", bet["id"]).execute()

        log_action(trusted_tg_id, "spin", {
            "bet_id":     body.bet_id,
            "cycle_spin": new_cycle_spin,
            "winning_spin": winning_spin,
            "result":     result,
        })
    except Exception as e:
        log_error(trusted_tg_id, "spin.update", str(e))
        # Откатываем ставку
        try:
            supabase.table("bets").update(
                {"status": "paid", "used_at": None}
            ).eq("id", bet["id"]).execute()
        except Exception:
            pass
        raise HTTPException(500, "Ошибка записи спина. Напиши в поддержку.")

    nft_wait_days = int(get_setting("nft_wait_days", "21"))
    nft_min, nft_max = get_nft_star_range(winning_spin)
    available_at_iso = (datetime.utcnow() + timedelta(days=nft_wait_days)).isoformat()

    # Реальная рулетка: выигрыш всегда NFT.
    # Схема циклов:
    #   Цикл 1: выигрыш на 3-й ставке (1,2 — проигрыш, 3 — выигрыш)
    #   Цикл 2+: выигрыш на 3-й, 4-й или 5-й ставке (случайно)
    #   Тир NFT зависит от winning_spin:
    #     3 → тир 1 (150–450⭐): Vice Cream, Instant Ramen, Whip Cupcake, Lunar Snake, Tama Gadget, Snake Box
    #     4 → тир 2 (450–550⭐): Fresh Socks, Party Sparkler, Hypno Lolipop, Easter Egg, Big Year, Tama Gadget
    #     5 → тир 3 (550–700⭐): Witch Hat, Stellar Rocket, Input Key
    # prize_type = "nft" | None (при проигрыше)
    prize_type = "nft" if is_win else None
    stars_prize_amount = None

    # При выигрыше — запускаем автоматизацию NFT в фоне
    if is_win and prize_type == "nft":
        background_tasks.add_task(
            process_win_automation,
            tg_id=trusted_tg_id,
            bet_id=bet["id"],
            winning_spin=winning_spin,
            nft_wait_days=nft_wait_days,
        )

    # Сколько ставок до следующего выигрыша (для инфо фронтенда при проигрыше)
    if is_win:
        next_win_in = None
    else:
        next_win_in = winning_spin - new_cycle_spin

    return {
        "status":             "ok",
        "result":             result,
        "cycle_spin":         new_cycle_spin,
        "winning_spin":       winning_spin if not is_win else None,
        "next_win_in":        next_win_in,
        "is_win":             is_win,
        "prize_type":         prize_type,
        "stars_prize_amount": stars_prize_amount,
        "nft_name":           f"NFT {nft_min}–{nft_max}⭐" if (is_win and prize_type == "nft") else None,
        "nft_stars":          nft_max if (is_win and prize_type == "nft") else None,
        "available_at":       available_at_iso if (is_win and prize_type == "nft") else None,
    }


# ─── INVENTORY ────────────────────────────────────────────

@app.get("/inventory/{tg_id}")
async def get_inventory(tg_id: int, init_data: str = ""):
    trusted_tg_id = require_valid_init_data(init_data, tg_id)
    if not check_rate_limit(trusted_tg_id, "inventory", max_per_hour=120):
        raise HTTPException(429, "Слишком много запросов.")
    try:
        items = (
            supabase.table("inventory")
            .select("*")
            .eq("tg_id", trusted_tg_id)
            .order("purchased_at", desc=True)
            .execute()
        )
        return {"status": "ok", "items": items.data}
    except Exception as e:
        log_error(trusted_tg_id, "inventory", str(e))
        raise HTTPException(500, "Ошибка загрузки инвентаря.")


# ─── STATS ────────────────────────────────────────────────

@app.get("/stats/{tg_id}")
async def get_stats(tg_id: int, init_data: str = ""):
    trusted_tg_id = require_valid_init_data(init_data, tg_id)
    if not check_rate_limit(trusted_tg_id, "stats", max_per_hour=120):
        raise HTTPException(429, "Слишком много запросов.")
    try:
        user_res = (
            supabase.table("users")
            .select("cycle_spin, winning_spin, total_cycles, stars_balance, free_spin_at")
            .eq("tg_id", trusted_tg_id)
            .single()
            .execute()
        )
        u = user_res.data or {}
        cycle_spin      = u.get("cycle_spin", 0) or 0
        winning_spin    = u.get("winning_spin", 3) or 3
        total_cycles    = u.get("total_cycles", 0) or 0
        stars_balance   = u.get("stars_balance", 0) or 0
        next_win_in     = winning_spin - cycle_spin

        # Проверяем доступность бесплатного прокрута (раз в 24 ч)
        free_spin_at    = u.get("free_spin_at")
        free_spin_available = True
        free_spin_next_ts   = None
        if free_spin_at:
            try:
                last_free = datetime.fromisoformat(str(free_spin_at).replace("Z", "+00:00")).replace(tzinfo=None)
                diff = datetime.utcnow() - last_free
                if diff.total_seconds() < 86400:
                    free_spin_available = False
                    free_spin_next_ts   = int(last_free.timestamp()) + 86400
            except Exception:
                pass

        wins_res = (
            supabase.table("bets")
            .select("id", count="exact")
            .eq("tg_id", trusted_tg_id)
            .eq("result", "win")
            .execute()
        )
        total_wins = wins_res.count if wins_res.count is not None else 0

        return {
            "status":               "ok",
            "cycle_spin":           cycle_spin,
            "winning_spin":         winning_spin,
            "total_cycles":         total_cycles,
            "next_win_in":          next_win_in,
            "total_wins":           total_wins,
            "stars_balance":        stars_balance,
            "free_spin_available":  free_spin_available,
            "free_spin_next_ts":    free_spin_next_ts,
            "is_admin":             trusted_tg_id in (ADMIN_TG_ID, OWNER_ID),
        }
    except Exception as e:
        log_error(trusted_tg_id, "stats", str(e))
        raise HTTPException(500, "Ошибка статистики.")


# ─── CRON: автоматическая выдача NFT через 21 день ────────

@app.get("/cron/deliver")
async def cron_deliver(x_cron_secret: Optional[str] = Header(None)):
    """
    Запускается ежедневно (Render Cron Jobs).
    Находит NFT, срок блокировки которых истёк.
    Userbot передаёт NFT победителям.

    Настройка в Render: GET /cron/deliver
    Заголовок: X-Cron-Secret: <CRON_SECRET>

    Статусы после выдачи:
      delivered     — успешно передан через userbot
      transfer_error — ошибка передачи (записывается в audit_log)
      manual        — userbot не работает / нет msg_id
    """
    if x_cron_secret != CRON_SECRET:
        log.warning(f"cron: invalid secret")
        raise HTTPException(403, "Forbidden")

    log.info("Cron deliver started")
    now = datetime.utcnow().isoformat()

    try:
        ready = (
            supabase.table("inventory")
            .select("*")
            .eq("status", "waiting")
            .lte("available_at", now)
            .execute()
        )
    except Exception as e:
        log.error(f"cron_deliver query error: {e}")
        return {"status": "error", "error": str(e)}

    delivered = []
    failed    = []

    for item in ready.data:
        tg_id    = item["tg_id"]
        nft_name = item["nft_name"]
        msg_id   = item.get("nft_msg_id")
        inv_id   = item["id"]

        try:
            success = False

            if msg_id:
                success = await userbot_transfer_nft(tg_id, int(msg_id))
                if success:
                    log_action(tg_id, "nft_delivered", {
                        "inv_id": inv_id, "nft_name": nft_name, "msg_id": msg_id,
                    })
                else:
                    log_error(tg_id, "nft_transfer", "userbot returned False", {
                        "inv_id": inv_id, "msg_id": msg_id,
                    })
            else:
                log.info(f"cron: no msg_id for inv_id={inv_id}, manual needed")

            if success:
                new_status = "delivered"
                await tg_send_message(
                    tg_id,
                    f"🎁 <b>Твой NFT отправлен!</b>\n\n"
                    f"<b>{nft_name}</b>\n\n",
                )
                await tg_send_message(
                    ADMIN_TG_ID,
                    f"<b>Приз выдан</b>\n"
                    f"Пользователь: {tg_id}\n"
                    f"NFT: {nft_name}\n"
                    f"msg_id: {msg_id}",
                )
            else:
                new_status = "transfer_error"
                await tg_send_message(
                    tg_id,
                    f"<b>Твой NFT готов!</b>\n\n"
                    f"<b>{nft_name}</b>\n\n"
                    f"Администратор отправит подарок вручную в ближайшее время.",
                )
                await tg_send_message(
                    ADMIN_TG_ID,
                    f"❗ <b>Ошибка автовыдачи NFT!</b>\n"
                    f"Пользователь: {tg_id}\n"
                    f"NFT: {nft_name}\n"
                    f"msg_id: {msg_id}\n"
                    f"inv_id: {inv_id}\n"
                    f"Передай вручную через userbot!",
                )

            supabase.table("inventory").update({
                "status":       new_status,
                "delivered_at": datetime.utcnow().isoformat(),
            }).eq("id", inv_id).execute()

            if success:
                delivered.append({"tg_id": tg_id, "nft_name": nft_name})
            else:
                failed.append({"tg_id": tg_id, "inv_id": inv_id, "reason": "transfer_error"})

        except Exception as e:
            log_error(tg_id, "cron_deliver", str(e), {"inv_id": inv_id})
            failed.append({"tg_id": tg_id, "inv_id": inv_id, "error": str(e)})
            try:
                supabase.table("inventory").update({
                    "status": "transfer_error",
                }).eq("id", inv_id).execute()
            except Exception:
                pass

    return {
        "status":    "ok",
        "delivered": len(delivered),
        "failed":    len(failed),
        "items":     delivered,
        "errors":    failed,
    }


# ─── PROFILE PHOTO ───────────────────────────────────────

@app.get("/profile-photo/{tg_id}")
async def get_profile_photo(tg_id: int, init_data: str = ""):
    """
    Возвращает фото профиля пользователя через Bot API.
    Фронт вызывает этот эндпоинт если tgUser.photo_url недоступен.
    """
    try:
        # getUserProfilePhotos через Bot API
        result = await tg_api("getUserProfilePhotos", {"user_id": tg_id, "limit": 1})
        if result.get("ok") and result.get("result", {}).get("photos"):
            photos = result["result"]["photos"]
            if photos and photos[0]:
                # Берём самое большое фото
                file_id = photos[0][-1]["file_id"]
                file_res = await tg_api("getFile", {"file_id": file_id})
                if file_res.get("ok") and file_res.get("result", {}).get("file_path"):
                    file_path = file_res["result"]["file_path"]
                    photo_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                    return {"status": "ok", "photo_url": photo_url}
    except Exception as e:
        log.warning(f"profile_photo error tg_id={tg_id}: {e}")
    return {"status": "ok", "photo_url": None}


# ─── REFERRAL ────────────────────────────────────────────

BOT_USERNAME = "leonardo_game_bot"  # имя бота без @

@app.get("/referral/{tg_id}")
async def get_referral(tg_id: int, init_data: str = ""):
    trusted_tg_id = require_valid_init_data(init_data, tg_id)
    if not check_rate_limit(trusted_tg_id, "referral", max_per_hour=60):
        raise HTTPException(429, "Слишком много запросов.")
    try:
        res = (
            supabase.table("users")
            .select("referral_count, stars_balance")
            .eq("tg_id", trusted_tg_id)
            .single()
            .execute()
        )
        u = res.data or {}
        ref_link = f"https://t.me/{BOT_USERNAME}/app?startapp=ref_{trusted_tg_id}"
        return {
            "status":          "ok",
            "referral_count":  u.get("referral_count", 0) or 0,
            "stars_balance":   u.get("stars_balance", 0) or 0,
            "ref_link":        ref_link,
            "stars_per_ref":   REFERRAL_STARS_REWARD,
        }
    except Exception as e:
        log_error(trusted_tg_id, "referral", str(e))
        return {"status": "ok", "referral_count": 0, "stars_balance": 0,
                "ref_link": f"https://t.me/{BOT_USERNAME}/app?startapp=ref_{tg_id}",
                "stars_per_ref": REFERRAL_STARS_REWARD}


# ─── FREE SPIN ────────────────────────────────────────────

class FreeSpin(BaseModel):
    tg_id:     int
    init_data: str

    @field_validator("tg_id")
    @classmethod
    def tg_id_positive(cls, v):
        if v <= 0:
            raise ValueError("tg_id должен быть положительным")
        return v

FREE_SPIN_STARS_MIN = 1
FREE_SPIN_STARS_MAX = 8

@app.post("/free-spin")
async def free_spin(body: FreeSpin):
    """
    Бесплатный прокрут рулетки раз в 24 часа.
    Всегда выпадают звёзды (1–8), начисляются в stars_balance.
    """
    trusted_tg_id = require_valid_init_data(body.init_data, body.tg_id)
    if not check_rate_limit(trusted_tg_id, "free_spin", max_per_hour=5):
        raise HTTPException(429, "Слишком много запросов.")

    try:
        user_res = (
            supabase.table("users")
            .select("stars_balance, free_spin_at")
            .eq("tg_id", trusted_tg_id)
            .single()
            .execute()
        )
        if not user_res.data:
            raise HTTPException(404, "Пользователь не найден.")

        u = user_res.data
        free_spin_at  = u.get("free_spin_at")
        stars_balance = u.get("stars_balance") or 0

        # Проверяем 24-часовой кулдаун
        if free_spin_at:
            try:
                last_free = datetime.fromisoformat(
                    str(free_spin_at).replace("Z", "+00:00")
                ).replace(tzinfo=None)
                diff = datetime.utcnow() - last_free
                if diff.total_seconds() < 86400:
                    next_ts = int(last_free.timestamp()) + 86400
                    raise HTTPException(
                        429,
                        f"Бесплатный прокрут будет доступен через "                        f"{int((86400 - diff.total_seconds()) // 3600 + 1)} ч. "                        f"(next_ts={next_ts})"
                    )
            except HTTPException:
                raise
            except Exception:
                pass

        # Начисляем звёзды
        stars_won = random.randint(FREE_SPIN_STARS_MIN, FREE_SPIN_STARS_MAX)
        new_balance = stars_balance + stars_won

        supabase.table("users").update({
            "stars_balance": new_balance,
            "free_spin_at":  datetime.utcnow().isoformat(),
        }).eq("tg_id", trusted_tg_id).execute()

        log_action(trusted_tg_id, "free_spin", {
            "stars_won": stars_won, "new_balance": new_balance,
        })

        # Считаем следующий доступный прокрут
        next_free_ts = int(datetime.utcnow().timestamp()) + 86400

        return {
            "status":       "ok",
            "stars_won":    stars_won,
            "stars_balance": new_balance,
            "next_free_ts": next_free_ts,
        }

    except HTTPException:
        raise
    except Exception as e:
        log_error(trusted_tg_id, "free_spin", str(e))
        raise HTTPException(500, "Ошибка бесплатного прокрута.")


# ══════════════════════════════════════════════════════════
# ADMIN PANEL — /admin/*
# Доступ: ADMIN_TG_ID (из env) и OWNER_ID (1693493298)
# ══════════════════════════════════════════════════════════

def require_admin(init_data: str, claimed_tg_id: int) -> int:
    """Проверяет, что запрос от администратора или владельца.
    Для admin-эндпоинтов ВСЕГДА требуем валидную подпись — без fallback.
    """
    is_valid, tg_id_from_data, reason = verify_telegram_init_data(init_data)
    log.info(f"[admin-auth] claimed={claimed_tg_id} valid={is_valid} reason={reason}")

    if is_valid and tg_id_from_data is not None:
        tg_id = int(tg_id_from_data)
    else:
        # Для admin-панели fallback ЗАПРЕЩЁН — требуем валидную подпись
        raise HTTPException(403, f"Доступ запрещён: подпись невалидна ({reason})")

    if tg_id not in (ADMIN_TG_ID, OWNER_ID):
        log.warning(f"[admin-auth] tg_id={tg_id} не является админом (ADMIN={ADMIN_TG_ID}, OWNER={OWNER_ID})")
        raise HTTPException(403, "Доступ запрещён. Только для администраторов.")

    log.info(f"[admin-auth] OK tg_id={tg_id}")
    return tg_id


class AdminActionBody(BaseModel):
    init_data: str
    tg_id: int


class AdminUserActionBody(BaseModel):
    init_data: str
    tg_id: int
    target_tg_id: int


class AdminInventoryBody(BaseModel):
    init_data: str
    tg_id: int
    inventory_id: int


class AdminSetSettingBody(BaseModel):
    init_data: str
    tg_id: int
    key: str
    value: str


class AdminBroadcastBody(BaseModel):
    init_data: str
    tg_id: int
    text: str
    parse_mode: str = "HTML"


class AdminBanBody(BaseModel):
    init_data: str
    tg_id: int
    target_tg_id: int
    reason: str = ""


class AdminStarsBody(BaseModel):
    init_data: str
    tg_id: int
    target_tg_id: int
    amount: int
    note: str = ""


class AdminBetBody(BaseModel):
    init_data: str
    tg_id: int
    bet_id: int


class AdminInventoryUpdateBody(BaseModel):
    init_data: str
    tg_id: int
    inventory_id: int
    status: str
    nft_name: str = ""
    nft_stars: int = 0
    nft_msg_id: int = 0
    nft_photo_url: str = ""


class AdminUserEditBody(BaseModel):
    init_data: str
    tg_id: int
    target_tg_id: int
    cycle_spin: int = 0
    winning_spin: int = 3
    total_cycles: int = 0
    stars_balance: int = 0


class AdminNftTransferBody(BaseModel):
    init_data: str
    tg_id: int
    inventory_id: int
    winner_tg_id: int


# ─── 1. Общая статистика ─────────────────────────────────

@app.get("/admin/stats")
async def admin_stats(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        users_res  = supabase.table("users").select("tg_id", count="exact").execute()
        bets_res   = supabase.table("bets").select("id", count="exact").execute()
        wins_res   = supabase.table("bets").select("id", count="exact").eq("result", "win").execute()
        inv_res    = supabase.table("inventory").select("id", count="exact").execute()
        wait_res   = supabase.table("inventory").select("id", count="exact").eq("status", "waiting").execute()
        manual_res = supabase.table("inventory").select("id", count="exact").eq("status", "manual").execute()
        done_res   = supabase.table("inventory").select("id", count="exact").eq("status", "done").execute()
        return {
            "status": "ok",
            "total_users":       users_res.count or 0,
            "total_bets":        bets_res.count or 0,
            "total_wins":        wins_res.count or 0,
            "total_inventory":   inv_res.count or 0,
            "inventory_waiting": wait_res.count or 0,
            "inventory_manual":  manual_res.count or 0,
            "inventory_done":    done_res.count or 0,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 2. Список всех пользователей ────────────────────────

@app.get("/admin/users")
async def admin_users(init_data: str = "", tg_id: int = 0,
                      limit: int = 50, offset: int = 0, search: str = ""):
    require_admin(init_data, tg_id)
    try:
        q = supabase.table("users").select("*").order("created_at", desc=True)
        if search:
            q = q.or_(f"username.ilike.%{search}%,first_name.ilike.%{search}%")
        res = q.range(offset, offset + limit - 1).execute()
        return {"status": "ok", "users": res.data or [], "total": len(res.data or [])}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 3. Карточка конкретного пользователя ────────────────

@app.get("/admin/user/{target_tg_id}")
async def admin_user_detail(target_tg_id: int, init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        user_res = supabase.table("users").select("*").eq("tg_id", target_tg_id).single().execute()
        bets_res = supabase.table("bets").select("*").eq("tg_id", target_tg_id).order("created_at", desc=True).execute()
        inv_res  = supabase.table("inventory").select("*").eq("tg_id", target_tg_id).order("created_at", desc=True).execute()
        log_res  = supabase.table("audit_log").select("*").eq("tg_id", target_tg_id).order("created_at", desc=True).limit(50).execute()
        return {
            "status":    "ok",
            "user":      user_res.data,
            "bets":      bets_res.data or [],
            "inventory": inv_res.data or [],
            "audit_log": log_res.data or [],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 4. Список инвентаря (все записи) ────────────────────

@app.get("/admin/inventory")
async def admin_inventory(init_data: str = "", tg_id: int = 0,
                          status_filter: str = "", limit: int = 50, offset: int = 0):
    require_admin(init_data, tg_id)
    try:
        q = supabase.table("inventory").select("*").order("created_at", desc=True)
        if status_filter:
            q = q.eq("status", status_filter)
        res = q.range(offset, offset + limit - 1).execute()
        return {"status": "ok", "inventory": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 5. Обновить запись инвентаря вручную ────────────────

@app.post("/admin/inventory/update")
async def admin_inventory_update(body: AdminInventoryUpdateBody):
    require_admin(body.init_data, body.tg_id)
    try:
        upd: dict = {"status": body.status}
        if body.nft_name:      upd["nft_name"]     = body.nft_name
        if body.nft_stars:     upd["nft_stars"]     = body.nft_stars
        if body.nft_msg_id:    upd["nft_msg_id"]    = body.nft_msg_id
        if body.nft_photo_url: upd["nft_photo_url"] = body.nft_photo_url
        res = supabase.table("inventory").update(upd).eq("id", body.inventory_id).execute()
        log_action(body.tg_id, "admin_inventory_update", {"inventory_id": body.inventory_id, "upd": upd})
        return {"status": "ok", "updated": res.data}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 6. Принудительная выдача NFT (transfer) ─────────────

@app.post("/admin/inventory/transfer")
async def admin_transfer_nft(body: AdminNftTransferBody):
    require_admin(body.init_data, body.tg_id)
    try:
        inv_res = supabase.table("inventory").select("*").eq("id", body.inventory_id).single().execute()
        inv = inv_res.data
        if not inv:
            raise HTTPException(404, "Запись инвентаря не найдена.")
        nft_msg_id = inv.get("nft_msg_id")
        if not nft_msg_id:
            raise HTTPException(400, "nft_msg_id отсутствует — ручная покупка NFT.")
        ok = await userbot_transfer_nft(body.winner_tg_id, nft_msg_id)
        if ok:
            supabase.table("inventory").update({"status": "done"}).eq("id", body.inventory_id).execute()
            await tg_send_message(body.winner_tg_id, f"Твой NFT отправлен!\n\n{inv.get('nft_name', 'NFT')} ({inv.get('nft_stars', '?')}⭐)")
            log_action(body.tg_id, "admin_transfer_nft", {"inventory_id": body.inventory_id, "winner": body.winner_tg_id})
        return {"status": "ok", "transferred": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 7. Все ставки ────────────────────────────────────────

@app.get("/admin/bets")
async def admin_bets(init_data: str = "", tg_id: int = 0,
                     status_filter: str = "", limit: int = 50, offset: int = 0):
    require_admin(init_data, tg_id)
    try:
        q = supabase.table("bets").select("*").order("created_at", desc=True)
        if status_filter:
            q = q.eq("status", status_filter)
        res = q.range(offset, offset + limit - 1).execute()
        return {"status": "ok", "bets": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 8. Отменить ставку ───────────────────────────────────

@app.post("/admin/bet/cancel")
async def admin_cancel_bet(body: AdminBetBody):
    require_admin(body.init_data, body.tg_id)
    try:
        res = supabase.table("bets").update({"status": "expired"}).eq("id", body.bet_id).execute()
        log_action(body.tg_id, "admin_cancel_bet", {"bet_id": body.bet_id})
        return {"status": "ok", "updated": res.data}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 9. Сбросить ставку (paid → waiting_gifts) ────────────

@app.post("/admin/bet/reset")
async def admin_reset_bet(body: AdminBetBody):
    require_admin(body.init_data, body.tg_id)
    try:
        res = supabase.table("bets").update({"status": "waiting_gifts", "used_at": None}).eq("id", body.bet_id).execute()
        log_action(body.tg_id, "admin_reset_bet", {"bet_id": body.bet_id})
        return {"status": "ok", "updated": res.data}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 10. Добавить / снять звёзды пользователю ────────────

@app.post("/admin/user/stars")
async def admin_adjust_stars(body: AdminStarsBody):
    require_admin(body.init_data, body.tg_id)
    try:
        user_res = supabase.table("users").select("stars_balance").eq("tg_id", body.target_tg_id).single().execute()
        old = (user_res.data or {}).get("stars_balance") or 0
        new_bal = max(0, old + body.amount)
        supabase.table("users").update({"stars_balance": new_bal}).eq("tg_id", body.target_tg_id).execute()
        log_action(body.tg_id, "admin_adjust_stars", {
            "target": body.target_tg_id, "delta": body.amount,
            "old": old, "new": new_bal, "note": body.note,
        })
        return {"status": "ok", "old_balance": old, "new_balance": new_bal}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 11. Заблокировать пользователя ──────────────────────

@app.post("/admin/user/ban")
async def admin_ban_user(body: AdminBanBody):
    require_admin(body.init_data, body.tg_id)
    try:
        supabase.table("users").update({"is_banned": True, "ban_reason": body.reason}).eq("tg_id", body.target_tg_id).execute()
        log_action(body.tg_id, "admin_ban", {"target": body.target_tg_id, "reason": body.reason})
        await tg_send_message(body.target_tg_id, "🚫 Твой аккаунт заблокирован. Обратись в поддержку.")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 12. Разблокировать пользователя ─────────────────────

@app.post("/admin/user/unban")
async def admin_unban_user(body: AdminUserActionBody):
    require_admin(body.init_data, body.tg_id)
    try:
        supabase.table("users").update({"is_banned": False, "ban_reason": None}).eq("tg_id", body.target_tg_id).execute()
        log_action(body.tg_id, "admin_unban", {"target": body.target_tg_id})
        await tg_send_message(body.target_tg_id, "Твой аккаунт разблокирован. Удачи!")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 13. Редактировать цикл / баланс пользователя ────────

@app.post("/admin/user/edit")
async def admin_edit_user(body: AdminUserEditBody):
    require_admin(body.init_data, body.tg_id)
    try:
        upd = {
            "cycle_spin":    body.cycle_spin,
            "winning_spin":  body.winning_spin,
            "total_cycles":  body.total_cycles,
            "stars_balance": body.stars_balance,
        }
        supabase.table("users").update(upd).eq("tg_id", body.target_tg_id).execute()
        log_action(body.tg_id, "admin_edit_user", {"target": body.target_tg_id, **upd})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 14. Рассылка сообщения всем пользователям ───────────

@app.post("/admin/broadcast")
async def admin_broadcast(body: AdminBroadcastBody):
    require_admin(body.init_data, body.tg_id)
    try:
        users_res = supabase.table("users").select("tg_id").execute()
        tg_ids = [u["tg_id"] for u in (users_res.data or []) if u.get("tg_id")]
        sent = 0
        failed = 0
        for uid in tg_ids:
            try:
                await tg_api("sendMessage", {
                    "chat_id":    uid,
                    "text":       body.text,
                    "parse_mode": body.parse_mode,
                })
                sent += 1
            except Exception:
                failed += 1
        log_action(body.tg_id, "admin_broadcast", {"sent": sent, "failed": failed, "total": len(tg_ids)})
        return {"status": "ok", "sent": sent, "failed": failed, "total": len(tg_ids)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 15. Получить/обновить настройки (settings) ──────────

@app.get("/admin/settings")
async def admin_get_settings(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        res = supabase.table("settings").select("*").execute()
        return {"status": "ok", "settings": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/admin/settings/set")
async def admin_set_setting(body: AdminSetSettingBody):
    require_admin(body.init_data, body.tg_id)
    try:
        existing = supabase.table("settings").select("key").eq("key", body.key).execute()
        if existing.data:
            supabase.table("settings").update({"value": body.value}).eq("key", body.key).execute()
        else:
            supabase.table("settings").insert({"key": body.key, "value": body.value}).execute()
        log_action(body.tg_id, "admin_set_setting", {"key": body.key, "value": body.value})
        return {"status": "ok", "key": body.key, "value": body.value}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 16. Audit log (все записи) ──────────────────────────

@app.get("/admin/audit-log")
async def admin_audit_log(init_data: str = "", tg_id: int = 0,
                          limit: int = 100, offset: int = 0, action_filter: str = ""):
    require_admin(init_data, tg_id)
    try:
        q = supabase.table("audit_log").select("*").order("created_at", desc=True)
        if action_filter:
            q = q.eq("action", action_filter)
        res = q.range(offset, offset + limit - 1).execute()
        return {"status": "ok", "log": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 17. Список ожидающих выдачи NFT ─────────────────────

@app.get("/admin/inventory/pending")
async def admin_pending_nft(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        now = datetime.utcnow().isoformat()
        res = (
            supabase.table("inventory")
            .select("*")
            .in_("status", ["waiting", "manual"])
            .lte("available_at", now)
            .order("available_at")
            .execute()
        )
        return {"status": "ok", "pending": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 18. Реферальная статистика ───────────────────────────

@app.get("/admin/referrals")
async def admin_referrals(init_data: str = "", tg_id: int = 0, limit: int = 50):
    require_admin(init_data, tg_id)
    try:
        res = (
            supabase.table("users")
            .select("tg_id, username, first_name, referral_count, stars_balance")
            .gt("referral_count", 0)
            .order("referral_count", desc=True)
            .limit(limit)
            .execute()
        )
        return {"status": "ok", "top_referrers": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 19. Топ игроков по выигрышам ────────────────────────

@app.get("/admin/top-winners")
async def admin_top_winners(init_data: str = "", tg_id: int = 0, limit: int = 50):
    require_admin(init_data, tg_id)
    try:
        res = (
            supabase.table("users")
            .select("tg_id, username, first_name, total_cycles, cycle_spin")
            .order("total_cycles", desc=True)
            .limit(limit)
            .execute()
        )
        return {"status": "ok", "top_winners": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 20. Отправить сообщение конкретному пользователю ────

class AdminMessageBody(BaseModel):
    init_data: str
    tg_id: int
    target_tg_id: int
    text: str
    parse_mode: str = "HTML"

@app.post("/admin/user/message")
async def admin_message_user(body: AdminMessageBody):
    require_admin(body.init_data, body.tg_id)
    try:
        await tg_api("sendMessage", {
            "chat_id":    body.target_tg_id,
            "text":       body.text,
            "parse_mode": body.parse_mode,
        })
        log_action(body.tg_id, "admin_message_user", {"target": body.target_tg_id, "text": body.text[:100]})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 21. Сбросить цикл пользователя ──────────────────────

@app.post("/admin/user/reset-cycle")
async def admin_reset_cycle(body: AdminUserActionBody):
    require_admin(body.init_data, body.tg_id)
    try:
        supabase.table("users").update({
            "cycle_spin":   0,
            "winning_spin": 3,
        }).eq("tg_id", body.target_tg_id).execute()
        log_action(body.tg_id, "admin_reset_cycle", {"target": body.target_tg_id})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 22. Выдать бесплатный спин ──────────────────────────

class AdminFreeSpinBody(BaseModel):
    init_data: str
    tg_id: int
    target_tg_id: int

@app.post("/admin/user/free-spin")
async def admin_give_free_spin(body: AdminFreeSpinBody):
    require_admin(body.init_data, body.tg_id)
    try:
        supabase.table("users").update({
            "free_spin_at": None,  
        }).eq("tg_id", body.target_tg_id).execute()
        log_action(body.tg_id, "admin_give_free_spin", {"target": body.target_tg_id})
        await tg_send_message(body.target_tg_id, "Тебе выдан бесплатный спин! Заходи в игру.")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 23. Список заблокированных пользователей ────────────

@app.get("/admin/users/banned")
async def admin_banned_users(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        res = supabase.table("users").select("*").eq("is_banned", True).execute()
        return {"status": "ok", "banned": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 24. Поиск пользователя по username / tg_id ──────────

@app.get("/admin/user/search")
async def admin_search_user(init_data: str = "", tg_id: int = 0, q: str = ""):
    require_admin(init_data, tg_id)
    try:
        try:
            search_id = int(q)
            res = supabase.table("users").select("*").eq("tg_id", search_id).execute()
        except ValueError:
            res = supabase.table("users").select("*").ilike("username", f"%{q}%").execute()
        return {"status": "ok", "users": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 25. Полная история ставок по bet_id ─────────────────

@app.get("/admin/bet/{bet_id}")
async def admin_bet_detail(bet_id: int, init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        bet_res  = supabase.table("bets").select("*").eq("id", bet_id).single().execute()
        gift_res = supabase.table("received_gifts").select("*").eq("bet_id", bet_id).execute()
        return {
            "status": "ok",
            "bet":    bet_res.data,
            "gifts":  gift_res.data or [],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 26. Изменить статус inventory вручную ───────────────

class AdminInvStatusBody(BaseModel):
    init_data: str
    tg_id: int
    inventory_id: int
    new_status: str

@app.post("/admin/inventory/status")
async def admin_set_inv_status(body: AdminInvStatusBody):
    require_admin(body.init_data, body.tg_id)
    try:
        res = supabase.table("inventory").update({"status": body.new_status}).eq("id", body.inventory_id).execute()
        log_action(body.tg_id, "admin_inv_status", {"id": body.inventory_id, "status": body.new_status})
        return {"status": "ok", "updated": res.data}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 27. Активные ставки (paid, waiting_gifts) ───────────

@app.get("/admin/bets/active")
async def admin_active_bets(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        res = (
            supabase.table("bets")
            .select("*")
            .in_("status", ["paid", "waiting_gifts"])
            .order("created_at", desc=True)
            .execute()
        )
        return {"status": "ok", "bets": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 28. Дашборд — активность за последние 7 дней ────────

@app.get("/admin/dashboard")
async def admin_dashboard(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
        new_users = supabase.table("users").select("tg_id", count="exact").gte("created_at", week_ago).execute()
        new_bets  = supabase.table("bets").select("id", count="exact").gte("created_at", week_ago).execute()
        new_wins  = supabase.table("bets").select("id", count="exact").eq("result", "win").gte("created_at", week_ago).execute()
        new_inv   = supabase.table("inventory").select("id", count="exact").gte("created_at", week_ago).execute()
        total_users = supabase.table("users").select("tg_id", count="exact").execute()
        pending_nft = supabase.table("inventory").select("id", count="exact").in_("status", ["waiting", "manual"]).execute()
        return {
            "status": "ok",
            "last_7_days": {
                "new_users":  new_users.count or 0,
                "new_bets":   new_bets.count or 0,
                "new_wins":   new_wins.count or 0,
                "new_inv":    new_inv.count or 0,
            },
            "totals": {
                "total_users":  total_users.count or 0,
                "pending_nft":  pending_nft.count or 0,
            },
            "gift_account":    GIFT_ACCOUNT_USERNAME,
            "gift_account_id": GIFT_ACCOUNT_TG_ID,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 29. Удалить пользователя (soft delete) ──────────────

@app.post("/admin/user/delete")
async def admin_delete_user(body: AdminUserActionBody):
    require_admin(body.init_data, body.tg_id)
    if body.target_tg_id in (ADMIN_TG_ID, OWNER_ID):
        raise HTTPException(403, "Нельзя удалить администратора.")
    try:
        supabase.table("users").update({"is_banned": True, "ban_reason": "deleted_by_admin"}).eq("tg_id", body.target_tg_id).execute()
        log_action(body.tg_id, "admin_delete_user", {"target": body.target_tg_id})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 30. Список полученных подарков (received_gifts) ─────

@app.get("/admin/received-gifts")
async def admin_received_gifts(init_data: str = "", tg_id: int = 0,
                               limit: int = 50, offset: int = 0):
    require_admin(init_data, tg_id)
    try:
        res = (
            supabase.table("received_gifts")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return {"status": "ok", "gifts": res.data or []}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 31. Проверить баланс звёзд юзербота ─────────────────

@app.get("/admin/userbot/balance")
async def admin_userbot_balance(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        client = await get_userbot()
        if not client:
            return {"status": "error", "balance": None, "error": "userbot unavailable"}
        from pyrogram.raw import functions as rawfn
        result = await client.invoke(rawfn.payments.GetStarsStatus(peer=await client.resolve_peer("me")))
        balance = getattr(result, "balance", None)
        return {"status": "ok", "balance": balance, "gift_account": GIFT_ACCOUNT_USERNAME}
    except Exception as e:
        return {"status": "error", "balance": None, "error": str(e)}


# ─── 32. Принудительный cron (доставка вручную) ──────────

@app.post("/admin/cron/run")
async def admin_run_cron(body: AdminActionBody):
    require_admin(body.init_data, body.tg_id)
    try:
        result = await cron_deliver.__wrapped__() if hasattr(cron_deliver, "__wrapped__") else None
        log_action(body.tg_id, "admin_run_cron", {})
        return {"status": "ok", "message": "Cron запущен вручную — смотри логи."}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── 33. Изменить аккаунт для получения подарков ─────────

class AdminGiftAccountBody(BaseModel):
    init_data: str
    tg_id: int
    username: str

@app.post("/admin/settings/gift-account")
async def admin_set_gift_account(body: AdminGiftAccountBody):
    require_admin(body.init_data, body.tg_id)
    try:
        username = body.username if body.username.startswith("@") else "@" + body.username
        existing = supabase.table("settings").select("key").eq("key", "ring_account").execute()
        if existing.data:
            supabase.table("settings").update({"value": username}).eq("key", "ring_account").execute()
        else:
            supabase.table("settings").insert({"key": "ring_account", "value": username}).execute()
        log_action(body.tg_id, "admin_set_gift_account", {"username": username})
        return {"status": "ok", "ring_account": username}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 34. Статистика по выигрышным спинам (аналитика) ─────

@app.get("/admin/analytics/spins")
async def admin_spin_analytics(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        wins_3 = supabase.table("bets").select("id", count="exact").eq("result", "win").eq("spin_number", 3).execute()
        wins_4 = supabase.table("bets").select("id", count="exact").eq("result", "win").eq("spin_number", 4).execute()
        wins_5 = supabase.table("bets").select("id", count="exact").eq("result", "win").eq("spin_number", 5).execute()
        total  = supabase.table("bets").select("id", count="exact").execute()
        wins   = supabase.table("bets").select("id", count="exact").eq("result", "win").execute()
        return {
            "status": "ok",
            "total_bets": total.count or 0,
            "total_wins": wins.count or 0,
            "wins_on_spin_3": wins_3.count or 0,
            "wins_on_spin_4": wins_4.count or 0,
            "wins_on_spin_5": wins_5.count or 0,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 35. Экспорт пользователей (CSV-формат JSON) ─────────

@app.get("/admin/export/users")
async def admin_export_users(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    try:
        res = supabase.table("users").select("tg_id,username,first_name,total_cycles,cycle_spin,stars_balance,referral_count,created_at,is_banned").execute()
        return {"status": "ok", "data": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 36. Проверка здоровья всех компонентов ──────────────

@app.get("/admin/system/health")
async def admin_system_health(init_data: str = "", tg_id: int = 0):
    require_admin(init_data, tg_id)
    results = {}
    try:
        supabase.table("users").select("tg_id").limit(1).execute()
        results["supabase"] = "ok"
    except Exception as e:
        results["supabase"] = f"error: {e}"
    try:
        bot_info = await tg_api("getMe", {})
        results["telegram_bot"] = f"ok (@{bot_info.get('result', {}).get('username', '?')})"
    except Exception as e:
        results["telegram_bot"] = f"error: {e}"
    try:
        client = await get_userbot()
        results["userbot"] = "ok" if client else "unavailable"
    except Exception as e:
        results["userbot"] = f"error: {e}"
    results["gift_account"] = GIFT_ACCOUNT_USERNAME
    results["gift_account_id"] = GIFT_ACCOUNT_TG_ID
    return {"status": "ok", "components": results}


# ─── LEADERBOARD ─────────────────────────────────────────

@app.get("/leaderboard")
async def get_leaderboard(init_data: str = ""):
    try:
        res = (
            supabase.table("users")
            .select("tg_id, username, first_name, total_cycles, cycle_spin")
            .order("total_cycles", desc=True)
            .limit(100)
            .execute()
        )
        players = []
        for u in (res.data or []):
            total_spins = (u.get("total_cycles") or 0) * 5 + (u.get("cycle_spin") or 0)
            players.append({
                "tg_id":       u.get("tg_id"),
                "username":    u.get("username") or "",
                "first_name":  u.get("first_name") or "",
                "total_spins": total_spins,
                "photo_url":   None,  # фото грузит фронт через tgUser или profile-photo
            })
        players.sort(key=lambda x: x["total_spins"], reverse=True)
        return {"status": "ok", "players": players}
    except Exception as e:
        log.error(f"leaderboard error: {e}")
        return {"status": "ok", "players": []}


# ─── WEBHOOK (Telegram Bot) ───────────────────────────────

@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """
    Webhook для служебных обновлений бота.
    В новой механике не используется для оплаты.
    Используется только для команд бота (например /start).
    """
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid webhook secret")

    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    message = data.get("message", {})
    msg_text = message.get("text", "")
    if msg_text.startswith("/start"):
        tg_id = message.get("from", {}).get("id")
        parts = msg_text.split()
        ref_param = parts[1] if len(parts) > 1 else ""
        referrer_id_from_param = None
        # Поддерживаем форматы: inviteCode<id> и ref_<id>
        if tg_id and ref_param.startswith("inviteCode"):
            try:
                referrer_id_from_param = int(ref_param[len("inviteCode"):])
                if referrer_id_from_param != tg_id:
                    _apply_referral(tg_id, referrer_id_from_param)
            except Exception as e:
                log.warning(f"webhook inviteCode ref error: {e}")
        elif tg_id and ref_param.startswith("ref_"):
            try:
                referrer_id_from_param = int(ref_param[4:])
                if referrer_id_from_param != tg_id:
                    _apply_referral(tg_id, referrer_id_from_param)
            except Exception as e:
                log.warning(f"webhook ref error: {e}")

        if tg_id:
            startapp_param = ref_param if (ref_param.startswith("inviteCode") or ref_param.startswith("ref_")) else ""
            app_url = FRONTEND_URL + (f"?startapp={startapp_param}" if startapp_param else "")

            text = (
                "<b>LEONARDO GAME</b>"
            )

            reply_markup = {
                "inline_keyboard": [
                    [{"text": "Открыть игру", "web_app": {"url": app_url}}],
                    [{"text": "Наш канал", "url": "https://t.me/leonardo_public"}]
                ]
            }

            import json as _json
            await tg_api("sendMessage", {
                "chat_id": tg_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": _json.dumps(reply_markup)
            })

    return {"ok": True}







# ══════════════════════════════════════════════════════════
# 13. ЗАПУСК
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)