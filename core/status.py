from datetime import datetime

from core.log_utils import get_logger

# Логгеры
logger = get_logger("orchestrator")
strapi_logger = get_logger("strapi")

# Статусы
ADD = "add"
UPDATE = "update"
SKIP = "skip"
ERROR = "error"
STATUSES = {ADD, UPDATE, SKIP, ERROR}

# Ключевые поля для сравнения проектов
MAIN_FIELDS = ["name", "svgLogo", "socialLinks", "coinData"]


# Текущее время (строка)
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Сравнение по ключевым полям
def compare_main_fields(d1, d2):
    for k in MAIN_FIELDS:
        if d1.get(k) != d2.get(k):
            return False
    return True


# Лог статуса операции
def log_status_info(status, app, domain, url, extra=""):
    msg = f"[{status}] {app} - {domain} - {url}"
    if extra:
        msg += f" [{extra}]"
    logger.info(msg)


# Лог статуса main.json
def log_mainjson_status(status, app, domain, url, error_msg=""):
    if status == ERROR:
        logger.error(f"[{status}] {app} - {domain} - {url} [{error_msg}]")
    elif status in STATUSES:
        log_status_info(status, app, domain, url)
    else:
        logger.error(f"[invalid_status] {status} for {app} - {domain} - {url}")


# Лог статуса Strapi
def log_strapi_status(status, app, domain, url, error_msg=""):
    if status == ERROR:
        strapi_logger.critical(f"[{status}] {app} - {domain} - {url} [{error_msg}]")
    elif status in STATUSES:
        strapi_logger.info(f"[{status}] {app} - {domain} - {url}")
    else:
        strapi_logger.error(f"[invalid_status] {status} for {app} - {domain} - {url}")


# Проверка обновления main.json
def check_mainjson_status(old_data, new_data):
    if compare_main_fields(old_data, new_data):
        return SKIP
    else:
        return UPDATE


# Проверка обновления Strapi
def check_strapi_status(main_data, strapi_data):
    if compare_main_fields(main_data, strapi_data):
        return SKIP
    else:
        return UPDATE


# Проверка заполненности ключевых полей
def check_fields_filled(data, fields=None):
    fields = fields or MAIN_FIELDS
    for k in fields:
        v = data.get(k)
        if v is None or v == "" or (isinstance(v, dict) and not v):
            return False
    return True


# Получение списка различий по ключевым полям
def diff_main_fields(d1, d2):
    diffs = []
    for k in MAIN_FIELDS:
        if d1.get(k) != d2.get(k):
            diffs.append(k)
    return diffs
