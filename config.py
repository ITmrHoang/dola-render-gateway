"""Dola Pool Configuration: Environment variable based with smart defaults."""
import os
from pathlib import Path


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins."""
    path = Path(__file__).with_name(".env.local")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Variables set in the real environment win over .env.local (dashboard edits cannot override them)
_REAL_ENV_KEYS = frozenset(os.environ)
_load_local_env()
HOST = os.getenv("DOLA_HOST", "0.0.0.0")
PORT = int(os.getenv("DOLA_PORT", "8000"))

# Service API keys (comma-separated; empty = no auth, local debug only)
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# Account pool cookie file (one dola.com cookie per line)
COOKIES_FILE = os.getenv("DOLA_COOKIES_FILE", "cookies.txt")

# Max concurrent video generation tasks
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "3"))

# Global pending task queue limit (queued + processing), 0 = unlimited
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "100"))

# Video generation timeout in seconds
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "300"))

# SQLite database path
DB_PATH = os.getenv("DOLA_DB_PATH", "tasks.db")

# Video download storage directory (served statically by FastAPI)
DOWNLOAD_DIR = os.getenv("DOLA_DOWNLOAD_DIR", "downloads")

# ============ Network identity: egress proxy + locale / timezone / region / GPS ============
# All of these can be edited at runtime from the admin dashboard (Accounts tab -> Proxy & Location);
# changes are written back to .env.local and apply to the next browser launch / download.
#
# DOLA_PROXY           explicit proxy (must point to JP/KR egress; empty = system proxy / direct)
#                      http://127.0.0.1:7890 | 127.0.0.1:7890 (http:// assumed) | socks5://127.0.0.1:10808
#                      | http://user:pass@host:port
# DOLA_PROXY_USERNAME  proxy credentials (win over credentials embedded in DOLA_PROXY)
# DOLA_PROXY_PASSWORD
# DOLA_PROXY_BYPASS    comma-separated hosts that skip the proxy in the browser, e.g. "localhost,127.0.0.1"
#
# Location fields: a value = use it as is; EMPTY = automatic, taken from the proxy's egress IP
# (detected through ipinfo.io by netgeo.py); if detection fails the Japan defaults below are used.
# DOLA_LOCALE          browser / Dola interface language: vi-VN (default when empty), en-US or ja-JP.
#                      Not taken from the proxy; the UI automation understands these three languages.
# DOLA_TIMEZONE        IANA timezone reported by the browser, e.g. Asia/Tokyo
# DOLA_REGION          region / sys_region query params on Dola API calls, e.g. JP
# DOLA_GEOLOCATION     GPS "lat,lon[,accuracy]" granted to pages; "off" = never expose a location
import re as _re
import threading as _threading
import time as _time

LOCAL_ENV_PATH = Path(__file__).with_name(".env.local")

# setting name -> environment variable
NETWORK_ENV_KEYS = {
    "proxy": "DOLA_PROXY",
    "proxy_username": "DOLA_PROXY_USERNAME",
    "proxy_password": "DOLA_PROXY_PASSWORD",
    "proxy_bypass": "DOLA_PROXY_BYPASS",
    "locale": "DOLA_LOCALE",
    "timezone": "DOLA_TIMEZONE",
    "region": "DOLA_REGION",
    "geolocation": "DOLA_GEOLOCATION",
}
LOCATION_FIELDS = ("locale", "timezone", "region", "geolocation")
# Used when a field is empty AND the proxy location could not be detected
# The interface language is not derived from the proxy: an empty DOLA_LOCALE always means Vietnamese.
NETWORK_DEFAULTS = {"locale": "vi-VN", "timezone": "Asia/Tokyo", "region": "JP", "geolocation": ""}
GEO_OFF = "off"
# Interface languages the UI automation (video_worker_ui.py) understands
SUPPORTED_LOCALES = ("vi-VN", "en-US", "ja-JP")

_LOCALE_RE = _re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_TIMEZONE_RE = _re.compile(r"^(UTC|GMT|[A-Za-z_]+(/[A-Za-z0-9_+\-]+){1,2})$")
_REGION_RE = _re.compile(r"^[A-Z]{2}$")
_network_lock = _threading.RLock()

# Last egress lookup through the current proxy:
# {"ip","country","city","timezone","loc","org","via","at"} or {"error","via","at"}; {} = not run yet
DETECTED_GEO: dict = {}


def _parse_proxy(raw: str, username: str, password: str, bypass: str) -> dict:
    """Splits a proxy URL into server / credentials and builds browser + aiohttp forms."""
    out = {"server": "", "username": "", "password": "", "browser": None, "http": "", "display": ""}
    if not raw:
        return out
    from urllib.parse import quote, unquote, urlsplit

    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Proxy không hợp lệ: {exc}") from exc
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks4"):
        raise ValueError(f"Proxy: không hỗ trợ giao thức '{scheme}' (dùng http, https, socks5, socks4)")
    if not parts.hostname:
        raise ValueError("Proxy: thiếu địa chỉ host")
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    hostport = f"{host}:{port}" if port else host
    user = username or unquote(parts.username or "")
    pwd = password or unquote(parts.password or "")
    if user and scheme.startswith("socks"):
        raise ValueError("Chromium không hỗ trợ proxy SOCKS có user/password; hãy dùng proxy HTTP")

    server = f"{scheme}://{hostport}"
    browser = {"server": server}
    if user:
        browser["username"] = user
        browser["password"] = pwd
    if bypass:
        browser["bypass"] = bypass
    # aiohttp only speaks HTTP(S) proxies; SOCKS downloads fall back to a direct connection.
    http = ""
    if scheme in ("http", "https"):
        auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user else ""
        http = f"{scheme}://{auth}{hostport}"
    out.update(server=server, username=user, password=pwd, browser=browser, http=http,
               display=server + (" (auth)" if user else ""))
    return out


def _parse_geolocation(raw: str):
    """'lat,lon[,accuracy]' -> Playwright geolocation dict; empty / 'off' = do not expose location."""
    raw = raw.strip()
    if not raw or raw.lower() == GEO_OFF:
        return None
    try:
        nums = [float(x) for x in raw.split(",")]
    except ValueError:
        raise ValueError(f"GPS phải có dạng 'vĩ độ,kinh độ[,độ chính xác]', nhận được '{raw}'")
    if len(nums) not in (2, 3) or not (-90 <= nums[0] <= 90) or not (-180 <= nums[1] <= 180):
        raise ValueError(f"GPS phải có dạng 'vĩ độ,kinh độ[,độ chính xác]' trong phạm vi hợp lệ, nhận được '{raw}'")
    geo = {"latitude": nums[0], "longitude": nums[1]}
    if len(nums) == 3:
        geo["accuracy"] = nums[2]
    return geo


def _default_accept_language(locale: str) -> str:
    lang = locale.split("-")[0]
    if lang == locale:
        return f"{locale},en-US;q=0.8,en;q=0.7"
    return f"{locale},{lang};q=0.9,en-US;q=0.8,en;q=0.7"


def _normalize_raw(raw: dict) -> dict:
    """Strips, validates non-empty values and returns the stored (user-entered) settings."""
    raw = {k: str(raw.get(k) or "").strip() for k in NETWORK_ENV_KEYS}
    raw["region"] = raw["region"].upper()
    if raw["geolocation"].lower() == GEO_OFF:
        raw["geolocation"] = GEO_OFF
    if raw["locale"] and not _LOCALE_RE.match(raw["locale"]):
        raise ValueError(f"Ngôn ngữ không hợp lệ: '{raw['locale']}' (vi-VN, en-US, ja-JP) hoặc để trống = tiếng Việt")
    if raw["timezone"] and not _TIMEZONE_RE.match(raw["timezone"]):
        raise ValueError(f"Timezone không hợp lệ: '{raw['timezone']}' (ví dụ Asia/Tokyo) hoặc để trống = tự động")
    if raw["region"] and not _REGION_RE.match(raw["region"]):
        raise ValueError(f"Region phải là mã quốc gia 2 chữ cái (JP, KR...) hoặc để trống = tự động, nhận được '{raw['region']}'")
    _parse_geolocation(raw["geolocation"])  # validate
    return raw


def _auto_values(detected: dict) -> dict:
    """Location values derived from the detected egress IP ({} when unknown)."""
    if not detected or detected.get("error"):
        return {}
    out = {}
    country = str(detected.get("country") or "").upper()
    if _REGION_RE.match(country):
        out["region"] = country
    tz = str(detected.get("timezone") or "")
    if _TIMEZONE_RE.match(tz):
        out["timezone"] = tz
    loc = str(detected.get("loc") or "")
    try:
        if _parse_geolocation(loc):
            out["geolocation"] = loc
    except ValueError:
        pass
    return out


def _build_network(raw: dict, detected: dict) -> dict:
    """Returns every derived module-level value from stored settings + detected proxy location."""
    proxy = _parse_proxy(raw["proxy"], raw["proxy_username"], raw["proxy_password"], raw["proxy_bypass"])
    auto = _auto_values(detected)
    effective, sources = {}, {}
    for field in LOCATION_FIELDS:
        if raw[field]:
            effective[field], sources[field] = raw[field], "manual"
        elif field in auto:
            effective[field], sources[field] = auto[field], "auto"
        else:
            effective[field], sources[field] = NETWORK_DEFAULTS[field], "default"
    geo_text = "" if effective["geolocation"] == GEO_OFF else effective["geolocation"]
    return {
        "NETWORK_RAW": raw,
        "NETWORK_SOURCES": sources,
        "PROXY": proxy["server"],
        "PROXY_SERVER": proxy["server"],
        "PROXY_USERNAME": proxy["username"],
        "PROXY_PASSWORD": proxy["password"],
        "PROXY_BYPASS": raw["proxy_bypass"],
        # dict for Playwright/Patchright `proxy=` (None = no explicit proxy)
        "BROWSER_PROXY": proxy["browser"],
        # URL for aiohttp `proxy=` ("" = direct; SOCKS is not supported by aiohttp)
        "HTTP_PROXY": proxy["http"],
        # password-free text safe for logs and the dashboard
        "PROXY_DISPLAY": proxy["display"],
        # Effective values sent to Dola
        "LOCALE": effective["locale"],
        "TIMEZONE": effective["timezone"],
        "REGION": effective["region"],
        "GEOLOCATION_RAW": geo_text,
        "GEOLOCATION": _parse_geolocation(geo_text),
        # Accept-Language for direct HTTP API calls (the browser derives its own from LOCALE)
        "ACCEPT_LANGUAGE": os.getenv("DOLA_ACCEPT_LANGUAGE", "").strip()
                           or _default_accept_language(effective["locale"]),
    }


def _apply(raw: dict, detected: dict):
    globals().update(_build_network(raw, detected))


def set_detected_geo(info: dict):
    """Stores the result of an egress lookup and recomputes automatic values."""
    global DETECTED_GEO
    with _network_lock:
        info = dict(info or {})
        info.setdefault("at", _time.time())
        DETECTED_GEO = info
        _apply(NETWORK_RAW, DETECTED_GEO)


def location_is_auto() -> bool:
    """True when at least one location field follows the proxy automatically."""
    return any(not NETWORK_RAW[f] for f in LOCATION_FIELDS)


def network_settings() -> dict:
    """Stored settings, effective values and detection status for the dashboard (no password)."""
    raw = NETWORK_RAW
    return {
        "proxy": PROXY_SERVER,
        "proxy_username": PROXY_USERNAME,
        "proxy_password_set": bool(PROXY_PASSWORD),
        "proxy_bypass": PROXY_BYPASS,
        "proxy_display": PROXY_DISPLAY,
        # what the user typed ("" = automatic)
        "locale": raw["locale"],
        "timezone": raw["timezone"],
        "region": raw["region"],
        "geolocation": raw["geolocation"],
        # what is actually sent to Dola, and where each value came from (manual / auto / default)
        "effective": {"locale": LOCALE, "timezone": TIMEZONE, "region": REGION,
                      "geolocation": GEOLOCATION_RAW or GEO_OFF},
        "sources": dict(NETWORK_SOURCES),
        "detected": {k: v for k, v in DETECTED_GEO.items()},
        "env_overrides": sorted(k for k in NETWORK_ENV_KEYS.values() if k in _REAL_ENV_KEYS),
    }


def _write_local_env(values: dict):
    """Updates/appends KEY=value lines in .env.local, keeping comments and other keys intact."""
    lines = LOCAL_ENV_PATH.read_text(encoding="utf-8-sig").splitlines() if LOCAL_ENV_PATH.exists() else []
    pending = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
                continue
        out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# Proxy & location (saved from admin dashboard; empty location = auto from proxy)")
        out.extend(f"{k}={v}" for k, v in pending.items())
    tmp = LOCAL_ENV_PATH.with_suffix(".local.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, LOCAL_ENV_PATH)


def update_network_settings(changes: dict) -> bool:
    """Validates, applies at runtime and persists to .env.local. Raises ValueError on bad input.

    `changes` uses NETWORK_ENV_KEYS names; a missing key keeps its current value, an empty
    location value means automatic, and proxy_password=None keeps the stored password.
    Returns True when the proxy itself changed (the caller should re-detect its location).
    """
    global DETECTED_GEO
    with _network_lock:
        merged = dict(NETWORK_RAW)
        merged["proxy"], merged["proxy_username"], merged["proxy_password"] = (
            PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD)
        for k, v in changes.items():
            if k in NETWORK_ENV_KEYS and v is not None:
                merged[k] = str(v).strip()
        if not merged["proxy"]:  # no proxy -> drop credentials too
            merged["proxy_username"] = merged["proxy_password"] = ""
        raw = _normalize_raw(merged)
        proxy = _parse_proxy(raw["proxy"], raw["proxy_username"], raw["proxy_password"], raw["proxy_bypass"])
        # Store credentials in their own keys, never inside the URL.
        raw["proxy"], raw["proxy_username"], raw["proxy_password"] = (
            proxy["server"], proxy["username"], proxy["password"])
        proxy_changed = (raw["proxy"], raw["proxy_username"], raw["proxy_password"]) != (
            PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD)
        _write_local_env({env: raw[k] for k, env in NETWORK_ENV_KEYS.items()})
        if proxy_changed:
            DETECTED_GEO = {}
        _apply(raw, DETECTED_GEO)
        return proxy_changed


_apply(_normalize_raw({k: os.getenv(env) for k, env in NETWORK_ENV_KEYS.items()}), DETECTED_GEO)

# Run browser in headless mode (login always runs with head)
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"

# Base public URL for returning static video links
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# Admin web dashboard password (empty = no auth)
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")

# Dola 30s / Watermark Removal Chromium extension path
EXTENSION_DIR = os.getenv("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Daily quota reset timezone (Japan midnight by default)
LIMIT_RESET_TZ = os.getenv("DOLA_LIMIT_RESET_TZ", "Asia/Tokyo")

# Conservative credit check before video generation (default 2 points)
VIDEO_REQUIRED_POINTS = int(os.getenv("DOLA_VIDEO_REQUIRED_POINTS", "2"))

# Public reference image download limits (Dola web UI supports maximum 10 images)
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "10"))

# Extended generation window for reference image tasks (seconds)
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "900"))
