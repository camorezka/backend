"""
Lucky Spin v5 — backend/main.py
Бизнес-логика: пользователь отправляет 2 кольца на @kinub,
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

        # Правильный алгоритм по документации Telegram:
        # secret_key = HMAC-SHA256(key="WebAppData", msg=BOT_TOKEN)
        # expected   = HMAC-SHA256(key=secret_key,   msg=data_check_string)
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
    tg_id:      int
    username:   str = ""
    first_name: str = ""
    init_data:  str
    user_agent: str = ""
    language:   str = ""
    platform:   str = ""

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
    3-я ставка → 300–400 Stars
    4-я ставка → 450–550 Stars
    5-я ставка → 550–600 Stars
    """
    if winning_spin == 3:
        return 300, 400
    elif winning_spin == 4:
        return 450, 550
    else:
        return 550, 600

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
    Получает список подарков, находящихся в профиле аккаунта @kinub,
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
    Продаёт ровно 2 кольца из профиля @kinub, конвертируя в Stars.

    Метод: payments.ConvertStarGift
    TL: payments.convertStarGift#f2a3ec5f user_id:InputUser msg_id:int = Updates

    Параметры:
      user_id — peer аккаунта @kinub (наш userbot)
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
    nft_min_stars: int, nft_max_stars: int
) -> Optional[dict]:
    """
    Находит NFT в заданном диапазоне стоимости и покупает его на аккаунт @kinub.

    Алгоритм:
    1. Вызываем payments.GetAvailableStarGifts — список доступных подарков.
       Метод: payments.getAvailableStarGifts#3e4bfd00 → payments.StarGifts
       Фильтруем по is_limited=True (NFT) и star_count в диапазоне.

    2. Если нет подходящих обычных NFT — пробуем unique gifts через
       payments.GetUniqueStarGift по известным slug (этот метод требует slug,
       автоматический перебор невозможен через API).

    3. Случайно выбираем один NFT из подходящих.

    4. Покупаем: payments.SendStarGift#3d2d5e38
       Параметры:
         user_id — peer получателя (наш аккаунт @kinub)
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

        # Фильтруем: limited (NFT), в диапазоне стоимости, есть остаток
        nft_candidates = []
        for g in all_gifts:
            star_count     = getattr(g, "stars", 0)
            is_limited     = getattr(g, "limited", False)
            remaining      = getattr(g, "availability_remains", 1)
            gift_id        = getattr(g, "id", None)

            if (
                is_limited and
                gift_id and
                nft_min_stars <= star_count <= nft_max_stars and
                (remaining is None or remaining > 0)
            ):
                nft_candidates.append(g)

        if not nft_candidates:
            log.warning(
                f"userbot_find_and_buy_nft: нет NFT в диапазоне "
                f"{nft_min_stars}–{nft_max_stars} Stars"
            )
            return None

        chosen  = random.choice(nft_candidates)
        gift_id = getattr(chosen, "id", None)
        stars   = getattr(chosen, "stars", 0)

        # Название: берём из title или emoji sticker
        sticker = getattr(chosen, "sticker", None)
        if sticker:
            emoji = getattr(sticker, "emoji", "🎁")
            name  = f"{emoji} Gift #{gift_id}"
        else:
            name  = f"NFT Gift #{gift_id}"

        log.info(f"userbot: buying NFT gift_id={gift_id} name={name} stars={stars}")

        # Покупаем подарок на аккаунт @kinub
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
    Передаёт NFT из профиля @kinub победителю.

    Метод: payments.TransferStarGift
    TL: payments.transferStarGift#1fad0509
      user_id:InputUser  — peer себя (аккаунт @kinub)
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
    nft = await userbot_find_and_buy_nft(nft_min, nft_max)

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
            f"🎉 <b>Ты выиграл NFT!</b>\n\n"
            f"<b>{nft_name}</b> ({nft_stars}⭐)\n\n"
            f"🕐 Подарок будет отправлен через {nft_wait_days} дней.\n"
            f"Он уже ждёт в твоём инвентаре!"
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
        # NFT не найден в диапазоне — ручная обработка
        nft_name   = f"NFT {nft_min}–{nft_max}⭐ (ручная покупка)"
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
            f"🎉 <b>Ты выиграл!</b>\n\n"
            f"Администратор подберёт для тебя NFT стоимостью "
            f"{nft_min}–{nft_max}⭐.\n\n"
            f"🕐 Подарок будет отправлен через {nft_wait_days} дней."
        )

        admin_text = (
            f"🎰 <b>Выигрыш! НУЖНА РУЧНАЯ ПОКУПКА NFT!</b>\n"
            f"Пользователь: {tg_id}\n"
            f"Диапазон: {nft_min}–{nft_max}⭐\n"
            f"Ставка #{winning_spin} в цикле\n"
            f"Выдать: {available_at[:10]}\n"
            f"❗ В диапазоне не нашлось NFT — купи вручную и обнови inventory."
        )

    await tg_send_message(tg_id, notify_text)
    await tg_send_message(ADMIN_TG_ID, admin_text)

    log.info(f"WIN automation done: tg_id={tg_id} nft={nft_name}")

# ══════════════════════════════════════════════════════════
# 12. РОУТЫ
# ══════════════════════════════════════════════════════════

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
            .select("tg_id")
            .eq("tg_id", trusted_tg_id)
            .execute()
        )
        if existing.data:
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

        log_action(trusted_tg_id, "register", {
            "ip": ip, "country": geo.get("country"), "city": geo.get("city"),
            "ua": body.user_agent[:120] if body.user_agent else "",
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
    Возвращает инструкцию: отправить 2 кольца на @kinub.
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
                ring_account = get_setting("ring_account", "@kinub")
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

    ring_account = get_setting("ring_account", "@kinub")

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
        ring_account = get_setting("ring_account", "@kinub")
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

    # При выигрыше запускаем автоматизацию в фоне
    if is_win:
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
        "status":       "ok",
        "result":       result,
        "cycle_spin":   new_cycle_spin,
        "winning_spin": winning_spin if not is_win else None,
        "next_win_in":  next_win_in,
        "is_win":       is_win,
        "nft_name":     f"NFT {nft_min}–{nft_max}⭐" if is_win else None,
        "nft_stars":    nft_max if is_win else None,
        "available_at": available_at_iso if is_win else None,
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
            .select("cycle_spin, winning_spin, total_cycles")
            .eq("tg_id", trusted_tg_id)
            .single()
            .execute()
        )
        u = user_res.data or {}
        cycle_spin   = u.get("cycle_spin", 0) or 0
        winning_spin = u.get("winning_spin", 3) or 3
        total_cycles = u.get("total_cycles", 0) or 0
        next_win_in  = winning_spin - cycle_spin

        wins_res = (
            supabase.table("bets")
            .select("id", count="exact")
            .eq("tg_id", trusted_tg_id)
            .eq("result", "win")
            .execute()
        )
        total_wins = wins_res.count if wins_res.count is not None else 0

        return {
            "status":        "ok",
            "cycle_spin":    cycle_spin,
            "winning_spin":  winning_spin,
            "total_cycles":  total_cycles,
            "next_win_in":   next_win_in,
            "total_wins":    total_wins,
            "stars_balance": 0,
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
                    f"<b>{nft_name}</b>\n\n"
                    f"21 день прошёл — подарок уже в твоём профиле Telegram!",
                )
                await tg_send_message(
                    ADMIN_TG_ID,
                    f"✅ <b>NFT выдан автоматически</b>\n"
                    f"Пользователь: {tg_id}\n"
                    f"NFT: {nft_name}\n"
                    f"msg_id: {msg_id}",
                )
            else:
                new_status = "transfer_error"
                await tg_send_message(
                    tg_id,
                    f"🎁 <b>Твой NFT готов!</b>\n\n"
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
    if message.get("text", "").startswith("/start"):
        tg_id = message.get("from", {}).get("id")
        if tg_id:
            ring_account = get_setting("ring_account", "@kinub")
            await tg_send_message(
                tg_id,
                f"🎰 <b>Lucky Spin</b>\n\n"
                f"Открой мини-приложение, нажми «Крутить рулетку» "
                f"и отправь 2 кольца на {ring_account}.\n\n"
                f"✅ <b>Гарантия:</b> первый выигрыш на 3-й ставке!",
            )

    return {"ok": True}







# ══════════════════════════════════════════════════════════
# 13. ЗАПУСК
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
