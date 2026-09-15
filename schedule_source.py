# ============================================================
# ЗАГРУЗКА И РАЗБОР РАСПИСАНИЯ ИЗ EXCEL-ФАЙЛА НА GOOGLE ДИСКЕ
# ============================================================
#
# Файл с расписанием — это настоящий Excel-файл (.xlsx),
# просто открытый для просмотра/редактирования в интерфейсе
# Google Диска. Google Sheets API с такими файлами не работает
# (он рассчитан только на "родные" Google Таблицы), поэтому
# используется другой подход:
#
#   1. Файл скачивается "как есть" через Google Drive API
#      (files.get?alt=media) — то есть ровно тот .xlsx,
#      который редактирует деканат.
#   2. Дальше он разбирается локально библиотекой openpyxl,
#      которая умеет читать в том числе объединённые ячейки —
#      это важно, т.к. объединение по горизонтали означает
#      общую (потоковую) лекцию сразу для нескольких групп.
#
# Для работы нужен Google API-ключ (переменная окружения
# GOOGLE_API_KEY) с включённым Google Drive API. Файл должен
# оставаться открытым "Доступно всем, у кого есть ссылка"
# (роль "Читатель") — тогда скачивание по API-ключу работает
# без OAuth и без сервисного аккаунта.

import io
import os
import re

import requests
from openpyxl import load_workbook


# ------------------------------------------------------------
# НАСТРОЙКИ ФАЙЛА
# ------------------------------------------------------------

# ID файла — это часть ссылки между /d/ и /edit
FILE_ID = "1uD_eZVFkavO3cQHEAQjDlsx7pkUrygu1"

# Название листа (вкладки) с расписанием.
# Если не найдётся лист с таким названием — будет
# использован первый лист в файле.
SHEET_NAME = "Лист1"

# Каким колонкам таблицы (0 = A, 1 = B, 2 = C, ...) соответствуют группы.
# Если в таблице поменяют порядок столбцов — поправить здесь.
GROUP_COLUMNS = {
    "510-1": 2,  # столбец C
    "510-2": 3,  # столбец D
    "511-1": 4,  # столбец E
    "511-2": 5,  # столбец F
}

FIRST_GROUP_COLUMN = min(GROUP_COLUMNS.values())
LAST_GROUP_COLUMN = max(GROUP_COLUMNS.values())  # включительно

DAY_NAMES = {
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
}

# Время в таблице записано как "08.30-10.00", в самом боте
# используется формат "08:30-10:00" — приводим к единому виду.
TIME_RE = re.compile(r"^\d{1,2}[.:]\d{2}-\d{1,2}[.:]\d{2}$")

# Ищем в тексте занятия куски вида "2-11 нед." или "3, 5-9 нед"
WEEKS_RE = re.compile(r"([\d,\s\-]+)\s*нед")

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3/files"


def _get_api_key() -> str:
    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Не задана переменная окружения GOOGLE_API_KEY. "
            "Она нужна для скачивания файла через Google Drive API "
            "(см. инструкцию по получению API-ключа)."
        )

    return api_key


def _fetch_xlsx_bytes(api_key: str) -> bytes:
    """
    Скачивает файл .xlsx "как есть" с Google Диска.
    """

    url = f"{DRIVE_API_BASE}/{FILE_ID}"

    response = requests.get(
        url,
        params={
            "key": api_key,
            "alt": "media",
        },
        timeout=30,
    )
    response.raise_for_status()

    return response.content


def _load_worksheet(xlsx_bytes: bytes):
    """
    Открывает нужный лист книги Excel.
    data_only=True — берём посчитанные значения ячеек,
    а не формулы (на случай, если где-то в таблице формулы).
    """

    workbook = load_workbook(io.BytesIO(xlsx_bytes), data_only=True)

    if SHEET_NAME in workbook.sheetnames:
        return workbook[SHEET_NAME]

    # Название листа могли изменить — на всякий случай
    # берём первый лист в книге, чтобы не падать совсем.
    return workbook.worksheets[0]


def _rows_from_worksheet(worksheet) -> list:
    """
    Превращает лист Excel в простую таблицу строк-списков строк.
    Пустые ячейки (в том числе "скрытые" части объединённых
    ячеек) превращаются в пустую строку "".
    """

    rows = []

    for row in worksheet.iter_rows(
        min_col=1,
        max_col=LAST_GROUP_COLUMN + 1,
        values_only=True,
    ):
        cells = [
            "" if value is None else str(value).strip()
            for value in row
        ]
        rows.append(cells)

    return rows


