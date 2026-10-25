# -*- coding: utf-8 -*-
# ==================== STANDARD IMPORTS ====================
import sys
import asyncio
import httpx
import random
import json
import socket
import struct
import time
import os
import uuid
import itertools
import math
import collections
import secrets
import traceback
import base64
import signal
import hashlib
import re
from urllib.parse import urlparse, quote
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser
from message_ids import MESSAGE_ID_TO_NAME
import CguTeam_pb2
import CguFF_pb2
import thunderFF_pb2

from dashboard_server import bot_state, start_web_dashboard, user_store
from dashboard_server import _read_accounts, _write_accounts, _entry_ids
from persist import store as persist_store          # MongoDB-backed storage (survives restarts / redeploys)

def token_key(token) -> str:
    """Stable per-token identifier built from the WHOLE token (the old code used only the first
    10/20 characters, so two tokens with the same beginning were treated as the same account:
    deleting one stopped them all)."""
    return "tok_" + hashlib.sha1(str(token).encode("utf-8")).hexdigest()[:20]


# ==================== CONFIGURATION ====================
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", 20333))
ACCOUNTS_FILE = "accounts.json"
TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
TOKEN_CACHE_TTL = 1200
SEARCH_STALL_TIMEOUT = float(os.environ.get("SEARCH_STALL_TIMEOUT", "240"))  # no match for this long -> fresh login (self-heal)
VERBOSE_LOGS = os.environ.get("VERBOSE_LOGS", "0").strip().lower() in ("1", "true", "yes")

START_MATCH_INTERVAL = 4.0
NEW_MATCH_DELAY = 5.0
MAX_MATCH_DURATION = 800
MATCH_IDLE_TIMEOUT = 15.0
MAX_CONCURRENT_MATCHES = 1
# MAX_PARALLEL_MATCHES (Railway variable, optional): how many matches ONE account may play at the same time.
# 0 (default) = no limit, exactly as before (the bot keeps starting new matches while old ones still run).
MAX_PARALLEL_MATCHES = max(0, int(os.environ.get("MAX_PARALLEL_MATCHES", "0") or 0))

EXP_REFRESH_INTERVAL = 12.0
EXP_REFRESH_AFTER_MATCH_DELAY = 3.0

# ---- speed / fairness when MANY accounts run together ----
LOGIN_CONCURRENCY = 10           # logins running at the same time (the rest wait in a fair queue)
REFRESH_CONCURRENCY = 20         # EXP refresh requests at the same time
VERSION_CACHE_TTL = 900          # Play Store / version server answer is reused for 15 minutes
EXP_REFRESH_MAX_INTERVAL = 45.0  # with hundreds of accounts the refresh slows down a little instead of choking
TOKEN_CACHE_FLUSH_INTERVAL = 5.0 # token_cache.json is written in the background, not on every update
LOGIN_BACKOFF_MAX = 120          # a failing account retries 15s, 30s, 60s, 120s... (not every 15s forever)
PLAN_GUARD_INTERVAL = 5.0        # how often a running account checks that its plan is still valid

_login_sem = asyncio.Semaphore(LOGIN_CONCURRENCY)
_refresh_sem = asyncio.Semaphore(REFRESH_CONCURRENCY)

MAX_CONSECUTIVE_PARSE_FAILURES = 5.0
NON_MATCH_RECONNECT_DELAY = 1.0

LONE_WOLF_UNLOCK_LEVEL = 3
LONE_WOLF_MODE_ID = 43
LONE_WOLF_MAP_ID = 11
BR_MODE_ID = 1
BR_MAP_ID = 1

ANTI_AFK_FIRE_CHANCE = 0.45
ANTI_AFK_MIN_INTERVAL = 1.8
ANTI_AFK_MAX_INTERVAL = 3.5

FALLBACK_UID = ""
FALLBACK_PASSWORD = ""


# ==================== DEVICE RANDOMIZER ====================
def get_device_for_account(account_identifier: str) -> dict:
    devices = persist_store.load("devices", DEVICES_FILE, {})
    if not isinstance(devices, dict):
        devices = {}
    acc_key = str(account_identifier)
    if acc_key in devices:
        return devices[acc_key]

    device_list = [
        ("Samsung", "SM-G998B", "Adreno (TM) 660", "Android OS 12 / API-31"),
        ("Xiaomi", "2201122G", "Adreno (TM) 730", "Android OS 13 / API-33"),
        ("Realme", "RMX3700", "Mali-G710", "Android OS 14 / API-34"),
        ("OnePlus", "CPH2451", "Adreno (TM) 740", "Android OS 13 / API-33"),
        ("OPPO", "CPH2611", "Adreno (TM) 720", "Android OS 14 / API-34"),
        ("Vivo", "V2203", "Mali-G710", "Android OS 12 / API-31"),
        ("Poco", "M2102J20SG", "Adreno (TM) 660", "Android OS 13 / API-33"),
    ]
    brand, model, gpu, os_ver = random.choice(device_list)
    new_device = {
        "unique_device_id": f"Google|{str(uuid.uuid4())}",
        "brand": brand, "model": model, "gpu_renderer": gpu,
        "system_software": os_ver,
        "screen_width": random.choice([1080, 1440, 720, 1280]),
        "screen_height": random.choice([2400, 3200, 1600, 2400]),
        "screen_dpi": str(random.randint(300, 420)),
        "memory": random.randint(2800, 6500),
        "processor_details": f"ARM64 FP ASIMD AES VMH | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
        "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}",
        "android_id": secrets.token_hex(8),
        "gsf_id": secrets.token_hex(8),
        "build_fingerprint": f"{brand.lower()}/{model.lower()}/{model.lower()}:{os_ver.split(' / ')[0].replace(' ', '_')}/user/release-keys",
        "serial_number": secrets.token_hex(8),
        "hardware_id": secrets.token_hex(8),
    }
    devices[acc_key] = new_device
    try:
        persist_store.save("devices", DEVICES_FILE, devices)
    except Exception as e:
        print_error(f"Failed to save device mapping: {e}")
    return new_device


# ==================== CLOUDFLARE DNS ====================
CLOUDFLARE_PRIMARY_DNS = "1.1.1.1"
CLOUDFLARE_SECONDARY_DNS = "1.0.0.1"
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_DNS_CACHE_TTL = 300.0


async def resolve_host_cloudflare(hostname: str) -> str:
    if not hostname:
        return hostname
    parts = hostname.split('.')
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return hostname
    now = time.time()
    if hostname in _DNS_CACHE:
        ip, exp = _DNS_CACHE[hostname]
        if now < exp:
            return ip

    def _query_cloudflare(server_ip: str) -> Optional[str]:
        s = None
        try:
            tx_id = random.randint(1000, 65535)
            header = struct.pack(">HHHHHH", tx_id, 0x0100, 1, 0, 0, 0)
            qname = b"".join(bytes([len(part)]) + part.encode('ascii') for part in hostname.split('.')) + b"\x00"
            query_pkt = header + qname + struct.pack(">HH", 1, 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.2)
            s.sendto(query_pkt, (server_ip, 53))
            resp, _ = s.recvfrom(1024)
            if len(resp) >= 12:
                ancount = struct.unpack(">H", resp[6:8])[0]
                if ancount > 0:
                    offset = 12 + len(qname) + 4
                    for _ in range(ancount):
                        if offset >= len(resp):
                            break
                        if (resp[offset] & 0xC0) == 0xC0:
                            offset += 2
                        else:
                            while offset < len(resp) and resp[offset] != 0:
                                offset += 1 + resp[offset]
                            offset += 1
                        if offset + 10 > len(resp):
                            break
                        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", resp[offset:offset+10])
                        offset += 10
                        if rtype == 1 and rdlen == 4 and offset + 4 <= len(resp):
                            return socket.inet_ntoa(resp[offset:offset+4])
                        offset += rdlen
        except Exception:
            pass
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        return None

    loop = asyncio.get_running_loop()
    ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_PRIMARY_DNS)
    if not ip:
        ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_SECONDARY_DNS)
    if not ip:
        try:
            ip_info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
            if ip_info:
                ip = ip_info[0][4][0]
        except Exception:
            ip = hostname
    if ip:
        _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
    return ip or hostname


def optimize_tcp_socket(sock: socket.socket):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    except Exception:
        pass


def optimize_udp_socket(sock: socket.socket):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
        if hasattr(socket, 'SIO_UDP_CONNRESET') and os.name == 'nt':
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except Exception:
                pass
    except Exception:
        pass


# ==================== NETWORK & CRYPTO ====================
client = httpx.AsyncClient(
    verify=False,
    timeout=httpx.Timeout(15.0, connect=8.0, pool=30.0),
    limits=httpx.Limits(max_connections=400, max_keepalive_connections=100)
)

headers = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Expect': '100-continue',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859',
    'X-GA': 'v1 1',
    'ReleaseVersion': 'OB55'
}

AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV = b'6oyZDr22E3ychjM%'

CRC7_TABLE = bytes([
    0, 9, 18, 27, 36, 45, 54, 63, 72, 65, 90, 83, 108, 101, 126, 119,
    25, 16, 11, 2, 61, 52, 47, 38, 81, 88, 67, 74, 117, 124, 103, 110,
    50, 59, 32, 41, 22, 31, 4, 13, 122, 115, 104, 97, 94, 87, 76, 69,
    43, 34, 57, 48, 15, 6, 29, 20, 99, 106, 113, 120, 71, 78, 85, 92,
    100, 109, 118, 127, 64, 73, 82, 91, 44, 37, 62, 55, 8, 1, 26, 19,
    125, 116, 111, 102, 89, 80, 75, 66, 53, 60, 39, 46, 17, 24, 3, 10,
    86, 95, 68, 77, 114, 123, 96, 105, 30, 23, 12, 5, 58, 51, 40, 33,
    79, 70, 93, 84, 107, 98, 121, 112, 7, 14, 21, 28, 35, 42, 49, 56,
    65, 72, 83, 90, 101, 108, 119, 126, 9, 0, 27, 18, 45, 36, 63, 54,
    88, 81, 74, 67, 124, 117, 110, 103, 16, 25, 2, 11, 52, 61, 38, 47,
    115, 122, 97, 104, 87, 94, 69, 76, 59, 50, 41, 32, 31, 22, 13, 4,
    106, 99, 120, 113, 78, 71, 92, 85, 34, 43, 48, 57, 6, 15, 20, 29,
    37, 44, 55, 62, 1, 8, 19, 26, 109, 100, 127, 118, 73, 64, 91, 82,
    60, 53, 46, 39, 24, 17, 10, 3, 116, 125, 102, 111, 80, 89, 66, 75,
    23, 30, 5, 12, 51, 58, 33, 40, 95, 86, 77, 68, 123, 114, 105, 96,
    14, 7, 28, 21, 42, 35, 56, 49, 70, 79, 84, 93, 98, 107, 112, 121,
])

_DELTA = 0x9E3779B9
_ROUNDS = 16
_FIELD_SIZES = {0: 1, 1: 2, 2: 2, 3: 1, 4: 2}
_FIELD_NAMES = {0: "sendOption", 1: "cmd", 2: "orderId", 3: "flags", 4: "length"}


class Colors:
    HEADER = '\033[95m'
    GREEN = '\033[92m'
    FAIL = '\033[91m'
    WARNING = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    ENDC = '\033[0m'


def print_colored(text, color=Colors.WHITE):
    try:
        print(f"{color}{text}{Colors.ENDC}")
    except Exception:
        try:
            print(f"{color}{text.encode('ascii', errors='replace').decode('ascii')}{Colors.ENDC}")
        except Exception:
            pass


def print_success(text):
    print_colored(f"[+] {text}", Colors.GREEN)
    try:
        bot_state.log(text, "success")
    except Exception:
        pass


def print_error(text):
    print_colored(f"[-] {text}", Colors.FAIL)
    try:
        bot_state.log(text, "error")
    except Exception:
        pass


def print_warning(text):
    print_colored(f"[!] {text}", Colors.WARNING)
    try:
        bot_state.log(text, "warning")
    except Exception:
        pass


def print_debug(text):
    """Very chatty lines (every search packet). Only shown with VERBOSE_LOGS=1 so 100s of accounts do not flood the log."""
    if VERBOSE_LOGS:
        print_info(text)


def print_info(text):
    print_colored(f"[i] {text}", Colors.CYAN)
    try:
        bot_state.log(text, "info")
    except Exception:
        pass


def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict):
        return default
    if key in d:
        val = d[key].get('data')
        return val if val is not None else default
    if str(key) in d:
        val = d[str(key)].get('data')
        return val if val is not None else default
    return default


# ==================== MATCH COUNTER ====================
_match_counters: Dict[str, int] = {}
_match_counter_lock = asyncio.Lock()


async def _inc_match(uid: str) -> int:
    async with _match_counter_lock:
        _match_counters[uid] = _match_counters.get(uid, 0) + 1
        return _match_counters[uid]


async def _dec_match(uid: str) -> int:
    async with _match_counter_lock:
        if uid in _match_counters and _match_counters[uid] > 0:
            _match_counters[uid] -= 1
        return _match_counters.get(uid, 0)


async def _get_match_count(uid: str) -> int:
    async with _match_counter_lock:
        return _match_counters.get(uid, 0)


async def _get_total_match_count() -> int:
    async with _match_counter_lock:
        return sum(_match_counters.values())


# ==================== TOKEN CACHE ====================
# The cache lives in memory. token_cache.json is only a backup that a background task
# writes every few seconds - before, EVERY exp refresh re-wrote and re-read the whole file,
# which froze the bot when many accounts were running.
_token_cache_store: Optional[Dict[str, Any]] = None
_token_cache_dirty = False


def _json_serializer(obj):
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes_hex__": bytes(obj).hex()}
    raise TypeError(f"Type {type(obj)} not serializable")


def _json_deserializer(obj):
    if isinstance(obj, dict):
        if "__bytes_hex__" in obj and len(obj) == 1:
            try:
                return bytes.fromhex(obj["__bytes_hex__"])
            except Exception:
                return b""
        return {k: _json_deserializer(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_deserializer(x) for x in obj]
    return obj


def _load_token_cache() -> Dict[str, Any]:
    global _token_cache_store
    if _token_cache_store is not None:
        return _token_cache_store
    cache: Dict[str, Any] = {}
    try:
        data = persist_store.load("token_cache", TOKEN_CACHE_FILE, {})
        if not isinstance(data, dict):
            raise ValueError("Cache root must be dict")
        cache = _json_deserializer(data)
    except Exception as e:
        print_error(f"Token cache unreadable -> starting empty: {e}")
        cache = {}
    _token_cache_store = cache
    return cache


def _save_token_cache(cache: Dict[str, Any]):
    global _token_cache_store, _token_cache_dirty
    _token_cache_store = cache
    _token_cache_dirty = True


def _flush_token_cache_sync():
    """Write the cache to disk (runs in a worker thread, or directly at shutdown)."""
    global _token_cache_dirty
    if not _token_cache_dirty or _token_cache_store is None:
        return
    _token_cache_dirty = False
    text = None
    for _ in range(4):
        try:
            text = json.dumps(_token_cache_store, default=_json_serializer)
            break
        except RuntimeError:          # dict changed while serialising -> try again
            time.sleep(0.05)
    if text is None:
        _token_cache_dirty = True
        return
    try:
        # local file right away; MongoDB at most every 20 s (a lost cache only costs one re-login)
        persist_store.save("token_cache", TOKEN_CACHE_FILE, text=text, min_interval=20.0)
    except Exception as e:
        _token_cache_dirty = True
        print_error(f"Token cache save error: {e}")


async def token_cache_flusher():
    while True:
        await asyncio.sleep(TOKEN_CACHE_FLUSH_INTERVAL)
        try:
            await asyncio.to_thread(_flush_token_cache_sync)
        except Exception as e:
            print_warning(f"[CACHE] background save failed: {e}")


def cache_get(uid: str) -> Optional[Dict]:
    cache = _load_token_cache()
    entry = cache.get(str(uid))
    if not entry:
        return None
    if time.time() - entry.get("cached_at", 0) > TOKEN_CACHE_TTL:
        print_info(f"[CACHE] UID {uid} expired. Re-login needed.")
        cache_invalidate(uid)
        return None
    if str(entry.get("account_id", "")).isdigit():
        entry["account_id"] = int(entry["account_id"])
    if not isinstance(entry.get("login_payload_data"), (bytes, bytearray)):
        print_warning(f"[CACHE] UID {uid} missing payload -> invalidating")
        cache_invalidate(uid)
        return None
    return entry


def cache_set(uid: str, account_data: Dict):
    cache = _load_token_cache()
    entry = dict(account_data)
    # small random offset so hundreds of accounts do not all expire (and re-login) in the same second
    entry["cached_at"] = time.time() - random.uniform(0, 180)
    cache[str(uid)] = entry
    _save_token_cache(cache)
    print_success(f"[CACHE] Saved credentials for UID {uid}")


def cache_invalidate(uid: str):
    cache = _load_token_cache()
    if str(uid) in cache:
        del cache[str(uid)]
        _save_token_cache(cache)
        print_warning(f"[CACHE] Invalidated: {uid}")


def _update_cache_profile(account_data: Dict, level: int, exp: int):
    try:
        auth_uid = account_data.get('auth_uid')
        auth_token = account_data.get('auth_token')
        keys = []
        if auth_uid:
            keys.append(str(auth_uid))
        if auth_token:
            keys.append(token_key(auth_token))
        if not keys:
            return
        cache = _load_token_cache()
        changed = False
        for k in keys:
            if k in cache and isinstance(cache[k], dict):
                cache[k]['level'] = level
                cache[k]['exp'] = exp
                changed = True
        if changed:
            _save_token_cache(cache)
    except Exception as e:
        print_warning(f"[EXP-REFRESH] cache update failed: {e}")


# ==================== ENCRYPTION ====================
async def aes_encrypt(payload, key, iv):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad(payload, AES.block_size))


_version_state: Dict[str, Any] = {"app_version": None, "app_time": 0.0, "config": None, "config_time": 0.0}
_version_lock = asyncio.Lock()


VER_URL = "https://version.ggwhitehawk.com/live/ver.php"
_VER_RE = re.compile(r"^\d{1,2}\.\d{1,3}\.\d{1,3}$")             # 1.132.11   (NOT "Varies with device")
DEFAULT_FALLBACK_VERSIONS = ("1.132.11", "1.132.8")               # asked when the Play Store gives nothing usable
MAX_VERSION_CANDIDATES = 4
_warn_times: Dict[str, float] = {}
_version_log = {"last": None}


def _warn_limited(key: str, msg: str, every: float = 60.0):
    """Same warning at most once per minute (no log spam when something keeps failing)."""
    now = time.time()
    if now - _warn_times.get(key, 0.0) >= every:
        _warn_times[key] = now
        print_warning(msg)


def _env_app_versions() -> List[str]:
    raw = os.environ.get("APP_VERSION", "") or ""
    return [v for v in (x.strip() for x in re.split(r"[,\s;]+", raw)) if _VER_RE.match(v)]


async def get_playstore_version():
    """A plausible app version (x.y.z) or None. Garena answers 'code 2' (no login server) for any version it does
    not know ('Varies with device', '2.133.11' ...), so junk from the Play Store scraper is ignored here."""
    env = _env_app_versions()
    if env:                                                       # APP_VERSION=1.132.11 in Railway = skip Play Store
        return env[0]
    now = time.time()
    if _version_state["app_version"] and now - _version_state["app_time"] < VERSION_CACHE_TTL * 2:
        return _version_state["app_version"]                      # a version Garena already accepted
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id')),
            timeout=25
        )
        version = str((result or {}).get("version") or "").strip()
        if _VER_RE.match(version):
            return version                                        # cached only after the version server accepts it
        _warn_limited("ps_junk", f"[VERSION] Play Store returned {version!r} (not a game version) -> ignored, using fallbacks")
    except Exception as e:
        _warn_limited("ps_fail", f"[VERSION] Play Store lookup failed: {type(e).__name__}: {e}")
    return _version_state["app_version"]                          # last known good (None if we never had one)


async def _ask_version_server(version: str):
    """-> (cfg | None, note, network_error)"""
    url = (f"{VER_URL}?version={version}&lang=hi&device=android&channel=android"
           "&appstore=googleplay&region=ID&whitelist_version=1.3.0&whitelist_sp_version=1.0.0")
    try:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return None, f"{version}: {type(e).__name__}", True
    if not isinstance(data, dict):
        return None, f"{version}: bad answer", False
    if data.get("is_server_open") is False:
        return None, f"{version}: game server closed (maintenance)", False
    code = data.get("code")
    server_url = data.get("server_url")
    remote_version = data.get("remote_version")
    latest_release_version = data.get("latest_release_version")
    if code not in (0, "0", None) or not (server_url and remote_version and latest_release_version):
        return None, f"{version}: code={code}", False
    return (latest_release_version, remote_version, server_url), version, False


async def _candidate_versions() -> List[str]:
    """App versions to ask the version server, best first: APP_VERSION env, Play Store, last good, built-in fallbacks."""
    candidates: List[str] = []

    def add(v):
        v = str(v or "").strip()
        if v and _VER_RE.match(v) and v not in candidates:
            candidates.append(v)

    for v in _env_app_versions():
        add(v)
    add(await get_playstore_version())
    add(_version_state["app_version"])
    for v in DEFAULT_FALLBACK_VERSIONS:
        add(v)
    return candidates


async def _fetch_version_config():
    """(release_version, remote_version, server_url) or None. Tries: APP_VERSION env, Play Store, last good, fallbacks."""
    candidates = await _candidate_versions()
    notes: List[str] = []
    for v in candidates[:MAX_VERSION_CANDIDATES]:
        cfg, note, net_err = await _ask_version_server(v)
        if cfg:
            _version_state["app_version"] = v
            _version_state["app_time"] = time.time()
            release, remote, srv = cfg
            line = f"[VERSION] OK app={v} remote={remote} release={release} server={srv}"
            if line != _version_log["last"]:
                _version_log["last"] = line
                extra = f"  (rejected before: {' | '.join(notes)})" if notes else ""
                (print_info if notes else print_success)(line + extra)
            return cfg
        notes.append(note)
        if net_err:                                               # version server unreachable: other candidates will not help
            break
    _warn_limited("ver_fail", "[VERSION] version server gave no login server. Tried: " + " | ".join(notes or ["nothing"]))
    return None


async def version_config():
    """Shared by every login. It used to hit the Play Store + version server once PER login,
    which was the main reason logins got slow when many accounts were running."""
    now = time.time()
    cfg = _version_state["config"]
    if cfg and now - _version_state["config_time"] < VERSION_CACHE_TTL:
        return cfg
    async with _version_lock:
        cfg = _version_state["config"]
        if cfg and time.time() - _version_state["config_time"] < VERSION_CACHE_TTL:
            return cfg
        fresh = await _fetch_version_config()
        if fresh:
            _version_state["config"] = fresh
            _version_state["config_time"] = time.time()
            return fresh
        return cfg       # stale answer is better than failing every login


# ==================== NEW MAJOR LOGIN SETTINGS (2026-10) ====================
LOGIN_CODE_VERSION = "2026-10-18-web"
# LOGIN_MODE (Railway variable, optional):
#   super  (DEFAULT)  = ONLY the login of Superfast.zip, never falls back to the older logins (web / lean / full / legacy
#                       send other device + channel values, e.g. channel 3rd_party).
#   auto              super  = login of Superfast.zip (its version query, token, MajorLogin + GetLoginData bodies
#                              and headers, exactly as in Superfast app.py)                  <- ALWAYS tried first
#                     web    = login of Web.zip (OB55 payload + vercfg_sign / cdn / client_ip from the version
#                              server + new headers + GetLoginData with the jwt)             <- fallback
#                     lean   = small LoginReq payload + new headers
#                     full   = old big payload + new headers
#                     legacy = exactly the old way (old payload + old headers)
#                     after 'super' the one that worked last time is tried first
#   super | web | lean | full | legacy   = use only that one
LOGIN_MODE = os.environ.get("LOGIN_MODE", "super").strip().lower()
if LOGIN_MODE not in ("auto", "super", "web", "lean", "full", "legacy"):
    LOGIN_MODE = "super"
LOGIN_MODE_ORDER = ("super", "web", "lean", "full", "legacy")
PLAY_VER = (os.environ.get("PLAY_VER", "1.132.1") or "1.132.1").strip()      # sent as 'PlAy_VeR' header
LOGIN_X_GA_SV = "1789534056"                                                  # new-style 'X-Ga-Sv'
GUEST_TOKEN_URLS = (
    "https://100067.connect.garena.com/oauth/guest/token/grant",              # Superfast.zip (first)
    "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant",      # fallback
)
GUEST_TOKEN_TRIES = 4                                                         # Superfast.zip: 4 tries, 0.7 s apart
GUEST_TOKEN_UA = "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B)"             # Superfast.zip user agent
TOKEN_INSPECT_URLS = (
    "https://100067.connect.garena.com/oauth/token/inspect",
    "https://ffmconnect.live.gop.garenanow.com/oauth/token/inspect",
)
LEGACY_GETLOGIN_HOST = "clientbp.ppmainecoonghj.com"                          # Host header the old code always forced
_guest_url_pref: Dict[str, Optional[str]] = {"url": None}

