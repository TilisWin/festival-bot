"""
Бот регистрации на фестиваль «Генерация».

Поток:
1. /start → баннер, приветствие и меню (Подать заявку / Правила / Стоимость)
2. «Подать заявку» → краткая инфа по фестивалю и файл с регламентом → «Продолжить»
3. Анкета: имя и ник → соцсети → страна и город → почта → о себе
4. Номинации кнопками (можно несколько)
5. Ссылка на папку с работами (диск, облако), в названии каждого файла указана номинация
6. Подтверждение → заявка уходит на модерацию в админ-чат, записывается в файл applications.csv
   и дублируется письмом на почту фестиваля
7. Админ нажимает «Одобрить» или «Отклонить» — участник получает уведомление, статус в файле меняется
8. Команда /export в админ-чате присылает весь список заявок файлом
"""

import asyncio
import csv
import html
import logging
import os
import re
import smtplib
import ssl
import threading
from datetime import datetime
from email.message import EmailMessage

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ---------- настройки из переменных окружения ----------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])  # id чата/группы модераторов (отрицательное число для групп)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(BASE_DIR, "assets")
BANNER_PATH = os.path.join(ASSETS, "banner_start.jpg")
REGLAMENT_PATH = os.path.join(ASSETS, "reglament_2026.docx")
APPLICATIONS_CSV = os.environ.get("APPLICATIONS_CSV", os.path.join(BASE_DIR, "applications.csv"))

# Почта, на которую дублируется каждая заявка
APPLICATIONS_EMAIL = os.environ.get("APPLICATIONS_EMAIL", "tattoogenerationfest@gmail.com")
# Отправитель писем (SMTP). Для Gmail нужен пароль приложения, а не обычный пароль от аккаунта.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

WORK_LINK_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

# ---------- тексты (взяты из регламента и ТЗ клиента) ----------

WELCOME_TEXT = (
    "Привет! «Генерация» — первый международный онлайн-фестиваль татуировки "
    "с открытым судейством по строгому регламенту в прямом эфире.\n\n"
    "Чтобы участвовать, заполни форму заявки. После модерации с тобой свяжутся менеджеры."
)

INFO_TEXT = (
    "«Генерация» проходит с 22 по 25 декабря 2026 года. Работы оценивают судьи в прямом эфире.\n\n"
    "<b>Главное:</b>\n"
    "➡️ Регистрация открыта до 15.12.2026, 23:59 (МСК).\n"
    "➡️ Одну работу можно подать только в одну номинацию.\n"
    "➡️ Участвовать можно в неограниченном количестве номинаций и подавать неограниченное количество неповторяющихся работ.\n"
    "➡️ Оплата только после модерации, по реквизитам из письма на твою почту.\n\n"
    "<b>Стоимость:</b>\n"
    "➡️ 2 500 ₽ за одну номинацию до 14.11.2026\n"
    "➡️ 3 000 ₽ с 15.11.2026\n\n"
    "Полный регламент — в файле ниже."
)

CONSENT_TEXT = (
    "<b>Для участия в фестивале нам нужно твоё согласие на обработку персональных данных.</b>\n"
    "Мы собираем имя и ник, ссылки на соцсети, страну и город, почту, описание о себе и фото или видео работ. "
    "Данные нужны только для отбора участников и связи с тобой. "
    "Мы не передаём их третьим лицам, кроме случаев, предусмотренных законом.\n\n"
    "<blockquote>Нажимая «Согласен», ты подтверждаешь, что ознакомлен и согласен с обработкой данных.</blockquote>"
)

RULES_TEXT = (
    "Правила участия — в файле регламента ниже.\n\n"
    "Главное: регистрация до 15.12.2026 23:59 (МСК), одна работа — одна номинация, "
    "участвовать можно в неограниченном количестве номинаций."
)

