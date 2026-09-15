import json
import logging
import os
from datetime import date, datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import schedule_source


logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задана переменная BOT_TOKEN")

# Как часто автоматически перечитывать таблицу с расписанием
# (в секундах). 5400 секунд = 1.5 часа.
SCHEDULE_REFRESH_SECONDS = 5400

# Файл, в который сохраняется последняя успешно скачанная
# версия расписания — на случай, если Google Таблицы будут
# временно недоступны при перезапуске бота.
SCHEDULE_CACHE_FILE = "schedule_cache.json"


# ============================================================
# МЕТАДАННЫЕ СЕМЕСТРА
# ============================================================
#
# Эти данные не хранятся в Google Таблице (там нет дат начала
# семестра и т.п.), поэтому они заданы прямо здесь.
# Меняются обычно один раз в семестр.

META = {
    "groups": {
        "510-1": "10.2-510, 1 подгруппа",
        "510-2": "10.2-510, 2 подгруппа",
        "511-1": "10.2-511, 1 подгруппа",
        "511-2": "10.2-511, 2 подгруппа",
    },
    "semester_start": "2026-09-01",
    "total_weeks": 17,
}

SEMESTER_START = datetime.strptime(
    META["semester_start"],
    "%Y-%m-%d"
).date()

TOTAL_WEEKS = META["total_weeks"]

GROUPS = list(META["groups"].keys())


# ============================================================
# ЗАГРУЗКА РАСПИСАНИЯ ИЗ GOOGLE ТАБЛИЦЫ
# ============================================================

# Глобальная переменная с текущим расписанием.
# Обновляется функцией refresh_schedule().
SCHEDULE = {}


