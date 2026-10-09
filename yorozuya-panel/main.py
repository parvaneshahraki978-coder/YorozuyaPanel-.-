"""
YorozuyaPanel v1.0.0 — پنل سبک مدیریت کانفیگ پروکسی 🍓

بک‌اند FastAPI در یک فایل. داده‌ها در DATA_DIR/yorozuya_state.json ذخیره می‌شوند
(بدون دیتابیس). هر ۳۰ ثانیه: کاربران منقضی غیرفعال می‌شوند (حذف نمی‌شوند) و
state روی دیسک ذخیره می‌شود.
"""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
import qrcode
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

# ----------------------------------------------------------------- تنظیمات
APP_VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "yorozuya_state.json"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "yorozuya"
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
COOKIE_NAME = "yorozuya_session"
SESSION_TTL = 12 * 3600
MAINTENANCE_INTERVAL = 30  # sweeper + ذخیره‌سازی پس‌زمینه (ثانیه)
GEO_CACHE_TTL = 7 * 24 * 3600  # کش کشور هر آی‌پی
SERVER_INFO_TTL = 10 * 60  # کش آی‌پی سرور
TCP_TIMEOUT = 5.0  # تایم‌اوت resolve و اتصال TCP نود
LOGIN_WINDOW = 300
LOGIN_MAX_FAILS = 10
MAX_RESTORE_BYTES = 20 * 1024 * 1024
GB = 1024**3
NO_CACHE = {"Cache-Control": "no-store"}
USERNAME_RE = re.compile(r"[\w.\-]{1,32}")
HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
GEO_FIELDS = "status,message,country,countryCode,city,query,org,isp"

# اپ‌های ایمپورت اشتراک. {url} = URL-encoded، {raw} = خام، {name} = URL-encoded
IMPORT_APPS = (
    ("v2rayng", "V2RayNG", "Android", "v2rayng://install-sub?url={url}&name={name}"),
    ("nekobox", "NekoBox", "Android", "sn://subscription?url={url}&name={name}"),
    ("streisand", "Streisand", "iOS / macOS", "streisand://import/{raw}#{name}"),
    ("v2box", "V2Box", "iOS / Android", "v2box://install-sub?url={url}&name={name}"),
    ("hiddify", "Hiddify", "Windows / Android / iOS", "hiddify://import/{raw}#{name}"),
    ("happ", "Happ", "iOS / Android / macOS", "happ://add/{raw}"),
)
SUB_INACTIVE_MESSAGES = {
    "disabled": "این اشتراک غیرفعاله. با پشتیبانی تماس بگیر 🍓",
    "expired": "مدت این اشتراک تمام شده. برای تمدید پیام بده ⏳",
    "limit_reached": "حجم این اشتراک تموم شده. برای افزایش حجم پیام بده 📦",
}