# ---------------- settings of the 'web' login (taken from Web.zip / app.py) ----------------
LOGIN_REGION = (os.environ.get("LOGIN_REGION", "ID") or "ID").strip().upper()     # 'region' of the version server + payload field 26
LOGIN_SERVER_URL_ENV = (os.environ.get("LOGIN_SERVER_URL", "") or "").strip()      # optional: force the MajorLogin server
WEB_LOGIN_FALLBACK_URLS = ("https://loginbp.ggpolarbear.com/",)                   # used by Web.zip, tried after the version server's own address
WEB_CLIENT_URLS = {                                                                # GetLoginData servers of Web.zip (fallback after the MajorLogin answer's url)
    "IND": "https://client.ind.freefiremobile.com/",
    "AMERICA": "https://client.us.freefiremobile.com/",
    "OTHERS": "https://clientbp.ggpolarbear.com/",
}
WEB_REGION_GROUP = {"IND": "IND", "BR": "AMERICA", "US": "AMERICA", "NA": "AMERICA", "SAC": "AMERICA"}
REGION_LANG = {"ME": "ar", "IND": "hi", "ID": "id", "VN": "vi", "TH": "th", "BD": "bn", "PK": "ur", "TW": "zh",
               "EUROPE": "fr", "RU": "ru", "NA": "na", "SAC": "es", "BR": "pt", "SG": "ms", "US": "us"}
WEB_SESSION_HASH = "1ac4b80ecf0478a44203bf8fac6120f5"
WEB_DEVICE_ID = "Google|c15d0cbb-0f10-4ad4-a960-739ca3891a93"                    # only used when an account has no device id of its own
WEB_SIG_BASE64 = ("KqsHTwFsNrz71lqUGAjS+qIySSj2KOGy6RrHPlpy6KkAVaY7JOEtuPh7yc+7ruAa8IoJQdj5KVD"
                  "vnuda+C89owjNtP1VCdBI3Q591QXRXy8qF6s/")
WEB_DEFAULT_CDN = ("https://dl.gmc.freefiremobile.com/live/ABHotUpdates/",
                   "https://core-gmc.freefiremobile.com/live/ABHotUpdates/")
WEB_MAJOR_TRIES = 2                                                                # MajorLogin tries per server when the network / server hiccups
_login_base_pref: Dict[str, Optional[str]] = {"url": None}                        # MajorLogin server that worked last time
_web_ver_lock = asyncio.Lock()
_login_seen: set = set()

# ---------------- settings of the 'super' login (taken from Superfast.zip / app.py) ----------------
SUPER_REGION = (os.environ.get("SUPER_REGION", "BD") or "BD").strip().upper()      # 'region' of the version server + payload field 26 (Superfast uses BD)
SUPER_LANG_CODE = "en"                                                             # payload field 21 (Superfast: "en")
SUPER_MAJOR_TRIES = 2                                                              # MajorLogin tries per server when the network / server hiccups
_super_ver_lock = asyncio.Lock()


def _norm_base(u) -> str:
    u = str(u or "").strip()
    return u if u.startswith(("http://", "https://")) else ""


def _web_ver_url(version: str) -> str:
    """Same query as Web.zip's fetch_ver_config(): channel 3rd_party, whitelist 1.7.0 + device data."""
    q = (("version", version), ("lang", REGION_LANG.get(LOGIN_REGION, "en")), ("device", "android"),
         ("channel", "3rd_party"), ("appstore", "thirdparty"), ("region", LOGIN_REGION),
         ("whitelist_version", "1.7.0"), ("whitelist_sp_version", "1.0.0"),
         ("device_name", "vivo V2538"), ("device_CPU", "ARM64 FP ASIMD AES"),
         ("device_GPU", "Adreno (TM) 722"), ("device_mem", "7282"))
    return VER_URL + "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in q)


async def _ask_web_ver(version: str):
    """-> (cfg dict | None, note, network_error, hint_version)"""
    try:
        response = await client.get(_web_ver_url(version))
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return None, f"{version}: {type(e).__name__}", True, None
    if not isinstance(data, dict):
        return None, f"{version}: bad answer", False, None
    if data.get("is_server_open") is False:
        return None, f"{version}: game server closed (maintenance)", False, None
    code = data.get("code")
    if code not in (0, "0"):
        remote = str(data.get("remote_version") or "").strip()
        return None, f"{version}: code={code}", False, (remote if _VER_RE.match(remote) else None)
    missing = [k for k in ("remote_version", "latest_release_version", "vercfg_sign") if not data.get(k)]
    if missing:
        return None, f"{version}: answer has no {','.join(missing)}", False, None
    return data, version, False, None


async def _fetch_web_config():
    """Full ver.php answer for the 'web' login (remote_version, latest_release_version, vercfg_sign, cdn_url,
    abhotupdate_cdn_url, client_ip ...) or None. When the server names another version (code != 0 + remote_version)
    that version is asked next - exactly like Web.zip does."""
    queue = list((await _candidate_versions())[:MAX_VERSION_CANDIDATES])
    tried: List[str] = []
    notes: List[str] = []
    while queue and len(tried) < MAX_VERSION_CANDIDATES + 1:
        v = queue.pop(0)
        if v in tried:
            continue
        tried.append(v)
        cfg, note, net_err, hint = await _ask_web_ver(v)
        if cfg:
            _version_state["app_version"] = v
            _version_state["app_time"] = time.time()
            line = (f"[WEBVER] OK app={v} remote={cfg.get('remote_version')} release={cfg.get('latest_release_version')} "
                    f"vercfg={cfg.get('vercfg_sign')} server={cfg.get('server_url')} region={LOGIN_REGION}")
            if line != _version_log.get("web"):
                _version_log["web"] = line
                print_success(line)
            return cfg
        notes.append(note)
        if net_err:
            break
        if hint and hint not in tried and hint not in queue:
            queue.insert(0, hint)                                 # the server told us which version it wants
    _warn_limited("webver_fail", "[WEBVER] version server gave no complete config. Tried: " + " | ".join(notes or ["nothing"]))
    return None


async def web_version_config():
    """Cached (VERSION_CACHE_TTL) full config for the 'web' login. A stale answer is better than none."""
    now = time.time()
    cfg = _version_state.get("web")
    if cfg and now - _version_state.get("web_time", 0.0) < VERSION_CACHE_TTL:
        return cfg
    async with _web_ver_lock:
        cfg = _version_state.get("web")
        if cfg and time.time() - _version_state.get("web_time", 0.0) < VERSION_CACHE_TTL:
            return cfg
        fresh = await _fetch_web_config()
        if fresh:
            _version_state["web"] = fresh
            _version_state["web_time"] = time.time()
            return fresh
        return cfg


def _guest_headers(url: str) -> Dict[str, str]:
    if "garenanow.com" in url:
        return {
            "User-Agent": headers["User-Agent"],
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded",
        }
    return {
        "Host": "100067.connect.garena.com",
        "User-Agent": GUEST_TOKEN_UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }


async def get_access_token(uid, password):
    data = {
        "uid": uid,
        "password": password,
        "response_type": "token",
        "client_type": "2",
        "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
        "client_id": "100067"
    }
    urls = list(GUEST_TOKEN_URLS)
    pref = _guest_url_pref["url"]
    if pref in urls:                                  # the address that worked last time goes first
        urls.remove(pref)
        urls.insert(0, pref)
    for attempt in range(GUEST_TOKEN_TRIES):
        for url in urls:
            try:
                response = await client.post(url, headers=_guest_headers(url), data=data)
                if response.status_code == 200:
                    response_data = response.json()
                    open_id = response_data.get("open_id")
                    access_token = response_data.get("access_token")
                    platform = response_data.get("platform", 4)
                    if open_id and access_token:
                        _guest_url_pref["url"] = url
                        return open_id, access_token, platform
                elif response.status_code == 429:
                    await asyncio.sleep(1)
            except Exception:
                pass
        await asyncio.sleep(0.7)
    return None


async def parse_results(parsed_results):
    result_dict = {}
    for result in parsed_results:
        field_data = {"wire_type": result.wire_type}
        if result.wire_type == "varint":
            field_data["data"] = result.data
        elif result.wire_type == "string":
            field_data["data"] = result.data
        elif result.wire_type == "bytes":
            field_data["data"] = result.data
        elif result.wire_type == "length_delimited":
            field_data["data"] = await parse_results(result.data.results)
        result_dict[result.field] = field_data
    return result_dict


async def decode_protobuf(data):
    parsed_results = Parser().parse(data)
    parsed_results_dict = await parse_results(parsed_results)
    return json.dumps(parsed_results_dict)


# ==================== MANUAL PROTOBUF ====================
def encode_varint(n):
    if n < 0:
        return b''
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def build_proto(fields):
    parts = []
    for k, v in fields.items():
        if isinstance(v, dict):
            nested = build_proto(v)
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(nested)) + nested)
        elif isinstance(v, int):
            parts.append(encode_varint((k << 3) | 0) + encode_varint(v))
        elif isinstance(v, (str, bytes)):
            ev = v.encode() if isinstance(v, str) else v
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(ev)) + ev)
    return b''.join(parts)


def encrypt_api(plain_hex):
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(
        pad(bytes.fromhex(plain_hex), AES.block_size)
    ).hex()


def encrypt_api_bytes(plain_bytes):
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(
        pad(plain_bytes, AES.block_size)
    )


# ==================== build_majorlogin_payload ====================
async def build_majorlogin_payload(open_id, access_token, platform, client_version, device_info):
    try:
        payload_dict = {
            3: str(datetime.now())[:-7], 4: "free fire", 5: "1",
            7: str(client_version) if client_version else "1.132.8",
            8: str(device_info.get("system_software", "Android OS 15 / API-35 (AP3A.240905.015.A2/OS2.0.202.0.VGTIDXM)")),
            9: "Handheld", 10: "Telkomsel", 11: "CarrierDataNetwork",
            12: str(device_info.get("screen_width", 1639)),
            13: str(device_info.get("screen_height", 720)),
            14: str(device_info.get("screen_dpi", 320)),
            15: str(device_info.get("processor_details", "ARM64 FP ASIMD AES | 2000 | 8")),
            16: str(device_info.get("memory", 5653)),
            17: str(device_info.get("gpu_renderer", "Mali-G52 MC2")),
            18: "OpenGL ES 3.2 v1.r49p1-03bet0.19498e0ae1d5dac223383c39a2e58f04",
            19: str(device_info.get("unique_device_id", f"Google|{uuid.uuid4()}")),
            20: str(device_info.get("client_ip", "182.5.135.225")),
            21: "en", 22: str(open_id), 23: str(platform), 24: "Handheld",
            25: str(device_info.get("model", "Xiaomi 2409BRN2CY")), 26: "ID",
            29: str(access_token), 30: "1", 41: "Telkomsel", 42: "4G",
            57: "7428b253defc164018c604a1ebbfebdf", 60: "109278", 61: "55402",
            62: "868", 64: "55927", 65: "109278", 66: "55927", 67: "109278", 73: "2",
            74: "/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/lib/arm64",
            76: "1",
            77: "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/base.apk",
            78: "3", 79: "2", 81: "64", 83: "2019121229", 85: "3", 86: "OpenGLES2",
            87: "8191", 88: str(platform), 90: "Cirebon", 91: "JB", 92: "14466", 93: "android",
            94: "KqsHTyb7yHjsAiyNWUUZa0DSOD6U0AhDQ76smAo1WeHHHpSmJ8KMRyRrxb2ttgJqO9YOqO/K6AORAvDMAX/qBS8+JahIkrM7kHfzlfULbgRAoZ9r",
            95: "111207", 96: "{\"cur_rate\":[60,90,120],\"support_etc2\":true}", 97: "1",
            98: "1", 99: str(platform), 100: str(platform), 102: "D]C\x15\x05T\x0eU3",
            103: "1", 104: "52837", 105: "1",
            106: "https://dl.gmc.freefiremobile.com/live/ABHotUpdates/|https://core-gmc.freefiremobile.com/live/ABHotUpdates/|211c933168f55902c7dfbfd8c4e2957d",
            107: "1.3fb8adbf45f44ce9",
        }
        raw_proto = build_proto(payload_dict)
        return encrypt_api_bytes(raw_proto)
    except Exception as e:
        print_error(f"Error building MajorLogin payload: {e}")
        traceback.print_exc()
        return None


# ==================== MAJORLOGIN (new lean LoginReq + legacy) ====================
def build_lean_payload(open_id, access_token, platform):
    """New MajorLogin body (LoginReq): open_id(22) open_id_type(23) login_token(29) orign_platform_type(99), AES-CBC."""
    plat = str(platform or 4)
    raw = build_proto({22: str(open_id), 23: plat, 29: str(access_token), 99: plat})
    return encrypt_api_bytes(raw)


def build_web_payload(open_id, access_token, platform, cfg, device_info=None, jwt=None):
    """Login body of Web.zip (build_major_login / build_get_login_data), OB55 values.
    cfg = full answer of the version server (remote_version, vercfg_sign, cdn_url, abhotupdate_cdn_url, client_ip).
    jwt given -> GetLoginData body (extra field 108), else MajorLogin body. AES-CBC encrypted."""
    try:
        plat = int(platform)
    except Exception:
        plat = 4
    device_info = device_info or {}
    cfg = cfg or {}
    cdn_url = cfg.get("cdn_url") or WEB_DEFAULT_CDN[0]
    abhot_cdn = cfg.get("abhotupdate_cdn_url") or WEB_DEFAULT_CDN[1]
    client_ip = str(cfg.get("client_ip") or device_info.get("client_ip") or "182.5.135.225")
    cdn_hash = hashlib.md5(f"{open_id}{time.time_ns()}".encode()).hexdigest()
    fields = {
        3: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        4: "free fire", 5: 1,
        7: str(cfg.get("remote_version") or ""),
        8: "Android OS 16 / API-36 (BP2A.250605.031.A3_V000L1/compiler260320183556)",
        9: "Handheld", 11: "CarrierDataNetwork",
        12: 1637, 13: 750, 14: "440",
        15: "ARM64 FP ASIMD AES | 5606 | 8", 16: 7282,
        17: "Adreno (TM) 722",
        18: "OpenGL ES 3.2 V@0800.69.2 (GIT@4659378004, I7f7aee143c, 1773055375) (Date:03/09/26)",
        19: str(device_info.get("unique_device_id") or WEB_DEVICE_ID),
        20: client_ip, 21: REGION_LANG.get(LOGIN_REGION, "en"),
        22: str(open_id), 23: str(plat),
        24: "Handheld", 25: "vivo V2538", 26: LOGIN_REGION,
        29: str(access_token), 30: 1,
        42: "4G", 57: WEB_SESSION_HASH,
        60: 223554, 61: 100654, 62: 4465,
        64: 100782, 65: 223554, 66: 100782,
        67: 223554, 70: 4, 73: 1,
        74: "/data/app/~~Hl4pq6GoyauPV457pS4N8g==/com.dts.freefireth-mRT8MtqxsyyrZkxcQBSRAA==/lib/arm64",
        76: 1,
        77: "066a589fa3f5658377634fe7b1d88556|/data/app/~~Hl4pq6GoyauPV457pS4N8g==/com.dts.freefireth-mRT8MtqxsyyrZkxcQBSRAA==/base.apk",
        78: 6, 79: 2, 81: "64", 83: "2019121227",
        85: 3, 86: "OpenGLES2", 87: 8191,
        88: plat, 92: 40843,
        93: "3rd_party", 94: WEB_SIG_BASE64,
        95: 111207,
        96: '{"cur_rate":[60,90,120],"support_etc2":false}',
        97: 1, 98: 1,
        99: str(plat), 100: str(plat),
        102: "42531243535e0e0531", 104: 84750, 105: 1,
        106: f"{cdn_url}|{abhot_cdn}|{cdn_hash}",
        107: str(cfg.get("vercfg_sign") or ""),
    }
    if jwt:
        fields[108] = str(jwt)
    return encrypt_api_bytes(build_proto(fields))


def _web_login_bases(cfg, server_url) -> List[str]:
    """MajorLogin servers for the 'web' login, best first (forced env, last good, version server, Web.zip's own)."""
    out: List[str] = []
    for u in (LOGIN_SERVER_URL_ENV, _login_base_pref["url"], (cfg or {}).get("server_url"), server_url, *WEB_LOGIN_FALLBACK_URLS):
        u = _norm_base(u)
        if u and u.rstrip("/") not in [x.rstrip("/") for x in out]:
            out.append(u)
    return out


def _web_client_bases(res_url, region) -> List[str]:
    """GetLoginData servers: the address in the MajorLogin answer first, then Web.zip's client url of the region."""
    reg = str(region or LOGIN_REGION).upper()
    group = WEB_REGION_GROUP.get(reg, "OTHERS")
    out: List[str] = []
    for u in (res_url, WEB_CLIENT_URLS.get(group)):
        u = _norm_base(u)
        if u and u.rstrip("/") not in [x.rstrip("/") for x in out]:
            out.append(u)
    return out


def _login_headers(release_version, style: str, token: Optional[str] = None) -> Dict[str, str]:
    """style 'legacy' = headers exactly as before, 'new' = headers of the lean login,
    'web' = headers of Web.zip (X-GA-SV = current time),
    'super' = headers of Superfast.zip (MajorLogin: 'Bearer' + the full UnityWebRequest user agent,
              GetLoginData: plain 'UnityPlayer/2018.4.12f1' user agent)."""
    if style == "legacy":
        h = headers.copy()
        h["ReleaseVersion"] = str(release_version)
    elif style == "super":
        h = {
            "Accept": "*/*",
            "Authorization": "Bearer",             # Superfast sends 'Bearer ' (aiohttp); httpx refuses a trailing space ("Illegal header value")
            "Content-Type": "application/x-www-form-urlencoded",
            "ReleaseVersion": str(release_version),
            "User-Agent": ("UnityPlayer/2018.4.12f1" if token
                           else "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"),
            "X-GA": "v1 1",
            "X-GA-SV": str(int(time.time())),
            "X-Unity-Version": "2018.4.12f1",
        }
    elif style == "web":
        h = {
            "User-Agent": headers["User-Agent"],
            "Accept": "*/*",
            "Accept-Encoding": "deflate, gzip",
            "Authorization": "Bearer",
            "X-GA": "v1 1",
            "X-GA-SV": str(int(time.time())),
            "ReleaseVersion": str(release_version),
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Unity-Version": "2018.4.12f1",
        }
    else:
        h = {
            "User-Agent": headers["User-Agent"],
            "Accept": "*/*",
            "Accept-Encoding": "deflate, gzip",
            "X-Ga-Sv": LOGIN_X_GA_SV,
            "Authorization": "Bearer",
            "X-Ga": "v1 1",
            "Releaseversion": str(release_version),
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Unity-Version": "2018.4.12f1",
            "PlAy_VeR": PLAY_VER,
            "Ob_VeR": str(release_version),
        }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _pb_varint(buf, i):
    n = shift = 0
    while True:
        b = buf[i]                                   # IndexError = cut-off data -> caught by the caller
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _pb_fields(buf) -> Dict[int, list]:
    """Strict protobuf wire reader: {field_number: [values]}  (varint -> int, everything else -> bytes)."""
    out: Dict[int, list] = {}
    i, n = 0, len(buf)
    while i < n:
        tag, i = _pb_varint(buf, i)
        fno, wt = tag >> 3, tag & 7
        if fno == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = _pb_varint(buf, i)
        elif wt == 2:
            ln, i = _pb_varint(buf, i)
            if i + ln > n:
                raise ValueError("cut off")
            v = bytes(buf[i:i + ln])
            i += ln
        elif wt == 1:
            if i + 8 > n:
                raise ValueError("cut off")
            v = bytes(buf[i:i + 8])
            i += 8
        elif wt == 5:
            if i + 4 > n:
                raise ValueError("cut off")
            v = bytes(buf[i:i + 4])
            i += 4
        else:
            raise ValueError("wire type")
        out.setdefault(fno, []).append(v)
    return out


class _LoginRes:
    """MajorLogin answer. Old (MajorLoginRes) and new (LoginRes) names share the same field numbers:
    1 account_id | 2 region | 8 token | 10 url (server_url) | 21 server_time | 22 aes_ak | 23 iv_i"""
    account_id = 0
    region = ""
    token = ""
    url = ""
    ttl = 0
    server_time = 0
    aes_ak = b""
    iv_i = b""
    field_ids: List[int] = []
    release_version = ""      # set by the 'super' login: the versions that login really used
    client_version = ""


def _jwt_region(token: str) -> str:
    """lock_region / noti_region inside the jwt (used when the answer has no region field)."""
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return str(data.get("lock_region") or data.get("noti_region") or "")
    except Exception:
        return ""


def _try_login_res(raw) -> Optional[_LoginRes]:
    try:
        f = _pb_fields(raw)
        acc = f.get(1, [None])[-1]               # protobuf rule: if a field repeats, the last value wins
        tok = f.get(8, [None])[-1]
        if not isinstance(acc, int) or acc <= 0 or not isinstance(tok, bytes) or not tok:
            return None

        def s(no):
            v = f.get(no, [None])[-1]
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""

        def i(no):
            v = f.get(no, [0])[-1]
            return v if isinstance(v, int) else 0

        def b(no):
            v = f.get(no, [None])[-1]
            return v if isinstance(v, bytes) else b""

        res = _LoginRes()
        res.account_id = acc
        res.token = tok.decode("utf-8", "replace")
        res.region = s(2) or s(3) or _jwt_region(res.token)          # lock_region, else noti_region, else from the jwt
        res.url = s(10)
        res.ttl = i(9)
        res.server_time = i(21)
        res.aes_ak = b(22)
        res.iv_i = b(23)
        if not (len(res.aes_ak) == 16 and len(res.iv_i) == 16):
            for kf, vf in ((24, 25), (6, 7)):             # Superfast.zip's fallback: other key / iv field pairs
                k, v = b(kf), b(vf)
                if len(k) == 16 and len(v) == 16:
                    res.aes_ak, res.iv_i = k, v
                    break
        res.field_ids = sorted(f.keys())
        return res
    except Exception:
        return None


def _scan_login_res(raw) -> Optional[_LoginRes]:
    res = _try_login_res(raw)
    if res:
        return res
    for i in range(1, min(len(raw), 256)):
        if raw[i] == 0x08:                           # tag of field 1 (account_id)
            res = _try_login_res(raw[i:])
            if res:
                return res
    return None


def _aes_decrypt_try(data) -> Optional[bytes]:
    try:
        if not data or len(data) % 16:
            return None
        out = AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(bytes(data))
        n = out[-1]
        if 1 <= n <= 16 and out.endswith(bytes([n]) * n):
            out = out[:-n]
        return out
    except Exception:
        return None


def parse_login_response(raw: bytes) -> Optional[_LoginRes]:
    """Reads the MajorLogin answer: as it is, with junk bytes in front, and (like Web.zip) after AES decryption,
    also with the first 64 bytes cut off."""
    res = _scan_login_res(raw)
    if res:
        return res
    for part in (raw, raw[64:]) if len(raw) > 64 else (raw,):
        dec = _aes_decrypt_try(part)
        if dec:
            res = _scan_login_res(dec)
            if res:
                return res
    return None


async def send_majorlogin(data, release_version, server_url, style="legacy", info=None):
    """info (optional dict) gets 'kind' on failure: net | http5xx (server busy) | http | parse."""
    tag = f"[MAJORLOGIN/{style}]"
    if info is None:
        info = {}
    url = ""
    try:
        url = f"{server_url}MajorLogin" if server_url.endswith('/') else f"{server_url}/MajorLogin"
        response = await client.post(url, headers=_login_headers(release_version, style), data=data)
    except Exception as e:
        info["kind"] = "net"
        print_error(f"{tag} connection error: {type(e).__name__}: {e} | url={url}")
        return None
    if response.status_code != 200:
        info["kind"] = "http5xx" if (response.status_code >= 500 or response.status_code == 429) else "http"
        info["status"] = response.status_code
        try:
            body = (response.text or "")[:80].replace("\n", " ")
        except Exception:
            body = ""
        print_error(f"{tag} server rejected payload: HTTP {response.status_code} {body!r} | url={url}")
        return None
    content = response.content
    res = parse_login_response(content)
    if res is None:
        info["kind"] = "parse"
        print_error(f"{tag} answer could not be read ({len(content)} bytes, starts {content[:12].hex()}) | url={url}")
        return None
    return res


_getlogin_seen_hosts = set()


