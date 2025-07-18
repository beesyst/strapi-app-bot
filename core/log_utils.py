import json
import logging
import os
from datetime import datetime

# Путь до корня и лог-файлов
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, "host.log")
AI_LOG = os.path.join(LOGS_DIR, "ai.log")
STRAPI_LOG = os.path.join(LOGS_DIR, "strapi.log")

# Настройка логгера (host.log — для системных логов)
logging.basicConfig(
    filename=LOG_FILE,
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# Универсальные лог-функции
def log_info(msg):
    logging.info(msg)


def log_warning(msg):
    logging.warning(msg)


def log_error(msg):
    logging.error(msg)


def log_critical(msg):
    logging.critical(msg)


# Очистка всех лог-файлов (по умолчанию — в LOGS_DIR)
def clear_all_logs(logs_dir=LOGS_DIR):
    for fname in os.listdir(logs_dir):
        fpath = os.path.join(logs_dir, fname)
        if os.path.isfile(fpath) and fname.endswith(".log"):
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] Log file cleared\n"
                )


# Инициализация setup.log (перезапись)
def init_setup_log(logs_dir=LOGS_DIR, filename="setup.log"):
    setup_log_path = os.path.join(logs_dir, filename)
    with open(setup_log_path, "w", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] setup.log started\n\n"
        )


# При запуске авто-очистка логов, если clear_logs = true
def auto_clear_logs_if_needed():
    CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        if config.get("clear_logs", False):
            clear_all_logs()
    except Exception as e:
        log_warning(f"Could not auto-clear logs: {e}")
