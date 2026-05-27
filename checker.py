#!/usr/bin/env python3
import base64
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import socket

# Используем requests для точного замера задержки (TCP Handshake + HTTP RTT)
import requests

from config import INTERNET_SUBS_POOL, WHITELISTED_SUBS_POOL, CONCURRENT_THREADS_CHECK_DEFAULT, INTERNET_CFGS_COUNT, WHITELISTED_CFGS_COUNT, MAX_LINKS_TO_CHECK_INTERNET, MAX_LINKS_TO_CHECK_WHITELIST

# Константы
TEST_CONNECT_TIMEOUT = 2
TEST_READ_TIMEOUT = 4
TEST_URL = "https://www.google.com/"
TEST_READ_BYTES = 2048
SOCKS_PORT_MIN = 20000
CONCURRENT_DEFAULT = CONCURRENT_THREADS_CHECK_DEFAULT

# Папка проекта для хранения ядра
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
XRAY_BIN_DIR = os.path.join(PROJECT_DIR, "xray_bin")

def get_xray_download_url() -> str:
    """Определяет ОС и архитектуру, запрашивает у GitHub API ссылку на актуальный Xray."""
    os_type = platform.system().lower()
    arch = platform.machine().lower()
    
    if os_type == "darwin": os_name = "macos"
    elif os_type == "windows": os_name = "windows"
    else: os_name = "linux"
        
    if arch in ("amd64", "x86_64"): arch_name = "64"
    elif arch in ("arm64", "aarch64"): arch_name = "arm64-v8a"
    else: arch_name = "32"
        
    api_url = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"
    req = Request(api_url, headers={"User-Agent": "v2ray-downloader"})
    
    try:
        with urlopen(req, timeout=10) as r:
            release_data = json.loads(r.read().decode())
        target_asset = f"Xray-{os_name}-{arch_name}".lower()
        print(f"[*] Ищем релиз: {target_asset}")
        for asset in release_data.get("assets", []):
            name = asset.get("name", "").lower()
            if target_asset in name and (name.endswith(".zip") or name.endswith(".gz")):
                return asset["browser_download_url"]
    except Exception as e:
        raise RuntimeError(f"Не удалось получить данные о релизах Xray: {e}")
    raise RuntimeError(f"Не найдена сборка Xray для вашей системы: {os_name} ({arch_name})")