async def send_getlogin(data, base_url, token, release_version, style="legacy"):
    try:
        base = str(base_url or "").rstrip("/")
        url = f"{base}/GetLoginData"
        url_host = urlparse(base).hostname or ""
    except Exception as e:
        _warn_limited("gl_badurl", f"[GETLOGIN] bad login url {base_url!r}: {e}")
        return None
    hosts: List[str] = []
    if style == "legacy":
        order = (LEGACY_GETLOGIN_HOST, url_host)
    elif style in ("web", "super"):
        order = (url_host,)                          # Web.zip / Superfast.zip: no forced Host header
    else:
        order = (url_host, LEGACY_GETLOGIN_HOST)
    for h in order:                                  # the old code always forced the legacy Host; the real host of the URL is the second try
        if h and h not in hosts:
            hosts.append(h)
    if url_host and url_host not in _getlogin_seen_hosts:
        _getlogin_seen_hosts.add(url_host)
        print_info(f"[GETLOGIN] login server host: {url_host}")
    tries: List[str] = []
    response = None
    for host in hosts:
        req_headers = _login_headers(release_version, style, token)
        req_headers["Host"] = host
        r = None
        net_err = None
        for _net_try in range(3):                    # network hiccup (ConnectTimeout ...): same request again, fresh headers
            try:
                if style in ("web", "super"):            # these styles send the current time as X-GA-SV
                    req_headers["X-GA-SV"] = str(int(time.time()))
                r = await client.post(url, headers=req_headers, data=data, timeout=httpx.Timeout(20.0, connect=12.0))
                net_err = None
                break
            except Exception as e:
                net_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(1.0 + _net_try)
        if net_err is not None or r is None:         # still a network problem: a second Host will not help
            tries.append(f"Host={host}: {net_err} (3 tries)")
            break
        if r.status_code == 200:
            response = r
            if host != hosts[0]:
                _warn_limited("gl_hostfix", f"[GETLOGIN] OK only with Host={host} ({hosts[0]} was refused)")
            break
        try:
            body = (r.text or "")[:80].replace("\n", " ")
        except Exception:
            body = ""
        tries.append(f"Host={host}: HTTP {r.status_code} {body!r}")
    if response is None:
        _warn_limited("gl_refused", f"[GETLOGIN] refused ({style}) -> {' ; '.join(tries) or 'no attempt'} | url={url}")
        return None
    try:
        response_content = response.content
        res_proto = thunderFF_pb2.GetLoginDataRes()
        parsed_successfully = False
        try:
            res_proto.ParseFromString(response_content)
            if res_proto.functional_addrs or res_proto.informational_addrs:
                parsed_successfully = True
        except Exception:
            pass
        if not parsed_successfully:
            for offset in range(min(128, len(response_content))):
                try:
                    candidate = thunderFF_pb2.GetLoginDataRes()
                    candidate.ParseFromString(response_content[offset:])
                    if candidate.functional_addrs or candidate.informational_addrs:
                        res_proto = candidate
                        break
                except Exception:
                    pass
        dict_res = {}
        try:
            parsed = Parser().parse(response_content.hex())
            dict_res = await parse_results(parsed)
        except Exception:
            pass
        return res_proto, dict_res
    except Exception as e:
        _warn_limited("gl_parse", f"[GETLOGIN] could not read the answer: {type(e).__name__}: {e}")
        return None


async def _login_classic(mode, open_id, access_token, platform, client_version, device_info, release_version, server_url):
    """lean / full / legacy: -> ((res, getlogin_result, payload, style) | None, why)"""
    style = "legacy" if mode == "legacy" else "new"
    if mode == "lean":
        payload = build_lean_payload(open_id, access_token, platform)
    else:
        payload = await build_majorlogin_payload(open_id, access_token, platform, client_version, device_info)
    if not payload:
        return None, f"{mode}: could not build payload"
    res = await send_majorlogin(payload, release_version, server_url, style)
    if res is None:
        return None, f"{mode}: MajorLogin refused"
    if not (res.aes_ak and res.iv_i and res.server_time):
        return None, f"{mode}: answer has no aes_ak/iv_i/server_time (fields in answer: {res.field_ids})"
    gl = await send_getlogin(payload, res.url, res.token, release_version, style)
    if gl is None:
        return None, f"{mode}: GetLoginData refused"
    return (res, gl, payload, style), ""


async def _login_web(open_id, access_token, platform, device_info, server_url):
    """The login of Web.zip: -> ((res, getlogin_result, getlogin_payload, 'web') | None, why)"""
    cfg = await web_version_config()
    if not cfg:
        return None, "web: no complete config from the version server (see [WEBVER] line)"
    release = str(cfg.get("latest_release_version"))
    payload = build_web_payload(open_id, access_token, platform, cfg, device_info)
    bases = _web_login_bases(cfg, server_url)
    res = None
    used_base = ""
    for base in bases:
        for attempt in range(WEB_MAJOR_TRIES):
            info: Dict[str, Any] = {}
            res = await send_majorlogin(payload, release, base, "web", info)
            if res is not None or info.get("kind") not in ("net", "http5xx"):
                break                                # only a network hiccup / busy server is worth the same address again
            await asyncio.sleep(1.5 * (attempt + 1))
        if res is not None:
            used_base = base
            _login_base_pref["url"] = base
            break
    if res is None:
        return None, f"web: MajorLogin refused (tried {', '.join(bases) or 'no server'})"
    if not (res.aes_ak and res.iv_i and res.server_time):
        return None, f"web: answer has no aes_ak/iv_i/server_time (fields in answer: {res.field_ids})"
    gl_payload = build_web_payload(open_id, access_token, platform, cfg, device_info, jwt=res.token)
    gl = None
    gl_base = ""
    for base in _web_client_bases(res.url, res.region):
        gl = await send_getlogin(gl_payload, base, res.token, release, "web")
        if gl is not None:
            gl_base = base
            break
    if gl is None:
        return None, "web: GetLoginData refused"
    if gl_base and gl_base.rstrip("/") != str(res.url or "").rstrip("/"):
        res.url = gl_base                            # EXP refresh asks GetLoginData at res.url -> use the address that worked
    seen = (used_base, gl_base)
    if seen not in _login_seen:
        _login_seen.add(seen)
        print_info(f"[LOGIN] web login servers: MajorLogin={used_base} | GetLoginData={gl_base}")
    return (res, gl, gl_payload, "web"), ""


# ==================== 'SUPER' LOGIN (taken from Superfast.zip / app.py) ====================
def _super_ver_url(version: str) -> str:
    """Same query as Superfast's version_config(): channel android / appstore googleplay, whitelist 1.3.0."""
    return (f"{VER_URL}?version={version}&lang={REGION_LANG.get(SUPER_REGION, 'en')}&device=android&channel=android"
            f"&appstore=googleplay&region={SUPER_REGION}&whitelist_version=1.3.0&whitelist_sp_version=1.0.0")


async def _ask_super_ver(version: str):
    """-> (cfg dict | None, note, network_error)"""
    try:
        response = await client.get(_super_ver_url(version))
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return None, f"{version}: {type(e).__name__}", True
    if not isinstance(data, dict):
        return None, f"{version}: bad answer", False
    if data.get("is_server_open") is False:
        return None, f"{version}: game server closed (maintenance)", False
    code = data.get("code")
    if code not in (0, "0", None) or not (data.get("server_url") and data.get("remote_version")
                                          and data.get("latest_release_version")):
        return None, f"{version}: code={code}", False
    return data, version, False


async def _fetch_super_config():
    """ver.php answer (server_url, remote_version, latest_release_version ...) for the 'super' login or None.
    App versions are tried in the same order as for every other login: APP_VERSION env, Play Store, last good, fallbacks."""
    notes: List[str] = []
    for v in (await _candidate_versions())[:MAX_VERSION_CANDIDATES]:
        cfg, note, net_err = await _ask_super_ver(v)
        if cfg:
            _version_state["app_version"] = v
            _version_state["app_time"] = time.time()
            line = (f"[SUPERVER] OK app={v} remote={cfg.get('remote_version')} release={cfg.get('latest_release_version')} "
                    f"server={cfg.get('server_url')} region={SUPER_REGION}")
            if line != _version_log.get("super"):
                _version_log["super"] = line
                print_success(line)
            return cfg
        notes.append(note)
        if net_err:
            break
    _warn_limited("superver_fail", "[SUPERVER] version server gave no login server. Tried: " + " | ".join(notes or ["nothing"]))
    return None


async def super_version_config():
    """Cached (VERSION_CACHE_TTL) config for the 'super' login. A stale answer is better than none."""
    now = time.time()
    cfg = _version_state.get("super")
    if cfg and now - _version_state.get("super_time", 0.0) < VERSION_CACHE_TTL:
        return cfg
    async with _super_ver_lock:
        cfg = _version_state.get("super")
        if cfg and time.time() - _version_state.get("super_time", 0.0) < VERSION_CACHE_TTL:
            return cfg
        fresh = await _fetch_super_config()
        if fresh:
            _version_state["super"] = fresh
            _version_state["super_time"] = time.time()
            return fresh
        return cfg


def build_super_payload(open_id, access_token, platform, version, lang_code=SUPER_LANG_CODE):
    """Login bodies of Superfast.zip (get_payload) -> (MajorLogin body, GetLoginData body), both AES-CBC encrypted.
    Every value is copied from Superfast's app.py. Both bodies share one fresh device id; the GetLoginData body has the
    extra fields 90 / 91 / 103 and field 99 = "0"."""
    try:
        plat = int(platform)
    except Exception:
        plat = 4
    device_id = f"Google|{uuid.uuid4()}"
    fields = {
        3: datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 4: "free fire", 5: 1,
        7: f"{version}",
        8: "Android OS 14 / API-34 (UP1A.231005.007/E135FXXSEEZE2)",
        9: "Handheld", 11: "CarrierDataNetwork",
        12: 1672, 13: 750, 14: "338",
        15: "ARMv7 VFPv3 NEON | 2002 | 8",
        16: 3702, 17: "Mali-G52",
        18: "OpenGL ES 3.2 v1.r38p1-01bet0-mbs2v41_0.6d20ec041e51b2f2d25dfc265586ebe8",
        19: device_id, 20: "104.28.197.151", 21: lang_code,
        22: f"{open_id}", 23: f"{plat}", 24: "Handheld",
        25: "samsung SM-E135F", 26: SUPER_REGION,
        29: f"{access_token}", 30: 1, 42: "Cellular",
        57: "7428b253defc164018c604a1ebbfebdf",
        60: 52037, 61: 5355, 62: 3723, 63: 3,
        64: 5483, 65: 52037, 66: 5483, 67: 52037,
        73: 2,
        74: "/data/app/~~yACsV8QOk9OexgajWQP30A==/com.dts.freefireth-Bi_wPVrrhYM2nYncc29q7Q==/lib/arm",
        76: 1,
        77: "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~yACsV8QOk9OexgajWQP30A==/com.dts.freefireth-Bi_wPVrrhYM2nYncc29q7Q==/base.apk",
        78: 3, 79: 1, 81: "32", 83: "2019121229",
        86: "OpenGLES2", 87: 8191, 88: plat,
        92: 16091, 93: "android",
        94: "KqsHT3Yeqo3RLRl8efNiL3aL/1SHwlSrtK0DwNAqdkNBQz70TeVy+icaIwuR3UK4b65Uf3IG5xBFEv0uu+jQZI+rOYCmNCsXjJT4QcZaYgoaaUGG",
        95: 111107,
        96: "{\"cur_rate\":null,\"support_etc2\":false}",
        97: 1, 98: 1,
        99: f"{plat}", 100: f"{plat}",
        102: "4503454457550c0766",
        104: 53792, 105: 1,
        106: "https://dl-bs.ggpolarbear.com/live/ABHotUpdates/|https://core-bs.ggpolarbear.com/live/ABHotUpdates/|6b2078db9d22dd98f8e9386a39af8462",
        107: "c8e41b7a93f02d56e1a94c7b8203f5d1",
    }
    getlogin = dict(fields)
    getlogin[90] = "Khulna"
    getlogin[91] = "D"
    getlogin[99] = "0"
    getlogin[103] = 1
    return encrypt_api_bytes(build_proto(fields)), encrypt_api_bytes(build_proto(getlogin))


async def _login_super(open_id, access_token, platform):
    """The login of Superfast.zip: -> ((res, getlogin_result, getlogin_payload, 'super') | None, why)"""
    cfg = await super_version_config()
    if not cfg:
        return None, "super: no config from the version server (see [SUPERVER] line)"
    release = str(cfg.get("latest_release_version"))
    remote = str(cfg.get("remote_version"))
    major_payload, gl_payload = build_super_payload(open_id, access_token, platform, remote)
    bases: List[str] = []
    for u in (LOGIN_SERVER_URL_ENV, cfg.get("server_url"), *WEB_LOGIN_FALLBACK_URLS):
        u = _norm_base(u)
        if u and u.rstrip("/") not in [x.rstrip("/") for x in bases]:
            bases.append(u)
    res = None
    used_base = ""
    for base in bases:
        for attempt in range(SUPER_MAJOR_TRIES):
            info: Dict[str, Any] = {}
            res = await send_majorlogin(major_payload, release, base, "super", info)
            if res is not None or info.get("kind") not in ("net", "http5xx"):
                break                                # only a network hiccup / busy server is worth the same address again
            await asyncio.sleep(1.5 * (attempt + 1))
        if res is not None:
            used_base = base
            break
    if res is None:
        return None, f"super: MajorLogin refused (tried {', '.join(bases) or 'no server'})"
    if not (res.aes_ak and res.iv_i and res.server_time):
        return None, f"super: answer has no aes_ak/iv_i/server_time (fields in answer: {res.field_ids})"
    gl = None
    gl_base = ""
    for base in _web_client_bases(res.url, res.region):
        gl = await send_getlogin(gl_payload, base, res.token, release, "super")
        if gl is not None:
            gl_base = base
            break
    if gl is None:
        return None, "super: GetLoginData refused"
    if gl_base and gl_base.rstrip("/") != str(res.url or "").rstrip("/"):
        res.url = gl_base                            # EXP refresh asks GetLoginData at res.url -> use the address that worked
    res.release_version = release                    # the rest of the bot (EXP refresh, match search) uses the versions this login used
    res.client_version = remote
    seen = ("super", used_base, gl_base)
    if seen not in _login_seen:
        _login_seen.add(seen)
        print_info(f"[LOGIN] super login servers: MajorLogin={used_base} | GetLoginData={gl_base}")
    return (res, gl, gl_payload, "super"), ""


_login_mode_state: Dict[str, Optional[str]] = {"mode": None}


async def login_sequence(open_id, access_token, platform, client_version, device_info,
                         release_version, server_url, tag="LOGIN"):
    """MajorLogin + GetLoginData as one unit, over the login modes (see LOGIN_MODE).
    -> (majorlogin_response, getlogin_result, payload, style)  or None"""
    if LOGIN_MODE == "auto":
        order = list(LOGIN_MODE_ORDER)               # 'super' stays first; among the fallbacks what worked last goes first
        last = _login_mode_state["mode"]
        if last in order and last != "super":
            order.remove(last)
            order.insert(1, last)
    else:
        order = [LOGIN_MODE]
    why: List[str] = []
    for mode in order:
        if mode == "super":
            out, reason = await _login_super(open_id, access_token, platform)
        elif mode == "web":
            out, reason = await _login_web(open_id, access_token, platform, device_info, server_url)
        else:
            out, reason = await _login_classic(mode, open_id, access_token, platform, client_version,
                                               device_info, release_version, server_url)
        if out is None:
            why.append(reason)
            continue
        res, gl, payload, style = out
        gl_proto, gl_dict = gl
        if not (gl_proto.functional_addrs or get_proto_field(gl_dict, 14)):
            why.append(f"{mode}: GetLoginData has no gateway address")
            continue
        if _login_mode_state["mode"] != mode:
            print_success(f"[LOGIN] login mode '{mode}' works -> used first from now on")
        _login_mode_state["mode"] = mode
        return res, gl, payload, style
    print_error(f"[{tag}] every login mode failed: " + " | ".join(why))
    return None


def _packet_info(res, n) -> str:
    """What a 'non-match big packet' contained (helps to see a cut-off packet or a new layout)."""
    try:
        top = sorted(res.keys(), key=int)
        f5 = (res.get("5") or {}).get("data")
        f5k = sorted(f5.keys(), key=int) if isinstance(f5, dict) else type(f5).__name__
        return f"packet={n}B top_fields={top} field5={f5k}"
    except Exception as e:
        return f"packet info unavailable ({type(e).__name__})"


# GATEWAY_STYLE (Railway variable, optional) = the first bytes of the TCP gateway packets (login packet, chat packet,
# keep-alive, StartMatch region byte).
#   super (DEFAULT) = the values of Superfast.zip (home region BD)
#   exe             = what this bot used before the Superfast update (home region ID)
# If accounts log in but never reach the gateway, set GATEWAY_STYLE=exe.
GATEWAY_STYLE = (os.environ.get("GATEWAY_STYLE", "super") or "super").strip().lower()
if GATEWAY_STYLE not in ("exe", "super"):
    GATEWAY_STYLE = "super"


def _gateway_codes(region):
    """-> (OnLine prefix, ChaT prefix, keep-alive) for the account's region."""
    home = "BD" if GATEWAY_STYLE == "super" else "ID"
    reg = str(region).upper() if region else home
    n = '19' if reg == home else ('14' if reg == 'IND' else '15')
    if GATEWAY_STYLE == "super":
        return f"72{n}", f"81{n}", f"02{n}"
    return f"71{n}", f"92{n}", f"02{n}"


async def build_tcp_startup_packet(account_id, token, server_time, key, iv, region="ID", typ='OnLine'):
    uid_hex = f"{int(account_id):016x}"
    timestamp_hex = f"{int(server_time):08x}"
    encode_token = token.encode()
    encrypted_packet = (await aes_encrypt(encode_token, key, iv)).hex()
    encrypted_packet_length = f"{len(encrypted_packet) // 2:08x}"
    online_prefix, chat_prefix, _ka = _gateway_codes(region)
    if typ == 'OnLine':
        return f"{online_prefix}{uid_hex}{timestamp_hex}00000000{encrypted_packet_length}{encrypted_packet}"
    else:
        return f"{chat_prefix}{uid_hex}{timestamp_hex}{encrypted_packet_length}{encrypted_packet}"


async def send_keep_alive(region="ID"):
    try:
        return bytes.fromhex(_gateway_codes(region)[2])
    except Exception:
        return bytes.fromhex("0219")


# START_REGION (Railway variable, optional): region sent in the match search.  account (default) = the account's own region
# (Superfast), or a fixed code, e.g. START_REGION=ID = what this bot sent before the Superfast update.
START_REGION = (os.environ.get("START_REGION", "account") or "account").strip()
# READY_PACKET=0 turns the 'ready' packet before the match search off (Superfast sends it).
READY_PACKET = os.environ.get("READY_PACKET", "1").strip().lower() not in ("0", "false", "no", "off")


def build_ready_packet(account_id, key, iv) -> bytes:
    """Superfast's ready_for_game: the 'ready' packet (cmd 0519) a real client sends BEFORE it searches for a match."""
    enc = encrypt_api_bytes_with(key, iv, build_proto({1: 15, 2: {1: int(account_id), 2: 1}})).hex()
    length_hex = hex(len(enc) // 2)[2:]
    if len(length_hex) % 2:
        length_hex = "0" + length_hex
    pad_zeros = max(0, 12 - len("0519") - len(length_hex))
    return bytes.fromhex("0519" + "0" * pad_zeros + length_hex + enc)


def encrypt_api_bytes_with(key, iv, plain_bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain_bytes, AES.block_size))

# ============================================================
# LONE WOLF / BR MATCH SEARCH
# ============================================================
async def start_game_lone_wolf(region, client_version, writer, key, iv):
    packet = bytes.fromhex("080112800a0a010b102b3a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815")
    proto = CguTeam_pb2.StartMatch()
    proto.ParseFromString(packet)
    if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
        proto.main.region_list[0].region = region
        if len(proto.main.region_list) > 1:
            proto.main.region_list[1].region = region
    if hasattr(proto.main, 'client_version'):
        proto.main.client_version.remote_version = client_version
    packet = proto.SerializeToString()
    encrypted_packet = (await aes_encrypt(packet, key, iv)).hex()
    packet_length = len(encrypted_packet) // 2
    hex_length = hex(packet_length)[2:]
    hex_length = hex_length if len(hex_length) > 1 else "0" + hex_length
    reg = str(region).upper() if region else "ID"
    reg_prefix = "031400"
    final_packet = reg_prefix + "0" * (6 - len(hex_length)) + hex_length + encrypted_packet
    writer.write(bytes.fromhex(final_packet))
    await writer.drain()
    print_debug(f"[LW] Lone Wolf Match Search Packet Sent ({packet_length} bytes, prefix: {reg_prefix}) | Region: {reg}")


async def start_game_battle_royale(region, client_version, writer, key, iv):
    packet = bytes.fromhex("080112800a0a010110013a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815")
    proto = CguFF_pb2.StartMatch()
    proto.ParseFromString(packet)
    if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
        proto.main.region_list[0].region = region
        if len(proto.main.region_list) > 1:
            proto.main.region_list[1].region = region
    if hasattr(proto.main, 'client_version'):
        proto.main.client_version.remote_version = client_version
    packet = proto.SerializeToString()
    encrypted_packet = (await aes_encrypt(packet, key, iv)).hex()
    packet_length = len(encrypted_packet) // 2
    hex_length = hex(packet_length)[2:]
    hex_length = hex_length if len(hex_length) > 1 else "0" + hex_length
    reg = str(region).upper() if region else "BD"
    reg_prefix = "03" + _gateway_codes(region)[2][2:] + "00"      # region byte follows GATEWAY_STYLE
    final_packet = reg_prefix + "0" * (6 - len(hex_length)) + hex_length + encrypted_packet
    writer.write(bytes.fromhex(final_packet))
    await writer.drain()
    print_debug(f"[BR] Battle Royale Match Search Packet Sent ({packet_length} bytes, prefix: {reg_prefix}) | Region: {reg}")


async def has_ssan_zig(n):
    z = (n << 1) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while z >= 0x80:
        out.append((z & 0x7F) | 0x80)
        z >>= 7
    out.append(z)
    return bytes(out)


