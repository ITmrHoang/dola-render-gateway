"""Egress IP geolocation: finds the proxy's country / timezone / coordinates via ipinfo.io.

Location fields left empty in the settings follow the proxy automatically (see config.py).
"""
import asyncio
import time

import aiohttp

import config

IPINFO_URL = "https://ipinfo.io/json"
REFRESH_INTERVAL = 30 * 60  # re-check periodically so rotating proxies stay in sync


async def lookup_egress(http_proxy: str = "", timeout: int = 20) -> dict:
    """Returns {"ip","country","city","timezone","loc","org"}; raises on network / HTTP errors.

    Without an explicit proxy, trust_env lets the lookup follow the same system proxy the browser
    uses (HTTP(S)_PROXY variables, or Windows Internet Settings), so the detected country matches.
    """
    async with aiohttp.ClientSession(trust_env=not http_proxy) as session:
        async with session.get(
            IPINFO_URL,
            proxy=http_proxy or None,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Accept": "application/json"},
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ipinfo.io trả về HTTP {resp.status}")
            data = await resp.json(content_type=None)
    return {
        "ip": str(data.get("ip") or ""),
        "country": str(data.get("country") or "").upper(),
        "city": str(data.get("city") or ""),
        "timezone": str(data.get("timezone") or ""),
        "loc": str(data.get("loc") or ""),
        "org": str(data.get("org") or ""),
    }


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def refresh_detected_geo() -> dict:
    """Looks up the saved proxy's egress location and stores it in config (never raises)."""
    via = config.PROXY_DISPLAY or "Direct"
    if config.PROXY_SERVER and not config.HTTP_PROXY:
        info = {"error": "Không tự nhận diện được vị trí qua proxy SOCKS; hãy nhập múi giờ / vị trí bằng tay.",
                "via": via}
    else:
        try:
            info = {**await lookup_egress(config.HTTP_PROXY), "via": via}
        except Exception as exc:
            info = {"error": _error_text(exc), "via": via}
    info["at"] = time.time()
    config.set_detected_geo(info)
    if info.get("error"):
        print(f"[netgeo] egress lookup via {via} failed: {info['error']}", flush=True)
    else:
        print(f"[netgeo] egress via {via}: {info['ip']} {info['country']} {info['timezone']} "
              f"-> locale={config.LOCALE} tz={config.TIMEZONE} region={config.REGION} "
              f"gps={config.GEOLOCATION_RAW or 'off'}", flush=True)
    return info


async def geo_refresh_loop(interval: int = REFRESH_INTERVAL):
    """Background task: refresh periodically while any location field is automatic."""
    while True:
        await asyncio.sleep(interval)
        if config.location_is_auto():
            await refresh_detected_geo()