def setup_xray_bin() -> str:
    """Проверяет наличие Xray локально, при отсутствии — скачивает."""
    os_ext = ".exe" if sys.platform == "win32" else ""
    xray_path = os.path.join(XRAY_BIN_DIR, f"xray{os_ext}")
    
    if os.path.exists(xray_path):
        return xray_path
        
    print("[*] Ядро Xray не найдено локально. Запуск автоматической загрузки...")
    os.makedirs(XRAY_BIN_DIR, exist_ok=True)
    
    download_url = get_xray_download_url()
    print(f"[+] Скачивание архива: {download_url}")
    
    tmp_file = os.path.join(XRAY_BIN_DIR, "xray_archive.tmp")
    req = Request(download_url, headers={"User-Agent": "v2ray-downloader"})
    
    try:
        with urlopen(req, timeout=30) as response, open(tmp_file, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
            
        print("[*] Распаковка ядра...")
        if download_url.endswith(".zip"):
            with zipfile.ZipFile(tmp_file, 'r') as zip_ref: zip_ref.extractall(XRAY_BIN_DIR)
        elif download_url.endswith((".tar.gz", ".tgz")):
            with tarfile.open(tmp_file, "r:gz") as tar_ref: tar_ref.extractall(XRAY_BIN_DIR)
                
        if sys.platform != "win32" and os.path.exists(xray_path):
            os.chmod(xray_path, 0o755)
            
        print("[+] Ядро Xray успешно установлено локально!")
        return xray_path
    finally:
        if os.path.exists(tmp_file): os.remove(tmp_file)

def _json_query_value(value: str) -> dict | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

def _decode_base64_text(value: str) -> str:
    value = value.strip()
    value += "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value).decode("utf-8", errors="replace")

def _build_stream_settings(query: dict) -> dict:
    network = query.get("type", "tcp")
    security = query.get("security", "none")

    stream_settings = {"network": network}
    if security != "none":
        stream_settings["security"] = security
    _add_transport_settings(stream_settings, query)
    _add_security_settings(stream_settings, query)
    return stream_settings

def _add_transport_settings(stream_settings: dict, query: dict) -> None:
    network = stream_settings["network"]
    path = query.get("path")
    host = query.get("host")

    if network == "ws":
        ws_settings = {}
        if path:
            ws_settings["path"] = path
        if host:
            ws_settings["headers"] = {"Host": host}
        if ws_settings:
            stream_settings["wsSettings"] = ws_settings
    elif network in ("http", "h2"):
        http_settings = {}
        if path:
            http_settings["path"] = path
        if host:
            http_settings["host"] = [host]
        if http_settings:
            stream_settings["httpSettings"] = http_settings
    elif network == "grpc":
        grpc_settings = {}
        service_name = query.get("serviceName") or path
        if service_name:
            grpc_settings["serviceName"] = service_name.lstrip("/")
        if query.get("authority"):
            grpc_settings["authority"] = query["authority"]
        if grpc_settings:
            stream_settings["grpcSettings"] = grpc_settings
    elif network == "xhttp":
        xhttp_settings = {}
        if host:
            xhttp_settings["host"] = host
        if path:
            xhttp_settings["path"] = path
        if query.get("mode"):
            xhttp_settings["mode"] = query["mode"]
        if query.get("extra"):
            extra = _json_query_value(query["extra"])
            if extra is not None:
                xhttp_settings["extra"] = extra
        if query.get("downloadSettings"):
            download_settings = _json_query_value(query["downloadSettings"])
            if download_settings is not None:
                xhttp_settings["downloadSettings"] = download_settings
        if xhttp_settings:
            stream_settings["xhttpSettings"] = xhttp_settings
    elif network in ("tcp", "raw"):
        header_type = query.get("headerType")
        if header_type:
            stream_settings["tcpSettings"] = {"header": {"type": header_type}}
    elif network in ("kcp", "mkcp"):
        kcp_settings = {}
        if query.get("seed"):
            kcp_settings["seed"] = query["seed"]
        if query.get("headerType"):
            kcp_settings["header"] = {"type": query["headerType"]}
        if kcp_settings:
            stream_settings["kcpSettings"] = kcp_settings
    elif network == "quic":
        quic_settings = {}
        if query.get("quicSecurity"):
            quic_settings["security"] = query["quicSecurity"]
        if query.get("key"):
            quic_settings["key"] = query["key"]
        if query.get("headerType"):
            quic_settings["header"] = {"type": query["headerType"]}
        if quic_settings:
            stream_settings["quicSettings"] = quic_settings
    elif network == "httpupgrade":
        httpupgrade_settings = {}
        if host:
            httpupgrade_settings["host"] = host
        if path:
            httpupgrade_settings["path"] = path
        if httpupgrade_settings:
            stream_settings["httpupgradeSettings"] = httpupgrade_settings

def _add_security_settings(stream_settings: dict, query: dict) -> None:
    security = stream_settings.get("security")
    if security == "reality":
        reality_settings = {}
        mapping = {
            "sni": "serverName",
            "fp": "fingerprint",
            "pbk": "publicKey",
            "sid": "shortId",
            "spx": "spiderX",
        }
        for source, target in mapping.items():
            if query.get(source):
                reality_settings[target] = query[source]
        if reality_settings:
            stream_settings["realitySettings"] = reality_settings
    elif security == "tls":
        tls_settings = {}
        if query.get("sni"):
            tls_settings["serverName"] = query["sni"]
        if query.get("fp"):
            tls_settings["fingerprint"] = query["fp"]
        if query.get("alpn"):
            tls_settings["alpn"] = [item for item in query["alpn"].split(",") if item]
        if query.get("allowInsecure"):
            tls_settings["allowInsecure"] = query["allowInsecure"].lower() == "true"
        if tls_settings:
            stream_settings["tlsSettings"] = tls_settings

def _parse_vless_link(link: str) -> tuple[str, dict] | None:
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError:
        return None

    if not parsed.username or not parsed.hostname or port is None:
        return None

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    remark = unquote(parsed.fragment) if parsed.fragment else "Untitled"

    user = {
        "id": unquote(parsed.username),
        "encryption": query.get("encryption", "none"),
    }
    if query.get("flow"):
        user["flow"] = query["flow"]

    outbound = {
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": parsed.hostname,
                "port": port,
                "users": [user],
            }]
        },
        "streamSettings": _build_stream_settings(query),
    }
    return remark, outbound

def _parse_trojan_link(link: str) -> tuple[str, dict] | None:
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError:
        return None

    if not parsed.username or not parsed.hostname or port is None:
        return None

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    remark = unquote(parsed.fragment) if parsed.fragment else "Untitled"
    outbound = {
        "protocol": "trojan",
        "settings": {
            "servers": [{
                "address": parsed.hostname,
                "port": port,
                "password": unquote(parsed.username),
            }]
        },
        "streamSettings": _build_stream_settings(query),
    }
    return remark, outbound

