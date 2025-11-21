import os

# Абсолютный корень проекта
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Подкаталоги
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")
STORAGE_DIR = os.path.join(PROJECT_ROOT, "storage")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

# Частные подпапки
STORAGE_APPS_DIR = os.path.join(STORAGE_DIR, "apps")

# Файлы
CONFIG_JSON = os.path.join(CONFIG_DIR, "config.json")
PROMPT_JSON = os.path.join(CONFIG_DIR, "prompt.json")
CONTENT_TEMPLATE = os.path.join(TEMPLATES_DIR, "content_template.json")
MAIN_TEMPLATE = os.path.join(TEMPLATES_DIR, "main_template.json")