PRICE_TEXT = (
    "Стоимость участия:\n"
    "• 2 500 ₽ за одну номинацию до 14.11.2026\n"
    "• 3 000 ₽ за одну номинацию с 15.11.2026\n\n"
    "Оплата после прохождения модерации по реквизитам из письма на твою почту. "
    "После оплаты приходит квитанция, затем письмо с подтверждением участия."
)

# Номинации и требования к работам — по правилам из материалов клиента.
# ВНИМАНИЕ: названия взяты из списка с правилами (картинка 2). В регламенте написано
# «Реализм ч/б» и «Реализм цвет», «Реалистичная графика» и «Графика реалистичная» поменяны местами —
# клиенту нужно подтвердить итоговые названия.
NOMINATION_RULES = [
    ("Миниатюра ч/б", "Любой стиль исполнения в ч/б, размер работы не более 10x10 см, наличие цвета не более 15%"),
    ("Миниатюра цвет", "Любой стиль исполнения в цвете, размер работы не более 10x10 см"),
    ("Миди ч/б", "Любой стиль исполнения в ч/б, размер работы не более формата А4, допустимо наличие цвета не более 15%"),
    ("Миди цвет", "Любой стиль исполнения в цвете, размер работы не более формата А4"),
    ("Большая ч/б", "Любой стиль или микс стилей исполнения, работа выполнена в ч/б гамме, наличие цвета не более 15%, размер работы от формата А4"),
    ("Большая цвет", "Любой стиль или микс стилей исполнения, работа выполнена в цвете, размер работы от формата А4"),
    ("Аниме цвет", "Любой стиль исполнения аниме тематики"),
    ("Микс", "Использование нескольких стилей исполнения в работе, которые не подходят под классические номинации"),
    ("Реал ч/б", "Реалистичные работы, выполненные в черно-белой гамме, наличие цвета не более 15%"),
    ("Реал цвет", "Реалистичные работы, выполненные в цвете"),
    ("Графика", "Соответствие канонам номинации, использование четкого лайна, а также наличие пикселя, наличие цвета не более 15%"),
    ("Графика реалистичная", "Реалистичные работы, выполненные пикселем, наличие другого цвета помимо черного не более 15%"),
    ("Файн-лайн", "Соответствие канонам номинации, тонкие линии, возможно наличие пикселя, размер не ограничен, наличие другого цвета помимо черного не более 15%"),
    ("Неотрад", "Соответствие канонам номинации, использование четкого лайна, различных техник покраски, теней"),
    ("Ньюскул", "Соответствие канонам номинации, использование четкого лайна, различных техник покраски, теней"),
    ("Абстракция", "Соответствие канонам номинации, использование формальных элементов, простых форм, цветовых пятен"),
    ("Япония/неояпония", "Соответствие канонам номинации, использование классических восточных сюжетов в современном прочтении"),
]

NOMINATIONS = [name for name, _ in NOMINATION_RULES]


def nomination_rules_messages() -> list[str]:
    """Текст всех номинаций (HTML: названия жирным, между номинациями пустая строка),
    разбитый на сообщения короче лимита Telegram (4096 символов)."""
    items = [f"<b>{html.escape(name)}</b>: {html.escape(rule)}" for name, rule in NOMINATION_RULES]
    header = "<b>Требования к работам по номинациям:</b>"
    messages, chunk = [], header
    for item in items:
        candidate = chunk + "\n\n" + item
        if len(candidate) > 3800:
            messages.append(chunk)
            chunk = item
        else:
            chunk = candidate
    messages.append(chunk)
    return messages


def works_hints(noms: list[int]) -> list[str]:
    """Дополнительные материалы, которые регламент предлагает приложить к выбранным номинациям."""
    names = [NOMINATIONS[i] for i in noms]
    hints = []
    if any(n.startswith("Миниатюра") for n in names):
        hints.append("Миниатюра: по желанию фото работы с линейкой")
    if any(n.startswith("Реал") for n in names):
        hints.append("Реал: по желанию фото с референсом")
    if any(n in ("Неотрад", "Ньюскул") for n in names):
        hints.append("Неотрад / Ньюскул: по желанию фото с эскизом")
    if any(n.startswith("Большая") for n in names):
        hints.append("Большая: по желанию видео работы")
    return hints


