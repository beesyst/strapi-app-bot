import logging
import os
from datetime import datetime

from core.paths import CONFIG_JSON, LOGS_DIR

# Пути к директориям и лог-файлам
os.makedirs(LOGS_DIR, exist_ok=True)

# Карта имен логгеров и их файлов
LOG_PATHS = {
    "host": os.path.join(LOGS_DIR, "host.log"),
    "ai": os.path.join(LOGS_DIR, "ai.log"),
    "strapi": os.path.join(LOGS_DIR, "strapi.log"),
    "setup": os.path.join(LOGS_DIR, "setup.log"),
}

# Формат логирования
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
STRAPI_LOG_FORMAT = "%(asctime)s [%(levelname)s] - %(message)s"
AI_LOG_FORMAT = "%(asctime)s [%(levelname)s] - %(message)s"
DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] - [%(name)s] %(message)s"


# Фабрика логгеров
def get_logger(name: str, level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        log_path = LOG_PATHS.get(name, LOG_PATHS["host"])
        if name == "strapi" or name == "ai":
            formatter = logging.Formatter(AI_LOG_FORMAT, DATE_FORMAT)
        else:
            formatter = logging.Formatter(DEFAULT_LOG_FORMAT, DATE_FORMAT)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.setLevel(level)
        logger.addHandler(handler)
    return logger


# Очистка всех лог-файлов
def clear_all_logs(logs_dir=LOGS_DIR):
    for fname in os.listdir(logs_dir):
        if fname.endswith(".log"):
            fpath = os.path.join(logs_dir, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now().strftime(DATE_FORMAT)} [INFO] - [log_utils] Лог очищен\n"
                )


# Иниц setup.log при запуске
def init_setup_log():
    with open(LOG_PATHS["setup"], "w", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().strftime(DATE_FORMAT)} [INFO] - [setup] setup.log запущен\n\n"
        )


# Авто-очистка логов при запуске, если выставлен clear_logs в config.json
def auto_clear_logs_if_needed():
    try:
        import json

        with open(CONFIG_JSON, "r", encoding="utf-8") as f:
            config = json.load(f)
        if config.get("clear_logs", False):
            clear_all_logs()
    except Exception as e:
        logger = get_logger("host")
        logger.warning(f"Не удается автоматически очистить лог: {e}")