def _parse_shadowsocks_link(link: str) -> tuple[str, dict] | None:
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError:
        return None

    if not parsed.hostname or port is None:
        return None

    user_info = unquote(parsed.username or "")
    if ":" not in user_info:
        try:
            user_info = _decode_base64_text(user_info)
        except Exception:
            return None
    if ":" not in user_info:
        return None

    method, password = user_info.split(":", 1)
    remark = unquote(parsed.fragment) if parsed.fragment else "Untitled"
    outbound = {
        "protocol": "shadowsocks",
        "settings": {
            "servers": [{
                "address": parsed.hostname,
                "port": port,
                "method": method,
                "password": password,
            }]
        },
    }
    return remark, outbound

def _parse_vmess_link(link: str) -> tuple[str, dict] | None:
    try:
        raw_config = _decode_base64_text(link.removeprefix("vmess://"))
        config = json.loads(raw_config)
        port = int(config["port"])
        address = config["add"]
        user_id = config["id"]
    except Exception:
        return None

    query = {
        "type": config.get("net") or "tcp",
        "security": config.get("tls") or "none",
        "path": config.get("path", ""),
        "host": config.get("host", ""),
        "sni": config.get("sni", ""),
        "fp": config.get("fp", ""),
        "alpn": config.get("alpn", ""),
        "headerType": config.get("type", ""),
        "mode": config.get("mode", ""),
    }
    outbound = {
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": address,
                "port": port,
                "users": [{
                    "id": user_id,
                    "alterId": int(config.get("aid") or 0),
                    "security": config.get("scy") or "auto",
                }],
            }]
        },
        "streamSettings": _build_stream_settings(query),
    }
    return config.get("ps") or "Untitled", outbound

def convert_link_via_xray(link: str, xray_path: str | None = None) -> tuple[str, dict] | None:
    """Парсит share-ссылку и возвращает outbound JSON для Xray."""
    link = link.strip()
    if not link:
        return None

    if link.startswith("vless://"):
        return _parse_vless_link(link)
    if link.startswith("trojan://"):
        return _parse_trojan_link(link)
    if link.startswith("ss://"):
        return _parse_shadowsocks_link(link)
    if link.startswith("vmess://"):
        return _parse_vmess_link(link)

    return None