# ---------- файл с заявками (applications.csv) ----------

# Файл открывается в Excel на русском без проблем с кодировкой (utf-8 с BOM) и с разделителем «;».
CSV_HEADER = ["Дата", "ID", "Ник", "Имя и ник", "Соцсети", "Страна и город", "Почта", "О себе", "Номинации", "Ссылка на работы", "Согласие", "Статус"]
CSV_LOCK = threading.Lock()  # чтобы две заявки одновременно не портили файл
STATUS_PENDING = "на модерации"


def append_application_sync(row: list[str]) -> None:
    with CSV_LOCK:
        if not os.path.exists(APPLICATIONS_CSV):
            with open(APPLICATIONS_CSV, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f, delimiter=";").writerow(CSV_HEADER)
        with open(APPLICATIONS_CSV, "a", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter=";").writerow(row)


def set_status_sync(user_id: int, status: str) -> None:
    """Меняет статус последней заявки участника (решение модератора можно менять несколько раз)."""
    with CSV_LOCK:
        if not os.path.exists(APPLICATIONS_CSV):
            return
        with open(APPLICATIONS_CSV, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f, delimiter=";"))
        for i in range(len(rows) - 1, 0, -1):
            if rows[i][1] == str(user_id):
                rows[i][-1] = status
                break
        with open(APPLICATIONS_CSV, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f, delimiter=";").writerows(rows)


BACKGROUND_TASKS: set = set()  # чтобы фоновые задачи не удалялись сборщиком мусора до завершения


async def send_email_background(subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(send_email_sync, subject, body)
    except Exception:
        logging.exception("Не удалось отправить заявку на почту")


def send_email_sync(subject: str, body: str) -> None:
    """Дублирует заявку письмом на почту фестиваля. Если SMTP не настроен, просто пропускает."""
    if not SMTP_USER or not SMTP_PASSWORD:
        logging.warning("SMTP_USER/SMTP_PASSWORD не заданы, письмо с заявкой не отправлено")
        return
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = APPLICATIONS_EMAIL
    msg["Subject"] = subject
    msg.set_content(body)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=15) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


# ---------- клавиатуры ----------

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Подать заявку")],
        [KeyboardButton(text="📜 Правила"), KeyboardButton(text="💰 Стоимость")],
    ],
    resize_keyboard=True,
)


def info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Продолжить заявку", callback_data="form_go")],
    ])


def nominations_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    buttons = []
    for i, name in enumerate(NOMINATIONS):
        mark = "✅ " if i in selected else ""
        buttons.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"nom:{i}"))
    # по две кнопки в ряд
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="ℹ️ Требования к работам", callback_data="nom_info")])
    rows.append([InlineKeyboardButton(text="➡️ Готово", callback_data="nom_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data="send"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel"),
    ]])


def moderation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"mod_ok:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_no:{user_id}"),
    ]])


# ---------- состояния анкеты ----------

class Form(StatesGroup):
    consent = State()  # согласие на обработку персональных данных
    name = State()
    socials = State()
    location = State()
    email = State()  # почта участника для связи с ним менеджеров
    about = State()
    nominations = State()
    works = State()   # ссылка на папку с работами
    confirm = State()


class Moderation(StatesGroup):
    reject_reason = State()  # модератор пишет причину отказа, которая уйдёт участнику


STATUS_MARK = "\n\nСтатус: "


def card_base_text(text: str | None) -> str:
    """Текст карточки без строки статуса, чтобы статус можно было менять сколько угодно раз."""
    return (text or "").split(STATUS_MARK)[0]


def format_application(data: dict) -> str:
    noms = ", ".join(NOMINATIONS[i] for i in data.get("noms", []))
    return (
        f"Имя и ник: {data['name']}\n"
        f"Соцсети: {data['socials']}\n"
        f"Страна и город: {data['location']}\n"
        f"Email: {data['email']}\n"
        f"О себе: {data['about']}\n"
        f"Номинации: {noms}\n"
        f"Работы: {data.get('work_link', '')}"
    )


