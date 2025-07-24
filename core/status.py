from datetime import datetime

from core.log_utils import log_critical, log_error, log_info

# Допустимые статусы
ADD = "add"
UPDATE = "update"
SKIP = "skip"
ERROR = "error"
STATUSES = {ADD, UPDATE, SKIP, ERROR}

# Ключевые поля для сравнения проектов
MAIN_FIELDS = ["name", "svgLogo", "socialLinks", "coinData"]


# Текущее время
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Сравнение только по ключевым полям
def compare_main_fields(d1, d2):
    for k in MAIN_FIELDS:
        if d1.get(k) != d2.get(k):
            return False
    return True


# Универсальный логгер для [status]
def log_status_info(status, app, domain, url, extra=""):
    msg = f"[{status}] {app} - {domain} - {url}"
    if extra:
        msg += f" [{extra}]"
    log_info(msg)


# Для сбора/обновления main.json
def log_mainjson_status(status, app, domain, url, error_msg=""):
    if status == ERROR:
        log_error(f"[error] {app} - {domain} - {url} [{error_msg}]")
    elif status in STATUSES:
        log_status_info(status, app, domain, url)
    else:
        log_error(f"[invalid_status] {status} for {app} - {domain} - {url}")


# Для отправки/обновления в Strapi
def log_strapi_status(status, app, domain, url, error_msg=""):
    if status == ERROR:
        log_critical(f"[error] {app} - {domain} - {url} [{error_msg}]")
    elif status in STATUSES:
        log_status_info(status, app, domain, url)
    else:
        log_error(f"[invalid_status] {status} for {app} - {domain} - {url}")


# Проверка необходимости обновления main.json
def check_mainjson_status(old_data, new_data):
    if compare_main_fields(old_data, new_data):
        return SKIP
    else:
        return UPDATE


# Проверка необходимости обновления Strapi
def check_strapi_status(main_data, strapi_data):
    if compare_main_fields(main_data, strapi_data):
        return SKIP
    else:
        return UPDATE


# Проверка: все ли ключевые поля заполнены
def check_fields_filled(data, fields=None):
    fields = fields or MAIN_FIELDS
    for k in fields:
        v = data.get(k)
        if v is None or v == "" or (isinstance(v, dict) and not v):
            return False
    return True


# Получить diff по основным полям
def diff_main_fields(d1, d2):
    diffs = []
    for k in MAIN_FIELDS:
        if d1.get(k) != d2.get(k):
            diffs.append(k)
    return diffs