async def uleb_encode(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


async def tea_enc(v0, v1, k0, k1, k2, k3):
    s = 0
    for _ in range(_ROUNDS):
        s = (s + _DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                      ((v1 + s) & 0xFFFFFFFF) ^
                      (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v1 = (v1 + (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                      ((v0 + s) & 0xFFFFFFFF) ^
                      (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
    return v0, v1


async def tea_dec(v0, v1, k0, k1, k2, k3):
    s = (_DELTA * _ROUNDS) & 0xFFFFFFFF
    for _ in range(_ROUNDS):
        v1 = (v1 - (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                      ((v0 + s) & 0xFFFFFFFF) ^
                      (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v0 = (v0 - (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                      ((v1 + s) & 0xFFFFFFFF) ^
                      (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        s = (s - _DELTA) & 0xFFFFFFFF
    return v0, v1


async def tea_cbc_encrypt(padded, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(padded))
    prev_cipher = bytearray(8)
    prev_intermediate = bytearray(8)
    for i in range(0, len(padded), 8):
        xored = bytearray(8)
        for j in range(8):
            xored[j] = padded[i + j] ^ prev_cipher[j]
        e0, e1 = await tea_enc(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3,
        )
        enc = bytearray(8)
        struct.pack_into("<I", enc, 0, e0)
        struct.pack_into("<I", enc, 4, e1)
        for j in range(8):
            out[i + j] = enc[j] ^ prev_intermediate[j]
        prev_cipher[:] = out[i:i + 8]
        prev_intermediate[:] = xored
    return bytes(out)


async def build_padded(content):
    pad_len = (8 - (len(content) + 10) % 8) % 8
    return bytes([pad_len, 0, 0]) + b"\x00" * pad_len + content + b"\x00" * 7


async def encode_header(layout, send_option, cmd, order_id, flags, length, k, v80):
    out = bytearray()
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: length}[code]
        if _FIELD_SIZES[code] == 1:
            out.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            out.append(v & 0xFF)
            out.append((v >> 8) & 0xFF)
    return bytes(out)


async def crc7_buff(crc, buf):
    c = crc & 0x7F
    for b in buf:
        c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F


async def sv_frame(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0]) + await encode_header(layout, send_option, cmd, order_id, flags, len(body), k, v80)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def build_match_startup_packets(token, udp_key, match_code, account_id, block_val,
                                      server_ip="", region="ID", client_version="1.132.6",
                                      client_version_code="2019121229", access_token="",
                                      mode_id=LONE_WOLF_MODE_ID, map_id=LONE_WOLF_MAP_ID):
    token = token.strip()
    udp_key = bytes.fromhex(udp_key)
    match_code = [int(ch) for ch in str(match_code).strip()]
    thunder_jwt = token[:660] if len(token) > 660 else token
    sharma_jwt = token[660:] if len(token) > 660 else ""
    encoded_thunder_jwt = thunder_jwt.encode() if isinstance(thunder_jwt, str) else thunder_jwt
    encoded_sharma_jwt = sharma_jwt.encode() if isinstance(sharma_jwt, str) else sharma_jwt
    garena420 = await has_ssan_zig(len(encoded_thunder_jwt)) + encoded_thunder_jwt
    reg = str(region).upper() if region else "ID"
    csoversea_block = bytes.fromhex(
        "ca0163736f7665727365612e7374726f6e67686f6c642e66726565666972656d6f62696c652e636f6d"
        "3b302e302e302e303b33342e3132362e37362e34353b33342e38372e3137372e31343b33342e38372e"
        "3137302e3233303b33352e3138352e3138332e35370000000000000100000000000000000000000001"
        "00000800000100000000000100a8a2d7bebd8d8bdf110200"
    )
    mid = bytes.fromhex('0000000001000102030101') + await has_ssan_zig(len(reg)) + reg.encode()
    mid += bytes.fromhex('0001030003000004')
    mid += await has_ssan_zig(len(client_version)) + client_version.encode()
    mid += await has_ssan_zig(len(client_version_code)) + client_version_code.encode()
    mid += csoversea_block
    clean_ip = server_ip.split(':')[0] if server_ip else "0.0.0.0"
    mid += await has_ssan_zig(len(clean_ip)) + clean_ip.encode()
    clean_acc_tok = access_token.strip() if access_token else ""
    if clean_acc_tok:
        mid += await has_ssan_zig(len(clean_acc_tok)) + clean_acc_tok.encode()
    mid += await has_ssan_zig(len(encoded_sharma_jwt)) + encoded_sharma_jwt
    tg_garena420 = (
        await uleb_encode(int(account_id)) +
        await uleb_encode(int(block_val)) +
        await uleb_encode(1) +
        await uleb_encode(int(mode_id)) +
        await uleb_encode(int(block_val)) +
        await uleb_encode(int(map_id)) +
        mid
    )
    process = await sv_frame(0x5E, match_code, 2, 447, 0, 1, garena420, udp_key)
    loading = await sv_frame(0x5A, match_code, 2, 448, 1, 1, tg_garena420, udp_key)
    return process.hex(), loading.hex()


async def produce_xor_key(secret_key):
    k = secret_key[0] if secret_key and len(secret_key) > 0 else 10
    return k, ((k << 8) | k) & 0xFFFF


async def parse_layout(layout):
    if isinstance(layout, str):
        return [int(ch) for ch in layout.strip()]
    return list(layout)


async def tea_cbc_decrypt(body, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(body))
    prev_intermediate = bytearray(8)
    prev_cipher = bytearray(8)
    xored = bytearray(8)
    dec = bytearray(8)
    for i in range(0, len(body), 8):
        for j in range(8):
            xored[j] = body[i + j] ^ prev_intermediate[j]
        d0, d1 = await tea_dec(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3
        )
        struct.pack_into("<I", dec, 0, d0)
        struct.pack_into("<I", dec, 4, d1)
        for j in range(8):
            out[i + j] = dec[j] ^ prev_cipher[j]
        prev_cipher[:] = body[i:i + 8]
        prev_intermediate[:] = dec
    return bytes(out)


async def build_hello_packet(text, key, layout):
    data = text.encode("utf-8")
    if len(data) > 25:
        raise ValueError(f"Text is too long ({len(data)} bytes)")
    content = b"\x10\x00\x00\x00" + data + b"\x00" * (29 - 4 - len(data))
    k, v80 = await produce_xor_key(key)
    layout = await parse_layout(layout)
    padded = await build_padded(content)
    enc_body = await tea_cbc_encrypt(padded, key)
    header_bytes = await encode_header(layout, 1, 1, 0, 1, len(enc_body), k, v80)
    packet = bytearray([0x63, 0x00]) + header_bytes + enc_body
    packet[1] = await crc7_buff(0, packet[2:]) & 0x7F
    return bytes(packet).hex()


async def classify(frame):
    cmd = frame["cmd"]
    msg_name = MESSAGE_ID_TO_NAME.get(cmd, f"UNKNOWN_{cmd}")
    if msg_name == "UDP_HELLO":
        return "HELLO"
    if msg_name == "UDP_ACK":
        return "ACK"
    if msg_name == "UDP_PING":
        return "PING"
    if msg_name == "RUDP_JOIN_MATCH":
        return "JOIN_MATCH"
    if msg_name.startswith("RUDP_"):
        return msg_name
    if msg_name.startswith("UDP_"):
        return msg_name
    return "DATA"


async def build_packet(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0])
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: len(body)}[code]
        if _FIELD_SIZES[code] == 1:
            hdr.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            hdr.append(v & 0xFF)
            hdr.append((v >> 8) & 0xFF)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def layouts_from_mask(mask):
    ru = [int(c) for c in str(mask).strip()]
    nr = [c for c in ru if c != 2]
    return ru, nr


async def reply_for(frame, key, mask, ack_key=0x68, ping_key=0x6D, hello_key=0x5B, ack_style="short"):
    ru, nr = await layouts_from_mask(mask)
    typ = await classify(frame)
    if typ == "HELLO":
        if ack_style == "echo":
            content = frame["content"] if frame["content"] else b"\x10\x00\x00\x00"
            return typ, await build_packet(hello_key, nr, 1, 1, None, 1, content, key)
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x01\x00", key)
    if typ == "ACK":
        content = frame["content"] if frame["content"] else b"\x01\x00"
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, content, key)
    if typ == "PING":
        c = frame["content"]
        counter = c[:4] if len(c) >= 4 else c
        return typ, await build_packet(ping_key, nr, 0, 3, None, 0, counter + b"\x00\x00\x00", key, encrypted=False)
    if typ == "JOIN_MATCH":
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x02\x00", key)
    return typ, None


async def keepalive_ping(sock, ip, port, key_bytes, mask, stop_event):
    nr = (await layouts_from_mask(mask))[1]
    ping_keys = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
    loop = asyncio.get_event_loop()
    i = 0
    while not stop_event.is_set():
        pk = ping_keys[i % len(ping_keys)]
        counter = int(time.time() * 1000) & 0xFFFFFFFF
        pkt = await build_packet(pk, nr, 0, 3, None, 0, struct.pack("<I", counter) + b"\x00\x00\x00", key_bytes, encrypted=False)
        try:
            await loop.sock_sendto(sock, pkt, (ip, port))
        except Exception:
            pass
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


async def try_header(buf, layout, k, v80):
    off = 2
    out = {}
    for code in layout:
        size = _FIELD_SIZES[code]
        if off + size > len(buf):
            return None
        out[_FIELD_NAMES[code]] = (buf[off] ^ k) if size == 1 else ((buf[off] | (buf[off + 1] << 8)) ^ v80) & 0xFFFF
        off += size
    out["headerLen"] = off
    return out


async def oicq_unpad(padded):
    if not padded or len(padded) < 8:
        return None
    if not all(padded[-1 - i] == 0 for i in range(7)):
        return None
    pad_len = padded[0] & 0x07
    s = 3 + pad_len
    e = len(padded) - 7
    return padded[s:e] if s < e else b""


async def decode_packet(packet, key, mask=None):
    data = bytes(packet) if isinstance(packet, bytes) else bytes.fromhex(packet)
    if len(data) < 8:
        return None
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    crc_ok = (data[1] & 0x7F) == await crc7_buff(0, data[2:])
    candidates = []
    if mask:
        ru, nr = await layouts_from_mask(mask)
        layouts = [("RUDP", ru), ("nonRUDP", nr)]
    else:
        layouts = [("RUDP", list(p)) for p in itertools.permutations([0, 1, 2, 3, 4])]
        layouts += [("nonRUDP", list(p)) for p in itertools.permutations([0, 1, 3, 4])]
    for kind, layout in layouts:
        f = await try_header(data, layout, k, v80)
        if not f:
            continue
        if f["flags"] > 7 or f["sendOption"] > 7:
            continue
        if f["length"] != len(data) - f["headerLen"]:
            continue
        body = data[f["headerLen"]:f["headerLen"] + f["length"]]
        content = None
        padded = None
        if f["flags"] & 1:
            if len(body) < 8 or len(body) % 8 != 0:
                continue
            padded = await tea_cbc_decrypt(body, key)
            content = await oicq_unpad(padded)
            if content is None:
                continue
        else:
            content = body
        score = (1 if crc_ok else 0) + (1 if content is not None else 0)
        candidates.append({
            "kind": kind, "layout": layout, "headerLen": f["headerLen"],
            "msgKey": data[0], "cmd": f["cmd"], "flags": f["flags"],
            "sendOption": f["sendOption"], "orderId": f.get("orderId"),
            "length": f["length"], "content": content, "crcOk": crc_ok,
            "padded": padded, "score": score, "total": len(data),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c["kind"] == "RUDP" or c["kind"] == "nonRUDP", c["score"]), reverse=True)
    return candidates[0]


# ===== MOVEMENT ENGINE BEGIN =====
# ============================================================
# MOVEMENT ENGINE  (ported from the Clash-Squad bot: app.py + app_part2.py)
# The bot really WALKS in the match now: it reads where the server says it stands (cmd 2001), plans a step
# every 1/MOVE_HZ seconds and sends the position back (old position update + new cmd-2001 packet).
#   MOVEMENT=0            turn the whole engine off (back to the old anti-AFK nudges)
#   MOVE_HZ=10            position updates per second (lower = less CPU when many matches run in parallel)
#   MOVE_PKT_MODE         both (default) | new | old
#   MOVE_PKT_PHASE        arena (default) | game | auto
#   ZONE_MODE_LW=avoid    Lone Wolf / round modes (like Clash Squad): avoid = never walk into the zone circle
#   ZONE_MODE_BR=safe     Battle Royale: safe = stand around the landing spot, walk in only when the zone closes on us
#                         (avoid = walk OUT of the circle; follow = walk to the zone centre)   ZONE_MODE=x sets both
#   BR_WAIT_GROUND=1      Battle Royale: send no steps while the server moves us fast (plane / free-fall)
#   BOT_EXTRAS=1 / BOT_JUMP / BOT_LOOK / BOT_STRAFE   jump, camera sweep, joystick wobble (OFF by default)
# ============================================================
def _mv_flag(name, default="1"):
    return str(os.environ.get(name, default) or default).strip().lower() not in ("0", "false", "no", "off", "")


def _mv_num(name, d, lo, hi):
    try:
        return max(lo, min(hi, float(os.environ.get(name, str(d)) or d)))
    except Exception:
        return float(d)


MOVEMENT_ENGINE = _mv_flag("MOVEMENT", "1")
SEND_HZ = _mv_num("MOVE_HZ", 10.0, 2.0, 20.0)
MOVE_INTERVAL = 1.0 / SEND_HZ
WALK_SPEED = 5150.0
STEP_PER_TICK = WALK_SPEED / SEND_HZ
ZONE_RADIUS_SCALE = 1000.0

MOVE_PKT_MODE = (os.environ.get("MOVE_PKT_MODE", "both") or "both").strip().lower()
if MOVE_PKT_MODE not in ("both", "new", "old"):
    MOVE_PKT_MODE = "both"
MOVE_PKT_PHASE = (os.environ.get("MOVE_PKT_PHASE", "arena") or "arena").strip().lower()
if MOVE_PKT_PHASE not in ("arena", "game", "auto"):
    MOVE_PKT_PHASE = "arena"

BOT_EXTRAS = _mv_flag("BOT_EXTRAS", "0")
_XDEF = "1" if BOT_EXTRAS else "0"
BOT_JUMP = _mv_flag("BOT_JUMP", _XDEF)
BOT_LOOK = _mv_flag("BOT_LOOK", _XDEF)
BOT_STRAFE = _mv_flag("BOT_STRAFE", _XDEF)
BOT_SIGNALS = _mv_flag("BOT_SIGNALS", "1")
BOT_UNSTUCK = _mv_flag("BOT_UNSTUCK", "1")
BOT_CMDSTATS = _mv_flag("BOT_CMDSTATS", "1")
BOT_STRAFE_DEG = _mv_num("BOT_STRAFE_DEG", 12.0, 0.0, 30.0)
BOT_LOOK_SPEED = _mv_num("BOT_LOOK_SPEED", 2.5, 0.5, 8.0)
BOT_LOOK_PITCH = _mv_num("BOT_LOOK_PITCH", 20.0, 0.0, 40.0)
BOT_JUMP_HEIGHT = _mv_num("BOT_JUMP_HEIGHT", 1000.0, 300.0, 2500.0)
BOT_IDLE_RADIUS = _mv_num("BOT_IDLE_RADIUS", 8000.0, 1000.0, 40000.0)
ZONE_AVOID_MARGIN = _mv_num("ZONE_AVOID_MARGIN", 1.0, 0.8, 2.0)
ZONE_BUFFER = _mv_num("ZONE_BUFFER", 3000.0, 0.0, 30000.0)
BOT_JUMP_BYTE = _mv_flag("BOT_JUMP_BYTE", "1")
JUMP_TICKS = 8
JUMP_V0 = 450
ALIVE_RECOVER_SEC = _mv_num("ALIVE_RECOVER_SEC", 20.0, 3.0, 300.0)
BR_WAIT_GROUND = _mv_flag("BR_WAIT_GROUND", "1")
# Battle Royale never sends cmd 101 (player join), so our in-match id is read from the cmd-100 reply ("join match") and
# confirmed with the first field of our own state packet (cmd 2001).  BR_ID_GUESS=1 = use the cmd-100 value even if it
# could not be confirmed (try it on a throw-away account first).
BR_ID_GUESS = _mv_flag("BR_ID_GUESS", "0")
FLIGHT_SPEED_MM_S = 12000.0          # the server moves us faster than this (12 m/s) = plane / free-fall, not walking


def _mv_zone_mode(env_name, default):
    v = (os.environ.get(env_name, "") or os.environ.get("ZONE_MODE", "") or default).strip().lower()
    return v if v in ("avoid", "follow", "safe") else default


ZONE_MODE_LW = _mv_zone_mode("ZONE_MODE_LW", "avoid")
ZONE_MODE_BR = _mv_zone_mode("ZONE_MODE_BR", "safe")

CMD_PLAYER_JOIN = 101
CMD_MATCH_END = 103
CMD_DEAD = 107
CMD_SAFE_ZONE = 117
CMD_SERVER_TIME = 2000
CMD_MY_STATE = 2001
CMD_OTHER_STATES = 2012
CMD_QUICK_CHAT = 551
CMD_GRI_SYNC = 1901
PHASE_PREPARE = 1
PHASE_FIGHT = 3
PHASE_ROUND_END = 5
POSE_STANDING = 0
POSE_DASHING = 2
QUICK_CHAT_FOLLOW = 1
QUICK_CHAT_HELP = 2


# ---------- small encoders / decoders ----------
def _put_varint(out, v):
    v = int(v)
    if v < 0:
        v &= 0xFFFFFFFFFFFFFFFF
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)


def _put_zigvarint(out, v):
    z = (int(v) << 1) ^ (int(v) >> 63)
    _put_varint(out, z & 0xFFFFFFFFFFFFFFFF)


def _put_float_bits(out, f):
    bits = struct.unpack("<I", struct.pack("<f", float(f)))[0]
    _put_varint(out, bits)


def _rd_varint(b, p):
    v = i = 0
    while True:
        c = b[p + i]
        v |= (c & 0x7F) << (7 * i)
        i += 1
        if not c & 0x80:
            return v, p + i


def _zag(v):
    return (v >> 1) ^ -(v & 1)


_MOVE_SEQ_KEY = 35371


async def build_movement_packet_2001(key_bytes, msg_key, player_state, x, y, z,
                                     tick, counter, move_state, account_uid,
                                     room_code, phase="arena"):
    """The 'new' position packet: raw body + its own header + CRC7 (identical to the Clash-Squad bot)."""
    if phase == "game":
        TAIL = (b"\x00" * 7 + b"\x93\xca\xd5\xf9\x0b" + b"\x8e\xfe\xfc\xf7\x0b"
                + b"\xf4\xe0\xfe\xf7\x03" + b"\x00\x00")
    else:
        TAIL = (b"\x00" * 7 + b"\xbd\xb6\xe1\xf3\x03" + b"\x00"
                + b"\xc4\xb6\xe1\xfb\x0b" + b"\x00\x00")
    body = bytearray()
    _put_varint(body, account_uid)
    _put_varint(body, player_state)
    _put_zigvarint(body, x)
    _put_zigvarint(body, y)
    _put_zigvarint(body, z)
    body += b"\x00"
    _put_varint(body, move_state)
    body += TAIL
    _put_varint(body, tick)
    body += ((_MOVE_SEQ_KEY ^ counter) & 0xFFFF).to_bytes(2, "little")
    body += b"\x00\x00\x00"
    _put_varint(body, counter)
    body = bytes(body)
    k = key_bytes[0]
    v80 = ((k << 8) | k) & 0xFFFF
    h = bytearray()
    h.append(msg_key & 0xFF)
    h.append(0x00)
    h.append(0x00 ^ k)
    h += ((2001 ^ v80) & 0xFFFF).to_bytes(2, "little")
    h += ((len(body) ^ v80) & 0xFFFF).to_bytes(2, "little")
    h.append(0x00 ^ k)
    h[1] = (await crc7_buff(0, bytes(h[2:]) + body)) & 0x7F
    return bytes(h) + body


def build_player_state_upload(pid, pos, rot, tdir, vel, aim, st,
                              ps=0, pp=0, plat=0, lg=0, ping=0,
                              jump=0, slide=0, custom=0):
    o = bytearray()
    _put_varint(o, int(pid))
    _put_zigvarint(o, pos[0]); _put_zigvarint(o, pos[1]); _put_zigvarint(o, pos[2])
    _put_zigvarint(o, rot[0]); _put_zigvarint(o, rot[1]); _put_zigvarint(o, rot[2])
    _put_zigvarint(o, tdir[0]); _put_zigvarint(o, tdir[1])
    _put_zigvarint(o, vel[0]); _put_zigvarint(o, vel[1]); _put_zigvarint(o, vel[2])
    o.append(ps & 0xFF); o.append(pp & 0xFF)
    _put_float_bits(o, aim[0]); _put_float_bits(o, aim[1]); _put_float_bits(o, aim[2])
    _put_varint(o, plat); o.append(1 if lg else 0)
    _put_varint(o, ping); o.append(jump & 0xFF)
    _put_varint(o, slide); o.append(custom & 0xFF)
    _put_varint(o, st & 0xFFFFFFFF)
    return bytes(o)


def parse_player_join(body):
    if body is None or len(body) < 4:
        return None
    try:
        p = 0
        uid, p = _rd_varint(body, p); _s, p = _rd_varint(body, p)
        pid, p = _rd_varint(body, p); rl, p = _rd_varint(body, p)
        n = _zag(rl)
        name = body[p:p + n].decode("utf-8", "replace")
    except (IndexError, ValueError):
        return None
    return uid, pid, name


def parse_join_reply(body, uid):
    """cmd 100 (join match reply): 0, match id, OUR uid, 0, 0, 0, 0, slot?, ext?, server time, 1, 0, 600, ... -> dict or None."""
    if body is None or len(body) < 20:
        return None
    try:
        p = 1; vals = []
        for _ in range(10):
            v, p = _rd_varint(body, p)
            vals.append(v)
    except (IndexError, ValueError):
        return None
    if len(vals) < 9 or vals[1] != int(uid):
        return None
    return {"slot": vals[6], "ext": vals[7], "tick": vals[8]}


def peek_first_varints(body, n=2):
    out = []
    try:
        p = 0
        for _ in range(n):
            v, p = _rd_varint(body, p)
            out.append(v)
    except (IndexError, ValueError):
        pass
    return out


def decode_my_state(body):
    if body is None or len(body) < 6:
        return None
    try:
        p = 0
        _mt, p = _rd_varint(body, p); _c, p = _rd_varint(body, p); p += 1
        x, p = _rd_varint(body, p); y, p = _rd_varint(body, p); z, p = _rd_varint(body, p)
    except (IndexError, ValueError):
        return None
    return (_zag(x), _zag(y), _zag(z))


def decode_players_v2(body, max_sid=255):
    players = {}
    if body is None or len(body) < 4:
        return players
    p = 2
    while p < len(body) - 1:
        try:
            sid = body[p]; p += 1
            x, p = _rd_varint(body, p); y, p = _rd_varint(body, p); z, p = _rd_varint(body, p)
            vx, p = _rd_varint(body, p); vy, p = _rd_varint(body, p); vz, p = _rd_varint(body, p)
            yr = body[p]; p += 1
            ay = body[p]; p += 1
            axz = body[p]; p += 1
            ps = body[p]; p += 1
            pp = body[p]; p += 1
            _pl, p = _rd_varint(body, p)
            _c = body[p]; p += 1
        except (IndexError, ValueError):
            break
        pos = (_zag(x), _zag(y), _zag(z))
        if not (1 <= sid <= max_sid):
            break
        if any(abs(v) > 4000000 for v in pos):
            break
        players[sid] = {"pos": pos, "vel": (_zag(vx), _zag(vy), _zag(vz)),
                        "yrot": yr, "aim_y": ay, "aim_xz": axz,
                        "physx_state": ps, "physx_pose": pp}
    return players


def decode_server_time(body):
    if not body:
        return None
    try:
        tick, p = _rd_varint(body, 0)
        phase = body[p] if p < len(body) else 0
    except (IndexError, ValueError):
        return None
    return tick, phase


def _read_vec2pos(b, p):
    x, p = _rd_varint(b, p); z, p = _rd_varint(b, p)
    return (_zag(x), _zag(z)), p


def decode_safezone(body):
    if not body:
        return None
    try:
        p = 0
        stage = body[p]; p += 1
        oc, p = _read_vec2pos(body, p); orad, p = _rd_varint(body, p)
        ic, p = _read_vec2pos(body, p); irad, p = _rd_varint(body, p)
        _t, p = (body[p] if p < len(body) else 0), p + 1
        st, p = _rd_varint(body, p); et, p = _rd_varint(body, p)
    except (IndexError, ValueError):
        return None
    return {"stage": stage, "outer_center": oc, "outer_radius": orad,
            "inner_center": ic, "inner_radius": irad, "start": st, "end": et}


def parse_gri_block(body):
    if body is None or len(body) < 2:
        return {}
    try:
        bl = struct.unpack_from("<H", body, 0)[0]
    except struct.error:
        return {}
    p = 2; end = min(2 + bl, len(body)); fields = {}
    while p < end:
        fid = body[p]; p += 1
        if p >= end:
            break
        try:
            v, p = _rd_varint(body, p)
        except (IndexError, ValueError):
            break
        fields[fid] = v
    return fields


def get_match_phase(body):
    fields = parse_gri_block(body)
    if 3 not in fields:
        return None
    v = fields[3]
    if v < 0x10000:
        return None
    return (v >> 16) & 0xFFFF, fields.get(2), (v >> 32) & 0xFFFFFFFF, v & 0xFFFF


def decode_quick_chat(body):
    if body is None or len(body) < 3:
        return None
    try:
        p = 0
        cid, p = _rd_varint(body, p)
        _i, p = _rd_varint(body, p)
        s, p = _rd_varint(body, p)
    except (IndexError, ValueError):
        return None
    return cid, s & 0xFF, s


# ---------- what the bot knows about the match ----------
class MovementState:
    def __init__(self, aid):
        self.account_id = int(aid)
        self.my_id = None; self.my_short = None; self.my_pos = None; self.server_pos = None
        self.target_short = None; self.target_pos = None; self.target_seen = None
        self.target_vel = (0.0, 0.0); self.target_pose = 0; self.target_state = 0
        self.target_yrot = 0; self.target_aim_y = 0; self.target_aim_xz = 0
        self.zone_center = None; self.zone_radius = 0.0
        self.phase = None; self.round_no = None
        self.server_tick = None; self.tick_local = None; self.tick_rate = 30.0
        self.alive = True; self.downed = {}; self.roster = {}
        self.joined_at = None; self.blocked = 0
        self.yaw = 0.0; self.orbit_angle = 0.0
        self.dead_since = None; self.need_resync = False
        self.round_started = None; self.round_gen = 0; self.seen_phases = set()
        self.sig = collections.Counter(); self.sig_last = {}; self.sig_moves = collections.Counter()
        self.srv_hist = collections.deque(maxlen=60)      # (time, server position) - "walking but not moving" check
        self.stuck_pending = False; self.stuck_times = collections.deque(maxlen=20)
        self.cmd_stats = collections.Counter()
        self.samples = collections.Counter()
        self.hint = None                                   # cmd-100 reply: {'slot','ext','tick'}
        self.id_source = None
        self.home = None                                   # where we spawned (centre of the idle wandering)
        self.sent = 0                                      # position updates sent in this match
        self.pos_seen = 0                                  # how often the server told us our position
        self.first_pos_at = None

    def record_signals(self, players):
        for sid, info in players.items():
            if sid == self.my_short:
                continue
            vy = info.get("vel", (0, 0, 0))[1]
            key = (info.get("physx_state", 0), info.get("physx_pose", 0), 1 if abs(vy) > 50 else 0)
            self.sig[key] += 1
            prev = self.sig_last.get(sid)
            if prev is not None and prev != key:
                self.sig_moves[(prev, key)] += 1
            self.sig_last[sid] = key

    def new_round(self, now):
        self.alive = True; self.dead_since = None
        self.downed.clear()
        self.target_pos = None; self.target_seen = None; self.target_vel = (0.0, 0.0)
        self.blocked = 0; self.need_resync = True
        self.round_started = now; self.round_gen += 1

    def teammates(self):
        if self.my_id is None:
            return set()
        team = (self.my_id >> 24) & 0xFF
        return {s for s, (fid, _) in self.roster.items()
                if (fid >> 24) & 0xFF == team and s != self.my_short}

    def set_identity(self, fid):
        self.my_id = int(fid); self.my_short = self.my_id & 0xFF
        if self.joined_at is None:
            self.joined_at = time.time()

    def add_player(self, fid, name):
        self.roster[int(fid) & 0xFF] = (int(fid), name)

    def seed_my_pos(self, pos):
        self.server_pos = pos
        self.pos_seen += 1
        if self.first_pos_at is None:
            self.first_pos_at = time.time()
        self.srv_hist.append((time.time(), pos))
        if self.need_resync:      # new round / landed: server put us somewhere new -> snap to it immediately
            self.my_pos = pos; self.blocked = 0; self.need_resync = False; self.home = pos
            return
        if self.my_pos is None:
            self.my_pos = pos; self.blocked = 0; self.home = pos
            return
        dx = pos[0] - self.my_pos[0]; dz = pos[2] - self.my_pos[2]
        gap = math.sqrt(dx * dx + dz * dz)
        if gap > 120000.0:
            self.my_pos = pos; self.blocked = 0
        elif gap > 1500.0:
            self.blocked += 1
            if self.blocked >= 5:
                self.my_pos = pos; self.blocked = 0
                self.stuck_pending = True        # the server keeps pulling us back -> something is in the way
        else:
            self.blocked = 0

    def needs_history(self, now):
        """Right after the first server position we cannot tell plane from ground yet: look for up to 3s first."""
        if self.first_pos_at is None or now - self.first_pos_at >= 3.0:
            return False
        h = [t for t, _p in self.srv_hist if now - t <= 2.0]
        return len(h) < 2 or h[-1] - h[0] < 0.6

    def in_flight(self, now):
        """True while the server itself moves us fast (plane / free-fall / parachute): no walking steps then."""
        h = [(t, p) for t, p in self.srv_hist if now - t <= 2.0]
        if len(h) < 2 or h[-1][0] - h[0][0] < 0.5:
            return False
        (t0, p0), (t1, p1) = h[0], h[-1]
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(p0, p1)))
        return d / (t1 - t0) > FLIGHT_SPEED_MM_S

    def update_target(self, info, now):
        prev_pos, prev_seen = self.target_pos, self.target_seen
        self.target_pos = info["pos"]; self.target_seen = now
        if prev_pos is not None and prev_seen is not None:
            dt = now - prev_seen
            if 0.01 < dt < 2.0:
                vx = (info["pos"][0] - prev_pos[0]) / dt
                vz = (info["pos"][2] - prev_pos[2]) / dt
                if abs(vx) < 30000 and abs(vz) < 30000:
                    self.target_vel = (vx, vz)
        self.target_yrot = info.get("yrot", self.target_yrot)
        self.target_pose = info.get("physx_pose", self.target_pose)

    def sync_clock(self, tick, now):
        if self.server_tick is not None and self.tick_local is not None:
            dt = now - self.tick_local; dtick = tick - self.server_tick
            if dt > 0.5 and 0 < dtick < 10000:
                self.tick_rate = self.tick_rate * 0.7 + (dtick / dt) * 0.3
        self.server_tick = tick; self.tick_local = now

    def server_time(self, now):
        if self.server_tick is None:
            if self.joined_at is None:
                return 0
            return int((now - self.joined_at) * 1000.0) & 0xFFFFFFFF
        elapsed = now - self.tick_local
        ticks = self.server_tick + elapsed * self.tick_rate
        return int(ticks * 1000.0 / self.tick_rate) & 0xFFFFFFFF


# ---------- where to walk ----------
class MovementPlanner:
    def __init__(self, zone_mode="avoid", freeze_on_round_end=True):
        self.zone_mode = zone_mode
        self.freeze_on_round_end = freeze_on_round_end
        self.step = float(STEP_PER_TICK)
        self.max_step = float(STEP_PER_TICK * 1.25)
        self.orbit_fraction = 0.55; self.orbit_speed = 6.0
        self.coast_limit = 3.0; self._last_yaw = 0.0
        t = time.time()
        self._ph1 = random.uniform(0, 6.28); self._ph2 = random.uniform(0, 6.28); self._ph3 = random.uniform(0, 6.28)
        self._spin = random.uniform(0, 360.0); self._spin_dir = random.choice((-1.0, 1.0))
        self._next_jump = t + random.uniform(2.5, 6.0); self._jk = -1; self._jump_cool = 0.0
        self.jumps = 0
        self._moving_since = 0.0; self._esc = None; self._esc_block_until = 0.0; self._esc_side = 1
        self.events = []                      # short texts for the log ("stuck -> left")
        self._zone_side = random.choice((-1, 1))
        self._idle_pt = None; self._idle_until = 0.0; self._idle_timeout = 0.0

    def next_yaw(self):
        return self._last_yaw

    def look_yaw(self, now, move_yaw, moving):
        """Camera direction in degrees (BOT_LOOK only)."""
        if not BOT_LOOK:
            return None
        k = BOT_LOOK_SPEED / 2.5
        if moving:
            sweep = 55.0 * math.sin(now * 2.0 * k + self._ph1) + 18.0 * math.sin(now * 4.4 * k + self._ph2)
            return (move_yaw + sweep) % 360.0
        if random.random() < 0.015:
            self._spin_dir = -self._spin_dir
        self._spin = (self._spin + self._spin_dir * 6.0 * k) % 360.0
        return self._spin

    def pitch(self, now):
        if not BOT_LOOK or BOT_LOOK_PITCH <= 0:
            return 0.0
        k = BOT_LOOK_SPEED / 2.5
        p = BOT_LOOK_PITCH * (math.sin(now * 1.7 * k + self._ph3) + 0.35 * math.sin(now * 3.9 * k + self._ph1))
        lim = BOT_LOOK_PITCH * 1.3
        return max(-lim, min(lim, p))

    def jump_step(self, now, moving, allow=True):
        """None, or the in-air state for this tick: (physx_state, pose, vertical speed cm/s, height mm)."""
        if not BOT_JUMP or not allow:
            self._jk = -1
            return None
        if self._jk < 0:
            esc_jump = bool(self._esc and self._esc.get("jump"))
            if (esc_jump and now >= self._jump_cool) or (moving and now >= self._next_jump):
                self._next_jump = now + random.uniform(3.0, 8.0)
                self._jk = 0; self.jumps += 1
            else:
                return None
        k = self._jk; self._jk += 1
        if self._jk >= JUMP_TICKS:
            self._jk = -1; self._jump_cool = now + 0.25
        u = (k + 0.5) / JUMP_TICKS
        return (1, 11 if moving else 0, int(JUMP_V0 * (1.0 - 2.0 * u)), int(4.0 * BOT_JUMP_HEIGHT * u * (1.0 - u)))

    # ---- stuck handling (BOT_UNSTUCK) ----
    def _stuck_now(self, st, now):
        if st.stuck_pending:
            st.stuck_pending = False
            return True
        if self._moving_since and now - self._moving_since >= 3.5:
            h = st.srv_hist
            if len(h) >= 3 and now - h[-1][0] < 2.0:
                t_new, p_new = h[-1]
                old = None
                for t, p in h:
                    if t_new - t <= 4.5:
                        old = (t, p)
                        break
                if old is not None and t_new - old[0] >= 3.0:
                    d = math.hypot(p_new[0] - old[1][0], p_new[2] - old[1][2])
                    if d < 1500.0:
                        return True      # we should have covered ~15 m in that time
        return False

    def _unstuck(self, st, now, my, new_y, ux, uz):
        esc = self._esc
        if esc is None:
            if now < self._esc_block_until or not self._stuck_now(st, now):
                return None
            recent = [t for t in st.stuck_times if now - t < 45.0]
            st.stuck_times.append(now); n = len(recent) + 1
            if n == 1:
                self._esc_side = random.choice((-1, 1))
            sd = self._esc_side
            if n == 1:
                segs = [(90 * sd, 1.6, True)]
            elif n == 2:
                segs = [(90 * sd, 3.0, True)]
            elif n == 3:
                segs = [(90 * sd, 5.0, True)]
            elif n == 4:
                segs = [(180, 1.2, False), (-90 * sd, 6.0, True)]
            elif n == 5:
                segs = [(-90 * sd, 8.0, True)]
            else:
                side = random.choice((-1, 1))
                segs = [(180, 1.5, False), (90 * side, 8.0, True)]
            esc = self._esc = {"segs": segs, "t0": now, "ux": ux, "uz": uz, "jump": segs[0][2], "n": n}
            st.srv_hist.clear()

            def _nm(a):
                return "back" if abs(a) == 180 else ("left" if a > 0 else "right")
            self.events.append("blocked (x%d) -> %s" % (n, " then ".join(_nm(a) for a, _d, _j in segs)))
        t_in = now - esc["t0"]; acc = 0.0; seg = None
        for a, d, j in esc["segs"]:
            if t_in < acc + d:
                seg = (a, d, j)
                break
            acc += d
        if seg is None:                       # finished -> back to normal walking
            self._esc = None; self._esc_block_until = now + 1.5; self._moving_since = now
            st.srv_hist.clear(); st.stuck_pending = False; st.blocked = 0
            return None
        esc["jump"] = seg[2]
        ca, sa = math.cos(math.radians(seg[0])), math.sin(math.radians(seg[0]))
        vx = esc["ux"] * ca - esc["uz"] * sa; vz = esc["ux"] * sa + esc["uz"] * ca
        step = float(self.step)
        return (int(my[0] + vx * step), new_y, int(my[2] + vz * step)), 1000.0, True

    # ---- zone handling ----
    def _zone_r(self, st):
        return st.zone_radius * ZONE_AVOID_MARGIN + ZONE_BUFFER

    def _in_zone(self, st, pt):
        if st.zone_center is None or st.zone_radius <= 0 or pt is None:
            return False
        cx, cz = st.zone_center[0], st.zone_center[-1]
        return math.hypot(pt[0] - cx, pt[2] - cz) < self._zone_r(st)

    def _zone_guard(self, st, my, new):
        """zone_mode=avoid: a step never ends inside the zone circle (slides along the edge / walks out)."""
        if self.zone_mode != "avoid" or st.zone_center is None or st.zone_radius <= 0 or my is None:
            return new
        cx, cz = st.zone_center[0], st.zone_center[-1]; R = self._zone_r(st); step = float(self.step)
        d_my = math.hypot(my[0] - cx, my[2] - cz); d_new = math.hypot(new[0] - cx, new[2] - cz)
        if d_new >= R:
            return new
        if d_my < R:
            if d_new > d_my:
                return new
            rx, rz = ((my[0] - cx) / d_my, (my[2] - cz) / d_my) if d_my > 1.0 else (1.0, 0.0)
            return (int(my[0] + rx * step), new[1], int(my[2] + rz * step))
        rx, rz = (my[0] - cx) / d_my, (my[2] - cz) / d_my
        tx, tz = -rz, rx
        ux, uz = new[0] - my[0], new[2] - my[2]; un = math.hypot(ux, uz) or 1.0
        tang = (ux * tx + uz * tz) / un
        if abs(tang) < 0.25:
            sgn = self._zone_side
        else:
            sgn = 1 if tang > 0 else -1; self._zone_side = sgn
        sx = my[0] + tx * sgn * step; sz = my[2] + tz * sgn * step
        dd = math.hypot(sx - cx, sz - cz)
        if dd < R * 1.005:
            k = (R * 1.005) / max(dd, 1.0); sx = cx + (sx - cx) * k; sz = cz + (sz - cz) * k
        return (int(sx), new[1], int(sz))

    def _zone_exit_point(self, st):
        if st.my_pos is None or not self._in_zone(st, st.my_pos):
            return None
        cx, cz = st.zone_center[0], st.zone_center[-1]
        dx = st.my_pos[0] - cx; dz = st.my_pos[2] - cz
        d = math.hypot(dx, dz)
        if d < 1.0:
            dx, dz, d = 1.0, 0.0, 1.0
        out = self._zone_r(st) * 1.08
        return (cx + dx / d * out, st.my_pos[1], cz + dz / d * out)

    def _idle_point(self, st, now, check_zone=True):
        """Wander slowly around the spawn point; None = stand still for a moment."""
        my = st.my_pos; home = st.home or my
        if self._idle_pt is not None:
            d = math.hypot(self._idle_pt[0] - my[0], self._idle_pt[2] - my[2])
            if d <= 400.0:
                self._idle_pt = None; self._idle_until = now + random.uniform(1.5, 4.0)
            elif now < self._idle_timeout and not (check_zone and self._in_zone(st, self._idle_pt)):
                return self._idle_pt
            else:
                self._idle_pt = None; self._idle_until = now + 1.0
        if now < self._idle_until:
            return None
        for _ in range(12):
            ang = random.uniform(0.0, 6.2832); rad = random.uniform(0.35, 1.0) * BOT_IDLE_RADIUS
            pt = (home[0] + rad * math.cos(ang), my[1], home[2] + rad * math.sin(ang))
            if not (check_zone and self._in_zone(st, pt)):
                self._idle_pt = pt; self._idle_timeout = now + 12.0
                return pt
        self._idle_until = now + 2.0
        return None

    def _aim_avoid(self, st, now):
        ex = self._zone_exit_point(st)
        if ex is not None:
            return ex
        r = self._rescue_point(st)
        if r is not None and not self._in_zone(st, r):
            return r
        t = self._target_point(st, now)
        if t is not None and not self._in_zone(st, t):
            return t
        return self._idle_point(st, now)

    def _aim_safe(self, st, now):
        """Battle Royale: stay near the landing spot; only walk in when the zone circle closes on us."""
        my = st.my_pos
        if st.zone_center is not None and st.zone_radius > 0:
            cx, cz = st.zone_center[0], st.zone_center[-1]
            d = math.hypot(my[0] - cx, my[2] - cz)
            if d > st.zone_radius * 0.85:
                k = (st.zone_radius * 0.6) / max(d, 1.0)
                return (cx + (my[0] - cx) * k, my[1], cz + (my[2] - cz) * k)
        r = self._rescue_point(st)
        if r is not None:
            return r
        t = self._target_point(st, now)
        if t is not None:
            return t
        return self._idle_point(st, now, check_zone=False)

    def aim_point(self, st, now):
        if self.zone_mode == "safe" and st.my_pos is not None:
            return self._aim_safe(st, now)
        if self.zone_mode == "avoid" and st.my_pos is not None:
            return self._aim_avoid(st, now)
        r = self._rescue_point(st)
        if r is not None:
            return r
        t = self._target_point(st, now); z = self._zone_point(st)
        if t is None:
            return z
        if z is not None and self._outside_zone(st, t):
            return z
        return t

    def _target_point(self, st, now):
        if st.target_pos is None:
            return None
        if st.target_seen is not None:
            gap = now - st.target_seen
            if 0.05 < gap < self.coast_limit:
                vx, vz = st.target_vel
                return (st.target_pos[0] + vx * gap, st.target_pos[1], st.target_pos[2] + vz * gap)
        return st.target_pos

    def _rescue_point(self, st):
        if not st.downed or st.my_pos is None:
            return None
        best, best_d = None, None
        for pos in st.downed.values():
            if pos is None:
                continue
            dx = pos[0] - st.my_pos[0]; dz = pos[2] - st.my_pos[2]
            d = math.sqrt(dx * dx + dz * dz)
            if best_d is None or d < best_d:
                best, best_d = pos, d
        return best

    def _zone_point(self, st):
        if st.zone_center is None or st.zone_radius <= 0:
            return None
        cx, cz = st.zone_center[0], st.zone_center[-1]
        y = st.my_pos[1] if st.my_pos else 0
        ring = st.zone_radius * self.orbit_fraction
        if st.my_pos is not None:
            dx = st.my_pos[0] - cx; dz = st.my_pos[2] - cz
            here = math.sqrt(dx * dx + dz * dz)
            if here <= ring * 1.15:
                if here > 1:
                    st.orbit_angle = math.degrees(math.atan2(dz, dx))
                stride = float(self.step)
                max_deg = math.degrees(stride / max(ring, 1)) * 0.85
                st.orbit_angle = (st.orbit_angle + min(self.orbit_speed, max_deg)) % 360.0
                rad = math.radians(st.orbit_angle)
                return (cx + ring * math.cos(rad), y, cz + ring * math.sin(rad))
        return (cx, y, cz)

    def _outside_zone(self, st, point):
        if st.zone_center is None or st.zone_radius <= 0:
            return False
        cx, cz = st.zone_center[0], st.zone_center[-1]
        dx = point[0] - cx; dz = point[2] - cz
        return math.sqrt(dx * dx + dz * dz) > st.zone_radius * 0.75

    def plan(self, st, now):
        r = self._plan_inner(st, now)
        if r is not None and r[2]:
            if not self._moving_since:
                self._moving_since = now
        else:
            self._moving_since = 0.0
            if self._esc is not None and st.phase == PHASE_ROUND_END:
                self._esc = None
        return r

    def _plan_inner(self, st, now):
        r = self._plan_raw(st, now)
        if r is None or not r[2]:
            return r
        g = self._zone_guard(st, st.my_pos, r[0])
        return r if g is r[0] else (g, r[1], True)

    def _plan_raw(self, st, now):
        if st.my_pos is None or st.my_id is None:
            return None
        my = st.my_pos
        if self.freeze_on_round_end and st.phase == PHASE_ROUND_END:
            return my, 0.0, False                       # stand still only while a round is ending
        aim = self.aim_point(st, now)
        if aim is None:
            return my, 0.0, False
        dx = aim[0] - my[0]; dz = aim[2] - my[2]
        dist = math.sqrt(dx * dx + dz * dz)
        ty = aim[1] if len(aim) > 2 else my[1]
        new_y = int(my[1] + (ty - my[1]) * 0.35)
        if dist <= 150.0 or dist < 1e-6:
            return (my[0], new_y, my[2]), dist, False
        if BOT_UNSTUCK:
            r = self._unstuck(st, now, my, new_y, dx / dist, dz / dist)
            if r is not None:
                return r
        step = float(self.step)
        if dist > 8000.0:
            step = self.max_step
        ux, uz = dx / dist, dz / dist
        if BOT_STRAFE and dist > 3000.0:
            ang = math.radians(BOT_STRAFE_DEG * math.sin(now * 0.9 + self._ph1)
                               + BOT_STRAFE_DEG * 0.4 * math.sin(now * 2.3 + self._ph2))
            ca, sa = math.cos(ang), math.sin(ang)
            ux, uz = ux * ca - uz * sa, ux * sa + uz * ca
            step *= 0.92 + 0.08 * math.sin(now * 0.5 + self._ph3)
        move = step if dist > step else dist
        nx = my[0] + ux * move; nz = my[2] + uz * move
        return (int(nx), new_y, int(nz)), dist, True


def _build_position_body(pid, old, new, yaw, st, moving, cam_yaw=None, pitch=0.0, jump=0, air=None):
    """cam_yaw/pitch = where the camera looks (None -> look where we walk), jump = jump flag byte."""
    ux = uz = None
    if moving and old is not None:
        dx = new[0] - old[0]; dz = new[2] - old[2]
        dist = math.sqrt(dx * dx + dz * dz)
        if dist >= 1:
            ux, uz = dx / dist, dz / dist
    cp = math.cos(math.radians(pitch)); sp = math.sin(math.radians(pitch))
    if ux is not None:
        vel = (int(ux * 500), 0, int(uz * 500))
        rot = (int(ux * 100), 0, int(uz * 100)); tdir = (int(ux * 100), int(uz * 100))
        if cam_yaw is None:
            aim = (ux, 0.0, uz)
        else:
            r = math.radians(cam_yaw); aim = (math.sin(r) * cp, sp, math.cos(r) * cp)
        pose = POSE_DASHING
    elif moving and old is not None and cam_yaw is None:
        vel = (0, 0, 0); rot = (0, 0, 0); tdir = (0, 0)
        aim = (0.0, 0.0, 0.0); pose = POSE_STANDING
    else:
        vel = (0, 0, 0)
        rad = math.radians(yaw if cam_yaw is None else cam_yaw); fx, fz = math.sin(rad), math.cos(rad)
        rot = (int(fx * 100), 0, int(fz * 100)); tdir = (int(fx * 100), int(fz * 100))
        aim = (fx * cp, sp, fz * cp); pose = POSE_STANDING
    ps = 0
    if air:                      # jumping: physx_state=1, pose 11 while running (0 standing), vertical speed, height
        ps = air[0]; pose = 11 if ux is not None else 0
        vel = (vel[0], int(air[2]), vel[2]); new = (new[0], new[1] + int(air[3]), new[2])
        jump = 1 if BOT_JUMP_BYTE else jump
    return build_player_state_upload(pid, new, rot, tdir, vel, aim, st, ps=ps, pp=pose, ping=90, jump=jump)


# ---------- what the match server tells us ----------
def mv_dispatch(cmd, body, st, tag, notes):
    """Feed one decoded server packet into the movement state (never raises)."""
    try:
        st.cmd_stats[cmd] += 1
        if body is None:
            return
        if cmd in (100, 101, 130, 117, 1901, 2001, 2012, 2005, 2038) and st.samples[cmd] < 2:
            st.samples[cmd] += 1
            notes.append(f"{tag} CMD-SAMPLE cmd={cmd} len={len(body)} hex={bytes(body[:48]).hex()}")
        if cmd == 100 and st.hint is None:
            h = parse_join_reply(body, st.account_id)
            if h:
                st.hint = h
                notes.append(f"{tag} join reply: our uid found, slot candidate={h['slot']} ext={h['ext']}")
        if cmd == CMD_PLAYER_JOIN:
            info = parse_player_join(body)
            if info is None:
                return
            uid, fid, name = info
            st.add_player(fid, name)
            if uid == st.account_id and st.my_id is None:
                st.set_identity(fid); st.id_source = "cmd101"
                notes.append(f"{tag} identity known (id 0x{fid:08X})")
        elif cmd == CMD_MY_STATE:
            if st.my_id is None and st.hint is not None:
                a = peek_first_varints(body, 2)
                if a and a[0] in (st.hint["slot"], st.hint["ext"]):
                    st.set_identity(a[0]); st.id_source = "cmd100+my-state"
                    notes.append(f"{tag} identity confirmed: {a[0]} (cmd 100 value == first field of our state)")
                elif st.samples["id_log"] < 2:
                    st.samples["id_log"] += 1
                    notes.append(f"{tag} my-state first fields={a} | cmd 100 slot={st.hint['slot']} ext={st.hint['ext']}")
            pos = decode_my_state(body)
            if pos is not None:
                first = st.pos_seen == 0
                st.seed_my_pos(pos)
                if first:
                    notes.append(f"{tag} server position known: x={pos[0]} y={pos[1]} z={pos[2]} (mm)")
        elif cmd == CMD_OTHER_STATES:
            players = decode_players_v2(body); mates = st.teammates()
            if BOT_SIGNALS:
                st.record_signals(players)
            for s, info in players.items():
                if s not in mates:
                    continue
                if info.get("physx_pose") == 8:
                    st.downed[s] = info["pos"]
                elif s in st.downed:
                    st.downed.pop(s, None)
            if st.target_short is not None:
                info = players.get(st.target_short)
                if info is not None:
                    st.update_target(info, time.time())
        elif cmd == CMD_SERVER_TIME:
            parsed = decode_server_time(body)
            if parsed is not None:
                st.sync_clock(parsed[0], time.time())
        elif cmd == CMD_GRI_SYNC:
            parsed = get_match_phase(body)
            if parsed is not None:
                phase, rn, _s, _e = parsed
                now_t = time.time()
                new_round = False
                if rn is not None and st.round_no is not None and rn != st.round_no:
                    new_round = True
                if phase != st.phase:
                    if st.phase == PHASE_ROUND_END and phase != PHASE_ROUND_END:
                        new_round = True
                    elif phase == PHASE_PREPARE and st.phase is not None:
                        new_round = True
                    st.phase = phase
                    if phase == PHASE_FIGHT:
                        notes.append(f"{tag} fight phase")
                    elif phase == PHASE_ROUND_END:
                        notes.append(f"{tag} round ended")
                if rn is not None:
                    st.round_no = rn
                if new_round:
                    st.new_round(now_t)
                    notes.append(f"{tag} new round {rn if rn is not None else ''} - movement resumed")
        elif cmd == CMD_SAFE_ZONE:
            zone = decode_safezone(body)
            if zone is None:
                return
            center = zone.get("inner_center") or zone.get("outer_center")
            radius = zone.get("inner_radius") or zone.get("outer_radius") or 0
            if center and radius > 0:
                st.zone_center = (center[0], 0, center[1]); st.zone_radius = radius * ZONE_RADIUS_SCALE
        elif cmd == CMD_QUICK_CHAT:
            parsed = decode_quick_chat(body)
            if parsed is None:
                return
            cid, short, _ = parsed
            if cid in (QUICK_CHAT_FOLLOW, QUICK_CHAT_HELP):
                st.target_short = short
        elif cmd == CMD_DEAD:
            try:
                pid, _ = _rd_varint(body, 0)
            except (IndexError, ValueError):
                return
            if st.my_id is not None and pid == st.my_id and st.alive:
                st.alive = False; st.dead_since = time.time()
                notes.append(f"{tag} we were downed")
    except Exception:
        pass


# ---------- the walking loop ----------
async def mv_movement_loop(sock, ra, ukb, mc, state, stop_evt, tag, mode_tag):
    br = (mode_tag == "BATTLE_ROYALE")
    planner = MovementPlanner(zone_mode=ZONE_MODE_BR if br else ZONE_MODE_LW, freeze_on_round_end=not br)
    wait_ground = br and BR_WAIT_GROUND
    nr = (await layouts_from_mask(mc))[1]
    loop = asyncio.get_event_loop()
    t_start = time.time(); next_tick = t_start
    move_ctr = 0; last_yaw_deg = 0.0
    last_stat = t_start; warned = False; logged_air = False; logged_moving = False
    while not stop_evt.is_set():
        now = time.time()
        if BOT_SIGNALS and now - last_stat >= 60 and state.cmd_stats:
            last_stat = now
            try:
                print_info(f"{tag} CMDS " + ", ".join(f"{c}:{n}" for c, n in state.cmd_stats.most_common(20)))
                if state.sig:
                    print_info(f"{tag} SIGNALS (other players) " + ", ".join(
                        f"ps={k[0]}/pp={k[1]}/air={k[2]}:{v}" for k, v in state.sig.most_common(10)))
            except Exception:
                pass
        if now < next_tick:
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=next_tick - now)
                break
            except asyncio.TimeoutError:
                pass
            continue
        next_tick += MOVE_INTERVAL
        if next_tick < now:
            next_tick = now + MOVE_INTERVAL
        if not state.sent and not warned and now - t_start > 25.0:
            warned = True
            top = ", ".join(f"{c}:{n}" for c, n in state.cmd_stats.most_common(12)) or "none"
            print_warning(f"{tag} not moving yet after 25s | identity={'yes' if state.my_id is not None else 'NO (no cmd 101 / cmd 100 id)'} "
                          f"| server position={'yes' if state.pos_seen else 'NO (no cmd 2001 from the server)'} | server cmds: {top}")
        if state.my_id is None and BR_ID_GUESS and state.hint is not None and state.pos_seen \
                and now - (state.first_pos_at or now) > 5.0:
            state.set_identity(state.hint["slot"]); state.id_source = "cmd100 (guess)"
            print_warning(f"{tag} identity GUESSED from cmd 100: {state.hint['slot']} (BR_ID_GUESS=1)")
        if state.my_id is None:
            continue
        if not state.alive:
            if state.dead_since is not None and now - state.dead_since > ALIVE_RECOVER_SEC:
                state.alive = True; state.dead_since = None; state.need_resync = True
                state.round_started = now
            else:
                continue
        if state.need_resync and state.round_started is not None \
                and now - state.round_started > 3.0 and state.server_pos is not None:
            state.my_pos = state.server_pos; state.blocked = 0; state.need_resync = False
        if wait_ground and (state.in_flight(now) or state.needs_history(now)):
            state.need_resync = True                    # snap to the real position when we land
            if not logged_air and state.in_flight(now):
                logged_air = True
                print_info(f"{tag} plane / free-fall -> waiting for the ground before walking")
            continue
        planned = planner.plan(state, now)
        if planned is None:
            continue
        new, dist, moving = planned
        old = state.my_pos; state.my_pos = new
        st = state.server_time(now)
        pkts = []
        if old is not None and moving:
            ddx = new[0] - old[0]; ddz = new[2] - old[2]
            if ddx * ddx + ddz * ddz >= 1:
                last_yaw_deg = math.degrees(math.atan2(ddx, ddz)) % 360.0
        cam = planner.look_yaw(now, last_yaw_deg, moving)
        air = planner.jump_step(now, moving, state.phase != PHASE_ROUND_END)
        while planner.events:
            print_info(f"{tag} {planner.events.pop(0)}")
        try:
            if MOVE_PKT_MODE != "new":
                body = _build_position_body(state.my_id, old, new, planner.next_yaw(), st, moving,
                                            cam_yaw=cam, pitch=planner.pitch(now), air=air)
                pkts.append(await build_packet(0x6B, nr, 0, CMD_MY_STATE, None, 1, body, ukb, True))
            if MOVE_PKT_MODE != "old":
                move_ctr = (move_ctr + 1) & 0xFFFFFFFF
                mphase = ("game" if state.phase == PHASE_FIGHT else "arena") if MOVE_PKT_PHASE == "auto" else MOVE_PKT_PHASE
                pkts.append(await build_movement_packet_2001(
                    ukb, 0x6B, (11 if (air and moving) else (POSE_DASHING if moving else POSE_STANDING)),
                    new[0], new[1] + (int(air[3]) if air else 0), new[2], st, move_ctr, 1 if moving else 0,
                    state.my_id, 0, mphase))
        except Exception as e:
            print_warning(f"{tag} could not build a movement packet: {type(e).__name__}: {e}")
            continue
        if not pkts:
            continue
        try:
            for pkt in pkts:
                await loop.sock_sendto(sock, pkt, ra)
            state.sent += 1
            if not logged_moving:
                logged_moving = True
                print_success(f"{tag} entered the map - moving now")
        except Exception:
            pass
# ===== MOVEMENT ENGINE END =====

# ============================================================
# ANTI-AFK
# ============================================================
async def send_anti_afk_movement(sock, server_addr, udp_key_bytes, match_code, account_id):
    try:
        nr = (await layouts_from_mask(match_code))[1]
        rand_x = random.randint(-100, 100)
        rand_y = random.randint(-50, 50)
        rand_z = random.randint(-100, 100)
        ts = int(time.time() * 1000) & 0xFFFFFFFF
        try:
            player_id = int(account_id)
        except Exception:
            player_id = 0
        body = struct.pack("<IIiiiH", player_id & 0xFFFFFFFF, ts, rand_x, rand_y, rand_z, 1)
        pkt = await build_packet(0x6B, nr, 0, 2001, None, 0, body, udp_key_bytes, encrypted=False)
        loop = asyncio.get_event_loop()
        await loop.sock_sendto(sock, pkt, server_addr)
        return True
    except Exception:
        return False


async def send_anti_afk_fire(sock, server_addr, udp_key_bytes, match_code, account_id):
    try:
        nr = (await layouts_from_mask(match_code))[1]
        try:
            player_id = int(account_id)
        except Exception:
            player_id = 0
        ts = int(time.time() * 1000) & 0xFFFFFFFF
        weapon_id = random.choice([101, 102, 103, 201, 202])
        fire_body = struct.pack("<IIHB", player_id & 0xFFFFFFFF, ts, weapon_id, 1)
        fire_pkt = await build_packet(0x68, nr, 0, 104, None, 0, fire_body, udp_key_bytes, encrypted=False)
        loop = asyncio.get_event_loop()
        await loop.sock_sendto(sock, fire_pkt, server_addr)
        await asyncio.sleep(random.uniform(0.15, 0.30))
        stop_body = struct.pack("<IIHB", player_id & 0xFFFFFFFF, ts + 200, weapon_id, 0)
        stop_pkt = await build_packet(0x68, nr, 0, 105, None, 0, stop_body, udp_key_bytes, encrypted=False)
        await loop.sock_sendto(sock, stop_pkt, server_addr)
        return True
    except Exception:
        return False


async def anti_afk_worker(sock, resolved_ip, port, udp_key_bytes, match_code,
                          account_id, stop_event, match_index, with_moves=True):
    server_addr = (resolved_ip, port)
    fire_count = 0
    move_count = 0
    try:
        await asyncio.sleep(2.5)
        while not stop_event.is_set():
            try:
                roll = random.random()
                if roll < ANTI_AFK_FIRE_CHANCE:
                    ok = await send_anti_afk_fire(sock, server_addr, udp_key_bytes, match_code, account_id)
                    if ok:
                        fire_count += 1
                    if with_moves and random.random() < 0.4:
                        await send_anti_afk_movement(sock, server_addr, udp_key_bytes, match_code, account_id)
                        move_count += 1
                elif with_moves:
                    ok = await send_anti_afk_movement(sock, server_addr, udp_key_bytes, match_code, account_id)
                    if ok:
                        move_count += 1
                wait_time = random.uniform(ANTI_AFK_MIN_INTERVAL, ANTI_AFK_MAX_INTERVAL)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_time)
                    break
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    finally:
        try:
            if fire_count + move_count > 0:
                print_info(f"[Anti-AFK] #{match_index} | Fires: {fire_count} | Moves: {move_count} | UID: {str(account_id)}")
        except Exception:
            pass


# ============================================================
# play_game
# ============================================================
# RUDP_ACK (Railway variable, optional): answer EVERY reliable packet of the match server (the Clash-Squad bot does this).
# Before, only the first two were answered with fixed values; the server then kept re-sending 'join finished' (cmd 130)
# again and again and never sent the player list / zone / our id.  RUDP_ACK=0 = old behaviour.
RUDP_ACK = os.environ.get("RUDP_ACK", "1").strip().lower() not in ("0", "false", "no", "off")


async def play_game(server_ip_port, thunder, sharma, udp_key, match_code,
                    account_id, player_region, client_version, key, iv,
                    match_index: int, on_match_complete=None, mode_tag: str = "BATTLE_ROYALE"):
    match_start_time = time.time()
    ping_task = None
    anti_afk_task = None
    mv_holder = {"task": None}
    move_stop = asyncio.Event()
    mv_state = None
    try:
        mv_state = MovementState(int(account_id)) if MOVEMENT_ENGINE else None
    except Exception:
        mv_state = None
    mv_tag = f"[MOVE #{match_index}]"
    sock = None
    ping_stop = asyncio.Event()
    anti_afk_stop = asyncio.Event()
    uid_str = str(account_id)
    completed_cleanly = False
    end_reason = "error / handshake"
    thunder_sent = False
    join_match_received = False
    cmd_counts: Dict[Any, int] = {}       # diagnostics: which commands the match server sent (and the last one before it went silent)
    undecoded = 0
    acks_sent = 0
    last_cmd = None
    last_cmd_at = match_start_time

    try:
        ip, port = server_ip_port.split(":")
        port = int(port)
        resolved_ip = await resolve_host_cloudflare(ip)
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('0.0.0.0', 0))
        except Exception:
            pass
        optimize_udp_socket(sock)
        sock.setblocking(False)
        udp_key_bytes = bytes.fromhex(udp_key)
        hello_packet = await build_hello_packet(f"{account_id}_2585", udp_key_bytes, match_code)
        ack_layout = (await layouts_from_mask(match_code))[1]
        await loop.sock_sendto(sock, bytes.fromhex(hello_packet), (resolved_ip, port))
        ack_state = "waiting_for_hello_reply"
        thunder_sent = False
        sharma_sent = False
        join_match_received = False
        local_closed = False
        send_lock = asyncio.Lock()
        anti_afk_started = False
        ping_task = asyncio.create_task(
            keepalive_ping(sock, resolved_ip, port, udp_key_bytes, match_code, ping_stop)
        )
        last_activity = time.time()
        MAX_IDLE_BEFORE_HELLO_RESEND = 7.0
        print_colored(
            f"[MATCH #{match_index}] UDP started -> {server_ip_port} (DNS: {resolved_ip})",
            Colors.MAGENTA
        )

        async def start_anti_afk_once():
            nonlocal anti_afk_task, anti_afk_started
            if anti_afk_started:
                return
            anti_afk_started = True
            anti_afk_task = asyncio.create_task(
                anti_afk_worker(sock, resolved_ip, port, udp_key_bytes, match_code,
                                account_id, anti_afk_stop, match_index,
                                with_moves=(mv_state is None))
            )
            print_success(f"[Anti-AFK] Worker Started | Match #{match_index}")
            if mv_state is not None and mv_holder["task"] is None:
                mv_holder["task"] = asyncio.create_task(
                    mv_movement_loop(sock, (resolved_ip, port), udp_key_bytes, match_code,
                                     mv_state, move_stop, mv_tag, mode_tag)
                )
                print_success(f"{mv_tag} Movement engine started ({mode_tag}, zone mode "
                              f"{ZONE_MODE_BR if mode_tag == 'BATTLE_ROYALE' else ZONE_MODE_LW})")

        async def send_thunder_sharma_inline():
            nonlocal ack_state, thunder_sent, sharma_sent
            if thunder_sent:
                return
            async with send_lock:
                if thunder_sent:
                    return
                try:
                    await loop.sock_sendto(sock, bytes.fromhex(thunder), (resolved_ip, port))
                    thunder_sent = True
                    await asyncio.sleep(0.1)
                    prepare_ack = await build_packet(
                        0x68, (await layouts_from_mask(match_code))[1],
                        0, 2, None, 1, b"\x01\x00", udp_key_bytes
                    )
                    await loop.sock_sendto(sock, prepare_ack, (resolved_ip, port))
                    await asyncio.sleep(0.2)
                    await loop.sock_sendto(sock, bytes.fromhex(sharma), (resolved_ip, port))
                    sharma_sent = True
                    ack_state = "thunder_sharma_sent"
                    print_success(f"[MATCH #{match_index}] Startup packets delivered!")
                    await start_anti_afk_once()
                except Exception as e:
                    print_error(f"[MATCH #{match_index}] send error: {e}")

        while not local_closed:
            if time.time() - match_start_time > MAX_MATCH_DURATION:
                break
            try:
                response, server_addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=1.5)
                if response:
                    last_activity = time.time()
                    frame = await decode_packet(response, udp_key_bytes, match_code)
                    if not frame:
                        undecoded += 1
                    if frame:
                        cmd_counts[frame['cmd']] = cmd_counts.get(frame['cmd'], 0) + 1
                        last_cmd, last_cmd_at = frame['cmd'], time.time()
                        if mv_state is not None:
                            mv_notes: List[str] = []
                            mv_dispatch(frame['cmd'], frame.get('content'), mv_state, mv_tag, mv_notes)
                            for _n in mv_notes:
                                print_info(_n)
                        if RUDP_ACK and frame.get('orderId') is not None and frame.get('msgKey', 255) < 100:
                            try:     # reliable packet -> tell the server we got it (order id + 1), like the Clash-Squad bot
                                await loop.sock_sendto(sock, await build_packet(
                                    0x68, ack_layout, 0, 2, None, 1,
                                    ((frame['orderId'] + 1) & 0xFFFF).to_bytes(2, "little"), udp_key_bytes), server_addr)
                                acks_sent += 1
                            except Exception:
                                pass
                        ptype = await classify(frame)
                        if frame['cmd'] in [103, 107]:
                            print_success(f"[MATCH #{match_index}] Completed (cmd {frame['cmd']})")
                            end_reason = f"server cmd {frame['cmd']}"
                            completed_cleanly = True
                            local_closed = True
                            continue
                        if frame['cmd'] == 101:
                            try:
                                if RUDP_ACK:
                                    continue          # already answered above with the right order id
                                ack_pkt = await build_packet(
                                    0x68, (await layouts_from_mask(match_code))[1],
                                    0, 2, None, 1, b"\x01\x00", udp_key_bytes
                                )
                                await loop.sock_sendto(sock, ack_pkt, server_addr)
                            except Exception:
                                pass
                            continue
                        if ptype in ["ACK", "PING", "HELLO", "JOIN_MATCH"]:
                            if ptype == "HELLO" and ack_state == "waiting_for_hello_reply":
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code, ack_style="short")
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                                ack_state = "ack_sent_waiting"
                            elif ptype == "ACK":
                                if ack_state == "waiting_for_hello_reply":
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        await loop.sock_sendto(sock, reply, server_addr)
                                    ack_state = "ready_to_send_thunder"
                                elif ack_state == "ack_sent_waiting":
                                    ack_state = "ready_to_send_thunder"
                                else:
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        await loop.sock_sendto(sock, reply, server_addr)
                            elif ptype == "PING":
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                            elif ptype == "JOIN_MATCH" and not join_match_received:
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                                    join_match_received = True
            except asyncio.TimeoutError:
                if ack_state == "ready_to_send_thunder" and not thunder_sent:
                    await send_thunder_sharma_inline()
                elif ack_state == "waiting_for_hello_reply":
                    if (time.time() - last_activity) > MAX_IDLE_BEFORE_HELLO_RESEND:
                        try:
                            pkt = await build_hello_packet(f"{account_id}_2585", udp_key_bytes, match_code)
                            await loop.sock_sendto(sock, bytes.fromhex(pkt), (resolved_ip, port))
                        except Exception:
                            pass
                        last_activity = time.time()
                    if (time.time() - match_start_time) > 25.0:
                        print_warning(f"[MATCH #{match_index}] Handshake timeout")
                        break
                elif ack_state == "thunder_sharma_sent":
                    if (time.time() - last_activity) > MATCH_IDLE_TIMEOUT:
                        print_success(f"[MATCH #{match_index}] Finished naturally")
                        end_reason = f"silent for {int(MATCH_IDLE_TIMEOUT)}s (server stopped answering)"
                        completed_cleanly = True
                        break
                continue
            except BlockingIOError:
                await asyncio.sleep(0.05)
            except OSError:
                await asyncio.sleep(0.5)
                continue
            except Exception:
                await asyncio.sleep(0.5)
                continue
            if ack_state == "ready_to_send_thunder" and not thunder_sent:
                await send_thunder_sharma_inline()
        return f"match #{match_index} finished"
    except Exception as e:
        print_error(f"[MATCH #{match_index}] error: {e}")
        return f"match #{match_index} error"
    finally:
        if completed_cleanly:
            try:
                bot_state.increment_match(uid_str)
            except Exception:
                pass
            if on_match_complete:
                try:
                    asyncio.create_task(on_match_complete())
                except Exception as e:
                    print_warning(f"[EXP-REFRESH] post-match trigger failed: {e}")
        move_stop.set()
        _mt = mv_holder["task"]
        if _mt:
            try:
                await asyncio.wait_for(_mt, timeout=2.0)
            except Exception:
                _mt.cancel()
                try:
                    await _mt
                except BaseException:
                    pass
        anti_afk_stop.set()
        if anti_afk_task:
            try:
                await asyncio.wait_for(anti_afk_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                anti_afk_task.cancel()
                try:
                    await anti_afk_task
                except Exception:
                    pass
        ping_stop.set()
        if ping_task:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        print_info(f"[MATCH #{match_index}] RESULT: {end_reason} after {int(time.time() - match_start_time)}s | "
                   f"startup packets sent={thunder_sent} | join-match seen={join_match_received}")
        try:
            top = sorted(cmd_counts.items(), key=lambda kv: -kv[1])[:8]
            if mv_state is not None:
                print_info(f"{mv_tag} ACKs sent={acks_sent} | RESULT: position updates sent={mv_state.sent} | server position seen={mv_state.pos_seen}x | "
                           f"identity={mv_state.id_source or 'no'} | zone={'yes' if mv_state.zone_center else 'no'}")
            print_info(f"[MATCH #{match_index}] SERVER CMDS: {', '.join(f'{c}x{n}' for c, n in top) or 'none'} | "
                       f"last cmd={last_cmd} at {int(last_cmd_at - match_start_time)}s | undecoded={undecoded}")
        except Exception:
            pass
        remaining = await _dec_match(uid_str)
        total = await _get_total_match_count()
        print_info(f"[MATCH #{match_index}] Closed. UID active: {remaining} | Total active: {total}")
        try:
            bot_state.update_status(uid_str, "IN_MATCH" if remaining > 0 else "ONLINE", remaining)
        except Exception:
            pass


# ============================================================
# functional_lone_wolf — WITH MODE SUPPORT
# ============================================================
async def _safe_close(w):
    """Close a TCP writer without ever hanging (a half-dead connection used to block wait_closed forever)."""
    if not w:
        return
    try:
        w.close()
        await asyncio.wait_for(w.wait_closed(), timeout=3)
    except Exception:
        pass


async def functional_lone_wolf(addrs, starter_packet, account_region, client_version,
                                key, iv, account_id="", account_data=None,
                                account_level: int = 2, max_reconnects=10,
                                mode: str = "auto"):
    reconnects = 0
    ip, port = addrs.split(":")
    play_matches: List[asyncio.Task] = []
    no_response_count = 0
    search_attempts = 0
    last_start_time = 0.0
    uid_str = str(account_id)
    current_level = int(account_level or 2)
    consecutive_parse_failures = 0
    current_token = starter_packet
    current_key = key
    current_iv = iv
    current_account_data = account_data

    _MODE = str(mode or "auto").strip().lower()
    if _MODE not in ("auto", "br", "lw"):
        _MODE = "auto"

    def _resolve_mode():
        if _MODE == "br":
            return "BATTLE_ROYALE", BR_MODE_ID, BR_MAP_ID
        if _MODE == "lw":
            return "LONE_WOLF", LONE_WOLF_MODE_ID, LONE_WOLF_MAP_ID
        if current_level >= LONE_WOLF_UNLOCK_LEVEL:
            return "LONE_WOLF", LONE_WOLF_MODE_ID, LONE_WOLF_MAP_ID
        return "BATTLE_ROYALE", BR_MODE_ID, BR_MAP_ID

    async def trigger_post_match_refresh():
        # the server may credit the match a few seconds late (Superfast checks at 5/8/12s) -> check 3 times
        for wait in (EXP_REFRESH_AFTER_MATCH_DELAY, 6.0, 10.0):
            await asyncio.sleep(wait)
            try:
                cd = bot_state.account_credentials.get(uid_str)
                if cd:
                    await refresh_account_profile(cd, log_same=True)
            except Exception as e:
                print_warning(f"[EXP-REFRESH] post-match refresh error: {e}")

    async def cancel_play_matches():
        """Stop every running match of this account (used by PAUSE, so EXP gain really stops)."""
        live = [m for m in play_matches if not m.done()]
        for m in live:
            m.cancel()
        if live:
            await asyncio.gather(*live, return_exceptions=True)
        play_matches.clear()

    try:
        while True:
            writer = None
            gateway_ping_task = None
            try:
                if bot_state.is_deleted(uid_str):
                    raise asyncio.CancelledError()
                if bot_state.is_stopped(uid_str):
                    await cancel_play_matches()
                    print_warning(f"[STOP] UID {uid_str} reached its stop level -> going offline")
                    return
                if bot_state.is_paused(uid_str):
                    # paused: no gateway connection, no StartMatch, no running match
                    await cancel_play_matches()
                    print_warning(f"[PAUSE] UID {uid_str} paused. Waiting...")
                    while bot_state.is_paused(uid_str):
                        if bot_state.is_deleted(uid_str):
                            raise asyncio.CancelledError()
                        try:
                            bot_state.update_status(uid_str, "PAUSED", 0)
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)
                    print_success(f"[RESUME] UID {uid_str} resumed.")
                if current_account_data:
                    fresh = None
                    if current_account_data.get('auth_type') == 'guest' and current_account_data.get('auth_uid'):
                        fresh = cache_get(str(current_account_data['auth_uid']))
                    elif current_account_data.get('auth_type') == 'token' and current_account_data.get('auth_token'):
                        fresh = cache_get(token_key(current_account_data['auth_token']))
                    if fresh:
                        current_account_data = fresh
                        current_key = fresh['aes_ak']
                        current_iv = fresh['iv_i']
                        current_token = await build_tcp_startup_packet(
                            fresh['account_id'], fresh['token'], fresh['server_time'],
                            current_key, current_iv,
                            region=fresh.get('region', account_region), typ='OnLine'
                        )
                        try:
                            lv = int(fresh.get('level', current_level) or current_level)
                            if lv != current_level:
                                print_info(f"[LEVEL] UID {uid_str}: {current_level} -> {lv} (cache)")
                                current_level = lv
                        except Exception:
                            pass
                    else:
                        print_warning(f"[FUNCTIONAL] Cache miss for {uid_str} -> re-login needed")
                        try:
                            if current_account_data.get('auth_uid'):
                                cache_invalidate(str(current_account_data['auth_uid']))
                            if current_account_data.get('auth_token'):
                                cache_invalidate(token_key(current_account_data['auth_token']))
                        except Exception:
                            pass
                        raise ConnectionError("Cache expired, triggering fresh login")
                try:
                    if uid_str in bot_state.accounts:
                        live_lv = int(bot_state.accounts[uid_str].get('level', current_level) or current_level)
                        if live_lv != current_level:
                            print_info(f"[LEVEL] UID {uid_str}: {current_level} -> {live_lv} (live)")
                            current_level = live_lv
                except Exception:
                    pass

                resolved_ip = await resolve_host_cloudflare(ip)
                reader, writer = await asyncio.wait_for(asyncio.open_connection(resolved_ip, int(port)), timeout=12)
                raw_sock = writer.get_extra_info('socket')
                if raw_sock:
                    optimize_tcp_socket(raw_sock)
                writer.write(bytes.fromhex(current_token))
                await asyncio.wait_for(writer.drain(), timeout=8)

                try:
                    init_ka = await send_keep_alive(account_region)
                    if init_ka and writer and not writer.is_closing():
                        writer.write(init_ka)
                        await asyncio.wait_for(writer.drain(), timeout=3)
                except Exception:
                    pass

                async def func_gateway_keepalive():
                    ka_bytes = await send_keep_alive(account_region)
                    while True:
                        await asyncio.sleep(5)
                        try:
                            if writer and not writer.is_closing():
                                writer.write(ka_bytes)
                                await writer.drain()
                        except Exception:
                            break

                gateway_ping_task = asyncio.create_task(func_gateway_keepalive())

                print_success(
                    f"[FUNCTIONAL] TCP Gateway Connected for UID: {uid_str} "
                    f"(DNS: {resolved_ip}, Lvl: {current_level}, Mode: {_MODE})"
                )
                reconnects = 0
                no_response_count = 0
                last_start_time = 0.0
                search_started = asyncio.get_running_loop().time()   # when this search began (stall watchdog)
                stall_limit = SEARCH_STALL_TIMEOUT + random.uniform(0, 30)   # jitter: accounts do not all re-login together

                def _force_fresh_login(reason: str):
                    """Throw the saved session away and log in again (fixes 'EXP stopped growing')."""
                    print_warning(f"[SELF-HEAL] UID {uid_str}: {reason} -> fresh login")
                    if current_account_data:
                        try:
                            if current_account_data.get('auth_uid'):
                                cache_invalidate(str(current_account_data['auth_uid']))
                            if current_account_data.get('auth_token'):
                                cache_invalidate(token_key(current_account_data['auth_token']))
                        except Exception:
                            pass
                    raise ConnectionError("Cache expired, " + reason)

                async def send_start_match():
                    nonlocal search_attempts, last_start_time
                    search_attempts += 1
                    if START_REGION.lower() == "account":
                        current_region = str((current_account_data or {}).get('region') or account_region or "ID").upper()   # the account's own region
                    else:
                        current_region = START_REGION.upper()
                    mode_tag, _mid, _map = _resolve_mode()
                    if mode_tag == "LONE_WOLF":
                        starter_func = start_game_lone_wolf
                    else:
                        starter_func = start_game_battle_royale
                    print_debug(f"[{mode_tag}] Sending StartMatch #{search_attempts} region: {current_region} (Lvl {current_level}, mode={_MODE})")
                    try:
                        await asyncio.sleep(random.uniform(0.2, 0.4))
                        try:                                    # Superfast start: 'ready' packet first, 1 s, then the match search
                            _own = (current_account_data or {}).get('account_id') or uid_str
                            if READY_PACKET and writer and not writer.is_closing():
                                writer.write(build_ready_packet(int(_own), current_key, current_iv))
                                await asyncio.wait_for(writer.drain(), timeout=5)
                        except Exception as _re:
                            print_debug(f"[READY] packet not sent: {_re}")
                        await asyncio.sleep(1.0)
                        await asyncio.wait_for(
                            starter_func(current_region, client_version, writer, current_key, current_iv),
                            timeout=10)
                        print_debug(f"[{mode_tag}] StartMatch packet sent")
                        active = await _get_match_count(uid_str)
                        try:
                            bot_state.update_status(uid_str, f"SEARCHING ({mode_tag})", active)
                        except Exception:
                            pass
                    except asyncio.TimeoutError:
                        raise ConnectionError("StartMatch write timed out (dead connection)")
                    except Exception as e:
                        print_error(f"start_game error: {e}")
                    last_start_time = asyncio.get_running_loop().time()

                if not bot_state.is_paused(uid_str):
                    if not (MAX_PARALLEL_MATCHES and await _get_match_count(uid_str) >= MAX_PARALLEL_MATCHES):
                        await send_start_match()

                while True:
                    if bot_state.is_deleted(uid_str):
                        print_warning(f"[DELETE] UID {uid_str} deleted -> stopping functional loop")
                        raise asyncio.CancelledError()
                    if bot_state.is_stopped(uid_str):
                        await cancel_play_matches()
                        print_warning(f"[STOP] UID {uid_str} reached its stop level -> going offline")
                        return
                    play_matches[:] = [m for m in play_matches if not m.done()]
                    active_count = await _get_match_count(uid_str)
                    try:
                        bot_state.update_status(uid_str, "ONLINE" if active_count == 0 else "IN_MATCH", active_count)
                    except Exception:
                        pass

                    if bot_state.is_paused(uid_str):
                        await cancel_play_matches()      # a match that is already running is stopped too
                        search_started = asyncio.get_running_loop().time()   # a pause is not a stall
                        try:
                            bot_state.update_status(uid_str, "PAUSED", 0)
                        except Exception:
                            pass
                        if not hasattr(send_start_match, "_pause_logged"):
                            print_warning(f"[PAUSE] UID {uid_str} paused. Waiting...")
                            send_start_match._pause_logged = True
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        if getattr(send_start_match, "_pause_logged", False):
                            print_success(f"[RESUME] UID {uid_str} resumed.")
                            send_start_match._pause_logged = False

                    now = asyncio.get_running_loop().time()
                    # ---- self-heal: old session / search that never finds a match ----
                    if SEARCH_STALL_TIMEOUT > 0 and now - search_started >= stall_limit:
                        _force_fresh_login(f"no match for {int(SEARCH_STALL_TIMEOUT)}s")
                    try:
                        _cached_at = float((current_account_data or {}).get('cached_at') or 0)
                    except Exception:
                        _cached_at = 0.0
                    if _cached_at and time.time() - _cached_at > TOKEN_CACHE_TTL + 60:
                        _force_fresh_login("session is older than the allowed age")
                    if MAX_PARALLEL_MATCHES and active_count >= MAX_PARALLEL_MATCHES:
                        search_started = now                 # waiting for a free slot is not a stalled search
                        no_response_count = 0                # ... and a quiet gateway is expected meanwhile
                    elif now - last_start_time >= START_MATCH_INTERVAL:
                        await send_start_match()

                    try:
                        data = await asyncio.wait_for(reader.read(8192), timeout=1.0)
                    except asyncio.TimeoutError:
                        no_response_count += 1
                        if no_response_count > 40:
                            print_warning(f"[FUNCTIONAL] Gateway silent ({uid_str}). Reconnecting...")
                            raise ConnectionError("Gateway idle timeout")
                        continue

                    if not data:
                        raise ConnectionError("Connection closed by server")

                    hex_data = data.hex()
                    packet_length = len(data)
                    no_response_count = 0

                    if hex_data.startswith("0300") and 10 < packet_length < 30:
                        print_debug("Match starting, please wait...")
                        continue

                    if hex_data.startswith("0300") and packet_length >= 300:
                        print_colored("=" * 60, Colors.GREEN)
                        print_colored(f"MATCH FOUND! Loading...", Colors.GREEN)
                        print_colored("=" * 60, Colors.GREEN)
                        try:
                            res = json.loads(await decode_protobuf(hex_data[10:]))
                            token = None
                            udp_key = None
                            match_code = None
                            server_ip_port = None
                            match_account_id = None
                            block_val = None
                            if '42' in res and 'data' in res['42']:
                                match_code = res['42']['data']
                            if '5' in res and 'data' in res['5']:
                                res_field5 = res['5']['data']
                                server_ip_port = res_field5.get('2', {}).get('data')
                                udp_key = res_field5.get('3', {}).get('data')
                                token = res_field5.get('4', {}).get('data')
                                if '42' in res_field5:
                                    match_code = res_field5['42']['data']
                            if '1' in res and 'data' in res['1']:
                                match_account_id = res['1']['data']
                            if '5' in res and 'data' in res['5']:
                                block_val = res['5']['data'].get('1', {}).get('data')
                            effective_acc_id = match_account_id or account_id or "BD_BOT"
                            if token and udp_key and match_code and server_ip_port:
                                acc_tok = ""
                                if current_account_data:
                                    acc_tok = current_account_data.get('access_token', '') or ""

                                mode_tag, selected_mode_id, selected_map_id = _resolve_mode()
                                print_info(f"[MATCH] Mode: {mode_tag} | mode_id={selected_mode_id} | map_id={selected_map_id} | UID: {uid_str}")

                                thunder, sharma = await build_match_startup_packets(
                                    token, udp_key, match_code, effective_acc_id, block_val or 0,
                                    server_ip=server_ip_port, region=account_region,
                                    client_version=client_version, access_token=acc_tok,
                                    mode_id=selected_mode_id, map_id=selected_map_id
                                )
                                match_index = await _inc_match(uid_str)
                                total = await _get_total_match_count()
                                print_colored(f"[MATCH #{match_index}] UDP starting ({mode_tag}) -> {server_ip_port}", Colors.CYAN)
                                print_success(f"[FUNCTIONAL] UDP task started. UID active: {match_index} | Total: {total}")
                                new_match = asyncio.create_task(
                                    play_game(
                                        server_ip_port, thunder, sharma, udp_key, match_code,
                                        effective_acc_id, "ID", client_version, current_key, current_iv,
                                        match_index=match_index, on_match_complete=trigger_post_match_refresh,
                                        mode_tag=mode_tag
                                    )
                                )
                                play_matches.append(new_match)
                                consecutive_parse_failures = 0
                                await _safe_close(writer)
                                print_info(f"[OFFLINE] {NEW_MATCH_DELAY}s offline -> reload token -> new StartMatch")
                                await asyncio.sleep(NEW_MATCH_DELAY)
                                reconnects = 0
                                break
                            else:
                                consecutive_parse_failures += 1
                                print_warning(f"[FUNCTIONAL] Non-match big packet (#{consecutive_parse_failures}/{MAX_CONSECUTIVE_PARSE_FAILURES}) -> reconnecting | {_packet_info(res, packet_length)}")
                                if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                                    print_error(f"[FUNCTIONAL] {MAX_CONSECUTIVE_PARSE_FAILURES}x parse failures -> invalidating cache for fresh login")
                                    if current_account_data:
                                        try:
                                            if current_account_data.get('auth_uid'):
                                                cache_invalidate(str(current_account_data['auth_uid']))
                                            if current_account_data.get('auth_token'):
                                                cache_invalidate(token_key(current_account_data['auth_token']))
                                        except Exception:
                                            pass
                                    consecutive_parse_failures = 0
                                await _safe_close(writer)
                                await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                                break
                        except Exception as e:
                            print_error(f"[FUNCTIONAL] Match packet error: {e}")
                            consecutive_parse_failures += 1
                            if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                                if current_account_data:
                                    try:
                                        if current_account_data.get('auth_uid'):
                                            cache_invalidate(str(current_account_data['auth_uid']))
                                        if current_account_data.get('auth_token'):
                                            cache_invalidate(token_key(current_account_data['auth_token']))
                                    except Exception:
                                        pass
                                consecutive_parse_failures = 0
                            await _safe_close(writer)
                            await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                            break

                    if 30 <= packet_length <= 40:
                        continue

            except asyncio.CancelledError:
                print_warning(f"[FUNCTIONAL] Cancelled — cancelling {len(play_matches)} UDP matches")
                for m in play_matches:
                    if not m.done():
                        m.cancel()
                if play_matches:
                    await asyncio.gather(*play_matches, return_exceptions=True)
                play_matches.clear()
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                raise
            except Exception as e:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                if "Cache expired" in str(e):
                    print_warning(f"[FUNCTIONAL] ({uid_str}) {e}")
                else:
                    print_error(f"[FUNCTIONAL] TCP state ({uid_str}): {e}")
                play_matches[:] = [m for m in play_matches if not m.done()]
                if writer:
                    await _safe_close(writer)
                if "Cache expired" in str(e):
                    print_warning(f"[FUNCTIONAL] Triggering re-login for {uid_str}")
                    break
                reconnects += 1
                if reconnects > max_reconnects:
                    print_error("[FUNCTIONAL] Max reconnects reached, retrying...")
                    reconnects = 0
                    await asyncio.sleep(3)
                    continue
                await asyncio.sleep(min(reconnects, 2))
            finally:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                if writer:
                    await _safe_close(writer)

    except asyncio.CancelledError:
        print_warning(f"[FUNCTIONAL] Outer cancelled. {len(play_matches)} UDP matches still running.")
        for m in play_matches:
            if not m.done():
                m.cancel()
        if play_matches:
            await asyncio.gather(*play_matches, return_exceptions=True)
        play_matches.clear()
        raise