# ---------- хендлеры ----------

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer_photo(
        FSInputFile(BANNER_PATH),
        caption=WELCOME_TEXT,
        reply_markup=MAIN_MENU,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заполнение заявки отменено.", reply_markup=MAIN_MENU)


@router.message(Command("rules"))
@router.message(F.text == "📜 Правила")
async def show_rules(message: Message):
    await message.answer(RULES_TEXT)
    await message.answer_document(FSInputFile(REGLAMENT_PATH), caption="Регламент фестиваля «Генерация»")


@router.message(F.text == "💰 Стоимость")
async def show_price(message: Message):
    await message.answer(PRICE_TEXT)


@router.message(F.text == "📝 Подать заявку")
async def form_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(INFO_TEXT, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await message.answer_document(FSInputFile(REGLAMENT_PATH), caption="Регламент фестиваля «Генерация»")
    await message.answer("Когда будешь готов, нажми «Продолжить».", reply_markup=info_keyboard())


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Согласен", callback_data="consent_yes"),
        InlineKeyboardButton(text="✖️ Не согласен", callback_data="consent_no"),
    ]])


@router.callback_query(F.data == "form_go")
async def form_go(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.consent)
    await cb.message.answer(CONSENT_TEXT, reply_markup=consent_keyboard(), parse_mode="HTML")
    await cb.answer()


@router.callback_query(Form.consent, F.data == "consent_yes")
async def consent_yes(cb: CallbackQuery, state: FSMContext):
    # дату согласия сохраняем в заявку, чтобы его можно было подтвердить
    await state.update_data(consent_at=datetime.now().strftime("%d.%m.%Y %H:%M"))
    await state.set_state(Form.name)
    await cb.message.answer(
        "Введи имя и ник, например: Анна, @anna_tattoo\n\n(отменить можно командой /cancel)"
    )
    await cb.answer()


@router.callback_query(Form.consent, F.data == "consent_no")
async def consent_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer(
        "Без согласия на обработку данных заявку подать нельзя. Если передумаешь, нажми «Подать заявку» снова.",
        reply_markup=MAIN_MENU,
    )
    await cb.answer()


@router.message(Form.name, F.text)
async def got_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(Form.socials)
    await message.answer("Ссылки на соцсети (Instagram и другие)")


@router.message(Form.socials, F.text)
async def got_socials(message: Message, state: FSMContext):
    await state.update_data(socials=message.text.strip())
    await state.set_state(Form.location)
    await message.answer("Страна и город")


@router.message(Form.location, F.text)
async def got_location(message: Message, state: FSMContext):
    await state.update_data(location=message.text.strip())
    await state.set_state(Form.email)
    await message.answer(
        "Твоя почта для связи. Лучше не Яндекс: сейчас с ним возможны перебои с получением писем."
    )


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.message(Form.email, F.text)
async def got_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if not EMAIL_RE.match(email):
        await message.answer("Похоже, это не почта. Введи адрес в формате name@example.com")
        return
    await state.update_data(email=email)
    await state.set_state(Form.about)
    await message.answer("Немного о себе")


@router.message(Form.about, F.text)
async def got_about(message: Message, state: FSMContext):
    await state.update_data(about=message.text.strip(), noms=[])
    await state.set_state(Form.nominations)
    await message.answer(
        "Выбери номинации, в которых хочешь участвовать. Можно несколько. "
        "Когда закончишь, нажми «Готово».",
        reply_markup=nominations_keyboard(set()),
    )


@router.message(StateFilter(Form.name, Form.socials, Form.location, Form.email, Form.about), ~F.text)
async def not_text(message: Message):
    await message.answer("Пожалуйста, ответь текстом.")