def check_single_config(outbound: dict, port: int, xray_path: str) -> float:
    """Запускает Xray, реально читает данные через SOCKS и меряет время до первых байтов."""
    config = {
        "log": {"loglevel": "error"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": True}}],
        # Подаем распарсенный outbound первым в список
        "outbounds": [outbound, {"protocol": "freedom", "tag": "direct"}]
    }
    
    fd, config_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(config, f)
            
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            [xray_path, "run", "-config", config_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
        )
        
        time.sleep(0.2) # Быстрый старт локального SOCKS-порта
        if proc.poll() is not None:
            return float('inf')
        
        proxies = {
            "http": f"socks5h://127.0.0.1:{port}",
            "https": f"socks5h://127.0.0.1:{port}"
        }
        
        t0 = time.perf_counter()
        try:
            with requests.get(
                TEST_URL,
                proxies=proxies,
                timeout=(TEST_CONNECT_TIMEOUT, TEST_READ_TIMEOUT),
                headers={"User-Agent": "Mozilla/5.0 v2ray-checker"},
                stream=True,
            ) as r:
                if r.status_code >= 500:
                    return float('inf')

                for chunk in r.iter_content(chunk_size=TEST_READ_BYTES):
                    if chunk:
                        return (time.perf_counter() - t0) * 1000
        except Exception:
            return float('inf')
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
    finally:
        try: os.unlink(config_path)
        except OSError: pass
        
    return float('inf')

def parse_subscription(source: str | None) -> list[str]:
    """Загружает подписку по ссылке или разбирает сырую строку (текст/base64)."""
    if not source:
        return []

    source = source.strip()
    if not source:
        return []

    if source.startswith(("http://", "https://")):
        try:
            req = Request(source, headers={"User-Agent": "v2ray-checker"})
            with urlopen(req, timeout=10) as r: 
                source = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[ОШИБКА] Не удалось загрузить подписку по URL: {e}")
            return []
            
    if not source.startswith(("vless://", "vmess://", "trojan://", "ss://")):
        try:
            missing_padding = len(source) % 4
            if missing_padding: source += '=' * (4 - missing_padding)
            source = base64.b64decode(source).decode("utf-8", errors="replace")
        except Exception: 
            pass

    source = html.unescape(source)
    pattern = re.compile(r"(?:vless|vmess|trojan|ss)://[^\s<>'\"]+")
    return [match.group(0).strip() for match in pattern.finditer(source)]

def check_configs(links: list[tuple[str, dict, str]], xray_path: str) -> list[tuple[float, str, str]]:
    """Параллельно проверяет список конфигов и возвращает отсортированный по задержке результат."""
    valid_configs = []
    links = [item for item in links if item is not None]
    if not links:
        return valid_configs

    with ThreadPoolExecutor(max_workers=CONCURRENT_DEFAULT) as executor:
        futures = {
            executor.submit(check_single_config, outbound, SOCKS_PORT_MIN + idx, xray_path): (remark, original_link)
            for idx, (remark, outbound, original_link) in enumerate(links)
        }
        
        for future in as_completed(futures):
            remark, link = futures[future]
            try:
                latency = future.result()
                if latency != float('inf'):
                    print(f"  [OK] {remark:<30} | Задержка (TCP+HTTP): {int(latency)} мс")
                    valid_configs.append((latency, remark, link))
                else:
                    print(f"  [FAIL] {remark:<30}")
            except Exception as e: 
                print(f"  [ОШИБКА] {remark}: {e}")
    
    valid_configs.sort(key=lambda x: x[0])
    return valid_configs

def parse_proxy_links(raw_links: list[str]) -> list[tuple[str, dict, str]]:
    """Парсит сырые share-ссылки и сохраняет оригинальную ссылку для вывода."""
    parsed_links = []
    for link in raw_links:
        parsed = convert_link_via_xray(link)
        if parsed is None:
            continue
        remark, outbound = parsed
        parsed_links.append((remark, outbound, link))
    return parsed_links

def save_results(links: list[str], output_path: str) -> None:
    """Сохраняет отсортированные рабочие ссылки в текстовый файл."""
    with open(output_path, "w", encoding="utf-8") as f:
        for link in links:
            f.write(f"{link}\n")

@lru_cache(maxsize=2048)
def detect_country(address: str) -> str:
    try:
        ip = socket.gethostbyname(address)
    except Exception:
        return "Unknown"

    try:
        r = requests.get(f"https://ipwho.is/{ip}", timeout=5)
        data = r.json()

        if not data.get("success", False):
            return "Unknown"

        country = data.get("country_code") or "Unknown"
        return country
    except Exception:
        return "Unknown"
    
def get_country_emoji(country_code: str) -> str:
    if country_code == "Unknown":
        return "❓"
    try:
        return chr(127397 + ord(country_code[0])) + chr(127397 + ord(country_code[1]))
    except Exception:
        return "❓"

def get_link_address(link: str) -> str:
    parsed = convert_link_via_xray(link)
    if parsed is None:
        return ""

    _, outbound = parsed
    settings = outbound.get("settings", {})
    if outbound.get("protocol") in ("vless", "vmess"):
        vnext = settings.get("vnext") or []
        return vnext[0].get("address", "") if vnext else ""

    servers = settings.get("servers") or []
    return servers[0].get("address", "") if servers else ""

def set_link_remark(link: str, remark: str) -> str:
    if link.startswith("vmess://"):
        try:
            config = json.loads(_decode_base64_text(link.removeprefix("vmess://")))
            config["ps"] = remark
            encoded = base64.urlsafe_b64encode(
                json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("ascii").rstrip("=")
            return f"vmess://{encoded}"
        except Exception:
            return link

    try:
        parsed = urlsplit(link)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, quote(remark, safe="")))
    except Exception:
        return link

def add_country_to_remarks(valid_links: list[tuple[float, str, str]], prefix: str) -> list[tuple[float, str, str]]:
    renamed_links = []
    for idx, (latency, _remark, link) in enumerate(valid_links, 1):
        address = get_link_address(link)
        country = detect_country(address)
        country_emoji = get_country_emoji(country)
        new_remark = f"{country_emoji} {prefix}№{idx} {latency:.0f}ms"
        renamed_links.append((latency, new_remark, set_link_remark(link, new_remark)))
    return renamed_links