async def informational(addrs, starter_packet, key, iv, region="ID", max_reconnects=3):
    reconnects = 0
    ip, port = addrs.split(":")
    while True:
        writer = None
        ping_task = None
        try:
            resolved_ip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(resolved_ip, int(port))
            raw_sock = writer.get_extra_info('socket')
            if raw_sock:
                optimize_tcp_socket(raw_sock)
            writer.write(bytes.fromhex(starter_packet))
            await writer.drain()
            reconnects = 0
            try:
                init_ka = await send_keep_alive(region)
                if init_ka and writer and not writer.is_closing():
                    writer.write(init_ka)
                    await asyncio.wait_for(writer.drain(), timeout=3)
            except Exception:
                pass

            async def info_keepalive():
                ka_bytes = await send_keep_alive(region)
                while True:
                    await asyncio.sleep(5)
                    try:
                        if writer and not writer.is_closing():
                            writer.write(ka_bytes)
                            await writer.drain()
                    except Exception:
                        break
            ping_task = asyncio.create_task(info_keepalive())
            while True:
                data = await reader.read(8192)
                if not data:
                    raise ConnectionError("Connection closed")
        except asyncio.CancelledError:
            if ping_task:
                ping_task.cancel()
            if writer:
                await _safe_close(writer)
            raise
        except Exception:
            if ping_task:
                ping_task.cancel()
            if writer:
                await _safe_close(writer)
            reconnects += 1
            if reconnects > max_reconnects:
                await asyncio.sleep(3)
                reconnects = 0
            else:
                await asyncio.sleep(1)