def _group_merges_by_row(worksheet) -> dict:
    """
    Строит словарь: номер строки (0-based) -> список диапазонов
    колонок (start, end), объединённых между собой в этой строке
    и относящихся к столбцам групп (C..F). Диапазон end не включён,
    как принято в остальном коде: например (2, 4) значит
    "столбцы C и D объединены".
    """

    merges_by_row = {}

    for merged_range in worksheet.merged_cells.ranges:

        # openpyxl использует 1-based индексы столбцов/строк,
        # причём min_row/max_row/min_col/max_col — включительно.
        start_col = merged_range.min_col - 1  # переводим в 0-based
        end_col = merged_range.max_col  # включительно -> уже "exclusive" в 0-based

        clipped_start = max(start_col, FIRST_GROUP_COLUMN)
        clipped_end = min(end_col, LAST_GROUP_COLUMN + 1)

        # Объединение затрагивает минимум 2 столбца групп —
        # иначе это не "потоковая лекция на несколько групп"
        if clipped_end - clipped_start < 2:
            continue

        start_row = merged_range.min_row - 1  # 0-based
        end_row = merged_range.max_row  # включительно -> exclusive в 0-based

        for row_index in range(start_row, end_row):
            merges_by_row.setdefault(row_index, []).append(
                (clipped_start, clipped_end)
            )

    return merges_by_row


def _resolve_source_column(column_index: int, row_merges: list) -> int:
    """
    Если столбец column_index входит в объединённый диапазон
    (row_merges для этой строки), возвращает начало этого
    диапазона — именно там физически лежит текст занятия.
    Если объединения нет — возвращает тот же столбец.
    """

    for start_col, end_col in row_merges:
        if start_col <= column_index < end_col:
            return start_col

    return column_index


def _parse_weeks(text: str, total_weeks: int) -> list:
    """
    Достаёт из текста занятия номера учебных недель.

    Если явных номеров недель не найдено — считаем,
    что занятие проходит каждую неделю семестра.
    """

    weeks = set()

    for match in WEEKS_RE.finditer(text):
        chunk = match.group(1)

        for part in chunk.split(","):
            part = part.strip()

            if not part:
                continue

            if "-" in part:
                start_str, _, end_str = part.partition("-")

                try:
                    start, end = int(start_str), int(end_str)
                except ValueError:
                    continue

                # Отбрасываем диапазоны, которые явно не могут быть
                # номерами учебных недель (например, случайно попавший
                # в текст номер аудитории вроде "ауд. 123, 7-10 нед.",
                # где "123" не относится к неделям).
                if not (1 <= start <= total_weeks and 1 <= end <= total_weeks):
                    continue

                if start <= end:
                    weeks.update(range(start, end + 1))
            else:
                try:
                    number = int(part)
                except ValueError:
                    continue

                if 1 <= number <= total_weeks:
                    weeks.add(number)

    if not weeks:
        return list(range(1, total_weeks + 1))

    return sorted(weeks)


def parse_schedule(rows: list, merges_by_row: dict, total_weeks: int) -> dict:
    """
    Разбирает таблицу (строки + информация об объединённых
    ячейках) в структуру:

    {
        "510-1": {
            "понедельник": {
                "08:30-10:00": [
                    {"text": "...", "weeks": [1, 2, 3, ...]},
                    ...
                ],
                ...
            },
            ...
        },
        ...
    }
    """

    schedule = {group: {} for group in GROUP_COLUMNS}

    current_day = None
    current_time = None

    for row_index, row in enumerate(rows):

        # На случай, если в какой-то строке меньше колонок, чем нужно
        if len(row) <= LAST_GROUP_COLUMN:
            row = row + [""] * (LAST_GROUP_COLUMN + 1 - len(row))

        day_cell = row[0].strip().lower()
        time_cell = row[1].strip()

        if day_cell in DAY_NAMES:
            current_day = day_cell
            current_time = None

        # Пока не встретили первый день недели — это ещё
        # "шапка" документа (название, гриф согласования и т.п.)
        if current_day is None:
            continue

        if time_cell:
            if TIME_RE.match(time_cell):
                current_time = time_cell.replace(".", ":")
            else:
                # Строка с "хвостом" документа после расписания
                # (подписи ответственных лиц и т.п.) — на этом
                # разбор таблицы можно закончить.
                break

        if current_time is None:
            continue

        row_merges = merges_by_row.get(row_index, [])

        for group, col_index in GROUP_COLUMNS.items():

            source_col = _resolve_source_column(col_index, row_merges)
            cell = row[source_col].strip()

            if not cell:
                continue

            # В одной ячейке может быть несколько занятий,
            # разделённых "//"
            for entry in cell.split("//"):

                entry = entry.strip(" \n\t;")

                if not entry:
                    continue

                weeks = _parse_weeks(entry, total_weeks)

                day_schedule = schedule[group].setdefault(
                    current_day, {}
                )

                lessons = day_schedule.setdefault(
                    current_time, []
                )

                lessons.append({
                    "text": entry,
                    "weeks": weeks,
                })

    return schedule


def load_schedule(total_weeks: int) -> dict:
    """
    Полный цикл: скачать xlsx-файл через Google Drive API
    и разобрать его в формат расписания бота.
    Исключения наружу не гасит — их обрабатывает вызывающий код.
    """

    api_key = _get_api_key()
    xlsx_bytes = _fetch_xlsx_bytes(api_key)
    worksheet = _load_worksheet(xlsx_bytes)

    rows = _rows_from_worksheet(worksheet)
    merges_by_row = _group_merges_by_row(worksheet)

    return parse_schedule(rows, merges_by_row, total_weeks)