def main():
    # старт
    try:
        xray_path = setup_xray_bin()
    except Exception as e:
        print(f"[КРИТИЧЕСКАЯ ОШИБКА] Не удалось настроить Xray: {e}")
        sys.exit(1)

    print('обработка ссылок:')
    for url in INTERNET_SUBS_POOL:
        print(f"  - internet: {url}")
    for url in WHITELISTED_SUBS_POOL:
        print(f"  - whitelist: {url}")



    print("\n[+] Загрузка подписок...")
    raw_links_internet = []
    raw_links_whitelist = []
    for url in INTERNET_SUBS_POOL:
        raw_links_internet.extend(parse_subscription(url))
    for url in WHITELISTED_SUBS_POOL:
        raw_links_whitelist.extend(parse_subscription(url))
    
    # загрузка подписок
    valid_links_internet = []
    valid_links_whitelist = []
    if raw_links_internet:
        print(f"\n[+] Загружено {len(raw_links_internet)} ссылок из интернет-пула.")

        # обрезаем до максимального количества для проверки, если указано в конфиге
        raw_links_internet = raw_links_internet[:MAX_LINKS_TO_CHECK_INTERNET]
        print(f"\n[+] Проверка {len(raw_links_internet)} ссылок из интернет-пула...")
        valid_links_internet = check_configs(parse_proxy_links(raw_links_internet), xray_path)[:INTERNET_CFGS_COUNT]
    if raw_links_whitelist:
        print(f"\n[+] Загружено {len(raw_links_whitelist)} ссылок из вайтлиста.")

        raw_links_whitelist = raw_links_whitelist[:MAX_LINKS_TO_CHECK_WHITELIST]
        print(f"\n[+] Проверка {len(raw_links_whitelist)} ссылок из вайтлиста...")
        valid_links_whitelist = check_configs(parse_proxy_links(raw_links_whitelist), xray_path)[:WHITELISTED_CFGS_COUNT]

    # Сортировка по задержке
    valid_links_internet.sort(key=lambda x: x[0])
    valid_links_whitelist.sort(key=lambda x: x[0])

    # Определение страны по IP/домену и смена имени в remark.
    print("\n[+] Определение страны по IP и домену для рабочих ссылок...")
    valid_links_internet = add_country_to_remarks(valid_links_internet, "ИНТЕРНЕТ 🌐")
    valid_links_whitelist = add_country_to_remarks(valid_links_whitelist, "БС 📋✓ ")

    # Вывод результатов
    print("\n" + "="*60)
    print(f"\n[РЕЗУЛЬТАТ] Рабочих ссылок из интернет-пула: {len(valid_links_internet)}")
    print(f"[РЕЗУЛЬТАТ] Рабочих ссылок из вайтлиста: {len(valid_links_whitelist)}")
    print("="*60)

    if valid_links_internet:
        print("\n🏆 Топ самых быстрых серверов:")
        for rank, (latency, remark, link) in enumerate(valid_links_internet, 1):
            print(f"  {rank}. [{int(latency)} мс] {remark}")

    if valid_links_whitelist:
        print("\n🔒 Рабочие ссылки из вайтлиста:")
        for rank, (latency, remark, link) in enumerate(valid_links_whitelist, 1):
            print(f"  {rank}. [{int(latency)} мс] {remark}")


    # Сохранение результатов в файлы
    if valid_links_internet:
        output_internet = os.path.join(PROJECT_DIR, "valid_internet_links.txt")
        save_results([link for _, _, link in valid_links_internet], output_internet)
        print(f"\n[+] Рабочие ссылки из интернет-пула сохранены в: {output_internet}")
    if valid_links_whitelist:
        output_whitelist = os.path.join(PROJECT_DIR, "valid_whitelist_links.txt")
        save_results([link for _, _, link in valid_links_whitelist], output_whitelist)
        print(f"\n[+] Рабочие ссылки из вайтлиста сохранены в: {output_whitelist}")

    # Формируем единый файл подписки с заголовками
    final_lines = [
        "#profile-title: Liexer vpn_2 (автогенерация для интернета и БС)",
        "#profile-update-interval: 1",
        "#announce: Скрипт автоматизирован для поиска конфигов Xray для интернета и Белых списков",
        ""  # Пустая строка перед контентом
    ]
    
    # Сначала добавляем вайтлист (БС), затем интернет-ссылки (или наоборот)
    if valid_links_whitelist:
        for _, _, link in valid_links_whitelist:
            final_lines.append(link)
            
    if valid_links_internet:
        for _, _, link in valid_links_internet:
            final_lines.append(link)

    output_sub = os.path.join(PROJECT_DIR, "v2ray_sub.txt")
    with open(output_sub, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines))
    print(f"[+] Финальный файл подписки сформирован: {output_sub}")

if __name__ == "__main__":
    main()
