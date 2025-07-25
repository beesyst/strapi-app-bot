import json
import logging
import os
from datetime import datetime

# Путь до корня и лог-файлов
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
HOST_LOG = os.path.join(LOGS_DIR, "host.log")
AI_LOG = os.path.join(LOGS_DIR, "ai.log")
STRAPI_LOG = os.path.join(LOGS_DIR, "strapi.log")

# Глобальный логгер для host.log
host_handler = logging.FileHandler(HOST_LOG, encoding="utf-8")
host_handler.setLevel(logging.INFO)
host_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] - [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
)
host_handler.setFormatter(host_formatter)

# Корневой логгер только для host.log
logging.basicConfig(
    level=logging.INFO,
    handlers=[host_handler],
)

# Логгер для AI (ai.log)
ai_logger = logging.getLogger("ai")
ai_handler = logging.FileHandler(AI_LOG, encoding="utf-8")
ai_handler.setLevel(logging.INFO)
ai_handler.setFormatter(host_formatter)
ai_logger.propagate = False
ai_logger.addHandler(ai_handler)

# Логгер для Strapi (strapi.log)
strapi_logger = logging.getLogger("strapi")
strapi_handler = logging.FileHandler(STRAPI_LOG, encoding="utf-8")
strapi_handler.setLevel(logging.INFO)
strapi_handler.setFormatter(host_formatter)
strapi_logger.propagate = False
strapi_logger.addHandler(strapi_handler)


# Универсальные функции логирования (host.log)
def log_info(msg):
    logging.getLogger("host").info(msg)


def log_warning(msg):
    logging.getLogger("host").warning(msg)


def log_error(msg):
    logging.getLogger("host").error(msg)


def log_critical(msg):
    logging.getLogger("host").critical(msg)


# Логирование для AI (ai.log)
def ai_log(msg, level="info"):
    if level == "info":
        ai_logger.info(msg)
    elif level == "warning":
        ai_logger.warning(msg)
    elif level == "error":
        ai_logger.error(msg)
    elif level == "critical":
        ai_logger.critical(msg)


# Логирование для Strapi (strapi.log)
def strapi_log(msg, level="info"):
    """Лог в strapi.log через strapi_logger"""
    if level == "info":
        strapi_logger.info(msg)
    elif level == "warning":
        strapi_logger.warning(msg)
    elif level == "error":
        strapi_logger.error(msg)
    elif level == "critical":
        strapi_logger.critical(msg)


# Очистка всех лог-файлов (по умолчанию — LOGS_DIR)
def clear_all_logs(logs_dir=LOGS_DIR):
    """Очистка всех лог-файлов в logs_dir"""
    for fname in os.listdir(logs_dir):
        fpath = os.path.join(logs_dir, fname)
        if os.path.isfile(fpath) and fname.endswith(".log"):
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] - [log_utils] Лог очищен\n"
                )


# Инициализация setup.log (перезапись)
def init_setup_log(logs_dir=LOGS_DIR, filename="setup.log"):
    setup_log_path = os.path.join(logs_dir, filename)
    with open(setup_log_path, "w", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] - [log_utils] setup.log запущен\n\n"
        )


# Авто-очистка логов при запуске, если clear_logs = true
def auto_clear_logs_if_needed():
    CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        if config.get("clear_logs", False):
            clear_all_logs()
    except Exception as e:
        log_warning(f"Не удается автоматически очистить лог: {e}")