@router.callback_query(Form.nominations, F.data.startswith("nom:"))
async def toggle_nomination(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    data = await state.get_data()
    selected = set(data.get("noms", []))
    selected ^= {idx}
    await state.update_data(noms=sorted(selected))
    await cb.message.edit_reply_markup(reply_markup=nominations_keyboard(selected))
    await cb.answer()


@router.callback_query(Form.nominations, F.data == "nom_info")
async def nominations_info(cb: CallbackQuery):
    for text in nomination_rules_messages():
        await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


@router.callback_query(Form.nominations, F.data == "nom_done")
async def nominations_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("noms"):
        await cb.answer("Выбери хотя бы одну номинацию", show_alert=True)
        return
    await state.set_state(Form.works)
    hints = works_hints(data["noms"])
    text = (
        "<b>Пришли одну ссылку на папку (Яндекс Диск, Google Диск, облако) со всеми работами.</b>\n\n"
        "<blockquote><b>Важно:</b>\n"
        "• открой доступ к папке «по ссылке», иначе мы не сможем её посмотреть;\n"
        "• в названии каждого файла укажи номинацию, например: «Миди цвет_1.jpg», «Аниме цвет_2.jpg»;\n"
        "• фото без обработки в сторонних программах, допускается только поляризационный фильтр.</blockquote>"
    )
    if hints:
        text += "\n\n<b>Для выбранных номинаций можно дополнительно положить в папку:</b>\n" + "\n".join(f"• {h}" for h in hints)
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


@router.message(Form.works, F.text)
async def got_work_link(message: Message, state: FSMContext):
    link = message.text.strip()
    if not WORK_LINK_RE.match(link):
        await message.answer("Похоже, это не ссылка. Пришли ссылку, которая начинается с https://")
        return
    await state.update_data(work_link=link)
    await state.set_state(Form.confirm)
    data = await state.get_data()
    await message.answer(
        "Проверь заявку:\n\n" + format_application(data),
        reply_markup=confirm_keyboard(),
    )


@router.message(Form.works)
async def works_not_text(message: Message):
    await message.answer("Пришли ссылку на папку с работами текстом.")


@router.callback_query(Form.confirm, F.data == "send")
async def send_application(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = cb.from_user
    username = f"@{user.username}" if user.username else ""
    noms = ", ".join(NOMINATIONS[i] for i in data["noms"])

    row = [
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        str(user.id),
        username,
        data["name"],
        data["socials"],
        data["location"],
        data["email"],
        data["about"],
        noms,
        data["work_link"],
        f"да, {data['consent_at']}",
        STATUS_PENDING,
    ]
    try:
        await asyncio.to_thread(append_application_sync, row)
    except Exception:
        logging.exception("Не удалось записать заявку в файл")

    # если админ-чат недоступен (сменился ID, бот не в группе), участник всё равно получает ответ,
    # а заявка уже сохранена в файле и ушла письмом
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"Новая заявка от {username or user.full_name} (id {user.id})\n\n"
            + format_application(data),
            reply_markup=moderation_keyboard(user.id),
        )
    except TelegramMigrateToChat as e:
        logging.error("Админ-чат сменил ID: в ADMIN_CHAT_ID нужно поставить %s", e.migrate_to_chat_id)
    except Exception:
        logging.exception("Не удалось отправить заявку в админ-чат")

    await state.clear()
    await cb.message.edit_text(
        "Спасибо! Заявка отправлена на модерацию. Если её одобрят, с тобой свяжутся менеджеры."
    )
    await cb.answer()

    # письмо на почту фестиваля отправляем в фоне, чтобы медленный SMTP не задерживал ответ участнику
    subject = f"Заявка «Генерация»: {data['name']}"
    body = f"Дата: {row[0]}\nTelegram: {username or '—'} (id {user.id})\n\n" + format_application(data)
    task = asyncio.create_task(send_email_background(subject, body))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


@router.callback_query(F.data == "cancel")
async def cancel_application(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Заявка отменена.")
    await cb.answer()


# ---------- модерация (только в админ-чате) ----------

async def save_status(user_id: int, status: str) -> None:
    try:
        await asyncio.to_thread(set_status_sync, user_id, status)
    except Exception:
        logging.exception("Не удалось обновить статус в файле заявок")


async def update_card(bot: Bot, chat_id: int, message_id: int, text: str, user_id: int) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=moderation_keyboard(user_id))
    except Exception:
        logging.exception("Не удалось обновить карточку заявки")


@router.callback_query(F.data.startswith("mod_ok:"))
async def moderate_approve(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != ADMIN_CHAT_ID:
        await cb.answer("Недоступно", show_alert=True)
        return
    user_id = int(cb.data.split(":")[1])
    note = ""
    try:
        await bot.send_message(
            user_id,
            "Твоя заявка одобрена! Скоро с тобой свяжутся менеджеры: они пришлют реквизиты "
            "для оплаты на указанную почту.",
        )
    except Exception:
        # участник мог заблокировать бота; решение всё равно сохраняем и предупреждаем модераторов
        logging.exception("Не удалось отправить решение участнику %s", user_id)
        note = " (участнику не доставлено)"
    await save_status(user_id, "одобрено")
    await update_card(
        bot, cb.message.chat.id, cb.message.message_id,
        card_base_text(cb.message.text) + STATUS_MARK + "✅ Одобрено" + note, user_id,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("mod_no:"))
async def moderate_reject_start(cb: CallbackQuery, state: FSMContext):
    if cb.message.chat.id != ADMIN_CHAT_ID:
        await cb.answer("Недоступно", show_alert=True)
        return
    user_id = int(cb.data.split(":")[1])
    # отказ не применяем сразу: сначала ждём причину от модератора
    await state.set_state(Moderation.reject_reason)
    await state.update_data(
        target_user_id=user_id,
        card_message_id=cb.message.message_id,
        card_text=card_base_text(cb.message.text),
    )
    await cb.message.reply(
        "Напиши причину отказа одним сообщением: я отправлю её участнику.\n"
        "Чтобы передумать, отправь /cancel."
    )
    await cb.answer()


@router.message(Moderation.reject_reason, F.text, F.chat.id == ADMIN_CHAT_ID)
async def moderate_reject_reason(message: Message, state: FSMContext, bot: Bot):
    reason = message.text.strip()
    if not reason:
        await message.reply("Причина не может быть пустой. Напиши её текстом.")
        return
    data = await state.get_data()
    user_id = data["target_user_id"]
    note = ""
    try:
        await bot.send_message(
            user_id,
            f"К сожалению, заявка не прошла модерацию.\n\nКомментарий модератора: {reason}",
        )
    except Exception:
        logging.exception("Не удалось отправить отказ участнику %s", user_id)
        note = " (участнику не доставлено)"
    await save_status(user_id, "отклонено")
    await update_card(
        bot, ADMIN_CHAT_ID, data["card_message_id"],
        data["card_text"] + STATUS_MARK + "❌ Отклонено" + note, user_id,
    )
    await state.clear()
    await message.reply("Отказ с комментарием отправлен участнику.")


# ---------- выгрузка списка заявок (только в админ-чате) ----------

@router.message(Command("export"))
async def cmd_export(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        return
    if not os.path.exists(APPLICATIONS_CSV):
        await message.answer("Заявок пока нет.")
        return
    await message.answer_document(FSInputFile(APPLICATIONS_CSV), caption="Все заявки на текущий момент")


# ---------- запасной ответ (ставим в конец, чтобы не перехватывать команды и админ-чат) ----------

@router.message(StateFilter(None), F.text, F.chat.type == "private")
async def no_state_text(message: Message):
    # текст вне анкеты в личке: бот перезапускался и потерял шаг заполнения, сообщаем об этом
    await message.answer(
        "Похоже, заполнение заявки сбросилось (бот перезапускался). Нажми «📝 Подать заявку», чтобы начать заново.",
        reply_markup=MAIN_MENU,
    )


# ---------- запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    # MemoryStorage хранит анкеты в памяти: при перезапуске незаконченные анкеты сбросятся
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