# ==================== ACCOUNT PROCESSORS ====================
def _register_credentials(account_data: Dict):
    try:
        acc_id = str(account_data['account_id'])
        bot_state.account_credentials[acc_id] = account_data
        if account_data.get('auth_uid'):
            bot_state.account_credentials[str(account_data['auth_uid'])] = account_data
        if account_data.get('auth_token'):
            bot_state.account_credentials[token_key(account_data['auth_token'])] = account_data
    except Exception:
        pass


async def get_player_personal_show(target_uid, token, server_url, release_version):
    """Superfast.zip's way to read level / exp: -> {'level', 'exp', 'nickname'} or None. Used only as a BACKUP when
    GetLoginData gives no level / exp."""
    try:
        base = str(server_url or "").rstrip("/")
        if not base:
            return None
        payload = encrypt_api_bytes(build_proto({1: int(target_uid)}))
        hdrs = {
            "authorization": f"Bearer {token}",
            "host": urlparse(base).hostname or "",
            "x-ga": "v1 1",
            "content-type": "application/x-www-form-urlencoded",
            "releaseversion": str(release_version),
            "user-agent": "UnityPlayer/2022.3.47f1",
            "accept": "*/*",
            "x-unity-version": "2022.3.47f1",
        }
        r = await client.post(f"{base}/GetPlayerPersonalShow", headers=hdrs, content=payload,
                              timeout=httpx.Timeout(15.0, connect=12.0))
        if r.status_code != 200 or not r.content:
            _warn_limited("ps_http", f"[EXP-REFRESH] GetPlayerPersonalShow: HTTP {r.status_code}, {len(r.content or b'')} bytes | url={base}", 120.0)
            return None
        parsed = await parse_results(Parser().parse(r.content.hex()))
        inner = get_proto_field(parsed, 1)
        if not isinstance(inner, dict):
            _warn_limited("ps_shape", f"[EXP-REFRESH] GetPlayerPersonalShow: unexpected answer, top fields={sorted(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__}", 120.0)
            return None
        exp = get_proto_field(inner, 7)
        if exp is None:
            _warn_limited("ps_noexp", f"[EXP-REFRESH] GetPlayerPersonalShow: no exp field, fields={sorted(inner.keys())}", 120.0)
            return None
        level = get_proto_field(inner, 6)
        return {"level": int(level) if level else 1, "exp": int(exp), "nickname": str(get_proto_field(inner, 3, "") or "")}
    except Exception as e:
        _warn_limited("ps_exc", f"[EXP-REFRESH] GetPlayerPersonalShow failed: {type(e).__name__}: {e}", 120.0)
        return None