def _load_cached_schedule() -> dict:
    """
    Пытается загрузить расписание из локального файла-кэша.
    Используется, если скачать таблицу не удалось
    (например, при первом запуске бота нет интернета).
    """

    if not os.path.exists(SCHEDULE_CACHE_FILE):
        return {}

    try:
        with open(SCHEDULE_CACHE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cached_schedule(schedule: dict) -> None:
    try:
        with open(SCHEDULE_CACHE_FILE, "w", encoding="utf-8") as file:
            json.dump(schedule, file, ensure_ascii=False, indent=2)
    except OSError:
        logger.warning("Не удалось сохранить кэш расписания на диск.")


def refresh_schedule() -> bool:
    """
    Скачивает свежую версию таблицы и обновляет глобальный SCHEDULE.

    Возвращает True, если обновление прошло успешно,
    и False, если возникла ошибка (тогда старое расписание
    остаётся как есть).
    """

    global SCHEDULE

    try:
        new_schedule = schedule_source.load_schedule(TOTAL_WEEKS)
    except Exception as error:  # noqa: BLE001 — здесь ловим любые сбои сети/парсинга
        logger.warning(
            "Не удалось обновить расписание из Google Таблицы: %s",
            error,
        )
        return False

    SCHEDULE = new_schedule
    _save_cached_schedule(new_schedule)

    logger.info("Расписание успешно обновлено из Google Таблицы.")

    return True


def load_initial_schedule() -> None:
    """
    Вызывается один раз при старте бота.
    Сначала пытается скачать актуальную таблицу,
    а если не получилось — использует последнюю сохранённую
    версию, чтобы бот всё равно смог запуститься.
    """

    global SCHEDULE

    if refresh_schedule():
        return

    cached = _load_cached_schedule()

    if cached:
        SCHEDULE = cached
        logger.warning(
            "Использую сохранённую ранее копию расписания "
            "(не удалось получить свежую версию)."
        )
    else:
        logger.error(
            "Расписание недоступно: не удалось ни скачать таблицу, "
            "ни найти локальный кэш."
        )


async def scheduled_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая задача автообновления расписания (JobQueue).
    """

    refresh_schedule()


# ============================================================
# ДНИ НЕДЕЛИ
# ============================================================

WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

WEEKDAY_NAMES = {
    "понедельник": "Понедельник",
    "вторник": "Вторник",
    "среда": "Среда",
    "четверг": "Четверг",
    "пятница": "Пятница",
    "суббота": "Суббота",
    "воскресенье": "Воскресенье",
}


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def groups_keyboard():
    """
    Кнопки выбора группы.
    """

    return ReplyKeyboardMarkup(
        [
            ["510-1", "510-2"],
            ["511-1", "511-2"],
        ],
        resize_keyboard=True
    )


def main_keyboard():
    """
    Главное меню после выбора группы.
    """

    return ReplyKeyboardMarkup(
        [
            ["📅 Сегодня", "📅 Завтра"],
            ["📚 Эта неделя"],
            ["📖 Расписание на следующую неделю"],
            ["🔄 Сменить группу"],
        ],
        resize_keyboard=True
    )


# ============================================================
# ОПРЕДЕЛЕНИЕ УЧЕБНОЙ НЕДЕЛИ
# ============================================================

def get_semester_week(target_date: date) -> int:
    """
    Определяет номер недели семестра.

    semester_start = начало 1-й недели.

    Например:

    01.09 - 07.09 -> неделя 1
    08.09 - 14.09 -> неделя 2
    15.09 - 21.09 -> неделя 3

    и т.д.
    """

    # Учебная неделя начинается в понедельник. Поскольку 01.09.2026
    # выпадает на вторник, первая учебная неделя длится 01.09-06.09,
    # вторая — 07.09-13.09, третья — 14.09-20.09 и т.д.
    semester_first_monday = (
        SEMESTER_START - timedelta(days=SEMESTER_START.weekday())
    )

    days_from_first_monday = (
        target_date - semester_first_monday
    ).days

    week = days_from_first_monday // 7 + 1

    return week


# ============================================================
# ПРОВЕРКА: ЕСТЬ ЛИ РАСПИСАНИЕ НА ЭТУ НЕДЕЛЮ
# ============================================================

def get_lessons_for_date(group: str, target_date: date):
    """
    Возвращает пары конкретной группы
    на конкретную дату.

    Учитываются:
    - день недели;
    - номер учебной недели;
    - массив weeks из JSON.
    """

    semester_week = get_semester_week(target_date)

    weekday = WEEKDAYS[target_date.weekday()]

    result = []

    # Если такой группы нет
    if group not in SCHEDULE:
        return result

    group_schedule = SCHEDULE[group]

    # Если в этот день нет расписания
    if weekday not in group_schedule:
        return result

    day_schedule = group_schedule[weekday]

    # Проходим по времени
    for time, lessons in day_schedule.items():

        # lessons — это список занятий
        for lesson in lessons:

            lesson_weeks = lesson.get("weeks", [])

            # Проверяем, проводится ли занятие
            # на текущей учебной неделе
            if semester_week in lesson_weeks:

                result.append({
                    "time": time,
                    "text": lesson.get("text", "")
                })

    return result


# ============================================================
# ФОРМАТИРОВАНИЕ РАСПИСАНИЯ НА ДЕНЬ
# ============================================================

def format_day_schedule(
    group: str,
    target_date: date
) -> str:

    weekday = WEEKDAYS[target_date.weekday()]
    weekday_name = WEEKDAY_NAMES[weekday]

    semester_week = get_semester_week(target_date)

    lessons = get_lessons_for_date(
        group,
        target_date
    )

    result = []

    result.append(
        f"📅 {weekday_name}, "
        f"{target_date.strftime('%d.%m.%Y')}"
    )

    result.append(
        f"👥 Группа: {group}"
    )

    result.append(
        f"📚 Учебная неделя: {semester_week}"
    )

    result.append("")

    # До начала или после окончания семестра
    if semester_week < 1:
        result.append(
            "Семестр ещё не начался."
        )
        return "\n".join(result)

    if semester_week > TOTAL_WEEKS:
        result.append(
            "Расписание семестра на эту дату "
            "не предусмотрено."
        )
        return "\n".join(result)

    # Если пар нет
    if not lessons:
        result.append(
            "🎉 Пар сегодня нет."
        )

        return "\n".join(result)

    # Выводим пары
    for number, lesson in enumerate(
        lessons,
        start=1
    ):

        result.append(
            f"🕐 {lesson['time']}"
        )

        result.append(
            f"📖 {lesson['text']}"
        )

        result.append("")

    return "\n".join(result)


# ============================================================
# РАСПИСАНИЕ НА НЕДЕЛЮ
# ============================================================

def format_week_schedule(
    group: str,
    start_date: date
) -> str:

    semester_week = get_semester_week(
        start_date
    )

    result = []

    result.append(
        f"📚 Расписание группы {group}"
    )

    result.append(
        f"Учебная неделя: {semester_week}"
    )

    result.append("")

    # 6 дней (без воскресенья — оно всегда не учебное)
    for i in range(6):

        current_date = (
            start_date +
            timedelta(days=i)
        )

        weekday = WEEKDAYS[
            current_date.weekday()
        ]

        weekday_name = WEEKDAY_NAMES[
            weekday
        ]

        lessons = get_lessons_for_date(
            group,
            current_date
        )

        result.append(
            f"━━━━━━━━━━━━━━━━━━"
        )

        result.append(
            f"📅 {weekday_name} "
            f"{current_date.strftime('%d.%m')}"
        )

        if not lessons:

            result.append(
                "Нет пар."
            )

            continue

        for lesson in lessons:

            result.append(
                f"\n🕐 {lesson['time']}"
            )

            result.append(
                f"📖 {lesson['text']}"
            )

    return "\n".join(result)


# ============================================================
# /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "👋 Привет!\n\n"
        "Я бот с расписанием.\n"
        "Сначала выбери свою группу:",
        reply_markup=groups_keyboard()
    )


# ============================================================
# ВЫБОР ГРУППЫ
# ============================================================

async def select_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    group = update.message.text

    if group not in GROUPS:
        return

    # Запоминаем группу пользователя
    context.user_data["group"] = group

    group_name = META["groups"].get(
        group,
        group
    )

    await update.message.reply_text(
        f"✅ Группа выбрана!\n\n"
        f"Группа: {group}\n"
        f"{group_name}\n\n"
        f"Теперь выбери, что показать:",
        reply_markup=main_keyboard()
    )


# ============================================================
# СЕГОДНЯ
# ============================================================

async def show_today(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    group = context.user_data.get("group")

    if not group:
        await update.message.reply_text(
            "Сначала выбери группу.\n"
            "Используй /start"
        )
        return

    today = date.today()

    text = format_day_schedule(
        group,
        today
    )

    await update.message.reply_text(
        text
    )


# ============================================================
# ЗАВТРА
# ============================================================

async def show_tomorrow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    group = context.user_data.get("group")

    if not group:
        await update.message.reply_text(
            "Сначала выбери группу.\n"
            "Используй /start"
        )
        return

    tomorrow = (
        date.today() +
        timedelta(days=1)
    )

    text = format_day_schedule(
        group,
        tomorrow
    )

    await update.message.reply_text(
        text
    )


# ============================================================
# ЭТА НЕДЕЛЯ
# ============================================================

async def show_current_week(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    group = context.user_data.get("group")

    if not group:
        await update.message.reply_text(
            "Сначала выбери группу.\n"
            "Используй /start"
        )
        return

    today = date.today()

    # Понедельник текущей недели
    monday = (
        today -
        timedelta(days=today.weekday())
    )

    text = format_week_schedule(
        group,
        monday
    )

    await update.message.reply_text(
        text
    )


# ============================================================
# РАСПИСАНИЕ НА СЛЕДУЮЩУЮ НЕДЕЛЮ
# ============================================================

async def show_next_week_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    group = context.user_data.get("group")

    if not group:
        await update.message.reply_text(
            "Сначала выбери группу.\n"
            "Используй /start"
        )
        return

    today = date.today()

    this_monday = (
        today -
        timedelta(days=today.weekday())
    )

    next_monday = (
        this_monday +
        timedelta(days=7)
    )

    text = format_week_schedule(
        group,
        next_monday
    )

    await update.message.reply_text(
        text
    )


# ============================================================
# СМЕНИТЬ ГРУППУ
# ============================================================

async def change_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "Выбери новую группу:",
        reply_markup=groups_keyboard()
    )


# ============================================================
# /reload — РУЧНОЕ ОБНОВЛЕНИЕ РАСПИСАНИЯ
# ============================================================

async def reload_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """
    Позволяет сразу подтянуть свежую версию таблицы,
    не дожидаясь автоматического обновления по таймеру.
    """

    await update.message.reply_text(
        "🔄 Обновляю расписание из таблицы..."
    )

    success = refresh_schedule()

    if success:
        await update.message.reply_text(
            "✅ Расписание обновлено!"
        )
    else:
        await update.message.reply_text(
            "⚠️ Не удалось обновить расписание "
            "(таблица недоступна). "
            "Использую последнюю сохранённую версию."
        )


# ============================================================
# ОБРАБОТКА КНОПОК
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text

    # -----------------------------------------
    # Если пользователь выбирает группу
    # -----------------------------------------

    if text in GROUPS:

        await select_group(
            update,
            context
        )

        return

    # -----------------------------------------
    # Сменить группу
    # -----------------------------------------

    if text == "🔄 Сменить группу":

        await change_group(
            update,
            context
        )

        return

    # -----------------------------------------
    # Сегодня
    # -----------------------------------------

    if text == "📅 Сегодня":

        await show_today(
            update,
            context
        )

        return

    # -----------------------------------------
    # Завтра
    # -----------------------------------------

    if text == "📅 Завтра":

        await show_tomorrow(
            update,
            context
        )

        return

    # -----------------------------------------
    # Эта неделя
    # -----------------------------------------

    if text == "📚 Эта неделя":

        await show_current_week(
            update,
            context
        )

        return

    # -----------------------------------------
    # Расписание на следующую неделю
    # -----------------------------------------

    if text == "📖 Расписание на следующую неделю":

        await show_next_week_schedule(
            update,
            context
        )

        return


# ============================================================
# ЗАПУСК
# ============================================================

def main():

    print("🚀 Бот запускается...")

    print("📥 Загружаю расписание из Google Таблицы...")
    load_initial_schedule()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    # Команда /start
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # Команда /reload — обновить расписание вручную
    app.add_handler(
        CommandHandler(
            "reload",
            reload_schedule
        )
    )

    # Все обычные сообщения
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    # Автоматическое обновление расписания по таймеру
    app.job_queue.run_repeating(
        scheduled_refresh_job,
        interval=SCHEDULE_REFRESH_SECONDS,
        first=SCHEDULE_REFRESH_SECONDS,
    )

    print("✅ Бот запущен!")

    app.run_polling()


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

if __name__ == "__main__":
    main()
