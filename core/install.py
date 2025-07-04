import os
import subprocess

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PATH = os.path.join(ROOT_DIR, "venv")
REQUIREMENTS_PATH = os.path.join(ROOT_DIR, "requirements.txt")
NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
PACKAGE_JSON_PATH = os.path.join(NODE_CORE_DIR, "package.json")

# Создать виртуальное окружение, если его нет
if not os.path.isdir(VENV_PATH):
    print("[install] Создаю виртуальное окружение venv ...")
    subprocess.run(["python3", "-m", "venv", VENV_PATH], check=True)

# pip install -r requirements.txt (через pip из venv)
pip_path = os.path.join(VENV_PATH, "bin", "pip")
if not os.path.exists(pip_path):
    pip_path = os.path.join(VENV_PATH, "Scripts", "pip.exe")
print("[install] Устанавливаю Python зависимости ...")
subprocess.run([pip_path, "install", "-r", REQUIREMENTS_PATH], check=True)

# npm install
if not os.path.isdir(os.path.join(NODE_CORE_DIR, "node_modules")):
    print("[install] Устанавливаю Node.js зависимости ...")
    subprocess.run(["npm", "install"], cwd=NODE_CORE_DIR, check=True)

# playwright browsers (через npx)
print("[install] Устанавливаю браузеры Playwright ...")
subprocess.run(["npx", "playwright", "install"], cwd=NODE_CORE_DIR, check=True)