async def refresh_account_profile(account_data_or_uid: Any, log_same: bool = False):
    try:
        if isinstance(account_data_or_uid, str):
            uid = str(account_data_or_uid)
            account_data = bot_state.account_credentials.get(uid)
        else:
            account_data = account_data_or_uid
            uid = str(account_data.get('account_id')) if account_data else "?"
        if not account_data:
            print_warning(f"[EXP-REFRESH] No credentials for UID {uid}")
            return
        url = account_data.get('server_url')
        token = account_data.get('token')
        release_version = account_data.get('release_version')
        payload = account_data.get('login_payload_data')
        if not (url and token and release_version and payload):
            print_warning(f"[EXP-REFRESH] Missing fields for {uid}")
            return
        res = None
        try:
            async with _refresh_sem:
                res = await send_getlogin(payload, url, token, release_version,
                                          style=account_data.get('login_style') or 'legacy')
        except Exception as e:
            print_error(f"[EXP-REFRESH] send_getlogin exception for {uid}: {e}")
            return
        res_proto, dict_res = (res if res else (None, {}))
        if not res:
            _warn_limited(f"exp_none_{uid}", f"[EXP-REFRESH] GetLoginData returned None for {uid} -> trying GetPlayerPersonalShow", 120.0)

        def _safe_int(v, default):
            try:
                if v is None or v == "":
                    return default
                return int(v)
            except Exception:
                return default

        level = _safe_int(get_proto_field(dict_res, 6, None), None)
        exp = _safe_int(get_proto_field(dict_res, 7, None), None)
        likes = _safe_int(get_proto_field(dict_res, 8, None), None)
        nickname = ""
        try:
            nickname = (res_proto.nickname if res_proto is not None else "") or get_proto_field(dict_res, 4, "") or ""
        except Exception:
            nickname = ""
        if level is None and exp is None:                           # backup: Superfast's GetPlayerPersonalShow
            async with _refresh_sem:
                ps = await get_player_personal_show(account_data.get('account_id'), token, url, release_version)
            if ps:
                level, exp = ps["level"], ps["exp"]
                nickname = nickname or ps["nickname"]
        if level is None and exp is None:
            print_warning(f"[EXP-REFRESH] No level/exp field parsed for {uid}")
            return
        level = level if level is not None else 1
        exp = exp if exp is not None else 0
        likes = likes if likes is not None else 0
        acc_id = str(account_data['account_id'])
        old_level = 0
        old_exp = 0
        try:
            if acc_id in bot_state.accounts:
                old_level = int(bot_state.accounts[acc_id].get('level', 0) or 0)
                old_exp = int(bot_state.accounts[acc_id].get('current_exp', 0) or 0)
        except Exception:
            pass
        try:
            bot_state.update_exp(acc_id, exp, level)
        except Exception as e:
            print_error(f"[EXP-REFRESH] update_exp failed for {acc_id}: {e}")
        try:
            stop_lv = int((bot_state.accounts.get(acc_id) or {}).get('stop_level') or 0)
            if stop_lv and level >= stop_lv and not bot_state.is_stopped(acc_id):
                bot_state.mark_stopped(acc_id, level)
                print_warning(f"[STOP] UID {acc_id}: level {level} reached the stop level {stop_lv} -> stopping (not deleting)")
        except Exception as e:
            print_warning(f"[STOP] check failed for {acc_id}: {e}")
        if likes > 0 and acc_id in bot_state.accounts:
            try:
                bot_state.accounts[acc_id]["likes"] = likes
            except Exception:
                pass
        if nickname and acc_id in bot_state.accounts:
            try:
                bot_state.accounts[acc_id]["nickname"] = nickname
            except Exception:
                pass
        _update_cache_profile(account_data, level, exp)
        account_data['level'] = level
        account_data['exp'] = exp
        if likes > 0:
            account_data['likes'] = likes
        if nickname:
            account_data['nickname'] = nickname
        if old_level > 0 and level > old_level:
            print_success(f"[LEVEL UP] UID {acc_id} : Lvl {old_level} -> {level} (EXP {old_exp} -> {exp})")
        elif old_exp != exp:
            print_success(f"[EXP] UID {acc_id} Lvl {level} | EXP {old_exp} -> {exp} (+{exp - old_exp})")
        elif log_same:
            print_info(f"[EXP-CHECK] UID {acc_id} Lvl {level} | EXP {exp} (server shows no change)")
    except Exception as e:
        print_error(f"refresh_account_profile error: {e}")
        traceback.print_exc()


# ==================== LEVEL RULES (max level to add / auto-stop level) ====================
def _gate_level(login_key, level: int):
    """Decide what to do with an account that just logged in.
    'ok' = run it, 'reject' = level is above the max level to add -> never added, 'stop' = already at its stop level."""
    lim = bot_state.account_limits.get(str(login_key))
    if not lim:
        return "ok", ""
    mx = int(lim.get("max") or 0)
    hi = int(lim.get("stop") or 0)
    if lim.get("accepted"):                       # accepted before (e.g. after a restart): only the stop rule matters
        if hi and level >= hi:
            return "stop", f"reached stop level {hi}"
        return "ok", ""
    if mx and level > mx:                         # max 21 -> level 21 or lower can be added
        return "reject", f"Level {level} is too high: max level {mx} (only level {mx} or lower can be added)"
    if hi and level >= hi:
        return "reject", f"Level {level} has already reached the stop level {hi}"
    return "ok", ""


def _limit_kwargs(login_key) -> Dict:
    lim = bot_state.account_limits.get(str(login_key)) or {}
    return {"stop_level": int(lim.get("stop") or 0), "max_level": int(lim.get("max") or 0)}


def _persist_accepted(login_key):
    try:
        entries = _read_accounts()
        changed = False
        for e in entries:
            k = str(e.get("uid")) if e.get("uid") else token_key(e.get("token", ""))
            if k == str(login_key) and not e.get("accepted"):
                e["accepted"] = True
                changed = True
        if changed:
            _write_accounts(entries)
    except Exception as ex:
        print_warning(f"[LEVEL] could not save accepted flag: {ex}")


def _finish_gate(login_key, acc_id, level: int, gate: str):
    """Called right after the account was registered."""
    lim = bot_state.account_limits.get(str(login_key))
    if not lim:
        return
    if gate == "stop":
        bot_state.mark_stopped(acc_id, level)
    elif not lim.get("accepted"):
        lim["accepted"] = True
        _persist_accepted(login_key)


async def _reject_login(login_key, display_uid, owner_id, reason: str):
    """The account's level is outside the allowed range: remove it completely, as if it was never added."""
    ids = {str(login_key), str(display_uid)}
    print_warning(f"[LEVEL] {display_uid}: {reason} -> NOT added")
    try:
        _write_accounts([e for e in _read_accounts() if not (_entry_ids(e) & ids)])
    except Exception as ex:
        print_warning(f"[LEVEL] could not clean accounts file: {ex}")
    bot_state.mark_deleted(ids)
    bot_state.account_workers.pop(str(login_key), None)
    bot_state.account_limits.pop(str(login_key), None)
    bot_state.reject_account(login_key, display_uid, owner_id, reason)
    cb = bot_state.refresh_callbacks.get("on_account_deleted")
    if cb:
        try:
            await cb(ids)
        except Exception as ex:
            print_warning(f"[LEVEL] cleanup failed: {ex}")


async def process_account_uid_pass(uid: str, password: str,
                                    owner_user_id: str = "", owner_username: str = "",
                                    mode: str = "auto") -> Optional[Dict]:
    cached = cache_get(uid)
    if cached:
        print_success(f"[CACHE HIT] UID {uid} loaded from token_cache.json (no login)")
        acc_id = str(cached['account_id'])
        c_level = int(cached.get('level', 1) or 1)
        gate, why = _gate_level(uid, c_level)
        if gate == "reject":
            return {"_gate": "reject", "_reason": why}
        bot_state.register_account(
            uid=acc_id, nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'ID'), level=c_level,
            exp=cached.get('exp', 0), likes=cached.get('likes', 0),
            mode=mode, owner_user_id=owner_user_id, owner_username=owner_username,
            **_limit_kwargs(uid)
        )
        _register_credentials(cached)
        _finish_gate(uid, acc_id, c_level, gate)
        return dict(cached, _gate="stop") if gate == "stop" else cached
    print_info(f"[LOGIN] Full login for UID {uid}...")
    try:
        verconfig_res = await version_config()
        if verconfig_res is None:
            print_error(f"[LOGIN] version_config failed for UID {uid}")
            return None
        release_version, client_version, server_url = verconfig_res
        tokengrant_response = await get_access_token(uid, password)
        if tokengrant_response is None:
            print_error(f"[LOGIN] get_access_token failed for UID {uid}")
            return None
        open_id, access_token, platform = tokengrant_response
        device_info = get_device_for_account(uid)
        seq = await login_sequence(open_id, access_token, platform, client_version, device_info,
                                   release_version, server_url, tag=f"UID {uid}")
        if seq is None:
            print_error(f"[LOGIN] MajorLogin / GetLoginData failed for UID {uid}")
            return None
        majorlogin_response, getlogin_result, login_payload_data, login_style = seq
        release_version = majorlogin_response.release_version or release_version      # 'super' login: versions it really used
        client_version = majorlogin_response.client_version or client_version
        res_proto, dict_res = getlogin_result
        acc_id = str(majorlogin_response.account_id)
        level = int(get_proto_field(dict_res, 6, 1) or 1)
        exp = int(get_proto_field(dict_res, 7, 0) or 0)
        likes = int(get_proto_field(dict_res, 8, 0) or 0)
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{acc_id}")
        region = majorlogin_response.region or get_proto_field(dict_res, 3, "ID")
        gate, why = _gate_level(uid, level)
        if gate == "reject":
            return {"_gate": "reject", "_reason": why}
        bot_state.register_account(
            uid=acc_id, nickname=nickname, region=region, level=level, exp=exp, likes=likes,
            mode=mode, owner_user_id=owner_user_id, owner_username=owner_username,
            **_limit_kwargs(uid)
        )
        mode_label = _mode_label(mode, level)
        print_success(f"[OK] Login Success: UID {acc_id} | {nickname} | Lvl {level} | Mode: {mode_label}")
        account_data = {
            'account_id': majorlogin_response.account_id, 'nickname': nickname,
            'region': region, 'level': level, 'exp': exp, 'likes': likes,
            'open_id': open_id, 'access_token': access_token, 'platform': str(platform),
            'token': majorlogin_response.token, 'server_time': majorlogin_response.server_time,
            'aes_ak': majorlogin_response.aes_ak, 'iv_i': majorlogin_response.iv_i,
            'functional_addrs': res_proto.functional_addrs or get_proto_field(dict_res, 14),
            'informational_addrs': res_proto.informational_addrs or get_proto_field(dict_res, 32),
            'release_version': release_version, 'client_version': client_version,
            'server_url': majorlogin_response.url, 'login_payload_data': login_payload_data,
            'login_style': login_style,
            'auth_type': 'guest', 'auth_uid': uid, 'auth_password': password,
            'mode': mode, 'owner_user_id': owner_user_id, 'owner_username': owner_username,
        }
        _register_credentials(account_data)
        cache_set(uid, account_data)
        _finish_gate(uid, acc_id, level, gate)
        return dict(account_data, _gate="stop") if gate == "stop" else account_data
    except Exception as e:
        print_error(f"process_account_uid_pass error: {e}")
        traceback.print_exc()
        return None


