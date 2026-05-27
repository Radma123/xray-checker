import os
import re
from dotenv import load_dotenv
load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(PROJECT_DIR, ".env")


def _read_multiline_env_value(name: str) -> str:
    if not os.path.exists(ENV_PATH):
        return ""

    lines = []
    collecting = False
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                if collecting:
                    break
                continue

            if line.startswith(f"{name}="):
                collecting = True
                lines.append(line.split("=", 1)[1].strip())
                continue

            if collecting:
                if re.match(r"^[A-Z_][A-Z0-9_]*\s*=", line):
                    break
                lines.append(line)

    return "\n".join(lines)


def _env_pool(name: str, legacy_name: str = "") -> list[str]:
    value = _read_multiline_env_value(name) or os.getenv(name) or (os.getenv(legacy_name) if legacy_name else "")
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


# Константы для работы с Xray
INTERNET_SUBS_POOL = _env_pool("INTERNET_SUBS_POOL", "INTERNET_SUBSCRIPTION_URL")
WHITELISTED_SUBS_POOL = _env_pool("WHITELISTED_SUBS_POOL", "WHITELISTED_SUBSCRIPTION_URL")

# количество топ конфигов на выходе
INTERNET_CFGS_COUNT = int(os.getenv("INTERNET_CFGS_COUNT", 3))
WHITELISTED_CFGS_COUNT = int(os.getenv("WHITELISTED_CFGS_COUNT", 3))

# Количество потоков для проверки ссылок и скачивания Xray
CONCURRENT_THREADS_CHECK_DEFAULT = int(os.getenv("CONCURRENT_THREADS_CHECK_DEFAULT", 8))

MAX_LINKS_TO_CHECK_INTERNET = int(os.getenv("MAX_LINKS_TO_CHECK_INTERNET", 1000))
MAX_LINKS_TO_CHECK_WHITELIST = int(os.getenv("MAX_LINKS_TO_CHECK_WHITELIST", 50000))