log = logging.getLogger("yorozuya")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ----------------------------------------------------------------- کمک‌های زمان و متن
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_expiry(value) -> str:
    """'' یعنی نامحدود. «YYYY-MM-DD» یعنی پایان همان روز (UTC). ISO کامل هم پذیرفته می‌شود."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            day = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(400, "تاریخ انقضا نامعتبره") from exc
        dt = day.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    else:
        dt = parse_iso(raw)
        if dt is None:
            raise HTTPException(400, "تاریخ انقضا نامعتبره")
    return dt.isoformat(timespec="seconds")


def is_past(iso_value: str) -> bool:
    dt = parse_iso(iso_value)
    return dt is not None and dt <= utcnow()


def flag_of(country_code: str) -> str:
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def clean_username(value: str) -> str:
    v = value.strip()
    if not USERNAME_RE.fullmatch(v):
        raise ValueError("نام کاربری فقط حروف، عدد، «_»، «.» و «-» (حداکثر ۳۲ کاراکتر)")
    return v


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


# ----------------------------------------------------------------- state و ذخیره‌سازی
USER_DEFAULTS = {
    "username": "",
    "uuid": "",
    "sub_key": "",
    "limit_gb": 0.0,
    "used_bytes": 0,
    "expires_at": "",
    "enabled": True,
    "auto_disabled": False,
    "note": "",
    "created_at": "",
}
NODE_DEFAULTS = {
    "name": "",
    "address": "",
    "port": 443,
    "protocol": "vless",
    "network": "tcp",
    "security": "none",
    "sni": "",
    "path": "",
    "host": "",
    "fp": "chrome",
    "alpn": "",
    "reality_pbk": "",
    "reality_sid": "",
    "reality_spx": "",
    "flow": "",
    "ss_method": "",
    "ss_password": "",
    "created_at": "",
    "last_check": "",
    "online": None,
    "latency_ms": None,
    "last_error": "",
    "resolved_ip": "",
    "country": "",
    "country_code": "",
}

_lock = threading.RLock()
_state: dict = {"users": [], "nodes": [], "settings": {"sub_base_url": ""}, "ip_cache": {}}
_dirty = False


def _mark_dirty() -> None:
    global _dirty
    _dirty = True


def _coerce(defaults: dict, raw: dict) -> dict:
    """مقادیر فایل/بکاپ را به نوع درست تبدیل می‌کند (کلیدهای ناشناس حذف می‌شوند)."""
    out = {}
    for key, default in defaults.items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            out[key] = bool(value)
        elif isinstance(default, int):
            out[key] = _to_int(value, default)
        elif isinstance(default, float):
            out[key] = _to_float(value, default)
        elif isinstance(default, str):
            out[key] = str(value or "")
        else:
            out[key] = value if value is None or isinstance(value, (bool, int, float, str)) else None
    return out


def _normalize_user(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    user = _coerce(USER_DEFAULTS, raw)
    user["username"] = user["username"].strip()
    if not USERNAME_RE.fullmatch(user["username"]):
        return None
    try:
        user["uuid"] = str(uuid.UUID(str(raw.get("uuid"))))
    except ValueError:
        user["uuid"] = str(uuid.uuid4())
    user["id"] = str(raw.get("id") or uuid.uuid4().hex)
    user["sub_key"] = user["sub_key"] or secrets.token_urlsafe(18)
    user["limit_gb"] = max(0.0, user["limit_gb"])
    user["used_bytes"] = max(0, user["used_bytes"])
    user["note"] = user["note"][:200]
    user["created_at"] = user["created_at"] or iso_now()
    return user


def _normalize_node(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    node = _coerce(NODE_DEFAULTS, raw)
    node["id"] = str(raw.get("id") or uuid.uuid4().hex)
    node["name"] = node["name"].strip() or "بی‌نام"
    node["address"] = node["address"].strip()
    if not node["address"] or node["protocol"] not in ("vless", "vmess", "trojan", "ss"):
        return None
    if node["network"] not in ("tcp", "ws", "grpc"):
        node["network"] = "tcp"
    if node["security"] not in ("none", "tls", "reality"):
        node["security"] = "none"
    return node


def _normalize_state(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("ساختار فایل معتبر نیست")
    users_raw, nodes_raw = raw.get("users", []), raw.get("nodes", [])
    if not isinstance(users_raw, list) or not isinstance(nodes_raw, list):
        raise ValueError("فیلدهای users و nodes باید لیست باشند")
    users, names, ids, keys = [], set(), set(), set()
    for item in users_raw:
        user = _normalize_user(item)
        if user is None or user["username"].lower() in names or user["id"] in ids or user["sub_key"] in keys:
            continue
        names.add(user["username"].lower())
        ids.add(user["id"])
        keys.add(user["sub_key"])
        users.append(user)
    nodes, node_ids = [], set()
    for item in nodes_raw:
        node = _normalize_node(item)
        if node is None or node["id"] in node_ids:
            continue
        node_ids.add(node["id"])
        nodes.append(node)
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    ip_cache = raw.get("ip_cache") if isinstance(raw.get("ip_cache"), dict) else {}
    return {
        "users": users,
        "nodes": nodes,
        "settings": {"sub_base_url": str(settings.get("sub_base_url") or "").strip().rstrip("/")},
        "ip_cache": {ip: v for ip, v in ip_cache.items() if isinstance(v, dict)},
    }


def _save_locked() -> None:
    """نوشتن اتمیک: اول فایل موقت، بعد جایگزینی. باید با _lock صدا زده شود."""
    global _dirty
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    payload = json.dumps(_state, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE_FILE)
    _dirty = False


def flush_state(force: bool = False) -> None:
    with _lock:
        if force or _dirty:
            _save_locked()


def load_state() -> None:
    global _state
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    loaded = _normalize_state({})
    if STATE_FILE.exists():
        try:
            loaded = _normalize_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # فایل خراب: کنار گذاشته می‌شود تا داده از بین نره
            broken = STATE_FILE.with_name(f"yorozuya_state.broken-{int(time.time())}.json")
            try:
                STATE_FILE.replace(broken)
            except OSError:
                pass
            log.error("فایل state خرابه و کنار گذاشته شد (%s): %s", broken.name, exc)
    with _lock:
        _state = loaded
        _save_locked()


def sweep_expired() -> int:
    """کاربرانی که تاریخشون گذشته غیرفعال می‌شن (حذف نمی‌شن)."""
    now = utcnow()
    changed = 0
    with _lock:
        for user in _state["users"]:
            exp = parse_iso(user["expires_at"])
            if exp and exp <= now and user["enabled"]:
                user["enabled"] = False
                user["auto_disabled"] = True
                changed += 1
        if changed:
            _mark_dirty()
    if changed:
        log.info("sweeper: %d کاربر منقضی شد و غیرفعال شد", changed)
    return changed


def _maintenance_loop(stop: threading.Event) -> None:
    while not stop.wait(MAINTENANCE_INTERVAL):
        try:
            sweep_expired()
            flush_state()
        except Exception:
            log.exception("خطا در تسک پس‌زمینه")


def _find(items: list, item_id: str) -> dict | None:
    return next((it for it in items if it["id"] == item_id), None)


def _username_taken(name: str, exclude_id: str = "") -> bool:
    low = name.lower()
    return any(u["username"].lower() == low and u["id"] != exclude_id for u in _state["users"])


def compute_status(user: dict, now: datetime | None = None) -> str:
    now = now or utcnow()
    exp = parse_iso(user["expires_at"])
    if exp and exp <= now:
        return "expired"
    if not user["enabled"]:
        return "disabled"
    if user["limit_gb"] > 0 and user["used_bytes"] >= user["limit_gb"] * GB:
        return "limit_reached"
    return "active"


def user_view(user: dict, now: datetime | None = None) -> dict:
    now = now or utcnow()
    exp = parse_iso(user["expires_at"])
    limit_bytes = int(user["limit_gb"] * GB)
    percent = round(min(100.0, user["used_bytes"] * 100 / limit_bytes), 1) if limit_bytes > 0 else None
    return {
        **user,
        "status": compute_status(user, now),
        "limit_bytes": limit_bytes,
        "usage_percent": percent,
        "days_left": (exp - now).days if exp else None,
    }


# ----------------------------------------------------------------- موقعیت آی‌پی (ip-api) و چک نود
class GeoError(Exception):
    pass


def _geo_from_api(item: dict) -> dict:
    return {
        "ip": str(item.get("query") or ""),
        "country": str(item.get("country") or ""),
        "country_code": str(item.get("countryCode") or "").upper(),
        "city": str(item.get("city") or ""),
        "isp": str(item.get("isp") or ""),
        "org": str(item.get("org") or ""),
    }


def _geo_cache_get(ip: str) -> dict | None:
    with _lock:
        item = _state["ip_cache"].get(ip)
        if not item or time.time() - _to_float(item.get("ts"), 0.0) > GEO_CACHE_TTL:
            return None
        return {k: str(item.get(k) or "") for k in ("ip", "country", "country_code", "city", "isp", "org")}


def _geo_cache_put(ip: str, geo: dict) -> None:
    with _lock:
        _state["ip_cache"][ip] = {**geo, "ip": ip, "ts": time.time()}
        _mark_dirty()


async def geo_one(client: httpx.AsyncClient, ip: str) -> dict:
    """یک آی‌پی (تکی، کش ۷ روزه)."""
    cached = _geo_cache_get(ip)
    if cached:
        return cached
    try:
        resp = await client.get(f"http://ip-api.com/json/{ip}", params={"fields": GEO_FIELDS})
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GeoError("سرویس موقعیت‌یاب در دسترس نیست") from exc
    if not isinstance(data, dict) or data.get("status") != "success":
        msg = data.get("message") if isinstance(data, dict) else ""
        raise GeoError(msg or "موقعیت این آی‌پی پیدا نشد")
    geo = _geo_from_api(data) | {"ip": ip}
    _geo_cache_put(ip, geo)
    return geo


async def geo_batch(client: httpx.AsyncClient, ips: list[str]) -> dict[str, dict]:
    """چند آی‌پی با /batch (هر درخواست حداکثر ۱۰۰ تا)."""
    result: dict[str, dict] = {}
    for start in range(0, len(ips), 100):
        chunk = ips[start : start + 100]
        try:
            resp = await client.post(
                "http://ip-api.com/batch", params={"fields": GEO_FIELDS}, json=[{"query": ip} for ip in chunk]
            )
            items = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeoError("سرویس موقعیت‌یاب در دسترس نیست") from exc
        if not isinstance(items, list):
            raise GeoError("پاسخ سرویس موقعیت‌یاب نامعتبره")
        for item in items:
            if isinstance(item, dict) and item.get("status") == "success":
                geo = _geo_from_api(item)
                result[geo["ip"]] = geo
                _geo_cache_put(geo["ip"], geo)
    return result


def _host_only(address: str) -> str:
    return address.strip().strip("[]")


async def resolve_host(host: str) -> str:
    if is_ip_literal(host):
        return host
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=TCP_TIMEOUT)
    if not infos:
        raise OSError("resolve نشد")
    ipv4 = [info for info in infos if info[0] == socket.AF_INET]
    return (ipv4 or infos)[0][4][0]


async def probe_node(address: str, port: int) -> dict:
    """resolve آسنک + اتصال TCP با تایم‌اوت ۵ ثانیه. latency = زمان اتصال TCP."""
    result = {"resolved_ip": "", "online": False, "latency_ms": None, "error": ""}
    try:
        ip = await resolve_host(_host_only(address))
    except (OSError, asyncio.TimeoutError):
        result["error"] = "DNS"
        return result
    result["resolved_ip"] = ip
    loop = asyncio.get_running_loop()
    writer = None
    started = loop.time()
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=TCP_TIMEOUT)
        result["latency_ms"] = int((loop.time() - started) * 1000)
        result["online"] = True
    except asyncio.TimeoutError:
        result["error"] = "timeout"
    except OSError as exc:
        result["error"] = exc.strerror or str(exc)
    finally:
        if writer is not None:
            writer.close()
    return result


async def run_node_checks(node_ids: set[str] | None) -> list[dict]:
    """چک نودها (همه یا انتخابی). یک نود → ip-api تکی؛ چند نود → /batch."""
    with _lock:
        targets = [
            (n["id"], n["address"], int(n["port"]))
            for n in _state["nodes"]
            if node_ids is None or n["id"] in node_ids
        ]
    if not targets:
        return []
    sem = asyncio.Semaphore(20)

    async def probe(target):
        async with sem:
            return target[0], await probe_node(target[1], target[2])

    results = await asyncio.gather(*(probe(t) for t in targets))
    geo_map: dict[str, dict] = {}
    missing = []
    for _, res in results:
        ip = res["resolved_ip"]
        if ip and ip not in geo_map:
            cached = _geo_cache_get(ip)
            if cached:
                geo_map[ip] = cached
            else:
                missing.append(ip)
    missing = sorted(set(missing))
    if missing:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                if len(missing) == 1:
                    geo_map[missing[0]] = await geo_one(client, missing[0])
                else:
                    geo_map.update(await geo_batch(client, missing))
            except GeoError as exc:
                log.warning("موقعیت‌یابی نودها ناموفق بود: %s", exc)
    stamp = iso_now()
    updated = []
    with _lock:
        for node_id, res in results:
            node = _find(_state["nodes"], node_id)
            if node is None:
                continue
            geo = geo_map.get(res["resolved_ip"])
            if geo:
                node["country"], node["country_code"] = geo["country"], geo["country_code"]
            elif node.get("resolved_ip") != res["resolved_ip"]:
                node["country"] = node["country_code"] = ""
            node.update(
                last_check=stamp,
                online=res["online"],
                latency_ms=res["latency_ms"],
                last_error=res["error"],
                resolved_ip=res["resolved_ip"],
            )
            updated.append(dict(node))
        _mark_dirty()
    return updated


_server_cache: dict = {"ts": 0.0, "ttl": 0, "data": None}


async def fetch_server_info() -> dict:
    info = {"ip": "", "country": "", "country_code": "", "city": "", "isp": "", "org": "", "error": ""}
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            resp = await client.get("http://ip-api.com/json/", params={"fields": GEO_FIELDS})
            data = resp.json()
            if isinstance(data, dict) and data.get("status") == "success":
                geo = _geo_from_api(data)
                info.update({k: geo[k] for k in ("ip", "country", "country_code", "city", "isp", "org")})
            else:
                message = data.get("message") if isinstance(data, dict) else None
                info["error"] = str(message or "خطای ip-api")
        except (httpx.HTTPError, ValueError) as exc:
            info["error"] = f"ip-api: {exc.__class__.__name__}"
        if not info["ip"]:  # فالبک: آی‌پی خروجی از ipify، بعد موقعیت همان آی‌پی
            try:
                resp = await client.get("https://api.ipify.org", params={"format": "json"})
                ip = str(resp.json().get("ip", ""))
            except (httpx.HTTPError, ValueError, AttributeError):
                ip = ""
            if ip:
                info["ip"] = ip
                try:
                    geo = await geo_one(client, ip)
                    info.update({k: geo[k] for k in ("country", "country_code", "city", "isp", "org")})
                    info["error"] = ""
                except GeoError as exc:
                    info["error"] = str(exc)
            elif not info["error"]:
                info["error"] = "آی‌پی سرور پیدا نشد"
    info["flag"] = flag_of(info["country_code"])
    return info


# ----------------------------------------------------------------- ساخت لینک‌ها
def _host_in_url(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def _ws_path(path: str) -> str:
    return path if path.startswith("/") else "/" + path


def _transport_params(node: dict) -> dict:
    net, sec = node["network"], node["security"]
    params = {"type": net, "security": sec}
    if sec in ("tls", "reality"):
        if node["sni"]:
            params["sni"] = node["sni"]
        params["fp"] = node["fp"] or "chrome"
        if node["alpn"]:
            params["alpn"] = node["alpn"]
    if sec == "reality":
        params["pbk"] = node["reality_pbk"]
        if node["reality_sid"]:
            params["sid"] = node["reality_sid"]
        if node["reality_spx"]:
            params["spx"] = node["reality_spx"]
    if net == "ws":
        if node["path"]:
            params["path"] = _ws_path(node["path"])
        if node["host"]:
            params["host"] = node["host"]
    elif net == "grpc" and node["path"]:
        params["serviceName"] = node["path"].lstrip("/")
    return params


def build_node_link(node: dict, user: dict) -> str:
    remark = quote(f"🍓 {node['name']}", safe="")
    proto = node["protocol"]
    host = _host_in_url(node["address"])
    port = int(node["port"])
    if proto == "ss":
        raw = f"{node['ss_method']}:{node['ss_password']}".encode()
        userinfo = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"ss://{userinfo}@{host}:{port}#{remark}"
    if proto == "vmess":
        obj = {
            "v": "2",
            "ps": f"🍓 {node['name']}",
            "add": node["address"],
            "port": str(port),
            "id": user["uuid"],
            "aid": "0",
            "scy": "auto",
            "net": node["network"],
            "type": "none",
            "host": node["host"],
            "path": _ws_path(node["path"]) if node["network"] == "ws" and node["path"] else node["path"],
            "tls": "tls" if node["security"] == "tls" else "",
            "sni": node["sni"],
            "alpn": node["alpn"],
            "fp": node["fp"] if node["security"] == "tls" else "",
        }
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return "vmess://" + base64.b64encode(payload).decode("ascii")
    params = _transport_params(node)
    if proto == "vless":
        params = {"encryption": "none", **params}
        if node["flow"] and node["network"] == "tcp":
            params["flow"] = node["flow"]
    # trojan: پسورد = uuid کاربر
    query = urlencode(params, quote_via=quote)
    return f"{proto}://{user['uuid']}@{host}:{port}?{query}#{remark}"


def build_all_links(user: dict) -> list[str]:
    with _lock:
        nodes = [dict(n) for n in _state["nodes"]]
    return [build_node_link(n, user) for n in nodes]


def build_import_apps(sub_url: str, title: str) -> list[dict]:
    url_enc, name_enc = quote(sub_url, safe=""), quote(title, safe="")
    return [
        {"id": app_id, "name": name, "platform": platform, "url": tpl.format(url=url_enc, raw=sub_url, name=name_enc)}
        for app_id, name, platform, tpl in IMPORT_APPS
    ]


def sub_headers(user: dict) -> dict:
    total = int(user["limit_gb"] * GB) if user["limit_gb"] > 0 else 0
    exp = parse_iso(user["expires_at"])
    title = base64.b64encode(f"🍓 {user['username']}".encode()).decode()
    return {
        "Subscription-Userinfo": (
            f"upload=0; download={user['used_bytes']}; total={total}; expire={int(exp.timestamp()) if exp else 0}"
        ),
        "Profile-Update-Interval": "12",
        "Profile-Title": f"base64:{title}",
        "Cache-Control": "no-store",
    }


def public_base_url(request: Request) -> str:
    with _lock:
        configured = _state["settings"].get("sub_base_url", "")
    if configured:
        return configured
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc)
    return f"{proto}://{host.split(',')[0].strip()}"


# ----------------------------------------------------------------- احراز هویت
_failed_logins: dict[str, list[float]] = {}


def _make_token() -> str:
    payload = {"exp": int(time.time()) + SESSION_TTL, "n": secrets.token_hex(8)}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return int(payload["exp"]) > time.time()
    except (ValueError, KeyError, TypeError):
        return False


def is_authed(request: Request) -> bool:
    return _token_valid(request.cookies.get(COOKIE_NAME))


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="اول وارد شو 🍓")


def _is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip() == "https"


# ----------------------------------------------------------------- مدل‌های ورودی
class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    limit_gb: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False)
    expires_at: str = Field(default="", max_length=40)
    enabled: bool = True
    note: str = Field(default="", max_length=200)

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        return clean_username(v)


class UserUpdate(BaseModel):
    username: Optional[str] = Field(default=None, min_length=1, max_length=32)
    limit_gb: Optional[float] = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    used_gb: Optional[float] = Field(default=None, ge=0, le=10_000_000, allow_inf_nan=False)
    expires_at: Optional[str] = Field(default=None, max_length=40)
    enabled: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=200)

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else clean_username(v)


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    address: str = Field(min_length=1, max_length=260)
    port: int = Field(default=443, ge=1, le=65535)
    protocol: Literal["vless", "vmess", "trojan", "ss"] = "vless"
    network: Literal["tcp", "ws", "grpc"] = "tcp"
    security: Literal["none", "tls", "reality"] = "none"
    sni: str = Field(default="", max_length=253)
    path: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=253)
    fp: str = Field(default="chrome", max_length=20)
    alpn: str = Field(default="", max_length=100)
    reality_pbk: str = Field(default="", max_length=100)
    reality_sid: str = Field(default="", max_length=32)
    reality_spx: str = Field(default="", max_length=200)
    flow: str = Field(default="", max_length=50)
    ss_method: str = Field(default="", max_length=60)
    ss_password: str = Field(default="", max_length=200)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("نام نود لازمه")
        return v

    @field_validator("address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        v = v.strip()
        if "://" in v:  # اگه URL کامل چسبوندن، هاست رو برمی‌داریم
            v = urlparse(v).hostname or ""
        v = v.strip("[]")
        if not v or not (is_ip_literal(v) or HOST_RE.fullmatch(v)):
            raise ValueError("آدرس نود باید IP یا دامنه معتبر باشه")
        return v

    @model_validator(mode="after")
    def _check_combo(self) -> "NodeIn":
        if self.protocol == "ss" and not (self.ss_method and self.ss_password):
            raise ValueError("برای Shadowsocks متد و رمز لازمه")
        if self.security == "reality":
            if self.protocol not in ("vless", "trojan"):
                raise ValueError("REALITY فقط با VLESS یا Trojan کار می‌کنه")
            if not self.reality_pbk:
                raise ValueError("Public key (pbk) برای REALITY لازمه")
        return self


class SettingsIn(BaseModel):
    sub_base_url: str = Field(default="", max_length=300)

    @field_validator("sub_base_url")
    @classmethod
    def _check_base(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not re.fullmatch(r"https?://[^\s/]+(?:/\S*)?", v):
            raise ValueError("آدرس باید با http:// یا https:// شروع بشه")
        return v


class LookupIn(BaseModel):
    query: str = Field(min_length=1, max_length=300)


# ----------------------------------------------------------------- اپلیکیشن
@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not os.environ.get("SECRET_KEY"):
        log.warning("SECRET_KEY تنظیم نشده؛ کلید موقت ساخته شد (با هر ری‌استارت سشن‌ها باطل می‌شن)")
    if not os.environ.get("ADMIN_PASSWORD"):
        log.warning("ADMIN_PASSWORD تنظیم نشده؛ رمز پیش‌فرض yorozuya فعاله. حتماً عوضش کن!")
    load_state()
    sweep_expired()
    stop = threading.Event()
    worker = threading.Thread(target=_maintenance_loop, args=(stop,), name="yorozuya-maintenance", daemon=True)
    worker.start()
    log.info("YorozuyaPanel v%s آماده است (state: %s)", APP_VERSION, STATE_FILE)
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=5)
        flush_state(force=True)


app = FastAPI(
    title="YorozuyaPanel",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
admin = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


@app.exception_handler(RequestValidationError)
async def _invalid_input(_request: Request, exc: RequestValidationError):
    parts = []
    for err in exc.errors():
        if err.get("type") == "missing":
            parts.append(f"فیلد «{err['loc'][-1]}» الزامیه")
        else:
            parts.append(str(err.get("msg", "")).replace("Value error, ", ""))
    return JSONResponse(status_code=400, content={"detail": "؛ ".join(parts) or "ورودی نامعتبره"})


# ---- صفحات
@app.get("/", include_in_schema=False)
def index(request: Request):
    return RedirectResponse("/panel" if is_authed(request) else "/login")


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if is_authed(request):
        return RedirectResponse("/panel")
    return FileResponse(STATIC_DIR / "login.html", headers=NO_CACHE)


@app.get("/panel", include_in_schema=False)
def panel_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "panel.html", headers=NO_CACHE)


@app.get("/p/{key}", include_in_schema=False)
def sub_page(key: str):
    return FileResponse(STATIC_DIR / "sub.html", headers=NO_CACHE)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": APP_VERSION}


# ---- ورود / خروج
@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = [t for t in _failed_logins.get(ip, []) if now - t < LOGIN_WINDOW]
    _failed_logins[ip] = hits
    if len(hits) >= LOGIN_MAX_FAILS:
        raise HTTPException(429, "تلاش زیاد! چند دقیقه صبر کن و دوباره امتحان کن")
    if not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        hits.append(now)
        raise HTTPException(401, "رمز اشتباهه")
    _failed_logins.pop(ip, None)
    response.set_cookie(
        COOKIE_NAME,
        _make_token(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@admin.get("/me")
def me():
    return {"ok": True, "version": APP_VERSION}


# ---- کاربران
@admin.get("/users")
def list_users():
    now = utcnow()
    with _lock:
        return [user_view(u, now) for u in _state["users"]]


@admin.post("/users", status_code=201)
def create_user(body: UserCreate):
    expires = normalize_expiry(body.expires_at)
    if body.enabled and is_past(expires):
        raise HTTPException(400, "تاریخ انقضا گذشته؛ اول تاریخ را آینده بگذار")
    with _lock:
        if _username_taken(body.username):
            raise HTTPException(409, "این نام کاربری قبلاً ثبت شده")
        user = {
            "id": uuid.uuid4().hex,
            "username": body.username,
            "uuid": str(uuid.uuid4()),
            "sub_key": secrets.token_urlsafe(18),
            "limit_gb": float(body.limit_gb),
            "used_bytes": 0,
            "expires_at": expires,
            "enabled": body.enabled,
            "auto_disabled": False,
            "note": body.note.strip(),
            "created_at": iso_now(),
        }
        _state["users"].append(user)
        _mark_dirty()
        return user_view(user)


@admin.put("/users/{user_id}")
def update_user(user_id: str, body: UserUpdate):
    changes = body.model_dump(exclude_none=True)
    with _lock:
        user = _find(_state["users"], user_id)
        if user is None:
            raise HTTPException(404, "کاربر پیدا نشد")
        draft = dict(user)
        if "username" in changes:
            if _username_taken(changes["username"], exclude_id=user_id):
                raise HTTPException(409, "این نام کاربری قبلاً ثبت شده")
            draft["username"] = changes["username"]
        if "limit_gb" in changes:
            draft["limit_gb"] = float(changes["limit_gb"])
        if "used_gb" in changes:
            draft["used_bytes"] = int(changes["used_gb"] * GB)
        if "note" in changes:
            draft["note"] = changes["note"].strip()
        if "expires_at" in changes:
            draft["expires_at"] = normalize_expiry(changes["expires_at"])
        if "enabled" in changes:
            draft["enabled"] = changes["enabled"]
            draft["auto_disabled"] = False
        if not is_past(draft["expires_at"]):
            draft["auto_disabled"] = False
        if draft["enabled"] and is_past(draft["expires_at"]):
            raise HTTPException(400, "کاربر منقضی را نمی‌شه فعال کرد؛ اول تاریخ انقضا را تمدید کن")
        user.update(draft)
        _mark_dirty()
        return user_view(user)


@admin.delete("/users/{user_id}")
def delete_user(user_id: str):
    with _lock:
        before = len(_state["users"])
        _state["users"][:] = [u for u in _state["users"] if u["id"] != user_id]
        if len(_state["users"]) == before:
            raise HTTPException(404, "کاربر پیدا نشد")
        _mark_dirty()
    return {"ok": True}


@admin.post("/users/{user_id}/reset-traffic")
def reset_traffic(user_id: str):
    with _lock:
        user = _find(_state["users"], user_id)
        if user is None:
            raise HTTPException(404, "کاربر پیدا نشد")
        user["used_bytes"] = 0
        _mark_dirty()
        return user_view(user)


@admin.post("/users/{user_id}/rotate-sub")
def rotate_sub(user_id: str):
    with _lock:
        user = _find(_state["users"], user_id)
        if user is None:
            raise HTTPException(404, "کاربر پیدا نشد")
        user["sub_key"] = secrets.token_urlsafe(18)
        _mark_dirty()
        return user_view(user)


@admin.get("/users/{user_id}/links")
def user_links(user_id: str, request: Request):
    base = public_base_url(request)
    with _lock:
        user = _find(_state["users"], user_id)
        if user is None:
            raise HTTPException(404, "کاربر پیدا نشد")
        user = dict(user)
        nodes = [dict(n) for n in _state["nodes"]]
    sub_url = f"{base}/api/sub/{user['sub_key']}"
    title = f"🍓 {user['username']}"
    return {
        "username": user["username"],
        "sub_url": sub_url,
        "qr_url": f"{sub_url}/qr",
        "page_url": f"{base}/p/{user['sub_key']}",
        "links": [
            {"node_id": n["id"], "name": n["name"], "protocol": n["protocol"], "link": build_node_link(n, user)}
            for n in nodes
        ],
        "apps": build_import_apps(sub_url, title),
    }


# ---- نودها
@admin.get("/nodes")
def list_nodes():
    with _lock:
        return [dict(n) for n in _state["nodes"]]


@admin.post("/nodes", status_code=201)
def create_node(body: NodeIn):
    node = {"id": uuid.uuid4().hex, **NODE_DEFAULTS, **body.model_dump(), "created_at": iso_now()}
    with _lock:
        _state["nodes"].append(node)
        _mark_dirty()
        return dict(node)


@admin.put("/nodes/{node_id}")
def update_node(node_id: str, body: NodeIn):
    fields = body.model_dump()
    with _lock:
        node = _find(_state["nodes"], node_id)
        if node is None:
            raise HTTPException(404, "نود پیدا نشد")
        endpoint_changed = node["address"] != fields["address"] or node["port"] != fields["port"]
        node.update(fields)
        if endpoint_changed:
            node.update(
                last_check="", online=None, latency_ms=None, last_error="", resolved_ip="", country="", country_code=""
            )
        _mark_dirty()
        return dict(node)


@admin.delete("/nodes/{node_id}")
def delete_node(node_id: str):
    with _lock:
        before = len(_state["nodes"])
        _state["nodes"][:] = [n for n in _state["nodes"] if n["id"] != node_id]
        if len(_state["nodes"]) == before:
            raise HTTPException(404, "نود پیدا نشد")
        _mark_dirty()
    return {"ok": True}


@admin.post("/nodes/check-all")
async def check_all_nodes():
    updated = await run_node_checks(None)
    return {"checked": len(updated), "nodes": updated}


@admin.post("/nodes/{node_id}/check")
async def check_one_node(node_id: str):
    with _lock:
        if _find(_state["nodes"], node_id) is None:
            raise HTTPException(404, "نود پیدا نشد")
    updated = await run_node_checks({node_id})
    return updated[0] if updated else {}


# ---- سرور، ابزار، تنظیمات
@admin.get("/server-info")
async def server_info(refresh: bool = False):
    now = time.time()
    if not refresh and _server_cache["data"] and now - _server_cache["ts"] < _server_cache["ttl"]:
        return _server_cache["data"]
    data = await fetch_server_info()
    _server_cache.update(ts=now, ttl=SERVER_INFO_TTL if not data["error"] else 60, data=data)
    return data


@admin.post("/tools/lookup")
async def tools_lookup(body: LookupIn):
    raw = body.query.strip()
    if "://" in raw:
        host = urlparse(raw).hostname or ""
    else:
        host = raw.split("/")[0]
        if host.count(":") == 1:  # host:port
            host = host.split(":")[0]
    host = host.strip().strip("[]")
    if not host or any(ch.isspace() for ch in host):
        raise HTTPException(400, "دامنه یا آی‌پی معتبر وارد کن")
    try:
        ip = await resolve_host(host)
    except (OSError, asyncio.TimeoutError):
        raise HTTPException(400, "این دامنه resolve نشد")
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            geo = await geo_one(client, ip)
        except GeoError as exc:
            raise HTTPException(502, str(exc))
    return {"query": raw, "host": host, **geo, "ip": ip, "flag": flag_of(geo["country_code"])}


@admin.get("/settings")
def get_settings(request: Request):
    with _lock:
        configured = _state["settings"].get("sub_base_url", "")
    return {"sub_base_url": configured, "effective_base_url": public_base_url(request), "version": APP_VERSION}


@admin.put("/settings")
def put_settings(body: SettingsIn):
    with _lock:
        _state["settings"]["sub_base_url"] = body.sub_base_url
        _mark_dirty()
    flush_state()
    return {"ok": True, "sub_base_url": body.sub_base_url}


@admin.get("/backup")
def backup():
    with _lock:
        payload = {
            "app": "YorozuyaPanel",
            "version": APP_VERSION,
            "exported_at": iso_now(),
            "users": [dict(u) for u in _state["users"]],
            "nodes": [dict(n) for n in _state["nodes"]],
            "settings": dict(_state["settings"]),
            "ip_cache": dict(_state["ip_cache"]),
        }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"yorozuya-backup-{utcnow():%Y%m%d-%H%M%S}.json"
    return Response(
        body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@admin.post("/restore")
async def restore(file: UploadFile = File(...)):
    global _state
    raw = await file.read(MAX_RESTORE_BYTES + 1)
    if len(raw) > MAX_RESTORE_BYTES:
        raise HTTPException(413, "فایل بکاپ خیلی بزرگه (حداکثر ۲۰ مگابایت)")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        new_state = _normalize_state(data)
    except Exception as exc:  # JSON خراب یا ساختار غلط
        raise HTTPException(400, f"فایل بکاپ معتبر نیست: {exc}")
    with _lock:
        _state = new_state
        _mark_dirty()
        flush_state(force=True)
    return {"ok": True, "users": len(new_state["users"]), "nodes": len(new_state["nodes"])}


app.include_router(admin)


# ---- اشتراک عمومی (بدون لاگین؛ با چک فعال/انقضا)
def _sub_user_or_404(key: str) -> dict:
    with _lock:
        for user in _state["users"]:
            if user["sub_key"] == key:
                return dict(user)
    raise HTTPException(404, "اشتراک پیدا نشد")


def _active_user_or_403(key: str) -> dict:
    user = _sub_user_or_404(key)
    status = compute_status(user)
    if status != "active":
        raise HTTPException(403, SUB_INACTIVE_MESSAGES.get(status, "این اشتراک فعال نیست"))
    return user


@app.get("/api/sub/{key}")
def subscription(key: str, format: str = "base64"):
    user = _active_user_or_403(key)
    text = "\n".join(build_all_links(user))
    if format == "text":
        return Response(text, media_type="text/plain; charset=utf-8", headers=sub_headers(user))
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return Response(encoded, media_type="text/plain; charset=utf-8", headers=sub_headers(user))


@app.get("/api/sub/{key}/qr")
def subscription_qr(key: str, request: Request):
    _active_user_or_403(key)
    url = f"{public_base_url(request)}/api/sub/{key}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#2b1a10", back_color="white")
    buf = BytesIO()
    image.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png", headers=NO_CACHE)


@app.get("/api/public/sub/{key}")
def public_subscription(key: str, request: Request):
    user = _sub_user_or_404(key)
    view = user_view(user)
    base = public_base_url(request)
    sub_url = f"{base}/api/sub/{key}"
    nodes = []
    if view["status"] == "active":
        with _lock:
            nodes = [
                {
                    "name": n["name"],
                    "protocol": n["protocol"],
                    "network": n["network"],
                    "security": n["security"],
                    "country": n["country"],
                    "country_code": n["country_code"],
                    "flag": flag_of(n["country_code"]),
                    "online": n["online"],
                    "latency_ms": n["latency_ms"],
                }
                for n in _state["nodes"]
            ]
    return {
        "username": user["username"],
        "status": view["status"],
        "used_bytes": user["used_bytes"],
        "limit_bytes": view["limit_bytes"],
        "usage_percent": view["usage_percent"],
        "expires_at": user["expires_at"] or None,
        "days_left": view["days_left"],
        "nodes": nodes,
        "sub_url": sub_url,
        "qr_url": f"{sub_url}/qr",
        "apps": build_import_apps(sub_url, f"🍓 {user['username']}"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
