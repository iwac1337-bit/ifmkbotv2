# ============================================================
# ЗАГРУЗКА И РАЗБОР РАСПИСАНИЯ ИЗ GOOGLE ТАБЛИЦЫ
# ============================================================
#
# Используется официальный Google Sheets API (v4), а не просто
# экспорт в CSV. Причина: в таблице встречаются объединённые
# по горизонтали ячейки — это означает, что для нескольких
# групп проходит ОДНА общая (потоковая) лекция. При экспорте
# в CSV информация об объединении ячеек теряется (текст остаётся
# только в левой верхней ячейке объединения), поэтому такие
# занятия было бы видно только у одной группы из нескольких.
# Sheets API отдаёт список объединённых диапазонов явно —
# это позволяет корректно "размножить" такую лекцию на все
# нужные группы.
#
# Для работы нужен Google API-ключ (переменная окружения
# GOOGLE_API_KEY). Таблица должна оставаться открытой
# "Доступно всем, у кого есть ссылка" (роль "Читатель") —
# тогда чтение по API-ключу работает без OAuth
# и без сервисного аккаунта.

import os
import re

import requests


# ------------------------------------------------------------
# НАСТРОЙКИ ТАБЛИЦЫ
# ------------------------------------------------------------

# ID таблицы — это часть ссылки между /d/ и /edit
SPREADSHEET_ID = "1uD_eZVFkavO3cQHEAQjDlsx7pkUrygu1"

# gid листа — это число после #gid= в ссылке.
# В терминах Sheets API это же число называется sheetId.
SHEET_ID = 413092878

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

SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


def _get_api_key() -> str:
    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Не задана переменная окружения GOOGLE_API_KEY. "
            "Она нужна для чтения таблицы через Google Sheets API "
            "(см. инструкцию по получению API-ключа)."
        )

    return api_key


def _find_sheet_title(api_key: str) -> str:
    """
    Находит название листа (вкладки) по его sheetId (gid).
    Название нужно, т.к. Sheets API запрашивает данные
    по имени листа, а не по gid.
    """

    url = f"{SHEETS_API_BASE}/{SPREADSHEET_ID}"

    response = requests.get(
        url,
        params={
            "key": api_key,
            "fields": "sheets.properties(sheetId,title)",
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()

    for sheet in payload.get("sheets", []):
        properties = sheet.get("properties", {})

        if properties.get("sheetId") == SHEET_ID:
            return properties["title"]

    raise RuntimeError(
        f"Лист с gid={SHEET_ID} не найден в таблице. "
        "Проверьте SHEET_ID в schedule_source.py."
    )


def _fetch_grid_data(api_key: str, sheet_title: str) -> dict:
    """
    Запрашивает содержимое листа вместе со списком
    объединённых ячеек (merges).
    """

    url = f"{SHEETS_API_BASE}/{SPREADSHEET_ID}"

    response = requests.get(
        url,
        params={
            "key": api_key,
            "ranges": sheet_title,
            "includeGridData": "true",
            "fields": "sheets(merges,data.rowData.values.formattedValue)",
        },
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    sheets = payload.get("sheets", [])

    if not sheets:
        raise RuntimeError("Google Sheets API вернул пустой ответ.")

    return sheets[0]


def _rows_from_grid(sheet_data: dict) -> list[list[str]]:
    """
    Превращает "сырые" данные листа в простую таблицу
    строк-списков строк (аналог того, что даёт csv.reader).
    """

    row_data = (
        sheet_data
        .get("data", [{}])[0]
        .get("rowData", [])
    )

    rows = []

    for row in row_data:
        values = row.get("values", [])

        cells = [
            cell.get("formattedValue", "")
            for cell in values
        ]

        rows.append(cells)

    return rows


def _group_merges_by_row(sheet_data: dict) -> dict:
    """
    Строит словарь: номер строки -> список диапазонов колонок
    (start, end), которые объединены между собой в этой строке
    И относятся к столбцам групп (C..F). Диапазон end не включён
    (как в API): например (2, 4) значит "столбцы C и D объединены".
    """

    merges_by_row = {}

    for merge in sheet_data.get("merges", []):
        start_col = merge.get("startColumnIndex", 0)
        end_col = merge.get("endColumnIndex", 0)

        # Нас интересуют только объединения внутри столбцов групп
        clipped_start = max(start_col, FIRST_GROUP_COLUMN)
        clipped_end = min(end_col, LAST_GROUP_COLUMN + 1)

        # Объединение затрагивает минимум 2 столбца групп —
        # иначе это не "потоковая лекция на несколько групп"
        if clipped_end - clipped_start < 2:
            continue

        start_row = merge.get("startRowIndex", 0)
        end_row = merge.get("endRowIndex", start_row + 1)

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
    Полный цикл: получить данные листа через Google Sheets API
    и разобрать их в формат расписания бота.
    Исключения наружу не гасит — их обрабатывает вызывающий код.
    """

    api_key = _get_api_key()
    sheet_title = _find_sheet_title(api_key)
    sheet_data = _fetch_grid_data(api_key, sheet_title)

    rows = _rows_from_grid(sheet_data)
    merges_by_row = _group_merges_by_row(sheet_data)

    return parse_schedule(rows, merges_by_row, total_weeks)
