import os
import subprocess
import sys

# Корень
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

from core.log_utils import auto_clear_logs_if_needed, init_setup_log

# Централизованная очистка и инициализация логов
auto_clear_logs_if_needed()
init_setup_log()

# Проверка venv
VENV_PATH = os.path.join(ROOT_DIR, "venv")
if sys.prefix == sys.base_prefix:
    if not os.path.isdir(VENV_PATH):
        print("[start] Virtual environment not found, creating venv...")
        subprocess.run(["python3", "-m", "venv", VENV_PATH], check=True)
    py_in_venv = os.path.join(VENV_PATH, "bin", "python")
    if not os.path.exists(py_in_venv):
        py_in_venv = os.path.join(VENV_PATH, "Scripts", "python.exe")
    os.execv(py_in_venv, [py_in_venv] + sys.argv)

# Зависимости (pip, npm, playwright)
INSTALL_PATH = os.path.join(ROOT_DIR, "core", "install.py")
setup_log_path = os.path.join(LOGS_DIR, "setup.log")
with open(setup_log_path, "a") as logf:
    subprocess.run([sys.executable, INSTALL_PATH], check=True, stdout=logf, stderr=logf)


# Импорт и запуск orchestrator
def run_orchestrator():
    import importlib.util

    orchestrator_path = os.path.join(ROOT_DIR, "core", "orchestrator.py")
    spec = importlib.util.spec_from_file_location("orchestrator", orchestrator_path)
    orchestrator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orchestrator)
    orchestrator.run_pipeline()


if __name__ == "__main__":
    print("[start] Go-go-go")
    try:
        run_orchestrator()
        print("[finish] Success")
    except Exception as e:
        print(f"[strapi-app-bot] Error: {e}")
