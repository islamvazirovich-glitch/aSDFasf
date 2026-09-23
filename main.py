import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo
TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    TelegramObject,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ==================== НАСТРОЙКИ ====================
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
with open(TOKEN_FILE, "r", encoding="utf-8") as f:
    BOT_TOKEN = f.read().strip()


TIME_WINDOWS = [(6, 8), (17, 21)]
SLOT_MINUTES = 30
ALLOWED_WEEKDAYS = {1, 3, 5}  # Вт, Чт, Сб

WEEKDAY_NAMES = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"
}

# ==================== ФАЙЛ ХРАНЕНИЯ ====================
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.json")

bookings: dict[str, dict[str, int]] = {}
users: dict[int, dict[str, str]] = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ==================== СОХРАНЕНИЕ / ЗАГРУЗКА ====================
def save_data() -> None:
    try:
        data = {
            "bookings": bookings,
            "users": {str(k): v for k, v in users.items()},
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Не удалось сохранить данные: {e}")


def load_data() -> None:
    global bookings, users
    if not os.path.exists(DATA_FILE):
        logging.info("Файл с данными не найден — начинаем с пустого списка.")
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        loaded_bookings = data.get("bookings", {})
        bookings = {
            date: {t: int(uid) for t, uid in day.items()}
            for date, day in loaded_bookings.items()
        }
        loaded_users = data.get("users", {})
        users = {int(k): v for k, v in loaded_users.items()}
        old_usernames = data.get("usernames", {})
        for k, v in old_usernames.items():
            uid = int(k)
            if uid not in users:
                users[uid] = {"name": v, "room": "—"}
        logging.info(
            f"Загружено: {sum(len(d) for d in bookings.values())} записей, "
            f"{len(users)} пользователей."
        )
    except Exception as e:
        logging.error(f"Не удалось загрузить данные: {e}.")
        bookings = {}
        users = {}


# ==================== ХЕЛПЕРЫ ====================
def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def next_allowed_dates(count: int = 5) -> list[str]:
    result = []
    d = now_local().date()
    while len(result) < count:
        if d.weekday() in ALLOWED_WEEKDAYS:
            result.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return result


def bookable_date() -> str | None:
    tmr = now_local().date() + timedelta(days=1)
    if tmr.weekday() in ALLOWED_WEEKDAYS:
        return tmr.strftime("%Y-%m-%d")
    return None


def human_date(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{WEEKDAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"


def generate_slots() -> list[str]:
    slots = []
    for start_h, end_h in TIME_WINDOWS:
        t = datetime.strptime(f"{start_h:02d}:00", "%H:%M")
        end = datetime.strptime(f"{end_h:02d}:00", "%H:%M")
        while t < end:
            slots.append(t.strftime("%H:%M"))
            t += timedelta(minutes=SLOT_MINUTES)
    return slots


def slot_start_dt(date_str: str, time_str: str) -> datetime:
    return datetime.strptime(
        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TIMEZONE)


def is_slot_finished(date_str: str, time_str: str) -> bool:
    return slot_start_dt(date_str, time_str) + timedelta(minutes=SLOT_MINUTES) <= now_local()


def user_label(user_id: int) -> str:
    u = users.get(user_id)
    if not u:
        return f"id{user_id}"
    return f"{u.get('name', '—')} (к. {u.get('room', '—')})"


def is_registered(user_id: int) -> bool:
    return user_id in users and bool(users[user_id].get("room"))


# ==================== НИЖНЯЯ ПАНЕЛЬ ====================
BTN_BOOK = "🚿 Записаться"
BTN_MY = "📋 Мои записи"
BTN_CANCEL = "❌ Отменить"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "ℹ️ Помощь"


def bottom_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_BOOK)
    kb.button(text=BTN_MY)
    kb.button(text=BTN_CANCEL)
    kb.button(text=BTN_PROFILE)
    kb.button(text=BTN_HELP)
    kb.adjust(2, 2, 1)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def bottom_kb_hidden() -> ReplyKeyboardMarkup:
    """
    Панель для незарегистрированного.
    По сути убирает все кнопки, оставляя пустую клавиатуру,
    чтобы человек не мог нажимать не то.
    """
    kb = ReplyKeyboardBuilder()
    return kb.as_markup(resize_keyboard=True, remove_keyboard=True)


# ==================== INLINE-МЕНЮ ====================
def main_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚿 Записаться", callback_data="menu|book")
    kb.button(text="📋 Мои записи", callback_data="menu|my")
    kb.button(text="❌ Отменить запись", callback_data="menu|cancel")
    kb.button(text="👤 Профиль", callback_data="menu|profile")
    kb.adjust(1)
    return kb


def dates_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    bookable = bookable_date()
    for d in next_allowed_dates(5):
        if d == bookable:
            text = f"🟢 {human_date(d)} — открыта запись"
        else:
            text = f"⚪ {human_date(d)}"
        kb.button(text=text, callback_data=f"date|{d}")
    kb.button(text="⬅️ В меню", callback_data="menu|main")
    kb.adjust(1)
    return kb


def slots_kb(date_str: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    day = bookings.get(date_str, {})
    for slot in generate_slots():
        uid = day.get(slot)
        if uid:
            kb.button(text=f"🔴 {slot} — {user_label(uid)}", callback_data=f"noop|{date_str}|{slot}")
        elif is_slot_finished(date_str, slot):
            kb.button(text=f"⬛ {slot} — прошло", callback_data=f"noop|{date_str}|{slot}")
        else:
            kb.button(text=f"🟢 {slot} — свободно", callback_data=f"book|{date_str}|{slot}")
    kb.button(text="⬅️ Назад к датам", callback_data="back")
    kb.adjust(1)
    return kb


# ==================== ТЕКСТЫ ====================
def ask_room_message() -> str:
    return (
        "👋 Привет!\n\n"
        "Чтобы пользоваться ботом, напиши <b>номер своей комнаты</b> "
        "(например: <code>305</code>).\n\n"
        "❗ Пока не напишешь номер — бот не пустит дальше."
    )


def help_text() -> str:
    return (
        "ℹ️ <b>Справка</b>\n\n"
        "📅 Дни записи: <b>Вт, Чт, Сб</b>\n"
        "⏰ Слоты (ЕКБ): 06:00–08:00 и 17:00–21:00\n"
        "⌛ 30 минут на человека, 1 человек на слот.\n\n"
        "❗ Запись открывается <b>только за день</b> до слота.\n\n"
        "Кнопки снизу:\n"
        "🚿 Записаться — выбрать слот\n"
        "📋 Мои записи — список твоих броней\n"
        "❌ Отменить — отменить бронь\n"
        "👤 Профиль — имя и комната\n"
        "ℹ️ Помощь — это сообщение"
    )


def main_menu_text(uid: int) -> str:
    return (
        f"🚿 <b>Бот записи в душ</b>\n\n"
        f"👤 {user_label(uid)}\n\n"
        "📅 Дни: Вт, Чт, Сб\n"
        "⏰ Слоты: 06:00–08:00 и 17:00–21:00 (ЕКБ)\n"
        "⌛ 30 минут, 1 человек на слот.\n\n"
        "<b>Запись открывается только за день до слота.</b>"
    )


# ==================== ЭКШЕНЫ ====================
async def show_main_menu(message: Message) -> None:
    await message.answer(
        main_menu_text(message.from_user.id),
        reply_markup=main_menu_kb().as_markup(),
        parse_mode="HTML",
    )


async def show_dates(message: Message) -> None:
    bookable = bookable_date()
    if bookable:
        hint = f"\n\n✅ Сейчас открыта запись на <b>{human_date(bookable)}</b>."
    else:
        hint = "\n\n⛔ Сейчас запись закрыта. Открывается за день до слота (Вт/Чт/Сб)."
    await message.answer(
        "Выбери дату:" + hint,
        reply_markup=dates_kb().as_markup(),
        parse_mode="HTML",
    )


async def show_my(message: Message) -> None:
    uid = message.from_user.id
    lines = []
    for date_str in sorted(bookings):
        for t in sorted(bookings[date_str]):
            if bookings[date_str][t] == uid:
                lines.append(f"• {human_date(date_str)} в {t}")
    text = "📋 <b>Твои записи:</b>\n" + "\n".join(lines) if lines else "📋 Записей нет."
    kb = InlineKeyboardBuilder()
    kb.button(text="🚿 Записаться", callback_data="menu|book")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


async def show_cancel(message: Message) -> None:
    uid = message.from_user.id
    kb = InlineKeyboardBuilder()
    found = False
    for date_str in sorted(bookings):
        for t in sorted(bookings[date_str]):
            if bookings[date_str][t] == uid:
                kb.button(text=f"{human_date(date_str)} {t}", callback_data=f"unbook|{date_str}|{t}")
                found = True
    if not found:
        await message.answer("❌ Нечего отменять.")
        return
    kb.adjust(1)
    await message.answer("Что отменить?", reply_markup=kb.as_markup())


async def show_profile(message: Message) -> None:
    uid = message.from_user.id
    u = users.get(uid, {})
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить комнату", callback_data="menu|change_room")
    kb.adjust(1)
    await message.answer(
        f"👤 <b>Профиль</b>\n\nИмя: {u.get('name', '—')}\nКомната: {u.get('room', '—')}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


# ==================== МИДЛВАРЬ: СТРОГАЯ БЛОКИРОВКА ====================
class RegistrationGateMiddleware(BaseMiddleware):
    """
    Пока пользователь не зарегистрирован (нет номера комнаты):
      • /start → пропускаем (обработчик снова попросит комнату)
      • /help  → пропускаем
      • любой текст (не команда) → пропускаем как ввод комнаты
      • всё остальное → блокируем и просим комнату
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if is_registered(user.id):
            return await handler(event, data)

        # --- незарегистрированный ---
        if isinstance(event, Message):
            text = (event.text or "").strip()

            # Разрешаем /start и /help
            if text.startswith("/"):
                cmd = text.split()[0].split("@")[0].lower()
                if cmd in ("/start", "/help"):
                    return await handler(event, data)
                # Любая другая команда — блокируем
                await event.answer(
                    ask_room_message(),
                    reply_markup=bottom_kb_hidden(),
                    parse_mode="HTML",
                )
                return None

            # Не команда и не пусто — это ввод комнаты, пропускаем
            return await handler(event, data)

        # Любой callback — блокируем
        if isinstance(event, CallbackQuery):
            await event.answer(
                "⛔ Сначала напиши номер комнаты.",
                show_alert=True,
            )
            return None

        # Всё прочее блокируем
        return None


dp.message.middleware(RegistrationGateMiddleware())
dp.callback_query.middleware(RegistrationGateMiddleware())


# ==================== КОМАНДЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    uid = message.from_user.id
    users.setdefault(uid, {})["name"] = message.from_user.full_name

    if not is_registered(uid):
        save_data()
        # Скрываем нижнюю панель, пока не зарегистрируется
        await message.answer(
            ask_room_message(),
            reply_markup=bottom_kb_hidden(),
            parse_mode="HTML",
        )
        return

    save_data()
    await message.answer(
        main_menu_text(uid),
        reply_markup=bottom_kb(),
        parse_mode="HTML",
    )
    await message.answer(
        "Меню 👇",
        reply_markup=main_menu_kb().as_markup(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    # /help доступен только зарегистрированным (до регистрации блокируется мидлварью)
    await message.answer(help_text(), reply_markup=bottom_kb(), parse_mode="HTML")


# ==================== ВВОД КОМНАТЫ / КНОПКИ ПАНЕЛИ ====================
@dp.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message):
    uid = message.from_user.id

    # Зарегистрирован → реагируем на кнопки нижней панели
    if is_registered(uid):
        text = (message.text or "").strip()
        if text == BTN_BOOK:
            await show_dates(message)
        elif text == BTN_MY:
            await show_my(message)
        elif text == BTN_CANCEL:
            await show_cancel(message)
        elif text == BTN_PROFILE:
            await show_profile(message)
        elif text == BTN_HELP:
            await message.answer(help_text(), reply_markup=bottom_kb(), parse_mode="HTML")
        else:
            await message.answer(
                "Не понял. Используй кнопки снизу 👇",
                reply_markup=bottom_kb(),
            )
        return

    # Не зарегистрирован → это ввод комнаты
    room = (message.text or "").strip()
    if not room:
        await message.answer(
            "Пустое сообщение. Напиши номер комнаты, например: 305",
            reply_markup=bottom_kb_hidden(),
        )
        return
    if len(room) > 20:
        await message.answer(
            "Слишком длинный номер. Напиши коротко, например: 305",
            reply_markup=bottom_kb_hidden(),
        )
        return

    users.setdefault(uid, {})["name"] = message.from_user.full_name
    users[uid]["room"] = room
    save_data()

    await message.answer(
        f"✅ Готово! Ты зарегистрирован как <b>{user_label(uid)}</b>.",
        reply_markup=bottom_kb(),
        parse_mode="HTML",
    )
    await show_main_menu(message)


# ==================== CALLBACK-И МЕНЮ ====================
@dp.callback_query(F.data == "menu|main")
async def cb_menu_main(call: CallbackQuery):
    await call.message.edit_text(
        main_menu_text(call.from_user.id),
        reply_markup=main_menu_kb().as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "menu|book")
async def cb_menu_book(call: CallbackQuery):
    bookable = bookable_date()
    if bookable:
        hint = f"\n\n✅ Сейчас открыта запись на <b>{human_date(bookable)}</b>."
    else:
        hint = "\n\n⛔ Сейчас запись закрыта. Открывается за день до слота (Вт/Чт/Сб)."
    await call.message.edit_text(
        "Выбери дату:" + hint,
        reply_markup=dates_kb().as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "menu|my")
async def cb_menu_my(call: CallbackQuery):
    uid = call.from_user.id
    lines = []
    for date_str in sorted(bookings):
        for t in sorted(bookings[date_str]):
            if bookings[date_str][t] == uid:
                lines.append(f"• {human_date(date_str)} в {t}")
    text = "📋 <b>Твои записи:</b>\n" + "\n".join(lines) if lines else "📋 Записей нет."
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu|main")
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "menu|cancel")
async def cb_menu_cancel(call: CallbackQuery):
    uid = call.from_user.id
    kb = InlineKeyboardBuilder()
    found = False
    for date_str in sorted(bookings):
        for t in sorted(bookings[date_str]):
            if bookings[date_str][t] == uid:
                kb.button(text=f"{human_date(date_str)} {t}", callback_data=f"unbook|{date_str}|{t}")
                found = True
    kb.button(text="⬅️ В меню", callback_data="menu|main")
    kb.adjust(1)
    if not found:
        await call.message.edit_text("❌ Нечего отменять.", reply_markup=kb.as_markup())
    else:
        await call.message.edit_text("Что отменить?", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data == "menu|profile")
async def cb_menu_profile(call: CallbackQuery):
    uid = call.from_user.id
    u = users.get(uid, {})
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить комнату", callback_data="menu|change_room")
    kb.button(text="⬅️ В меню", callback_data="menu|main")
    kb.adjust(1)
    await call.message.edit_text(
        f"👤 <b>Профиль</b>\n\nИмя: {u.get('name', '—')}\nКомната: {u.get('room', '—')}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "menu|change_room")
async def cb_change_room(call: CallbackQuery):
    uid = call.from_user.id
    users.setdefault(uid, {})["name"] = call.from_user.full_name
    users[uid]["room"] = ""
    save_data()
    await call.message.edit_text(ask_room_message(), parse_mode="HTML")
    await call.answer()


# ==================== CALLBACK-И ДЛЯ СЛОТОВ ====================
@dp.callback_query(F.data == "back")
async def cb_back(call: CallbackQuery):
    bookable = bookable_date()
    if bookable:
        hint = f"\n\n✅ Сейчас открыта запись на <b>{human_date(bookable)}</b>."
    else:
        hint = "\n\n⛔ Сейчас запись закрыта."
    await call.message.edit_text(
        "Выбери дату:" + hint,
        reply_markup=dates_kb().as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("date|"))
async def cb_date(call: CallbackQuery):
    date_str = call.data.split("|", 1)[1]
    bookable = bookable_date()
    if bookable is None:
        await call.answer("⛔ Запись сейчас закрыта.", show_alert=True)
        return
    if date_str != bookable:
        await call.answer(
            f"⛔ Запись только за день. Сейчас можно на {human_date(bookable)}.",
            show_alert=True,
        )
        return
    await call.message.edit_text(
        f"📅 <b>{human_date(date_str)}</b>\nВыбери слот (время ЕКБ):",
        reply_markup=slots_kb(date_str).as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("book|"))
async def cb_book(call: CallbackQuery):
    uid = call.from_user.id
    parts = call.data.split("|")
    date_str, time_str = parts[1], parts[2]
    bookable = bookable_date()
    if bookable is None or date_str != bookable:
        await call.answer("⛔ Запись только за день до слота.", show_alert=True)
        return
    day = bookings.setdefault(date_str, {})
    if time_str in day and day[time_str] != uid:
        await call.answer("Слот уже занят!", show_alert=True)
        return
    if is_slot_finished(date_str, time_str):
        await call.answer("Слот уже прошёл.", show_alert=True)
        return
    for t, u in day.items():
        if u == uid and t != time_str:
            await call.answer(f"У тебя уже есть запись на {t}.", show_alert=True)
            return
    day[time_str] = uid
    save_data()
    await call.answer(f"✅ Записан на {time_str}", show_alert=True)
    await call.message.edit_reply_markup(reply_markup=slots_kb(date_str).as_markup())


@dp.callback_query(F.data.startswith("noop|"))
async def cb_noop(call: CallbackQuery):
    await call.answer("Недоступно.", show_alert=False)


@dp.callback_query(F.data.startswith("unbook|"))
async def cb_unbook(call: CallbackQuery):
    parts = call.data.split("|")
    date_str, time_str = parts[1], parts[2]
    uid = call.from_user.id
    day = bookings.get(date_str, {})
    if day.get(time_str) == uid:
        del day[time_str]
        save_data()
        await call.message.edit_text(f"✅ Отменено: {human_date(date_str)} {time_str}")
    else:
        await call.answer("Не найдено.", show_alert=True)
    await call.answer()


# ==================== ЗАПУСК ====================
async def main():
    load_data()
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())