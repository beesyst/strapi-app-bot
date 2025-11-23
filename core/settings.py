from __future__ import annotations

import json
import random
from typing import Any, Dict, List

from core.paths import CONFIG_JSON

# Глобальный кэш настроек
_SETTINGS: Dict[str, Any] = {}

try:
    with open(CONFIG_JSON, "r", encoding="utf-8") as f:
        _SETTINGS = json.load(f) or {}
except Exception as e:
    raise RuntimeError(f"Не удалось прочитать конфиг {CONFIG_JSON}: {e}")


# Сырой dict с содержимым config.json
def get_settings() -> Dict[str, Any]:
    return _SETTINGS


# HTTP/User-Agent ротация
_HTTP_CFG: Dict[str, Any] = _SETTINGS.get("http") or {}

# Список UA из конфига
_HTTP_UA_LIST: List[str] = [
    str(u).strip() for u in (_HTTP_CFG.get("ua") or []) if str(u).strip()
]
# Жесткое требование: без UA из конфигурации работать нельзя
if not _HTTP_UA_LIST:
    raise RuntimeError(
        "В config.json не задан ни один User-Agent. "
        "Ожидается секция вида:\n"
        '"http": { "ua": ["UA1", "UA2", ...], "strategy": "single|random|round_robin" }'
    )

# Стратегия ротации UA: single/random/round_robin
_HTTP_UA_STRATEGY: str = str(_HTTP_CFG.get("strategy") or "single").lower()
if _HTTP_UA_STRATEGY not in ("single", "random", "round_robin"):
    _HTTP_UA_STRATEGY = "single"

# Состояние round-robin курсора для UA
_HTTP_UA_RR_STATE = {"idx": 0}


# Возврат User-Agent для HTTP/Playwright-запроса в соответствии со стратегией
def get_http_ua() -> str:
    if _HTTP_UA_STRATEGY == "single":
        # всегда первый UA - стабильно, без ротации
        return _HTTP_UA_LIST[0]

    if _HTTP_UA_STRATEGY == "random":
        return random.choice(_HTTP_UA_LIST)

    # round_robin
    idx = _HTTP_UA_RR_STATE["idx"] % len(_HTTP_UA_LIST)
    ua = _HTTP_UA_LIST[idx]
    _HTTP_UA_RR_STATE["idx"] = (idx + 1) % len(_HTTP_UA_LIST)
    return ua
