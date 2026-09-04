# -*- coding: utf-8 -*-

"""
Telegram Gifts for FunPay Cardinal
----------------------------------

Автоматическая выдача Telegram-подарков после покупки лота FunPay.

Сценарий:

1. Покупатель покупает лот.
2. Плагин определяет подарок по названию лота.
3. Плагин просит Telegram username.
4. Покупатель отправляет @username.
5. Плагин показывает получателя и ждёт "+".
6. После "+" подарок отправляется через Telegram MTProto.
7. Только после успешного ответа Telegram заказ считается выданным.
8. При ошибке заказ НЕ считается выполненным.
9. Доступен !возврат.
10. Состояние заказов сохраняется в JSON.
11. Telethon подключается лениво, чтобы отсутствие зависимости
    не мешало загрузке самого плагина Cardinal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import importlib.util

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from FunPayAPI.types import MessageTypes


# ============================================================
# CARDINAL PLUGIN DATA
# ============================================================

NAME = "Telegram Gifts"
VERSION = "1.1.0"
DESCRIPTION = "Автоматическая выдача Telegram-подарков после покупки на FunPay."
CREDITS = "@podarckov"

# ВАЖНО:
# Это настоящий UUID4.
UUID = "7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102"

# Обязательное поле Cardinal.
SETTINGS_PAGE = False

# Обязательное поле Cardinal.
BIND_TO_DELETE = None


# ============================================================
# TELEGRAM API
# ============================================================

API_ID = 32493973
API_HASH = "e470a990253e9502835f62cc5958aed7"

SESSION_NAME = "telegram_gifts"


# ============================================================
# TELEGRAM DEPENDENCY
# ============================================================

TELETHON_PACKAGE = "telethon"

_telethon_install_lock = threading.Lock()


def ensure_telethon():
    """
    Проверяет наличие Telethon.

    Если Telethon отсутствует, пытается установить его через pip.

    Это специально выполняется НЕ при импорте плагина.
    Благодаря этому отсутствие Telethon не мешает Cardinal
    обнаружить сам плагин.
    """

    if importlib.util.find_spec("telethon") is not None:
        return True

    with _telethon_install_lock:

        if importlib.util.find_spec("telethon") is not None:
            return True

        logger.warning(
            "Telegram Gifts: Telethon не найден. "
            "Устанавливаю зависимость..."
        )

        try:

            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "telethon>=1.36,<2"
                ]
            )

        except Exception:

            logger.exception(
                "Telegram Gifts: не удалось установить Telethon."
            )

            return False

    return importlib.util.find_spec("telethon") is not None


# ============================================================
# PATHS
# ============================================================

PLUGIN_DIR = os.path.join(
    "storage",
    "plugins",
    UUID
)

ORDERS_FILE = os.path.join(
    PLUGIN_DIR,
    "orders.json"
)

SESSION_FILE = os.path.join(
    PLUGIN_DIR,
    SESSION_NAME
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger("telegram_gifts")


# ============================================================
# ORDER STATUSES
# ============================================================

STATUS_USERNAME = "await_username"
STATUS_CONFIRM = "await_confirm"
STATUS_SENDING = "sending"
STATUS_DELIVERED = "delivered"
STATUS_REFUNDED = "refunded"
STATUS_ERROR = "error"


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


# ============================================================
# GIFT MESSAGE
# ============================================================

GIFT_MESSAGE = ""


# ============================================================
# GLOBAL STATE
# ============================================================

_cardinal = None

_orders_lock = threading.RLock()

_orders = {}

_worker = None


# ============================================================
# STATE
# ============================================================

def load_orders():
    global _orders

    os.makedirs(
        PLUGIN_DIR,
        exist_ok=True
    )

    if not os.path.exists(ORDERS_FILE):
        _orders = {}
        return

    try:

        with open(
            ORDERS_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if isinstance(data, dict):
            _orders = data
        else:
            _orders = {}

        logger.info(
            "Telegram Gifts: загружено заказов: %s",
            len(_orders)
        )

    except Exception:

        logger.exception(
            "Telegram Gifts: ошибка загрузки orders.json"
        )

        _orders = {}


def save_orders():

    os.makedirs(
        PLUGIN_DIR,
        exist_ok=True
    )

    temporary = ORDERS_FILE + ".tmp"

    with _orders_lock:
        data = dict(_orders)

    try:

        with open(
            temporary,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=4
            )

        os.replace(
            temporary,
            ORDERS_FILE
        )

    except Exception:

        logger.exception(
            "Telegram Gifts: ошибка сохранения orders.json"
        )


def update_order(
    order_id,
    **kwargs
):

    order_id = str(order_id)

    with _orders_lock:

        if order_id not in _orders:
            return

        _orders[order_id].update(
            kwargs
        )

    save_orders()


# ============================================================
# FUNPAY MESSAGE
# ============================================================

def send_funpay_message(
    chat_id,
    text
):

    if _cardinal is None:
        return

    try:

        _cardinal.send_message(
            chat_id,
            text
        )

    except Exception:

        logger.exception(
            "Telegram Gifts: не удалось отправить сообщение "
            "в FunPay chat=%s",
            chat_id
        )


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    return (
        str(text)
        .strip()
        .lower()
        .replace("ё", "е")
    )


def find_gift(
    lot_title
):

    normalized_lot = normalize_text(
        lot_title
    )

    if not normalized_lot:
        return None

    # Сначала точное совпадение.
    for gift_id, gift_name in GIFTS.items():

        if normalized_lot == normalize_text(
            gift_name
        ):

            return (
                int(gift_id),
                gift_name
            )

    # Потом частичное.
    for gift_id, gift_name in GIFTS.items():

        normalized_name = normalize_text(
            gift_name
        )

        if normalized_name in normalized_lot:

            return (
                int(gift_id),
                gift_name
            )

    return None


# ============================================================
# USERNAME
# ============================================================

USERNAME_RE = re.compile(
    r"^@?[A-Za-z0-9_]{5,32}$"
)


def normalize_username(
    text
):

    if not text:
        return None

    text = text.strip()

    if not USERNAME_RE.fullmatch(
        text
    ):
        return None

    if not text.startswith("@"):
        text = "@" + text

    return text


# ============================================================
# ACTIVE ORDER
# ============================================================

def find_order_by_chat(
    chat_id
):

    chat_id = str(chat_id)

    with _orders_lock:

        # Сначала активные.
        for order_id, order in _orders.items():

            if (
                str(order.get("chat_id")) == chat_id
                and order.get("status") in (
                    STATUS_USERNAME,
                    STATUS_CONFIRM,
                    STATUS_SENDING,
                    STATUS_ERROR
                )
            ):

                return (
                    order_id,
                    order
                )

    return (
        None,
        None
    )


# ============================================================
# REFUND
# ============================================================

def refund_order(
    order_id
):

    order_id = str(order_id)

    with _orders_lock:
        order = _orders.get(
            order_id
        )

    if not order:
        return

    if order.get("status") == STATUS_REFUNDED:
        return

    try:

        _cardinal.account.refund(
            order_id
        )

        update_order(
            order_id,
            status=STATUS_REFUNDED,
            last_error=None
        )

        send_funpay_message(
            order["chat_id"],
            "❌ Заказ отменён.\n\n"
            "Средства возвращены."
        )

        logger.info(
            "Telegram Gifts: заказ возвращён: %s",
            order_id
        )

    except Exception as exc:

        logger.exception(
            "Telegram Gifts: ошибка возврата order=%s",
            order_id
        )

        update_order(
            order_id,
            last_error=str(exc)
        )

        send_funpay_message(
            order["chat_id"],
            "⚠️ Не удалось автоматически оформить возврат.\n\n"
            "Продавец обработает возврат вручную."
        )


# ============================================================
# TELEGRAM WORKER
# ============================================================

class TelegramGiftWorker:

    def __init__(
        self,
        cardinal
    ):

        self.cardinal = cardinal

        self.loop = None
        self.thread = None
        self.queue = None

        self.client = None

        self.ready = threading.Event()
        self.stop_event = threading.Event()

        self.telethon_available = False

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    def start(self):

        if (
            self.thread
            and self.thread.is_alive()
        ):
            return

        self.thread = threading.Thread(
            target=self._thread_main,
            name="TelegramGiftWorker",
            daemon=True
        )

        self.thread.start()

        self.ready.wait(
            timeout=10
        )

    # --------------------------------------------------------
    # THREAD
    # --------------------------------------------------------

    def _thread_main(self):

        self.loop = asyncio.new_event_loop()

        asyncio.set_event_loop(
            self.loop
        )

        self.queue = asyncio.Queue()

        self.ready.set()

        try:

            self.loop.run_until_complete(
                self._worker_loop()
            )

        except Exception:

            logger.exception(
                "Telegram Gifts: критическая ошибка worker"
            )

        finally:

            try:

                self.loop.run_until_complete(
                    self._disconnect()
                )

            except Exception:
                pass

            try:
                self.loop.close()
            except Exception:
                pass

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    async def _worker_loop(self):

        while not self.stop_event.is_set():

            try:

                job = await asyncio.wait_for(
                    self.queue.get(),
                    timeout=1
                )

            except asyncio.TimeoutError:
                continue

            try:

                await self._process_job(
                    job
                )

            except Exception:

                logger.exception(
                    "Telegram Gifts: ошибка обработки job"
                )

    # --------------------------------------------------------
    # PROCESS
    # --------------------------------------------------------

    async def _process_job(
        self,
        job
    ):

        order_id = str(
            job["order_id"]
        )

        with _orders_lock:
            order = _orders.get(
                order_id
            )

        if not order:

            logger.error(
                "Telegram Gifts: заказ %s отсутствует",
                order_id
            )

            return

        if order.get("status") == STATUS_DELIVERED:
            return

        if order.get("status") != STATUS_SENDING:
            return

        recipient = order.get(
            "recipient"
        )

        gift_id = order.get(
            "gift_id"
        )

        gift_name = order.get(
            "gift_name"
        )

        # ----------------------------------------------------
        # CLIENT
        # ----------------------------------------------------

        try:

            await self._ensure_client()

        except Exception as exc:

            logger.exception(
                "Telegram Gifts: не удалось запустить Telegram"
            )

            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error=str(exc)
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Не удалось подключиться к Telegram.\n\n"
                "Подарок не отправлен. "
                "Заказ не считается выполненным."
            )

            return

        # ----------------------------------------------------
        # ENTITY
        # ----------------------------------------------------

        try:

            user = await self.client.get_entity(
                recipient
            )

        except Exception as exc:

            error_text = str(
                exc
            )

            logger.error(
                "Telegram Gifts: пользователь %s не найден: %s",
                recipient,
                error_text
            )

            update_order(
                order_id,
                status=STATUS_USERNAME,
                last_error=error_text
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Не удалось найти Telegram-пользователя "
                f"{recipient}.\n\n"
                "Проверьте username и отправьте его ещё раз."
            )

            return

        # ----------------------------------------------------
        # INPUT ENTITY
        # ----------------------------------------------------

        try:

            input_peer = await self.client.get_input_entity(
                user
            )

        except Exception as exc:

            logger.exception(
                "Telegram Gifts: не удалось получить input entity"
            )

            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error=str(exc)
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Не удалось определить получателя Telegram.\n\n"
                "Подарок не отправлен."
            )

            return

        # ----------------------------------------------------
        # IMPORT TELETHON TYPES
        # ----------------------------------------------------

        try:

            from telethon.tl.functions.payments import (
                GetPaymentFormRequest,
                SendStarsFormRequest,
            )

            from telethon.tl.types import (
                InputInvoiceStarGift,
                TextWithEntities,
            )

        except Exception as exc:

            logger.exception(
                "Telegram Gifts: ошибка импорта Telethon"
            )

            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error=str(exc)
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Ошибка библиотеки Telegram.\n\n"
                "Подарок не отправлен."
            )

            return

        # ----------------------------------------------------
        # MESSAGE
        # ----------------------------------------------------

        message = None

        if GIFT_MESSAGE:

            message = TextWithEntities(
                text=GIFT_MESSAGE,
                entities=[]
            )

        # ----------------------------------------------------
        # INVOICE
        # ----------------------------------------------------

        try:

            invoice = InputInvoiceStarGift(
                peer=input_peer,
                gift_id=int(gift_id),
                message=message
            )

        except Exception as exc:

            logger.exception(
                "Telegram Gifts: не удалось создать invoice"
            )

            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error=str(exc)
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Telegram не позволил создать запрос "
                "на отправку подарка.\n\n"
                "Подарок не отправлен."
            )

            return

        # ----------------------------------------------------
        # PAYMENT FORM
        # ----------------------------------------------------

        try:

            logger.info(
                "Telegram Gifts: получаю payment form "
                "order=%s gift=%s recipient=%s",
                order_id,
                gift_id,
                recipient
            )

            form = await self.client(
                GetPaymentFormRequest(
                    invoice=invoice
                )
            )

        except Exception as exc:

            await self._handle_telegram_exception(
                order_id,
                order,
                exc
            )

            return

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        try:

            logger.info(
                "Telegram Gifts: отправляю подарок "
                "order=%s gift=%s recipient=%s",
                order_id,
                gift_id,
                recipient
            )

            await self.client(
                SendStarsFormRequest(
                    form_id=form.form_id,
                    invoice=invoice
                )
            )

        except Exception as exc:

            await self._handle_telegram_exception(
                order_id,
                order,
                exc
            )

            return

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        update_order(
            order_id,
            status=STATUS_DELIVERED,
            last_error=None
        )

        send_funpay_message(
            order["chat_id"],
            "✅ Подарок "
            f"«{gift_name}» успешно отправлен "
            f"на {recipient}!\n\n"
            "Спасибо за покупку! "
            "Пожалуйста, подтвердите получение заказа "
            "и оставьте отзыв ❤️"
        )

        logger.info(
            "Telegram Gifts: подарок успешно отправлен "
            "order=%s",
            order_id
        )

    # --------------------------------------------------------
    # TELEGRAM EXCEPTION
    # --------------------------------------------------------

    async def _handle_telegram_exception(
        self,
        order_id,
        order,
        exc
    ):

        error = str(
            exc
        )

        upper = error.upper()

        logger.error(
            "Telegram Gifts: Telegram RPC error "
            "order=%s error=%s",
            order_id,
            error
        )

        # ----------------------------------------------------
        # FLOOD WAIT
        # ----------------------------------------------------

        try:

            from telethon import errors

            if isinstance(
                exc,
                errors.FloodWaitError
            ):

                seconds = int(
                    exc.seconds
                )

                logger.warning(
                    "Telegram Gifts: FloodWait %s сек.",
                    seconds
                )

                if seconds <= 300:

                    send_funpay_message(
                        order["chat_id"],
                        "⏳ Telegram временно ограничил отправку.\n\n"
                        f"Повторная попытка через {seconds} сек."
                    )

                    await asyncio.sleep(
                        seconds
                    )

                    with _orders_lock:
                        current = _orders.get(
                            order_id
                        )

                    if (
                        current
                        and current.get("status") == STATUS_SENDING
                    ):

                        await self._process_job(
                            {
                                "order_id": order_id
                            }
                        )

                    return

                update_order(
                    order_id,
                    status=STATUS_ERROR,
                    last_error=f"FloodWait {seconds}"
                )

                send_funpay_message(
                    order["chat_id"],
                    "❌ Telegram временно ограничил отправку "
                    f"подарков на {seconds} сек.\n\n"
                    "Заказ пока не считается выполненным."
                )

                return

        except Exception:
            pass

        # ----------------------------------------------------
        # BALANCE
        # ----------------------------------------------------

        if (
            "BALANCE_TOO_LOW" in upper
            or (
                "BALANCE" in upper
                and "STAR" in upper
            )
        ):

            update_order(
                order_id,
                status=STATUS_CONFIRM,
                last_error=error
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Не удалось отправить подарок.\n\n"
                "У продавца недостаточно Stars.\n\n"
                "После пополнения Stars отправьте «+», "
                "чтобы повторить отправку."
            )

            return

        # ----------------------------------------------------
        # GIFT NOT FOUND
        # ----------------------------------------------------

        if (
            "STARGIFT_NOT_FOUND" in upper
            or "STARGIFT_INVALID" in upper
        ):

            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error=error
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Сейчас этот подарок невозможно отправить.\n\n"
                "Заказ не считается выполненным."
            )

            return

        # ----------------------------------------------------
        # USAGE LIMITED
        # ----------------------------------------------------

        if "STARGIFT_USAGE_LIMITED" in upper:

            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error=error
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Этот подарок больше нельзя отправить "
                "из-за ограничения Telegram.\n\n"
                "Заказ не считается выполненным."
            )

            return

        # ----------------------------------------------------
        # USERNAME
        # ----------------------------------------------------

        if (
            "USERNAME_INVALID" in upper
            or "USERNAME_NOT_OCCUPIED" in upper
            or "USER_NOT_FOUND" in upper
            or "PEER_ID_INVALID" in upper
        ):

            update_order(
                order_id,
                status=STATUS_USERNAME,
                last_error=error
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Telegram не нашёл указанного пользователя.\n\n"
                "Отправьте другой Telegram username."
            )

            return

        # ----------------------------------------------------
        # FORBIDDEN
        # ----------------------------------------------------

        if (
            "CHAT_WRITE_FORBIDDEN" in upper
            or "USER_IS_BLOCKED" in upper
            or "USER_RESTRICTED" in upper
            or "FORBIDDEN" in upper
        ):

            update_order(
                order_id,
                status=STATUS_CONFIRM,
                last_error=error
            )

            send_funpay_message(
                order["chat_id"],
                "❌ Telegram не разрешил отправить подарок "
                "этому пользователю.\n\n"
                "Проверьте получателя."
            )

            return

        # ----------------------------------------------------
        # GENERIC ERROR
        # ----------------------------------------------------

        update_order(
            order_id,
            status=STATUS_ERROR,
            last_error=error
        )

        send_funpay_message(
            order["chat_id"],
            "❌ Telegram вернул ошибку при отправке подарка.\n\n"
            "Заказ не считается выполненным.\n"
            "Продавец проверит ситуацию."
        )

    # --------------------------------------------------------
    # ENSURE CLIENT
    # --------------------------------------------------------

    async def _ensure_client(self):

        if not self.telethon_available:

            self.telethon_available = ensure_telethon()

            if not self.telethon_available:

                raise RuntimeError(
                    "Telethon не установлен."
                )

        from telethon import TelegramClient

        if self.client is None:

            self.client = TelegramClient(
                SESSION_FILE,
                API_ID,
                API_HASH,
                device_model="FunPay Cardinal Telegram Gifts",
                system_version="Linux",
                app_version="1.1.0",
                lang_code="en",
                system_lang_code="en-US",
            )

        if not self.client.is_connected():

            await self.client.connect()

        if not await self.client.is_user_authorized():

            await self._authorize()

    # --------------------------------------------------------
    # AUTH
    # --------------------------------------------------------

    async def _authorize(self):

        if not API_ID or not API_HASH:

            raise RuntimeError(
                "Не указаны API_ID / API_HASH."
            )

        from telethon import errors

        from telethon.tl.functions.auth import (
            SendCodeRequest,
            SignInRequest,
            CheckPasswordRequest,
        )

        from telethon.tl.functions.account import (
            GetPasswordRequest
        )

        from telethon.tl.types import (
            CodeSettings
        )

        from telethon.password import (
            compute_check
        )

        print()
        print("=" * 60)
        print(" TELEGRAM GIFTS — ПЕРВАЯ АВТОРИЗАЦИЯ")
        print("=" * 60)

        phone = input(
            "Введи номер Telegram "
            "(например +79991234567): "
        ).strip()

        print(
            "Запрашиваю код Telegram..."
        )

        result = await self.client(
            SendCodeRequest(
                phone_number=phone,
                api_id=API_ID,
                api_hash=API_HASH,
                settings=CodeSettings(
                    allow_flashcall=False,
                    current_number=False,
                    allow_app_hash=False,
                )
            )
        )

        print(
            "Telegram отправил код через:",
            type(result.type).__name__
        )

        if getattr(
            result,
            "next_type",
            None
        ):

            print(
                "Следующий способ:",
                type(result.next_type).__name__
            )

        if getattr(
            result,
            "timeout",
            None
        ):

            print(
                "Таймаут:",
                result.timeout,
                "сек."
            )

        code = input(
            "Введи код из Telegram: "
        ).strip()

        try:

            await self.client(
                SignInRequest(
                    phone_number=phone,
                    phone_code_hash=result.phone_code_hash,
                    phone_code=code
                )
            )

        except errors.SessionPasswordNeededError:

            password = input(
                "Введи облачный пароль Telegram (2FA): "
            ).strip()

            pwd = await self.client(
                GetPasswordRequest()
            )

            await self.client(
                CheckPasswordRequest(
                    password=compute_check(
                        pwd,
                        password
                    )
                )
            )

        me = await self.client.get_me()

        print()
        print(
            "Telegram авторизован: "
            f"{me.first_name} "
            f"(id={me.id})"
        )
        print()

    # --------------------------------------------------------
    # DISCONNECT
    # --------------------------------------------------------

    async def _disconnect(self):

        if self.client:

            try:

                await self.client.disconnect()

            except Exception:
                pass

    # --------------------------------------------------------
    # SUBMIT
    # --------------------------------------------------------

    def submit(
        self,
        order_id
    ):

        if (
            not self.loop
            or not self.queue
        ):

            logger.error(
                "Telegram Gifts: worker ещё не запущен."
            )

            return None

        return asyncio.run_coroutine_threadsafe(
            self.queue.put(
                {
                    "order_id": str(
                        order_id
                    )
                }
            ),
            self.loop
        )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    def stop(self):

        self.stop_event.set()

        if self.loop:

            try:

                asyncio.run_coroutine_threadsafe(
                    self._disconnect(),
                    self.loop
                )

            except Exception:
                pass


# ============================================================
# NEW ORDER
# ============================================================

def bind_to_new_order(
    c,
    e
):

    try:

        order_id = str(
            e.order.id
        )

        with _orders_lock:

            if order_id in _orders:
                return

        # ----------------------------------------------------
        # FULL ORDER
        # ----------------------------------------------------

        try:

            full_order = c.account.get_order(
                e.order.id
            )

        except Exception:

            full_order = e.order

        # ----------------------------------------------------
        # LOT TITLE
        # ----------------------------------------------------

        lot_title = (
            getattr(
                full_order,
                "title",
                None
            )
            or getattr(
                full_order,
                "short_description",
                None
            )
            or getattr(
                full_order,
                "description",
                None
            )
            or getattr(
                e.order,
                "description",
                None
            )
            or ""
        )

        lot_title = str(
            lot_title
        ).strip()

        gift = find_gift(
            lot_title
        )

        if not gift:

            logger.info(
                "Telegram Gifts: заказ %s не является "
                "лотом Telegram Gifts: %s",
                order_id,
                lot_title
            )

            return

        gift_id, gift_name = gift

        # ----------------------------------------------------
        # CHAT
        # ----------------------------------------------------

        chat_id = getattr(
            e.order,
            "chat_id",
            None
        )

        buyer = (
            getattr(
                full_order,
                "buyer_username",
                None
            )
            or getattr(
                e.order,
                "buyer_username",
                None
            )
            or ""
        )

        if not chat_id:

            try:

                chat = c.account.get_chat_by_name(
                    buyer
                )

                if chat:
                    chat_id = chat.id

            except Exception:

                logger.exception(
                    "Telegram Gifts: не удалось определить "
                    "chat_id заказа %s",
                    order_id
                )

        if not chat_id:

            logger.error(
                "Telegram Gifts: у заказа %s отсутствует chat_id",
                order_id
            )

            return

        # ----------------------------------------------------
        # SAVE ORDER
        # ----------------------------------------------------

        with _orders_lock:

            _orders[order_id] = {

                "order_id": order_id,

                "gift_id": int(
                    gift_id
                ),

                "gift_name": gift_name,

                "lot_title": lot_title,

                "buyer": buyer,

                "chat_id": chat_id,

                "recipient": None,

                "status": STATUS_USERNAME,

                "last_error": None,

                "created_at": time.time(),
            }

        save_orders()

        # ----------------------------------------------------
        # MESSAGE
        # ----------------------------------------------------

        send_funpay_message(
            chat_id,
            "🎁 Спасибо за покупку!\n\n"
            "Отправьте ваш Telegram username, "
            "куда отправить подарок.\n\n"
            "Пример: @username\n\n"
            "❗ Для отмены заказа отправьте: !возврат"
        )

        logger.info(
            "Telegram Gifts: создан заказ "
            "order=%s gift=%s id=%s",
            order_id,
            gift_name,
            gift_id
        )

    except Exception:

        logger.exception(
            "Telegram Gifts: ошибка обработки нового заказа"
        )


# ============================================================
# NEW MESSAGE
# ============================================================

def msg_hook(
    c,
    e
):

    try:

        msg = e.message

        # ----------------------------------------------------
        # IGNORE OUR MESSAGE
        # ----------------------------------------------------

        if msg.author_id == c.account.id:
            return

        # ----------------------------------------------------
        # SYSTEM
        # ----------------------------------------------------

        if msg.type != MessageTypes.NON_SYSTEM:
            return

        text = (
            msg.text
            or ""
        ).strip()

        if not text:
            return

        # ----------------------------------------------------
        # ORDER
        # ----------------------------------------------------

        order_id, order = find_order_by_chat(
            msg.chat_id
        )

        if not order:
            return

        status = order.get(
            "status"
        )

        # ====================================================
        # USERNAME
        # ====================================================

        if status == STATUS_USERNAME:

            if text.lower() == "!возврат":

                refund_order(
                    order_id
                )

                return

            username = normalize_username(
                text
            )

            if not username:

                send_funpay_message(
                    msg.chat_id,
                    "❌ Некорректный Telegram username.\n\n"
                    "Отправьте username в формате:\n"
                    "@username"
                )

                return

            update_order(
                order_id,
                recipient=username,
                status=STATUS_CONFIRM,
                last_error=None
            )

            send_funpay_message(
                msg.chat_id,
                "📋 Получатель: "
                f"{username}\n\n"
                "Если всё верно — отправьте «+».\n"
                "Если хотите изменить получателя — "
                "отправьте новый username.\n\n"
                "❌ Для возврата: !возврат"
            )

            return

        # ====================================================
        # CONFIRM
        # ====================================================

        if status == STATUS_CONFIRM:

            if text.lower() == "!возврат":

                refund_order(
                    order_id
                )

                return

            # ------------------------------------------------
            # CONFIRM
            # ------------------------------------------------

            if text == "+":

                update_order(
                    order_id,
                    status=STATUS_SENDING,
                    last_error=None
                )

                send_funpay_message(
                    msg.chat_id,
                    "⏳ Отправляю подарок...\n\n"
                    "Пожалуйста, подождите."
                )

                if _worker:

                    _worker.submit(
                        order_id
                    )

                else:

                    update_order(
                        order_id,
                        status=STATUS_ERROR,
                        last_error="Worker unavailable"
                    )

                    send_funpay_message(
                        msg.chat_id,
                        "❌ Telegram worker не запущен.\n\n"
                        "Подарок не отправлен."
                    )

                return

            # ------------------------------------------------
            # CHANGE USERNAME
            # ------------------------------------------------

            username = normalize_username(
                text
            )

            if username:

                update_order(
                    order_id,
                    recipient=username,
                    status=STATUS_CONFIRM,
                    last_error=None
                )

                send_funpay_message(
                    msg.chat_id,
                    "📋 Получатель изменён:\n"
                    f"{username}\n\n"
                    "Если всё верно — отправьте «+».\n"
                    "Для изменения отправьте новый username.\n"
                    "Для возврата: !возврат"
                )

                return

            send_funpay_message(
                msg.chat_id,
                "❓ Не понял сообщение.\n\n"
                "Отправьте «+» для подтверждения,\n"
                "новый @username для изменения получателя\n"
                "или !возврат для отмены."
            )

            return

        # ====================================================
        # SENDING
        # ====================================================

        if status == STATUS_SENDING:

            send_funpay_message(
                msg.chat_id,
                "⏳ Подарок уже отправляется.\n"
                "Пожалуйста, подождите."
            )

            return

        # ====================================================
        # ERROR
        # ====================================================

        if status == STATUS_ERROR:

            if text.lower() == "!возврат":

                refund_order(
                    order_id
                )

                return

            if (
                text == "+"
                and order.get("recipient")
            ):

                update_order(
                    order_id,
                    status=STATUS_SENDING,
                    last_error=None
                )

                send_funpay_message(
                    msg.chat_id,
                    "⏳ Повторяю отправку подарка..."
                )

                if _worker:

                    _worker.submit(
                        order_id
                    )

                return

            username = normalize_username(
                text
            )

            if username:

                update_order(
                    order_id,
                    recipient=username,
                    status=STATUS_CONFIRM,
                    last_error=None
                )

                send_funpay_message(
                    msg.chat_id,
                    "📋 Новый получатель:\n"
                    f"{username}\n\n"
                    "Если всё верно — отправьте «+»."
                )

                return

        # ====================================================
        # DELIVERED
        # ====================================================

        if status == STATUS_DELIVERED:
            return

        # ====================================================
        # REFUNDED
        # ====================================================

        if status == STATUS_REFUNDED:
            return

    except Exception:

        logger.exception(
            "Telegram Gifts: ошибка обработки сообщения"
        )


# ============================================================
# POST INIT
# ============================================================

def post_init(
    c
):

    global _cardinal
    global _worker

    _cardinal = c

    logger.info(
        "Telegram Gifts: инициализация..."
    )

    load_orders()

    # --------------------------------------------------------
    # Проверяем зависимость, но НЕ импортируем Telethon
    # на этапе загрузки самого plugin.py.
    # --------------------------------------------------------

    if importlib.util.find_spec(
        "telethon"
    ) is None:

        logger.warning(
            "Telegram Gifts: Telethon не установлен. "
            "Он будет установлен автоматически при первом "
            "запуске Telegram worker."
        )

    # --------------------------------------------------------
    # WORKER
    # --------------------------------------------------------

    if not _worker:

        _worker = TelegramGiftWorker(
            c
        )

        _worker.start()

        logger.info(
            "Telegram Gifts: worker запущен."
        )

    logger.info(
        "Telegram Gifts: плагин успешно загружен."
    )


# ============================================================
# POST STOP
# ============================================================

def post_stop(
    c
):

    global _worker

    if _worker:

        _worker.stop()

        _worker = None

    logger.info(
        "Telegram Gifts: worker остановлен."
    )


# ============================================================
# CARDINAL BINDS
# ============================================================

BIND_TO_POST_INIT = [
    post_init
]

BIND_TO_NEW_ORDER = [
    bind_to_new_order
]

BIND_TO_NEW_MESSAGE = [
    msg_hook
]

BIND_TO_POST_STOP = [
    post_stop
]

BIND_TO_DELETE = None

# === COMPLETE FIXES ===
import html, unicodedata
LOT_BINDINGS_FILE=os.path.join(PLUGIN_DIR,'lot_bindings.json')
try:
    with open(LOT_BINDINGS_FILE,'r',encoding='utf-8') as f: LOT_BINDINGS=json.load(f)
except Exception: LOT_BINDINGS={}

def _clean(x): return unicodedata.normalize('NFKC',str(x or '')).replace('\u200b','').replace('\u200c','').replace('\u200d','').replace('\ufeff','').strip()
def _refund(x): return _clean(x).casefold().replace(' ','')=='!возврат'
def _plus(x): return _clean(x) in ('+','＋')
def _gid(x):
    m=re.search(r'(?i)\bID\s*:\s*(\d{10,25})\b',str(x or '')); return int(m.group(1)) if m else None
def _lid(c,o):
    for a in ('lot_id','offer_id'):
        v=getattr(o,a,None)
        if v is not None:return str(v)
    try:
        sub=getattr(o,'subcategory',None); lots=c.profile.get_sorted_lots(2).get(sub,{}) if sub else {}
        text=str(getattr(o,'description','') or ''); best=None; n=-1
        for l in lots.values():
            q=', '.join(str(x) for x in (getattr(l,'server',None),getattr(l,'side',None),getattr(l,'description',None)) if x)
            if q and q in text and len(q)>n:best,n=l,len(q)
        return str(best.id) if best else None
    except Exception:return None
def _resolve(c,e,o):
    for text in (getattr(o,'full_description',None),getattr(o,'description',None),getattr(o,'short_description',None),getattr(e.order,'description',None)):
        gid=_gid(text)
        if gid is not None:return gid,GIFTS.get(gid,'Подарок %s'%gid),_lid(c,o)
    lid=_lid(c,o); b=LOT_BINDINGS.get(str(lid)) if lid else None
    if b:
        gid=int(b['gift_id']);return gid,GIFTS.get(gid,b.get('gift_name','Подарок %s'%gid)),lid
    g=find_gift(getattr(o,'short_description',None) or getattr(o,'title',None) or getattr(o,'description',None) or '')
    return (g[0],g[1],lid) if g else None
def _save_bind():
    os.makedirs(PLUGIN_DIR,exist_ok=True); tmp=LOT_BINDINGS_FILE+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:json.dump(LOT_BINDINGS,f,ensure_ascii=False,indent=4)
    os.replace(tmp,LOT_BINDINGS_FILE)
def _new(c,e):
    try:
        oid=str(e.order.id)
        if oid in _orders:return
        o=c.account.get_order(e.order.id); r=_resolve(c,e,o)
        if not r:return
        gid,name,lid=r; chat=getattr(e.order,'chat_id',None) or getattr(o,'chat_id',None); buyer=getattr(o,'buyer_username',None) or getattr(e.order,'buyer_username',None) or ''
        if not chat and buyer:
            try:q=c.account.get_chat_by_name(buyer); chat=q.id if q else None
            except Exception:pass
        if not chat:return
        with _orders_lock:_orders[oid]={'order_id':oid,'gift_id':gid,'gift_name':name,'lot_id':lid,'buyer':buyer,'buyer_id':getattr(o,'buyer_id',None),'chat_id':chat,'recipient':None,'status':STATUS_USERNAME,'last_error':None,'amount':max(1,int(getattr(o,'amount',1) or 1)),'created_at':time.time()}
        save_orders();send_funpay_message(chat,'🎁 Спасибо за покупку!\n\nПодарок: «%s»\nКоличество: %s\n\nОтправьте ваш Telegram username, куда отправить подарок.\n\nПример: @username\n\n❗ Для отмены заказа отправьте: !возврат'%(name,_orders[oid]['amount']))
    except Exception:logger.exception('Telegram Gifts: fixed new order')
def _find(m):
    oid,o=find_order_by_chat(getattr(m,'chat_id',None))
    if o:return oid,o
    a=str(getattr(m,'author','') or '').lstrip('@').casefold()
    with _orders_lock:
        for i,v in _orders.items():
            if v.get('status') in (STATUS_USERNAME,STATUS_CONFIRM,STATUS_SENDING,STATUS_ERROR) and a and str(v.get('buyer','')).lstrip('@').casefold()==a:return i,v
    return None,None
def _msg(c,e):
    try:
        m=e.message;t=_clean(getattr(m,'text','') or ''); oid,o=_find(m)
        if not t or not o:return
        aid=getattr(m,'author_id',None)
        if aid is not None and str(aid)==str(c.account.id):return
        if aid is not None and o.get('buyer_id') is not None and str(aid)!=str(o['buyer_id']):return
        cid=getattr(m,'chat_id',None) or o['chat_id']; st=o['status']
        if _refund(t):
            if st in (STATUS_USERNAME,STATUS_CONFIRM,STATUS_ERROR):refund_order(oid)
            else:send_funpay_message(cid,'⏳ Подарок уже отправляется. Дождитесь результата текущей попытки.')
            return
        if st==STATUS_USERNAME:
            u=normalize_username(t)
            if not u:send_funpay_message(cid,'❌ Некорректный username. Отправьте @username или !возврат');return
            update_order(oid,recipient=u,status=STATUS_CONFIRM,last_error=None);send_funpay_message(cid,'📋 Получатель: %s\n\nЕсли всё верно — отправьте «+».\nДля изменения отправьте новый username.\nДля возврата: !возврат'%u);return
        if st==STATUS_CONFIRM and _plus(t):
            update_order(oid,status=STATUS_SENDING,last_error=None);send_funpay_message(cid,'⏳ Отправляю подарок...')
            if _worker:_worker.submit(oid)
            return
        if st==STATUS_CONFIRM:
            u=normalize_username(t)
            if u:update_order(oid,recipient=u,status=STATUS_CONFIRM,last_error=None);send_funpay_message(cid,'📋 Получатель изменён: %s\n\nОтправьте «+». '%u);return
        if st==STATUS_ERROR and _plus(t) and o.get('recipient'):
            update_order(oid,status=STATUS_SENDING,last_error=None);send_funpay_message(cid,'⏳ Повторяю отправку подарка...')
            if _worker:_worker.submit(oid)
    except Exception:logger.exception('Telegram Gifts: fixed message')
async def _stars(self):
    from telethon.tl.functions.payments import GetStarsStatusRequest
    from telethon.tl.types import InputPeerSelf
    return await self.client(GetStarsStatusRequest(peer=InputPeerSelf()))
async def _info(self):
    await self._ensure_client();me=await self.client.get_me();s=await self._stars();b=getattr(getattr(s,'balance',None),'amount',0) or 0
    n=' '.join(x for x in (getattr(me,'first_name',None),getattr(me,'last_name',None)) if x) or '—';u='@'+me.username if getattr(me,'username',None) else 'нет username'
    return '🎁 <b>Telegram Gifts — аккаунт</b>\n\n👤 Имя: <code>%s</code>\n🔗 Username: <code>%s</code>\n🆔 ID: <code>%s</code>\n⭐ Stars: <b>%s</b>'%(html.escape(n),html.escape(u),me.id,int(b))
def _commands(c):
    if not getattr(c,'telegram',None):return
    tg=c.telegram
    c.add_telegram_commands(UUID,[('gift_account','Telegram-аккаунт и баланс Stars',True),('gift_lots','Привязанные лоты',True),('gift_bind','Привязать: /gift_bind LOT_ID GIFT_ID',False),('gift_unbind','Отвязать: /gift_unbind LOT_ID',False)])
    def ok(m):
        try:return m.from_user.id in tg.authorized_users
        except:return False
    def account(m):
        if not ok(m) or not _worker:return
        try:tg.bot.send_message(m.chat.id,_worker.run_coro(_worker.info()).result(timeout=45))
        except Exception as ex:tg.bot.send_message(m.chat.id,'❌ Ошибка: <code>%s</code>'%html.escape(str(ex)))
    def lots(m):
        if not ok(m):return
        if not LOT_BINDINGS:tg.bot.send_message(m.chat.id,'📦 Привязанных лотов нет.');return
        tg.bot.send_message(m.chat.id,'📦 <b>Привязанные лоты</b>\n\n'+'\n'.join('• <code>%s</code> → «%s» (<code>%s</code>)'%(k,html.escape(str(v.get('gift_name') or GIFTS.get(int(v['gift_id']),v['gift_id']))),v.get('gift_id')) for k,v in sorted(LOT_BINDINGS.items())))
    def bind(m):
        if not ok(m):return
        x=_clean(m.text).split()
        if len(x)!=3 or not x[1].isdigit() or not x[2].isdigit():tg.bot.send_message(m.chat.id,'Использование: <code>/gift_bind LOT_ID GIFT_ID</code>');return
        gid=int(x[2])
        if gid not in GIFTS:tg.bot.send_message(m.chat.id,'❌ Неизвестный gift_id.');return
        LOT_BINDINGS[x[1]]={'gift_id':gid,'gift_name':GIFTS[gid]};_save_bind();tg.bot.send_message(m.chat.id,'✅ Лот привязан.')
    def unbind(m):
        if not ok(m):return
        x=_clean(m.text).split()
        if len(x)!=2 or not x[1].isdigit():tg.bot.send_message(m.chat.id,'Использование: <code>/gift_unbind LOT_ID</code>');return
        old=LOT_BINDINGS.pop(x[1],None);_save_bind();tg.bot.send_message(m.chat.id,'✅ Лот отвязан.' if old else 'ℹ️ Привязка не найдена.')
    tg.msg_handler(account,commands=['gift_account']);tg.msg_handler(lots,commands=['gift_lots']);tg.msg_handler(bind,commands=['gift_bind']);tg.msg_handler(unbind,commands=['gift_unbind'])
TelegramGiftWorker.run_coro=lambda self,coro: asyncio.run_coroutine_threadsafe(coro,self.loop)
TelegramGiftWorker.stars=stars_fixed if 'stars_fixed' in globals() else _stars
TelegramGiftWorker.info=_info
_old_init=post_init
def post_init_fixed(c): _old_init(c);_commands(c)
BIND_TO_POST_INIT=[post_init_fixed]
BIND_TO_NEW_ORDER=[_new]
BIND_TO_NEW_MESSAGE=[_msg]
VERSION='2.0.1'
