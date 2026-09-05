# -*- coding: utf-8 -*-
"""Telegram Gifts for FunPay Cardinal.

Полноценный плагин автоматической выдачи Telegram-подарков.

Главный принцип выбора подарка:
1. Явная привязка LOT_ID -> GIFT_ID из lot_bindings.json.
2. ID:... в ПОЛНОМ описании самого лота FunPay.
3. Только как запасной вариант — название лота.

Это важно: название заказа больше не имеет приоритета над привязкой.
Поэтому лот "Мишка романтика" не сможет случайно получить ID другого подарка,
если у него есть правильная привязка.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata

from FunPayAPI.types import MessageTypes


# ============================================================
# CARDINAL PLUGIN DATA
# ============================================================

NAME = "Telegram Gifts"
VERSION = "2.1.0"
DESCRIPTION = "Автоматическая выдача Telegram-подарков после покупки на FunPay."
CREDITS = "@podarckov"
UUID = "7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102"
SETTINGS_PAGE = False
BIND_TO_DELETE = None


# ============================================================
# TELEGRAM API
# ============================================================

API_ID = 32493973
API_HASH = "e470a990253e9502835f62cc5958aed7"
SESSION_NAME = "telegram_gifts"
TELETHON_PACKAGE = "telethon>=1.36,<2"


# ============================================================
# GIFTS
# ============================================================

GIFTS = {
    5956217000635139069: "Новогодний мишка",
    5922558454332916696: "Елочка",
    5800655655995968830: "Мишка на 14 февраля",
    5801108895304779062: "Сердце",
    5866352046986232958: "Любовный мишка",
    5893356958802511476: "Мишка-лепрекон",
    5935895822435615975: "Мишка-клоун",
    5969796561943660080: "Мишка-зайчик",
    5974210632977745012: "Футбольный мишка",
    6046178578163303744: "Мишка-террорист",
}

GIFT_MESSAGE = ""


# ============================================================
# PATHS / LOGGER
# ============================================================

logger = logging.getLogger("telegram_gifts")

PLUGIN_DIR = os.path.join("storage", "plugins", UUID)
ORDERS_FILE = os.path.join(PLUGIN_DIR, "orders.json")
LOT_BINDINGS_FILE = os.path.join(PLUGIN_DIR, "lot_bindings.json")
SESSION_FILE = os.path.join(PLUGIN_DIR, SESSION_NAME)

_orders_lock = threading.RLock()
_telethon_install_lock = threading.Lock()
_orders: dict[str, dict] = {}
_lot_bindings: dict[str, dict] = {}
_cardinal = None
_worker = None


# ============================================================
# STATUSES
# ============================================================

STATUS_USERNAME = "await_username"
STATUS_CONFIRM = "await_confirm"
STATUS_SENDING = "sending"
STATUS_DELIVERED = "delivered"
STATUS_REFUNDED = "refunded"
STATUS_ERROR = "error"

ACTIVE_STATUSES = (
    STATUS_USERNAME,
    STATUS_CONFIRM,
    STATUS_SENDING,
    STATUS_ERROR,
)


# ============================================================
# DEPENDENCY
# ============================================================

def ensure_telethon() -> bool:
    if importlib.util.find_spec("telethon") is not None:
        return True

    with _telethon_install_lock:
        if importlib.util.find_spec("telethon") is not None:
            return True
        logger.warning("Telegram Gifts: Telethon не найден, устанавливаю зависимость...")
        try:
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                TElethon_PACKAGE if False else TELETHON_PACKAGE,
            ])
        except Exception:
            logger.exception("Telegram Gifts: не удалось установить Telethon")
            return False

    return importlib.util.find_spec("telethon") is not None


# ============================================================
# JSON STATE
# ============================================================

def _load_json(path: str, default):
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except Exception:
        logger.exception("Telegram Gifts: ошибка чтения %s", path)
        return default


def _save_json(path: str, value) -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=4)
        os.replace(tmp, path)
    except Exception:
        logger.exception("Telegram Gifts: ошибка сохранения %s", path)


def load_state() -> None:
    global _orders, _lot_bindings
    with _orders_lock:
        orders = _load_json(ORDERS_FILE, {})
        bindings = _load_json(LOT_BINDINGS_FILE, {})
        _orders = orders if isinstance(orders, dict) else {}
        _lot_bindings = bindings if isinstance(bindings, dict) else {}
    logger.info("Telegram Gifts: загружено заказов: %s, привязок лотов: %s", len(_orders), len(_lot_bindings))


def save_orders() -> None:
    with _orders_lock:
        data = dict(_orders)
    _save_json(ORDERS_FILE, data)


def save_bindings() -> None:
    with _orders_lock:
        data = dict(_lot_bindings)
    _save_json(LOT_BINDINGS_FILE, data)


def update_order(order_id, **kwargs) -> None:
    oid = str(order_id)
    with _orders_lock:
        if oid not in _orders:
            return
        _orders[oid].update(kwargs)
    save_orders()


# ============================================================
# TEXT / INPUT HELPERS
# ============================================================

def clean_text(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "").strip()


def normalize_text(value) -> str:
    return clean_text(value).casefold().replace("ё", "е")


def is_refund_command(value) -> bool:
    return clean_text(value).casefold().replace(" ", "") == "!возврат"


def is_plus(value) -> bool:
    return clean_text(value) in ("+", "＋")


USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
ID_RE = re.compile(r"(?i)\bID\s*:\s*(\d{10,25})\b")


def normalize_username(value):
    text = clean_text(value)
    if not USERNAME_RE.fullmatch(text):
        return None
    return text if text.startswith("@") else "@" + text


def extract_gift_id(value):
    match = ID_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def gift_name(gift_id) -> str:
    gid = int(gift_id)
    return GIFTS.get(gid, f"Подарок {gid}")


def find_gift_by_name(value):
    text = normalize_text(value)
    if not text:
        return None
    for gid, name in GIFTS.items():
        if text == normalize_text(name):
            return gid, name
    for gid, name in GIFTS.items():
        if normalize_text(name) in text:
            return gid, name
    return None


# ============================================================
# FUNPAY HELPERS
# ============================================================

def send_funpay_message(chat_id, text) -> bool:
    if _cardinal is None or chat_id is None:
        return False
    try:
        _cardinal.send_message(chat_id, text)
        return True
    except Exception:
        logger.exception("Telegram Gifts: не удалось отправить сообщение в FunPay chat=%s", chat_id)
        return False


def get_full_order(c, event):
    try:
        return c.account.get_order(event.order.id)
    except Exception:
        logger.debug("Telegram Gifts: get_order не сработал", exc_info=True)
        return event.order


def find_order_lot(c, event, order):
    """Определяет реальный LOT_ID через профиль, а не через текст покупателя."""
    direct = getattr(order, "lot_id", None)
    if direct is not None:
        return str(direct)

    direct = getattr(event.order, "lot_id", None)
    if direct is not None:
        return str(direct)

    subcategory = getattr(event.order, "subcategory", None) or getattr(order, "subcategory", None)
    if subcategory is None:
        return None

    description = clean_text(getattr(event.order, "description", None) or "")
    if not description:
        return None

    try:
        grouped = c.profile.get_sorted_lots(2)
        lots = grouped.get(subcategory, {})
        candidates = []
        for lot in lots.values():
            parts = [
                clean_text(getattr(lot, "server", None)),
                clean_text(getattr(lot, "side", None)),
                clean_text(getattr(lot, "description", None)),
            ]
            signature = ", ".join(x for x in parts if x)
            if signature and signature in description:
                candidates.append((len(signature), lot))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return str(candidates[0][1].id)
    except Exception:
        logger.exception("Telegram Gifts: не удалось определить LOT_ID по профилю")
    return None


def fetch_lot_description(c, lot_id):
    """Читает настоящее подробное описание лота FunPay."""
    if lot_id is None:
        return ""

    try:
        page = c.account.get_lot_page(int(lot_id))
        if page:
            text = getattr(page, "full_description", None)
            if text:
                return clean_text(text)
    except Exception:
        logger.debug("Telegram Gifts: get_lot_page(%s) не сработал", lot_id, exc_info=True)

    try:
        fields = c.account.get_lot_fields(int(lot_id))
        for attr in ("description_ru", "description_en", "description"):
            text = getattr(fields, attr, None)
            if text:
                return clean_text(text)
    except Exception:
        logger.debug("Telegram Gifts: get_lot_fields(%s) не сработал", lot_id, exc_info=True)

    return ""


def resolve_gift(c, event, order):
    """Возвращает (gift_id, gift_name, lot_id, source)."""
    lot_id = find_order_lot(c, event, order)

    # 1. Явная привязка — самый высокий приоритет.
    if lot_id is not None:
        with _orders_lock:
            binding = _lot_bindings.get(str(lot_id))
        if isinstance(binding, dict) and binding.get("gift_id") is not None:
            gid = int(binding["gift_id"])
            return gid, gift_name(gid), str(lot_id), "binding"

    # 2. ID в подробном описании реального лота.
    detailed = fetch_lot_description(c, lot_id)
    gid = extract_gift_id(detailed)
    if gid is not None:
        return gid, gift_name(gid), lot_id, "description"

    # 3. ID в данных заказа — запасной вариант.
    for text in (
        getattr(order, "full_description", None),
        getattr(order, "description", None),
        getattr(order, "short_description", None),
        getattr(event.order, "description", None),
    ):
        gid = extract_gift_id(text)
        if gid is not None:
            return gid, gift_name(gid), lot_id, "order_description"

    # 4. Название — только fallback.
    for text in (
        getattr(order, "short_description", None),
        getattr(order, "title", None),
        getattr(event.order, "description", None),
    ):
        found = find_gift_by_name(text)
        if found:
            return found[0], found[1], lot_id, "name_fallback"

    return None


# ============================================================
# ORDER LOOKUP
# ============================================================

def find_order_by_message(msg):
    chat_id = getattr(msg, "chat_id", None)
    author_id = getattr(msg, "author_id", None)
    author = clean_text(getattr(msg, "author", None)).lstrip("@").casefold()

    with _orders_lock:
        if chat_id is not None:
            candidates = [
                (oid, order)
                for oid, order in _orders.items()
                if str(order.get("chat_id")) == str(chat_id)
                and order.get("status") in ACTIVE_STATUSES
            ]
            if candidates:
                # Самый свежий заказ в чате.
                candidates.sort(key=lambda x: float(x[1].get("created_at", 0)), reverse=True)
                return candidates[0]

        # Запасной вариант для Cardinal-конфигураций, где chat_id события отсутствует.
        if author:
            for oid, order in sorted(_orders.items(), key=lambda x: float(x[1].get("created_at", 0)), reverse=True):
                if order.get("status") not in ACTIVE_STATUSES:
                    continue
                buyer = clean_text(order.get("buyer", "")).lstrip("@").casefold()
                if buyer and buyer == author:
                    return oid, order

        # Если есть author_id, не позволяем другому пользователю управлять заказом.
        if author_id is not None:
            for oid, order in _orders.items():
                if order.get("status") in ACTIVE_STATUSES and order.get("buyer_id") is not None:
                    if str(order.get("buyer_id")) == str(author_id):
                        return oid, order

    return None, None


# ============================================================
# REFUND
# ============================================================

def refund_order(order_id) -> bool:
    oid = str(order_id)
    with _orders_lock:
        order = _orders.get(oid)
    if not order:
        return False

    status = order.get("status")
    if status in (STATUS_DELIVERED, STATUS_REFUNDED, STATUS_SENDING):
        if status == STATUS_DELIVERED:
            send_funpay_message(order.get("chat_id"), "ℹ️ Подарок уже был успешно отправлен. Возврат через !возврат после выдачи недоступен.")
        elif status == STATUS_SENDING:
            send_funpay_message(order.get("chat_id"), "⏳ Подарок уже отправляется. Дождитесь результата текущей попытки.")
        return False

    try:
        _cardinal.account.refund(oid)
        update_order(oid, status=STATUS_REFUNDED, last_error=None, refunded_at=time.time())
        send_funpay_message(order.get("chat_id"), "❌ Заказ отменён.\n\nСредства возвращены.")
        logger.info("Telegram Gifts: возврат выполнен order=%s", oid)
        return True
    except Exception as exc:
        logger.exception("Telegram Gifts: ошибка возврата order=%s", oid)
        update_order(oid, last_error=str(exc))
        send_funpay_message(order.get("chat_id"), "⚠️ Не удалось автоматически оформить возврат.\n\nПродавец обработает возврат вручную.")
        return False


# ============================================================
# TELEGRAM WORKER
# ============================================================

class TelegramGiftWorker:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.loop = None
        self.thread = None
        self.queue = None
        self.client = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.telethon_available = False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._thread_main, name="TelegramGiftWorker", daemon=True)
        self.thread.start()
        self.ready.wait(timeout=10)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.ready.set()
        try:
            self.loop.run_until_complete(self._worker_loop())
        except Exception:
            logger.exception("Telegram Gifts: критическая ошибка worker")
        finally:
            try:
                self.loop.run_until_complete(self._disconnect())
            except Exception:
                pass
            try:
                self.loop.close()
            except Exception:
                pass

    async def _worker_loop(self):
        while not self.stop_event.is_set():
            try:
                job = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process_job(job)
            except Exception:
                logger.exception("Telegram Gifts: ошибка обработки job")

    async def _process_job(self, job):
        oid = str(job["order_id"])
        with _orders_lock:
            order = dict(_orders.get(oid, {}))
        if not order or order.get("status") != STATUS_SENDING:
            return

        recipient = order.get("recipient")
        gift_id = int(order.get("gift_id"))
        gift_name_value = order.get("gift_name") or gift_name(gift_id)

        try:
            await self._ensure_client()
        except Exception as exc:
            logger.exception("Telegram Gifts: Telegram client недоступен")
            update_order(oid, status=STATUS_ERROR, last_error=str(exc))
            send_funpay_message(order.get("chat_id"), "❌ Не удалось подключиться к Telegram.\n\nПодарок не отправлен. Заказ не считается выполненным.")
            return

        try:
            user = await self.client.get_entity(recipient)
            input_peer = await self.client.get_input_entity(user)
        except Exception as exc:
            await self._handle_telegram_exception(oid, order, exc)
            return

        try:
            from telethon.tl.functions.payments import GetPaymentFormRequest, SendStarsFormRequest
            from telethon.tl.types import InputInvoiceStarGift, TextWithEntities
        except Exception as exc:
            logger.exception("Telegram Gifts: ошибка импорта Telethon API")
            update_order(oid, status=STATUS_ERROR, last_error=str(exc))
            send_funpay_message(order.get("chat_id"), "❌ Ошибка библиотеки Telegram.\n\nПодарок не отправлен.")
            return

        message = TextWithEntities(text=GIFT_MESSAGE, entities=[]) if GIFT_MESSAGE else None

        try:
            invoice = InputInvoiceStarGift(
                peer=input_peer,
                gift_id=gift_id,
                message=message,
            )
            logger.info("Telegram Gifts: получаю payment form order=%s lot=%s gift=%s recipient=%s", oid, order.get("lot_id"), gift_id, recipient)
            form = await self.client(GetPaymentFormRequest(invoice=invoice))
        except Exception as exc:
            await self._handle_telegram_exception(oid, order, exc)
            return

        try:
            logger.info("Telegram Gifts: отправляю подарок order=%s gift=%s recipient=%s", oid, gift_id, recipient)
            await self.client(SendStarsFormRequest(form_id=form.form_id, invoice=invoice))
        except Exception as exc:
            await self._handle_telegram_exception(oid, order, exc)
            return

        update_order(oid, status=STATUS_DELIVERED, last_error=None, delivered_at=time.time())
        send_funpay_message(order.get("chat_id"), f"✅ Подарок «{gift_name_value}» успешно отправлен на {recipient}!\n\nСпасибо за покупку! Пожалуйста, подтвердите получение заказа и оставьте отзыв ❤️")
        logger.info("Telegram Gifts: подарок успешно отправлен order=%s gift=%s", oid, gift_id)

    async def _handle_telegram_exception(self, oid, order, exc):
        error = str(exc)
        upper = error.upper()
        logger.error("Telegram Gifts: Telegram RPC error order=%s error=%s", oid, error)

        try:
            from telethon import errors
            if isinstance(exc, errors.FloodWaitError):
                seconds = int(exc.seconds)
                if seconds <= 300:
                    send_funpay_message(order.get("chat_id"), f"⏳ Telegram временно ограничил отправку.\n\nПовторная попытка через {seconds} сек.")
                    await asyncio.sleep(seconds)
                    with _orders_lock:
                        current = _orders.get(oid)
                    if current and current.get("status") == STATUS_SENDING:
                        await self._process_job({"order_id": oid})
                    return
                update_order(oid, status=STATUS_ERROR, last_error=f"FloodWait {seconds}")
                send_funpay_message(order.get("chat_id"), f"❌ Telegram временно ограничил отправку подарков на {seconds} сек.\n\nЗаказ пока не считается выполненным.")
                return
        except Exception:
            pass

        if (
            "BALANCE_TOO_LOW" in upper
            or "STARS_TOO_LOW" in upper
            or ("BALANCE" in upper and "STAR" in upper)
            or "NOT_ENOUGH" in upper and "STAR" in upper
        ):
            update_order(oid, status=STATUS_ERROR, last_error=error)
            send_funpay_message(order.get("chat_id"), "❌ Не удалось отправить подарок.\n\n⭐ Недостаточно Stars на Telegram-аккаунте продавца.\n\nПополните Stars и отправьте «+», чтобы повторить отправку.")
            return

        if "STARGIFT_NOT_FOUND" in upper or "STARGIFT_INVALID" in upper:
            update_order(oid, status=STATUS_ERROR, last_error=error)
            send_funpay_message(order.get("chat_id"), "❌ Telegram не нашёл указанный подарок.\n\nЗаказ не считается выполненным.")
            return

        if "STARGIFT_USAGE_LIMITED" in upper:
            update_order(oid, status=STATUS_ERROR, last_error=error)
            send_funpay_message(order.get("chat_id"), "❌ Этот подарок сейчас нельзя отправить из-за ограничения Telegram.\n\nЗаказ не считается выполненным.")
            return

        if any(x in upper for x in ("USERNAME_INVALID", "USERNAME_NOT_OCCUPIED", "USER_NOT_FOUND", "PEER_ID_INVALID")):
            update_order(oid, status=STATUS_USERNAME, last_error=error)
            send_funpay_message(order.get("chat_id"), "❌ Telegram не нашёл указанного пользователя.\n\nОтправьте другой Telegram username.")
            return

        if any(x in upper for x in ("CHAT_WRITE_FORBIDDEN", "USER_IS_BLOCKED", "USER_RESTRICTED")):
            update_order(oid, status=STATUS_ERROR, last_error=error)
            send_funpay_message(order.get("chat_id"), "❌ Telegram не разрешил отправить подарок этому пользователю.\n\nОтправьте другой username и затем «+».")
            return

        update_order(oid, status=STATUS_ERROR, last_error=error)
        send_funpay_message(order.get("chat_id"), "❌ Telegram вернул ошибку при отправке подарка.\n\nЗаказ не считается выполненным.\nМожно отправить «+» для повторной попытки или !возврат для отмены.")

    async def _ensure_client(self):
        if not self.telethon_available:
            self.telethon_available = ensure_telethon()
            if not self.telethon_available:
                raise RuntimeError("Telethon не установлен")

        from telethon import TelegramClient
        if self.client is None:
            self.client = TelegramClient(
                SESSION_FILE,
                API_ID,
                API_HASH,
                device_model="FunPay Cardinal Telegram Gifts",
                system_version="Linux",
                app_version=VERSION,
                lang_code="en",
                system_lang_code="en-US",
            )
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            await self._authorize()

    async def _authorize(self):
        if not API_ID or not API_HASH:
            raise RuntimeError("Не указаны API_ID / API_HASH")

        from telethon import errors
        from telethon.tl.functions.auth import SendCodeRequest, SignInRequest, CheckPasswordRequest
        from telethon.tl.functions.account import GetPasswordRequest
        from telethon.tl.types import CodeSettings
        from telethon.password import compute_check

        print("\n" + "=" * 60)
        print(" TELEGRAM GIFTS — ПЕРВАЯ АВТОРИЗАЦИЯ")
        print("=" * 60)
        phone = input("Введи номер Telegram (например +79991234567): ").strip()
        result = await self.client(SendCodeRequest(
            phone_number=phone,
            api_id=API_ID,
            api_hash=API_HASH,
            settings=CodeSettings(allow_flashcall=False, current_number=False, allow_app_hash=False),
        ))
        print("Telegram отправил код через:", type(result.type).__name__)
        code = input("Введи код из Telegram: ").strip()
        try:
            await self.client(SignInRequest(phone_number=phone, phone_code_hash=result.phone_code_hash, phone_code=code))
        except errors.SessionPasswordNeededError:
            password = input("Введи облачный пароль Telegram (2FA): ").strip()
            pwd = await self.client(GetPasswordRequest())
            await self.client(CheckPasswordRequest(password=compute_check(pwd, password)))
        me = await self.client.get_me()
        print(f"Telegram авторизован: {me.first_name or ''} (id={me.id})")

    async def _disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    def submit(self, order_id):
        if not self.loop or not self.queue:
            logger.error("Telegram Gifts: worker ещё не запущен")
            return None
        return asyncio.run_coroutine_threadsafe(self.queue.put({"order_id": str(order_id)}), self.loop)

    def run_coro(self, coro):
        if not self.loop:
            raise RuntimeError("Worker loop не запущен")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        self.stop_event.set()
        if self.loop:
            try:
                asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
            except Exception:
                pass

    async def get_stars_status(self):
        await self._ensure_client()
        from telethon.tl.functions.payments import GetStarsStatusRequest
        from telethon.tl.types import InputPeerSelf
        return await self.client(GetStarsStatusRequest(peer=InputPeerSelf()))

    async def account_info(self):
        await self._ensure_client()
        me = await self.client.get_me()
        status = await self.get_stars_status()
        balance = getattr(status, "balance", None)
        amount = getattr(balance, "amount", 0) or 0
        name = " ".join(x for x in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if x) or "—"
        username = "@" + me.username if getattr(me, "username", None) else "нет username"
        return (
            "🎁 <b>Telegram Gifts — аккаунт</b>\n\n"
            f"👤 Имя: <code>{html.escape(name)}</code>\n"
            f"🔗 Username: <code>{html.escape(username)}</code>\n"
            f"🆔 ID: <code>{me.id}</code>\n"
            f"⭐ Stars: <b>{int(amount)}</b>"
        )


# ============================================================
# NEW ORDER
# ============================================================

def new_order_handler(c, e):
    try:
        oid = str(e.order.id)
        with _orders_lock:
            if oid in _orders:
                return

        order = get_full_order(c, e)
        resolved = resolve_gift(c, e, order)
        if not resolved:
            logger.info("Telegram Gifts: заказ %s не распознан как Telegram Gift", oid)
            return

        gid, name, lot_id, source = resolved
        chat_id = getattr(e.order, "chat_id", None) or getattr(order, "chat_id", None)
        buyer = getattr(order, "buyer_username", None) or getattr(e.order, "buyer_username", None) or ""
        buyer_id = getattr(order, "buyer_id", None) or getattr(e.order, "buyer_id", None)

        if not chat_id and buyer:
            try:
                chat = c.account.get_chat_by_name(buyer, True)
                chat_id = chat.id if chat else None
            except Exception:
                logger.debug("Telegram Gifts: не удалось получить чат покупателя", exc_info=True)

        if chat_id is None:
            logger.error("Telegram Gifts: у заказа %s отсутствует chat_id", oid)
            return

        amount = getattr(order, "amount", None) or 1
        try:
            amount = max(1, int(amount))
        except Exception:
            amount = 1

        record = {
            "order_id": oid,
            "gift_id": int(gid),
            "gift_name": name,
            "lot_id": lot_id,
            "gift_source": source,
            "buyer": buyer,
            "buyer_id": buyer_id,
            "chat_id": chat_id,
            "recipient": None,
            "status": STATUS_USERNAME,
            "last_error": None,
            "amount": amount,
            "created_at": time.time(),
        }
        with _orders_lock:
            _orders[oid] = record
        save_orders()

        send_funpay_message(
            chat_id,
            f"🎁 Спасибо за покупку!\n\nПодарок: «{name}»\nКоличество: {amount}\n\n"
            "Отправьте ваш Telegram username, куда отправить подарок.\n\n"
            "Пример: @username\n\n"
            "❗ Для отмены заказа отправьте: !возврат"
        )
        logger.info("Telegram Gifts: заказ=%s lot=%s gift=%s source=%s", oid, lot_id, gid, source)
    except Exception:
        logger.exception("Telegram Gifts: ошибка обработки нового заказа")


# ============================================================
# NEW MESSAGE
# ============================================================

def message_handler(c, e):
    try:
        msg = e.message
        if getattr(msg, "type", None) != MessageTypes.NON_SYSTEM:
            return

        author_id = getattr(msg, "author_id", None)
        if author_id is not None and str(author_id) == str(c.account.id):
            return

        text = clean_text(getattr(msg, "text", None) or "")
        if not text:
            return

        oid, order = find_order_by_message(msg)
        if not order:
            return

        stored_buyer_id = order.get("buyer_id")
        if author_id is not None and stored_buyer_id is not None and str(author_id) != str(stored_buyer_id):
            return

        chat_id = getattr(msg, "chat_id", None) or order.get("chat_id")
        status = order.get("status")

        # Возврат обрабатывается раньше username/+.
        if is_refund_command(text):
            if status in (STATUS_USERNAME, STATUS_CONFIRM, STATUS_ERROR):
                refund_order(oid)
            elif status == STATUS_SENDING:
                send_funpay_message(chat_id, "⏳ Подарок уже отправляется. Дождитесь результата текущей попытки.")
            elif status == STATUS_DELIVERED:
                send_funpay_message(chat_id, "ℹ️ Подарок уже был отправлен. Возврат через !возврат после выдачи недоступен.")
            return

        if status == STATUS_USERNAME:
            username = normalize_username(text)
            if not username:
                send_funpay_message(chat_id, "❌ Некорректный Telegram username.\n\nОтправьте username в формате @username или !возврат.")
                return
            update_order(oid, recipient=username, status=STATUS_CONFIRM, last_error=None)
            send_funpay_message(chat_id, f"📋 Получатель: {username}\n\nЕсли всё верно — отправьте «+».\nЕсли хотите изменить получателя — отправьте новый username.\n\n❌ Для возврата: !возврат")
            return

        if status == STATUS_CONFIRM:
            if is_plus(text):
                if not order.get("recipient"):
                    update_order(oid, status=STATUS_USERNAME)
                    send_funpay_message(chat_id, "❌ Сначала отправьте Telegram username.")
                    return
                update_order(oid, status=STATUS_SENDING, last_error=None)
                send_funpay_message(chat_id, "⏳ Отправляю подарок...\n\nПожалуйста, подождите.")
                if _worker:
                    _worker.submit(oid)
                else:
                    update_order(oid, status=STATUS_ERROR, last_error="Worker unavailable")
                    send_funpay_message(chat_id, "❌ Telegram worker не запущен.\n\nПодарок не отправлен.")
                return

            username = normalize_username(text)
            if username:
                update_order(oid, recipient=username, status=STATUS_CONFIRM, last_error=None)
                send_funpay_message(chat_id, f"📋 Получатель изменён: {username}\n\nЕсли всё верно — отправьте «+».\nДля возврата: !возврат")
                return

            send_funpay_message(chat_id, "❓ Не понял сообщение.\n\nОтправьте «+», новый @username или !возврат.")
            return

        if status == STATUS_SENDING:
            send_funpay_message(chat_id, "⏳ Подарок уже отправляется.\nПожалуйста, подождите.")
            return

        if status == STATUS_ERROR:
            if is_plus(text) and order.get("recipient"):
                update_order(oid, status=STATUS_SENDING, last_error=None)
                send_funpay_message(chat_id, "⏳ Повторяю отправку подарка...")
                if _worker:
                    _worker.submit(oid)
                return
            username = normalize_username(text)
            if username:
                update_order(oid, recipient=username, status=STATUS_CONFIRM, last_error=None)
                send_funpay_message(chat_id, f"📋 Новый получатель: {username}\n\nОтправьте «+» для подтверждения.")
                return
            send_funpay_message(chat_id, "❓ Заказ ожидает повторной попытки. Отправьте «+», новый @username или !возврат.")
    except Exception:
        logger.exception("Telegram Gifts: ошибка обработки сообщения")


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def register_commands(c):
    if not getattr(c, "telegram", None):
        logger.warning("Telegram Gifts: Telegram-панель Cardinal отключена, команды не зарегистрированы")
        return

    commands = [
        ("gift_account", "Telegram-аккаунт и баланс Stars", True),
        ("gift_lots", "Привязанные лоты", True),
        ("gift_bind", "Привязать лот: /gift_bind LOT_ID GIFT_ID", False),
        ("gift_unbind", "Отвязать лот: /gift_unbind LOT_ID", False),
    ]
    try:
        c.add_telegram_commands(UUID, commands)
    except Exception:
        logger.exception("Telegram Gifts: не удалось добавить команды в меню Cardinal")
        return

    tg = c.telegram
    bot = tg.bot

    def authorized(message):
        try:
            return message.from_user.id in tg.authorized_users
        except Exception:
            return False

    def account_command(message):
        if not authorized(message) or not _worker:
            return
        try:
            future = _worker.run_coro(_worker.account_info())
            text = future.result(timeout=60)
            bot.send_message(message.chat.id, text, parse_mode="HTML")
        except Exception as exc:
            logger.exception("Telegram Gifts: /gift_account")
            bot.send_message(message.chat.id, f"❌ Ошибка: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")

    def lots_command(message):
        if not authorized(message):
            return
        with _orders_lock:
            bindings = dict(_lot_bindings)
        if not bindings:
            bot.send_message(message.chat.id, "📦 Привязанных лотов нет.")
            return
        lines = ["📦 <b>Привязанные лоты</b>", ""]
        for lot_id, binding in sorted(bindings.items(), key=lambda x: str(x[0])):
            gid = binding.get("gift_id")
            name = binding.get("gift_name") or gift_name(gid)
            lines.append(f"• <code>{html.escape(str(lot_id))}</code> → «{html.escape(str(name))}» (<code>{gid}</code>)")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")

    def bind_command(message):
        if not authorized(message):
            return
        parts = clean_text(message.text).split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            bot.send_message(message.chat.id, "Использование: <code>/gift_bind LOT_ID GIFT_ID</code>", parse_mode="HTML")
            return
        lot_id = str(int(parts[1]))
        gid = int(parts[2])
        if gid not in GIFTS:
            bot.send_message(message.chat.id, "❌ Неизвестный gift_id. Проверьте ID подарка в конфигурации плагина.")
            return
        with _orders_lock:
            _lot_bindings[lot_id] = {"gift_id": gid, "gift_name": GIFTS[gid], "updated_at": time.time()}
        save_bindings()
        bot.send_message(message.chat.id, f"✅ Лот <code>{lot_id}</code> привязан к подарку «{html.escape(GIFTS[gid])}» (<code>{gid}</code>).", parse_mode="HTML")
        logger.info("Telegram Gifts: привязка lot=%s -> gift=%s", lot_id, gid)

    def unbind_command(message):
        if not authorized(message):
            return
        parts = clean_text(message.text).split()
        if len(parts) != 2 or not parts[1].isdigit():
            bot.send_message(message.chat.id, "Использование: <code>/gift_unbind LOT_ID</code>", parse_mode="HTML")
            return
        lot_id = str(int(parts[1]))
        with _orders_lock:
            existed = _lot_bindings.pop(lot_id, None)
        save_bindings()
        bot.send_message(message.chat.id, "✅ Лот отвязан." if existed else "ℹ️ Такой привязки не было.")
        logger.info("Telegram Gifts: отвязка lot=%s", lot_id)

    tg.msg_handler(account_command, commands=["gift_account"])
    tg.msg_handler(lots_command, commands=["gift_lots"])
    tg.msg_handler(bind_command, commands=["gift_bind"])
    tg.msg_handler(unbind_command, commands=["gift_unbind"])


# ============================================================
# LIFECYCLE
# ============================================================

def post_init(c):
    global _cardinal, _worker
    _cardinal = c
    logger.info("Telegram Gifts: инициализация...")
    load_state()

    if importlib.util.find_spec("telethon") is None:
        logger.warning("Telegram Gifts: Telethon не установлен. Он будет установлен автоматически при первом обращении к Telegram.")

    if _worker is None:
        _worker = TelegramGiftWorker(c)
        _worker.start()
        logger.info("Telegram Gifts: worker запущен")

    register_commands(c)
    logger.info("Telegram Gifts: плагин успешно загружен v%s", VERSION)


def post_stop(c):
    global _worker
    if _worker:
        _worker.stop()
        _worker = None
    logger.info("Telegram Gifts: worker остановлен")


# ============================================================
# CARDINAL BINDS
# ============================================================

BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