async def process_account_token(access_token: str,
                                 owner_user_id: str = "", owner_username: str = "",
                                 mode: str = "auto") -> Optional[Dict]:
    cache_key = token_key(access_token)
    cached = cache_get(cache_key)
    if cached:
        print_success(f"[CACHE HIT] Token {access_token[:10]}... loaded from cache")
        acc_id = str(cached['account_id'])
        c_level = int(cached.get('level', 1) or 1)
        gate, why = _gate_level(cache_key, c_level)
        if gate == "reject":
            return {"_gate": "reject", "_reason": why}
        bot_state.register_account(
            uid=acc_id, nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'ID'), level=c_level,
            exp=cached.get('exp', 0), likes=cached.get('likes', 0),
            mode=mode, owner_user_id=owner_user_id, owner_username=owner_username,
            **_limit_kwargs(cache_key)
        )
        _register_credentials(cached)
        _finish_gate(cache_key, acc_id, c_level, gate)
        return dict(cached, _gate="stop") if gate == "stop" else cached
    print_info("[LOGIN] Full login with Access Token...")
    try:
        verconfig_res = await version_config()
        if verconfig_res is None:
            print_error("[LOGIN] version_config failed (token flow)")
            return None
        release_version, client_version, server_url = verconfig_res
        import requests
        data = {"error": "no answer"}
        for _base in TOKEN_INSPECT_URLS:
            _host = urlparse(_base).hostname
            hdrs = {
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "close",
                "Content-Type": "application/x-www-form-urlencoded",
                "Host": _host,
                "User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 9;en;US;)"
            }
            try:
                resp = await asyncio.to_thread(requests.get, f"{_base}?token={access_token}", headers=hdrs, timeout=10)
                data = resp.json()
            except Exception as _e:
                data = {"error": f"{type(_e).__name__}"}
            if isinstance(data, dict) and 'error' not in data:
                break
        if 'error' in data:
            print_error(f"[LOGIN] Token inspect error: {data.get('error')}")
            return None
        open_id = data.get('open_id')
        platform = data.get('platform', 4)
        if not open_id:
            print_error("[LOGIN] No open_id in token inspect response")
            return None
        device_info = get_device_for_account(open_id)
        seq = await login_sequence(open_id, access_token, str(platform), client_version, device_info,
                                   release_version, server_url, tag="token flow")
        if seq is None:
            print_error("[LOGIN] MajorLogin / GetLoginData failed (token flow)")
            return None
        majorlogin_response, getlogin_result, login_payload_data, login_style = seq
        release_version = majorlogin_response.release_version or release_version      # 'super' login: versions it really used
        client_version = majorlogin_response.client_version or client_version
        res_proto, dict_res = getlogin_result
        acc_id = str(majorlogin_response.account_id)
        level = int(get_proto_field(dict_res, 6, 1) or 1)
        exp = int(get_proto_field(dict_res, 7, 0) or 0)
        likes = int(get_proto_field(dict_res, 8, 0) or 0)
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{acc_id}")
        region = majorlogin_response.region or get_proto_field(dict_res, 3, "ID")
        gate, why = _gate_level(cache_key, level)
        if gate == "reject":
            return {"_gate": "reject", "_reason": why}
        bot_state.register_account(
            uid=acc_id, nickname=nickname, region=region, level=level, exp=exp, likes=likes,
            mode=mode, owner_user_id=owner_user_id, owner_username=owner_username,
            **_limit_kwargs(cache_key)
        )
        mode_label = _mode_label(mode, level)
        print_success(f"[OK] Login Success (Token): UID {acc_id} | {nickname} | Lvl {level} | Mode: {mode_label}")
        account_data = {
            'account_id': majorlogin_response.account_id, 'nickname': nickname,
            'region': region, 'level': level, 'exp': exp, 'likes': likes,
            'open_id': open_id, 'access_token': access_token, 'platform': str(platform),
            'token': majorlogin_response.token, 'server_time': majorlogin_response.server_time,
            'aes_ak': majorlogin_response.aes_ak, 'iv_i': majorlogin_response.iv_i,
            'functional_addrs': res_proto.functional_addrs or get_proto_field(dict_res, 14),
            'informational_addrs': res_proto.informational_addrs or get_proto_field(dict_res, 32),
            'release_version': release_version, 'client_version': client_version,
            'server_url': majorlogin_response.url, 'login_payload_data': login_payload_data,
            'login_style': login_style,
            'auth_type': 'token', 'auth_token': access_token,
            'mode': mode, 'owner_user_id': owner_user_id, 'owner_username': owner_username,
        }
        _register_credentials(account_data)
        cache_set(cache_key, account_data)
        _finish_gate(cache_key, acc_id, level, gate)
        return dict(account_data, _gate="stop") if gate == "stop" else account_data
    except Exception as e:
        print_error(f"process_account_token error: {e}")
        traceback.print_exc()
        return None


def _mode_label(mode: str, level: int) -> str:
    m = str(mode or "auto").lower()
    if m == "br":
        return "Battle Royale (locked)"
    if m == "lw":
        return "Lone Wolf (locked)"
    return "Lone Wolf" if level >= LONE_WOLF_UNLOCK_LEVEL else "Battle Royale"


def _worker_ids(account_data: Dict) -> List[str]:
    tok = account_data.get('auth_token') or ''
    return [str(x) for x in (account_data.get('account_id'), account_data.get('auth_uid'), tok, token_key(tok) if tok else '') if x]


async def _wait_until_allowed(owner_id: str, *ids) -> bool:
    """Block while the owner's plan is expired / the account is over its slot limit.
    Returns False if the account was deleted meanwhile."""
    announced = False
    while bot_state.is_blocked(owner_id, *ids):
        if bot_state.is_deleted(*ids):
            return False
        if not announced:
            print_warning(f"[PLAN] {ids[0]} stopped: plan expired or over slot limit. Waiting for renewal...")
            announced = True
        try:
            bot_state.update_status(str(ids[0]), "EXPIRED", 0)
        except Exception:
            pass
        await asyncio.sleep(PLAN_GUARD_INTERVAL * 2)
    if announced:
        print_success(f"[PLAN] {ids[0]} allowed again -> starting")
    return True


# ==================== HTTP KEEP-ALIVE PING (from Superfast.zip) ====================
ENABLE_HTTP_PING = os.environ.get("HTTP_PING", "1").strip().lower() not in ("0", "false", "no", "off")
PING_INTERVAL = 20.0
PING_TIMEOUT = 8.0
PING_REAL_IP = "103.109.214.211"
_ping_sem = asyncio.Semaphore(30)          # with hundreds of accounts the pings never flood the connection pool


async def send_ping(account_id, token, release_version, ping_url) -> bool:
    try:
        try:
            acc = int(account_id)
        except Exception:
            acc = 0
        now_ms = int(time.time() * 1000)
        payload = struct.pack("<Q", acc & 0xFFFFFFFFFFFFFFFF) + struct.pack("<Q", now_ms & 0xFFFFFFFFFFFFFFFF)
        hdrs = {
            'accept': '*/*', 'accept-encoding': 'deflate, gzip',
            'authorization': f'Bearer {token}',
            'content-type': 'application/x-www-form-urlencoded',
            'releaseversion': str(release_version),
            'user-agent': 'UnityPlayer/2018.4.12f1',
            'x-ga': 'v1 1', 'x-ga-sv': str(int(time.time())),
            'x-real-ip': PING_REAL_IP, 'x-request-start': str(now_ms),
            'x-unity-version': '2018.4.12f1',
        }
        async with _ping_sem:
            r = await client.post(ping_url, headers=hdrs, content=payload, timeout=PING_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


async def ping_worker(acc_id: str, owner_id: str = ""):
    """Pings <server_url>/Ping every ~20 s with the account's CURRENT token (read again each time, so a re-login is
    picked up). Never raises, never blocks the game; pauses while the account is paused / stopped."""
    ok_count = 0
    fails = 0
    try:
        await asyncio.sleep(5.0 + random.uniform(0, 2.0))
        while True:
            if bot_state.is_deleted(acc_id):
                return
            if not (bot_state.is_paused(acc_id) or bot_state.is_stopped(acc_id)):
                cd = bot_state.account_credentials.get(acc_id) or {}
                base = str(cd.get('server_url') or "").rstrip("/")
                if base and cd.get('token') and cd.get('release_version'):
                    ok = await send_ping(acc_id, cd['token'], cd['release_version'], base + "/Ping")
                    if ok:
                        ok_count += 1
                        fails = 0
                    else:
                        fails += 1
                        if fails <= 3:
                            _warn_limited(f"ping_{acc_id}", f"[PING] {acc_id} failed (#{fails})", 120.0)
            await asyncio.sleep(max(5.0, PING_INTERVAL + random.uniform(-3.0, 3.0)))
    except asyncio.CancelledError:
        pass


async def run_account_worker(account_data: Dict, label: str, mode: str = "auto", owner_user_id: str = ""):
    acc_id = str(account_data['account_id'])
    owner_id = str(owner_user_id or account_data.get('owner_user_id') or "")
    informational_task = None
    exp_task = None
    functional_task = None
    guard_task = None
    ping_task = None
    try:
        reg = account_data.get('region', 'ID')
        tcp_packet_online = await build_tcp_startup_packet(
            account_data['account_id'], account_data['token'], account_data['server_time'],
            account_data['aes_ak'], account_data['iv_i'], region=reg, typ='OnLine'
        )
        tcp_packet_chat = await build_tcp_startup_packet(
            account_data['account_id'], account_data['token'], account_data['server_time'],
            account_data['aes_ak'], account_data['iv_i'], region=reg, typ='ChaT'
        )
        informational_task = asyncio.create_task(
            informational(account_data['informational_addrs'], tcp_packet_chat,
                          account_data['aes_ak'], account_data['iv_i'], region=reg)
        )

        def _next_exp_delay() -> float:
            # few accounts -> every 12s; hundreds of accounts -> slower (max 45s) + random spread
            n = max(1, len(bot_state.accounts))
            base = max(EXP_REFRESH_INTERVAL, min(EXP_REFRESH_MAX_INTERVAL, n * 0.12))
            return base + random.uniform(0, base * 0.25)

        async def exp_refresher():
            try:
                fresh = bot_state.account_credentials.get(acc_id)
                if fresh:
                    await refresh_account_profile(fresh)
            except Exception as e:
                print_warning(f"[EXP-REFRESH] initial refresh error ({acc_id}): {e}")
            while True:
                await asyncio.sleep(_next_exp_delay())
                if bot_state.is_deleted(acc_id):
                    return
                try:
                    fresh = bot_state.account_credentials.get(acc_id)
                    if fresh:
                        await refresh_account_profile(fresh)
                except Exception as e:
                    print_warning(f"[EXP-REFRESH] periodic refresh error ({acc_id}): {e}")

        async def plan_guard():
            while True:
                await asyncio.sleep(PLAN_GUARD_INTERVAL)
                if bot_state.is_blocked(owner_id, *_worker_ids(account_data)):
                    return True

        exp_task = asyncio.create_task(exp_refresher())
        functional_task = asyncio.create_task(
            functional_lone_wolf(
                account_data['functional_addrs'], tcp_packet_online,
                account_data['region'], account_data['client_version'],
                account_data['aes_ak'], account_data['iv_i'],
                account_id=acc_id, account_data=account_data,
                account_level=int(account_data.get('level', 2) or 2),
                mode=mode
            )
        )
        guard_task = asyncio.create_task(plan_guard())
        if ENABLE_HTTP_PING:
            ping_task = asyncio.create_task(ping_worker(acc_id, owner_id))
        done, _pending = await asyncio.wait({functional_task, guard_task}, return_when=asyncio.FIRST_COMPLETED)
        if guard_task in done and not functional_task.done():
            print_warning(f"[PLAN] {acc_id} stopped: plan expired or over slot limit")
            functional_task.cancel()
            try:
                await functional_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                bot_state.update_status(acc_id, "EXPIRED", 0)
            except Exception:
                pass
        else:
            await functional_task          # re-raises errors exactly like before
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print_error(f"run_account_worker error for {label}: {e}")
    finally:
        tasks = (informational_task, exp_task, functional_task, guard_task, ping_task)
        for t in tasks:
            if t and not t.done():
                t.cancel()
        for t in tasks:
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


async def account_loop_guest(uid: str, password: str, mode: str = "auto",
                              owner_user_id: str = "", owner_username: str = ""):
    fail_count = 0
    bot_state.add_pending(uid, uid, owner_user_id, owner_username, mode)    # visible at once as "CONNECTING"
    await asyncio.sleep(random.uniform(0, 2.0))          # spread the start-up of many accounts
    while True:
        if bot_state.is_deleted(uid):
            print_warning(f"[DELETE] Guest UID {uid} deleted -> worker exit")
            break
        try:
            if not await _wait_until_allowed(owner_user_id, uid):
                print_warning(f"[DELETE] Guest UID {uid} deleted while waiting -> worker exit")
                break
            print_info(f"[LOGIN] Starting login for Guest UID: {uid} (mode: {mode})...")
            try:
                bot_state.update_status(str(uid), "CONNECTING")
            except Exception:
                pass
            async with _login_sem:                       # fair queue: max LOGIN_CONCURRENCY logins at once
                if bot_state.is_deleted(uid):
                    continue
                account_data = await process_account_uid_pass(
                    uid, password, owner_user_id=owner_user_id,
                    owner_username=owner_username, mode=mode
                )
            if bot_state.is_deleted(uid, (account_data or {}).get('account_id')):
                print_warning(f"[DELETE] Guest UID {uid} deleted during login -> worker exit")
                break
            gate = (account_data or {}).get("_gate")
            if gate == "reject":                         # level outside the allowed range -> never added
                await _reject_login(uid, uid, owner_user_id, account_data.get("_reason", "Level not allowed"))
                break
            if not account_data:
                fail_count += 1
                delay = min(LOGIN_BACKOFF_MAX, 15 * (2 ** min(fail_count - 1, 4)))
                print_error(f"Login failed for UID: {uid}. Retrying in {delay} seconds...")
                bot_state.set_pending(uid, "LOGIN_FAILED", "Login failed. Check UID / password. Retrying...")
                try:
                    bot_state.update_status(str(uid), "ERROR")
                except Exception:
                    pass
                await asyncio.sleep(delay + random.uniform(0, 3))
                continue
            fail_count = 0
            bot_state.clear_pending(uid)
            if gate == "stop":                           # restart: already at its stop level -> stays STOPPED
                print_warning(f"[STOP] UID {uid}: stop level reached -> not running")
                break
            await run_account_worker(account_data, uid, mode=mode, owner_user_id=owner_user_id)
            if bot_state.is_stopped(account_data.get('account_id')):
                print_warning(f"[STOP] UID {uid}: stop level reached -> worker finished (account kept, status STOPPED)")
                break
            print_warning(f"Session finished for {uid}. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Worker for {uid} stopped.")
            bot_state.clear_pending(uid)
            try:
                bot_state.update_status(str(uid), "OFFLINE")
            except Exception:
                pass
            break
        except Exception as e:
            print_error(f"Error for UID {uid}: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


async def account_loop_token(token: str, mode: str = "auto",
                              owner_user_id: str = "", owner_username: str = ""):
    token_label = token_key(token)
    fail_count = 0
    bot_state.add_pending(token_label, token_label, owner_user_id, owner_username, mode)
    await asyncio.sleep(random.uniform(0, 2.0))
    while True:
        if bot_state.is_deleted(token, token_label):
            print_warning(f"[DELETE] Token {token_label} deleted -> worker exit")
            break
        try:
            if not await _wait_until_allowed(owner_user_id, token_label, token):
                print_warning(f"[DELETE] Token {token_label} deleted while waiting -> worker exit")
                break
            print_info(f"[LOGIN] Starting login with Access Token (mode: {mode})...")
            async with _login_sem:
                if bot_state.is_deleted(token, token_label):
                    continue
                account_data = await process_account_token(
                    token, owner_user_id=owner_user_id,
                    owner_username=owner_username, mode=mode
                )
            if bot_state.is_deleted(token, token_label, (account_data or {}).get('account_id')):
                print_warning(f"[DELETE] Token {token_label} deleted during login -> worker exit")
                break
            gate = (account_data or {}).get("_gate")
            if gate == "reject":
                await _reject_login(token_label, token_label, owner_user_id, account_data.get("_reason", "Level not allowed"))
                break
            if not account_data:
                fail_count += 1
                delay = min(LOGIN_BACKOFF_MAX, 15 * (2 ** min(fail_count - 1, 4)))
                print_error(f"Login failed for Token. Retrying in {delay} seconds...")
                bot_state.set_pending(token_label, "LOGIN_FAILED", "Login failed. Token may be expired. Retrying...")
                await asyncio.sleep(delay + random.uniform(0, 3))
                continue
            fail_count = 0
            bot_state.clear_pending(token_label)
            acc_id = str(account_data['account_id'])
            if gate == "stop":
                print_warning(f"[STOP] UID {acc_id}: stop level reached -> not running")
                break
            await run_account_worker(account_data, acc_id, mode=mode, owner_user_id=owner_user_id)
            if bot_state.is_stopped(acc_id):
                print_warning(f"[STOP] UID {acc_id}: stop level reached -> worker finished (account kept, status STOPPED)")
                break
            print_warning("Token session finished. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Worker for token {token_label} stopped.")
            bot_state.clear_pending(token_label)
            break
        except Exception as e:
            print_error(f"Token error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


def load_accounts():
    accounts = []
    try:
        accounts = [a for a in _read_accounts() if isinstance(a, dict)]       # MongoDB (or local file when no MongoDB)
    except Exception as e:
        print_error(f"Could not load accounts: {e}")
    if not accounts and FALLBACK_UID and FALLBACK_PASSWORD:
        accounts.append({"uid": FALLBACK_UID, "password": FALLBACK_PASSWORD})
    return accounts


# ==================== MAIN ====================
async def main():
    print_colored("=" * 60, Colors.CYAN)
    print_colored("    EXE LEVEL UP - FreeFire Bot (Web Dashboard Mode)", Colors.GREEN)
    print_colored("   Persistent Device ID + TRUE Parallel + Smart DNS", Colors.WHITE)
    print_colored("=" * 60, Colors.CYAN)
    print_info(f"Start Match Interval: {START_MATCH_INTERVAL}s")
    print_info(f"Login mode: {LOGIN_MODE} (order {'>'.join(LOGIN_MODE_ORDER)}) | super region: {SUPER_REGION} | web region: {LOGIN_REGION} | "
               f"gateway style: {GATEWAY_STYLE} | PlAy_VeR: {PLAY_VER} | "
               f"APP_VERSION override: {','.join(_env_app_versions()) or '-'} | login code {LOGIN_CODE_VERSION}")
    print_info(f"Offline Wait: {NEW_MATCH_DELAY}s (after match found) | parallel matches per account: {MAX_PARALLEL_MATCHES or 'unlimited'}")
    print_info(f"EXP Refresh: {EXP_REFRESH_INTERVAL}s interval (auto-slower with many accounts) + post-match trigger")
    print_info(f"Login queue: {LOGIN_CONCURRENCY} at a time | Refresh queue: {REFRESH_CONCURRENCY} at a time")
    print_info("Device System: 1 ID = 1 Persistent Device ID (devices.json)")
    print_colored("-" * 60, Colors.MAGENTA)
    print_info("MODE SWITCH:")
    print_info("   * auto  -> BR (Lvl<3) / LW (Lvl>=3)")
    print_info("   * br    -> Battle Royale only")
    print_info("   * lw    -> Lone Wolf only")
    print_colored("=" * 60, Colors.CYAN)

    main_task = asyncio.current_task()
    try:
        loop = asyncio.get_running_loop()
        for _sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(_sig, main_task.cancel)     # clean save on Render / Railway restarts
    except Exception:
        pass

    try:
        await start_web_dashboard(host=WEB_HOST, port=WEB_PORT)
        print_success(f"Web Dashboard live at port {WEB_PORT}")
    except Exception as e:
        print_error(f"Could not start web dashboard: {e}")

    flusher_task = asyncio.create_task(token_cache_flusher())

    async def on_account_added_handler(data):
        mode = str(data.get("mode", "auto")).strip().lower()
        if mode not in ("auto", "br", "lw"):
            mode = "auto"
        owner_id = str(data.get("owner_user_id", "") or "")
        owner_name = str(data.get("owner_username", "") or "")

        async def _replace_worker(key, coro):
            old = bot_state.account_workers.pop(key, None)
            if old and not old.done():
                old.cancel()
                await asyncio.wait([old], timeout=10)
            bot_state.account_workers[key] = asyncio.create_task(coro)

        if "token" in data and data["token"]:
            t = str(data["token"]).strip()
            await _replace_worker(token_key(t), account_loop_token(
                t, mode=mode, owner_user_id=owner_id, owner_username=owner_name
            ))
        elif "uid" in data and "password" in data:
            u = str(data["uid"]).strip()
            p = str(data["password"]).strip()
            await _replace_worker(u, account_loop_guest(
                u, p, mode=mode, owner_user_id=owner_id, owner_username=owner_name
            ))

    async def on_refresh_account_handler(uid):
        await refresh_account_profile(uid)

    async def on_account_deleted_handler(ids):
        ids = {str(i) for i in ids}
        for key in list(_load_token_cache().keys()):
            if key in ids:
                cache_invalidate(key)
        try:
            devices = persist_store.load("devices", DEVICES_FILE, {})
            remaining = {k: v for k, v in devices.items() if k not in ids}
            if len(remaining) != len(devices):
                persist_store.save("devices", DEVICES_FILE, remaining)
        except Exception as e:
            print_error(f"[DELETE] devices cleanup failed: {e}")
        async with _match_counter_lock:
            for i in ids:
                _match_counters.pop(i, None)
        print_success(f"[DELETE] Account fully wiped: {', '.join(sorted(i for i in ids if not i.startswith('tok_')))[:120]}")

    bot_state.refresh_callbacks["on_account_added"] = on_account_added_handler
    bot_state.refresh_callbacks["on_account_deleted"] = on_account_deleted_handler
    bot_state.refresh_callbacks["on_refresh_account"] = on_refresh_account_handler

    accounts = load_accounts()
    if not accounts:
        print_warning("No accounts saved yet. Add them from the Web Dashboard.")

    for acc in accounts:
        mode = str(acc.get("mode", "auto")).strip().lower()
        if mode not in ("auto", "br", "lw"):
            mode = "auto"
        owner_id = str(acc.get("owner_user_id", "") or "")
        owner_name = str(acc.get("owner_username", "") or "")
        if acc.get("max_level") or acc.get("stop_level"):
            _lk = token_key(acc["token"]) if acc.get("token") else str(acc.get("uid", ""))
            bot_state.account_limits[_lk] = {"max": int(acc.get("max_level") or 0),
                                             "stop": int(acc.get("stop_level") or 0),
                                             "accepted": bool(acc.get("accepted"))}
        if "token" in acc and acc["token"]:
            t = asyncio.create_task(account_loop_token(
                acc["token"], mode=mode, owner_user_id=owner_id, owner_username=owner_name
            ))
            bot_state.account_workers[token_key(acc["token"])] = t
        elif "uid" in acc and "password" in acc and acc["uid"]:
            u = str(acc["uid"])
            t = asyncio.create_task(account_loop_guest(
                u, acc["password"], mode=mode, owner_user_id=owner_id, owner_username=owner_name
            ))
            bot_state.account_workers[u] = t

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print_warning("\n[STOP] Shutting down all accounts...")
        for t in list(bot_state.account_workers.values()):
            t.cancel()
        await asyncio.gather(*bot_state.account_workers.values(), return_exceptions=True)
        flusher_task.cancel()
        try:
            _flush_token_cache_sync()
            await user_store.flush_now()
            try:
                import dashboard_server as _ds
                _ds._save_stats_now()                       # EXP gained / matches per account
            except Exception as e:
                print_warning(f"[STORAGE] stats save at shutdown failed: {e}")
            await asyncio.to_thread(persist_store.flush, 8.0)   # accounts / orders / devices -> MongoDB
        except Exception as e:
            print_error(f"Final save failed: {e}")
        print_success("All sessions cleanly closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_warning("\nProgram stopped by user.")
