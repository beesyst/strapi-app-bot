import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.paths import PROJECT_ROOT as _PROJECT_ROOT

PROJECT_ROOT = _PROJECT_ROOT


# Корневые пути проекта / окружения
ROOT_DIR = PROJECT_ROOT
VENV_PATH = os.path.join(ROOT_DIR, "venv")
REQUIREMENTS_PATH = os.path.join(ROOT_DIR, "requirements.txt")

# Node-проект живет в core/, а JS-скрипты парсеров — в core/parser/
NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
PACKAGE_JSON_PATH = os.path.join(NODE_CORE_DIR, "package.json")

# Создать виртуальное окружение, если его нет
if not os.path.isdir(VENV_PATH):
    print("[install] Создаю виртуальное окружение venv ...")
    subprocess.run(["python3", "-m", "venv", VENV_PATH], check=True)

# pip install -r requirements.txt (через pip из venv)
print("[install] Устанавливаю Python зависимости ...")
subprocess.run(
    [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_PATH], check=True
)


# npm install - только если есть package.json в core/
if os.path.isfile(PACKAGE_JSON_PATH):
    if not os.path.isdir(os.path.join(NODE_CORE_DIR, "node_modules")):
        print("[install] Устанавливаю Node.js зависимости ...")
        subprocess.run(["npm", "install"], cwd=NODE_CORE_DIR, check=True)

    # playwright browsers (через npx из core/, где лежит package.json)
    print("[install] Устанавливаю браузеры Playwright ...")
    subprocess.run(["npx", "playwright", "install"], cwd=NODE_CORE_DIR, check=True)
else:
    print("[install] Пропускаю npm и playwright: package.json не найден в core/")
