# -*- coding: utf-8 -*-
"""
EXE LEVEL UP - FreeFire Bot Web Dashboard Backend (Full)
Supports: user auth, plans, orders, admin panel, free plan requests,
          bulk account add, pause/restart, credentials download.
Hardened: signed payment webhook, rate limits, plan-expiry enforcement,
          slot limits on every add path, non-blocking storage.
Embedded Async Web Server (aiohttp)
"""

import asyncio
import aiohttp
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import uuid
import zipfile
from collections import deque
from typing import Dict, List, Any, Optional
from aiohttp import web
try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None


# ==================== CONFIG ====================
ADMIN_PASSWORD_DEFAULT = "youadminpass"
ADMIN_TOKEN_TTL = 7 * 24 * 3600
USER_TOKEN_TTL = 30 * 24 * 3600
FREE_PLAN_HOURS = 12
FREE_PLAN_SLOTS = 3
NEW_USER_SLOTS = 0   # brand-new users get 0 slots until they claim the free plan or buy a plan
MAX_ACCOUNTS_PER_REQUEST = 500
PLAN_ENFORCE_INTERVAL = 10          # seconds between plan-expiry / slot checks
MAX_USERS_PER_IP_PER_DAY = 3        # signups from one IP per 24h (stops free-trial farming)

# Telegram links shown in the panel
TG_CHANNEL_1 = "https://t.me/BrExeee"
TG_CHANNEL_2 = "https://t.me/Exeprooff"
TG_SUPPORT = "https://t.me/Brexxee"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")

USERS_FILE = os.path.join(BASE_DIR, "users.json")
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")
FREE_PLAN_FILE = os.path.join(BASE_DIR, "free_plan_requests.json")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
DEVICES_FILE = os.path.join(BASE_DIR, "devices.json")
TOKEN_CACHE_FILE = os.path.join(BASE_DIR, "token_cache.json")
PLANS_FILE = os.path.join(BASE_DIR, "plans.json")
PLANS_VERSION_FILE = os.path.join(BASE_DIR, "plans.version")

# Plans: 1 / 3 / 10 / 20 days. Prices and slots are unchanged from the old plans.
PLANS_VERSION = "days-1-3-10-20-v1"
DEFAULT_PLANS = [
    {"id": "basic-1",  "name": "Basic", "duration_text": "1 Day",   "duration_hours": 24,  "price_inr": 99,   "price_usd": 1.29,  "slots": 5,   "badge": None,         "popular": False},
    {"id": "pro-3",    "name": "Pro",   "duration_text": "3 Days",  "duration_hours": 72,  "price_inr": 299,  "price_usd": 3.99,  "slots": 15,  "badge": "POPULAR",    "popular": True},
    {"id": "ultra-10", "name": "Ultra", "duration_text": "10 Days", "duration_hours": 240, "price_inr": 599,  "price_usd": 7.99,  "slots": 40,  "badge": "BEST VALUE", "popular": False},
    {"id": "mega-20",  "name": "Mega",  "duration_text": "20 Days", "duration_hours": 480, "price_inr": 1499, "price_usd": 19.99, "slots": 100, "badge": None,         "popular": False},
]

# ---- admin password: the old public default is NOT accepted any more.
_env_admin_pw = (os.environ.get("ADMIN_PASSWORD") or "").strip()
ADMIN_PASSWORD_GENERATED = False
if (not _env_admin_pw) or _env_admin_pw == ADMIN_PASSWORD_DEFAULT or len(_env_admin_pw) < 8:
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    ADMIN_PASSWORD_GENERATED = True
else:
    ADMIN_PASSWORD = _env_admin_pw

# Keep the payment key server-only. Railway currently provides it as `Key`.
ZAPUPI_API_KEY = (os.environ.get("Key") or os.environ.get("ZAPUPI_API_KEY", "")).strip()
ZAPUPI_CREATE_ORDER_URL = os.environ.get("ZAPUPI_CREATE_ORDER_URL", "https://pay.zapupi.com/api/create-order")
ZAPUPI_WEBHOOK_URL = os.environ.get("ZAPUPI_WEBHOOK_URL", "")
APP_URL = (os.environ.get("APP_URL") or "https://nxc-bot-production.up.railway.app").strip().rstrip("/")

# Secret used to sign the per-order webhook URL (an attacker cannot forge "payment success").
_WEBHOOK_SECRET = (os.environ.get("WEBHOOK_SECRET") or ZAPUPI_API_KEY or secrets.token_hex(32)).encode("utf-8")
# How many reverse proxies sit in front of the app (Railway/Render = 1).
TRUSTED_PROXY_HOPS = max(1, int(os.environ.get("TRUSTED_PROXY_HOPS", "1") or 1))


def _webhook_sig(order_id: str) -> str:
    return hmac.new(_WEBHOOK_SECRET, ("zapupi|" + str(order_id)).encode("utf-8"), hashlib.sha256).hexdigest()[:40]


# ==================== RATE LIMITER ====================
class RateLimiter:
    def __init__(self):
        self.hits: Dict[str, deque] = {}

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.time()
        q = self.hits.get(key)
        if q is None:
            q = self.hits[key] = deque()
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def cleanup(self, max_age: float = 86400):
        now = time.time()
        for k in list(self.hits):
            q = self.hits[k]
            while q and q[0] <= now - max_age:
                q.popleft()
            if not q:
                self.hits.pop(k, None)


rate_limiter = RateLimiter()


def _client_ip(request: web.Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if parts:
        idx = len(parts) - TRUSTED_PROXY_HOPS
        return parts[idx if idx >= 0 else 0]
    return request.remote or "?"


def _limited(request: web.Request, bucket: str, limit: int, window: float, who: str = "") -> bool:
    """True when the caller is over the limit."""
    key = f"{bucket}|{who or _client_ip(request)}"
    return not rate_limiter.allow(key, limit, window)


def _too_many():
    return _json({"status": "error", "error": "Too many attempts. Please wait a few minutes and try again."}, 429)


def _norm_email(email: str) -> str:
    """Gmail ignores dots and +tags, so a.b+1@gmail.com == ab@gmail.com (stops duplicate free trials)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _norm_tg(tg: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (tg or "").lower().replace("https://t.me/", ""))


# ==================== STORAGE ====================
def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[STORAGE] save failed {path}: {e}")


def _write_text_atomic(path: str, text: str):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[STORAGE] save failed {path}: {e}")


def _hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 310000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def _verify_pw(pw: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return hmac.compare_digest(encoded, hashlib.sha256(pw.encode("utf-8")).hexdigest())
        candidate = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


# ==================== BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self._paused: Dict[str, bool] = {}
        # Each deleted account is stored as a group of every identifier it was known by
        # (game account id, guest login uid, access token, token prefixes) so that any
        # in-flight login or worker loop can detect the deletion and stop for good.
        self._deleted_groups: List[set] = []
        # Plan enforcement (filled by plan_enforcer): owners whose plan is over/missing and
        # individual account identifiers that are above their owner's slot limit.
        self.blocked_owners: set = set()
        self.blocked_ids: set = set()

    def is_blocked(self, owner_id, *identifiers) -> bool:
        owner = str(owner_id or "")
        if owner and owner != "admin" and owner in self.blocked_owners:
            return True
        ids = {str(i) for i in identifiers if i not in (None, "")}
        return bool(ids & self.blocked_ids)

    def is_deleted(self, *identifiers) -> bool:
        ids = {str(i) for i in identifiers if i not in (None, "")}
        return any(ids & group for group in self._deleted_groups)

    def mark_deleted(self, identifiers: set):
        ids = {str(i) for i in identifiers if i not in (None, "")}
        if ids:
            self._deleted_groups.append(ids)

    def undelete(self, *identifiers):
        ids = {str(i) for i in identifiers if i not in (None, "")}
        if ids:
            self._deleted_groups = [g for g in self._deleted_groups if not (ids & g)]

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "message": message, "uid": uid}
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid: str, nickname: str, region: str, level: int,
                         exp: int, likes: int = 0, mode: str = "auto",
                         owner_user_id: str = "", owner_username: str = ""):
        uid_str = str(uid)
        if self.is_deleted(uid_str):
            return
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "mode": mode or "auto",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_updated_at": int(time.time()),
                "registered_at": time.time(),
                "owner_user_id": owner_user_id,
                "owner_username": owner_username,
            }
        else:
            acc = self.accounts[uid_str]
            if nickname: acc["nickname"] = nickname
            if region: acc["region"] = region
            if level: acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            if mode: acc["mode"] = mode
            if owner_user_id: acc["owner_user_id"] = owner_user_id
            if owner_username: acc["owner_username"] = owner_username
            acc["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            acc["last_updated_at"] = int(time.time())
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            acc["last_updated_at"] = int(time.time())
            diff = current_exp - old_exp
            if diff > 0:
                self.log(f"{acc['nickname']} gained +{diff} EXP! Total: +{acc['gained_exp']}", "success", uid_str)
            self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.accounts[uid_str]["last_updated_at"] = int(time.time())

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.accounts[uid_str]["last_updated_at"] = int(time.time())

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())

    def is_paused(self, uid: str) -> bool:
        return bool(self._paused.get(str(uid), False))

    def set_paused(self, uid: str, paused: bool):
        self._paused[str(uid)] = bool(paused)


bot_state = BotState()


# ==================== USERS ====================
class UserStore:
    def __init__(self):
        self.mongo = None
        self.user_collection = None
        self.token_collection = None
        self._dirty = False
        self._flush_lock: Optional[asyncio.Lock] = None
        self._last_users: Dict[str, str] = {}
        self._last_tokens: Dict[str, str] = {}
        mongo_url = (os.environ.get("MONGODB_CONNECTION_STRING_2") or os.environ.get("MONGODB_CONNECTION_STRING", "")).strip()
        if mongo_url and MongoClient:
            for attempt in range(3):         # a short Mongo hiccup at start-up must not wipe plans/users
                try:
                    self.mongo = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
                    self.mongo.admin.command("ping")
                    break
                except Exception as exc:
                    print(f"[STORAGE] Mongo connect attempt {attempt + 1}/3 failed: {exc}")
                    self.mongo = None
                    time.sleep(2)
            try:
                if self.mongo is None:
                    raise RuntimeError("MongoDB not reachable")
                try:
                    database = self.mongo.get_default_database()
                except Exception:
                    database = self.mongo["exe_level_up"]
                self.user_collection = database["users"]
                self.token_collection = database["sessions"]
                self.user_collection.create_index("username", unique=True)
                self.user_collection.create_index("email", unique=True, sparse=True)
            except Exception as exc:
                print(f"[STORAGE] Mongo unavailable, using local fallback: {exc}")
                self.mongo = None
        self.users: Dict[str, Dict] = _load_json(USERS_FILE, {})
        self.tokens: Dict[str, Dict] = _load_json(USERS_FILE + ".tokens", {})
        if self.user_collection is not None:
            self.users = {u["username"]: u for u in self.user_collection.find({}, {"_id": 0})}
            self.tokens = {s["token"]: s for s in self.token_collection.find({}, {"_id": 0})}
            self._last_users = {k: json.dumps(v, sort_keys=True, default=str) for k, v in self.users.items()}
            self._last_tokens = {k: json.dumps(dict(v, token=k), sort_keys=True, default=str) for k, v in self.tokens.items()}
        self._cleanup_tokens()

    # ---- persistence: callers only mark the store dirty; a background task writes it
    #      off the event loop and only sends CHANGED users to MongoDB.
    def _persist(self):
        self._dirty = True

    def _write_snapshot(self, users_json: str, tokens_json: str, user_strs: Dict[str, str], token_strs: Dict[str, str]):
        _write_text_atomic(USERS_FILE, users_json)
        _write_text_atomic(USERS_FILE + ".tokens", tokens_json)
        if self.user_collection is None:
            return
        try:
            for name, text in user_strs.items():
                if self._last_users.get(name) != text:
                    self.user_collection.replace_one({"username": name}, json.loads(text), upsert=True)
            gone = [n for n in self._last_users if n not in user_strs]
            if gone:
                self.user_collection.delete_many({"username": {"$in": gone}})
            for tok, text in token_strs.items():
                if self._last_tokens.get(tok) != text:
                    self.token_collection.replace_one({"token": tok}, json.loads(text), upsert=True)
            gone_t = [t for t in self._last_tokens if t not in token_strs]
            if gone_t:
                self.token_collection.delete_many({"token": {"$in": gone_t}})
            self._last_users = user_strs
            self._last_tokens = token_strs
        except Exception as exc:
            print(f"[STORAGE] Mongo write failed (will retry): {exc}")
            self._dirty = True

    async def flush_now(self):
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        async with self._flush_lock:
            if not self._dirty:
                return
            self._dirty = False
            try:
                user_strs = {k: json.dumps(v, sort_keys=True, default=str) for k, v in self.users.items()}
                token_strs = {k: json.dumps(dict(v, token=k), sort_keys=True, default=str) for k, v in self.tokens.items()}
                users_json = json.dumps(self.users, indent=2, default=str)
                tokens_json = json.dumps(self.tokens, indent=2, default=str)
                await asyncio.to_thread(self._write_snapshot, users_json, tokens_json, user_strs, token_strs)
            except Exception as exc:
                self._dirty = True
                print(f"[STORAGE] flush failed: {exc}")

    async def flusher(self):
        while True:
            await asyncio.sleep(1.5)
            try:
                await self.flush_now()
            except Exception as exc:
                print(f"[STORAGE] flusher error: {exc}")

    def _cleanup_tokens(self):
        now = time.time()
        dead = [t for t, v in self.tokens.items() if v.get("expires", 0) < now]
        for t in dead:
            del self.tokens[t]
        if dead:
            self._persist()

    def find(self, identity: str) -> Optional[Dict]:
        value = identity.strip().lower()
        user = self.users.get(value)
        if user:
            return user
        return next((item for item in self.users.values() if item.get("email", "").lower() == value), None)

    def create(self, username: str, email: str, password_hash: str, display_name: str, reg_ip: str = "") -> Optional[Dict]:
        u = username.strip().lower()
        if not u or len(u) < 3:
            return None
        email = email.strip().lower()
        norm = _norm_email(email)
        if u in self.users or any(_norm_email(item.get("email", "")) == norm for item in self.users.values()):
            return None
        user = {
            "id": str(uuid.uuid4()),
            "username": u,
            "email": email,
            "display_name": display_name.strip() or u,
            "password_hash": password_hash,
            "reg_ip": reg_ip,
            "role": "user",
            "plan": "Free",
            "slots": NEW_USER_SLOTS,
            "plan_expires_at": 0,
            "free_plan_claimed": False,
            "created_at": int(time.time()),
            "last_login": 0,
        }
        self.users[u] = user
        self._persist()
        return user

    def verify(self, username: str, password: str) -> Optional[Dict]:
        u = self.find(username)
        if not u:
            return None
        if not _verify_pw(password, u.get("password_hash", "")):
            return None
        return u

    def issue_token(self, username: str) -> str:
        tok = secrets.token_urlsafe(32)
        uname = username.lower()
        self.tokens[tok] = {"username": uname, "expires": int(time.time()) + USER_TOKEN_TTL}
        mine = sorted((t for t, v in self.tokens.items() if v.get("username") == uname),
                      key=lambda t: self.tokens[t].get("expires", 0))
        for old in mine[:-8]:          # keep the 8 newest sessions per user
            self.tokens.pop(old, None)
        self._persist()
        return tok

    def revoke_token(self, token: str):
        if token in self.tokens:
            del self.tokens[token]
            self._persist()

    def user_from_token(self, token: str) -> Optional[Dict]:
        if not token:
            return None
        entry = self.tokens.get(token)
        if not entry:
            return None
        if entry.get("expires", 0) < time.time():
            del self.tokens[token]
            self._persist()
            return None
        return self.find(entry.get("username", ""))

    def update(self, username: str, **fields):
        u = self.find(username)
        if not u:
            return
        for k, v in fields.items():
            u[k] = v
        self._persist()

    def delete(self, username: str):
        u = self.find(username)
        if not u:
            return
        dead = [t for t, v in self.tokens.items() if v.get("username") == u["username"]]
        for t in dead:
            del self.tokens[t]
        del self.users[u["username"]]
        self._persist()

    def all_users(self) -> List[Dict]:
        return list(self.users.values())


user_store = UserStore()


# ==================== PLANS ====================
def load_plans() -> List[Dict]:
    """plans.json from an older release is replaced once (version marker); later hand edits are kept."""
    plans = _load_json(PLANS_FILE, None)
    version = _load_json(PLANS_VERSION_FILE, None)
    if plans and isinstance(plans, list) and version == PLANS_VERSION:
        return plans
    _save_json(PLANS_FILE, DEFAULT_PLANS)
    _save_json(PLANS_VERSION_FILE, PLANS_VERSION)
    return DEFAULT_PLANS


# ==================== ADMIN SESSIONS ====================
_admin_sessions: Dict[str, int] = {}


def _admin_new_token() -> str:
    tok = secrets.token_urlsafe(32)
    _admin_sessions[tok] = int(time.time()) + ADMIN_TOKEN_TTL
    return tok


def _admin_valid(token: str) -> bool:
    if not token:
        return False
    exp = _admin_sessions.get(token)
    if not exp:
        return False
    if exp < time.time():
        del _admin_sessions[token]
        return False
    return True


# ==================== HELPERS ====================
def _get_user(request: web.Request) -> Optional[Dict]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return user_store.user_from_token(auth[7:].strip())


def _get_admin(request: web.Request) -> bool:
    return _admin_valid(request.headers.get("X-Admin-Token", ""))


def _json(data, status: int = 200):
    return web.json_response(data, status=status)


def _has_plan(u: Optional[Dict]) -> bool:
    """True once a user has ever had a plan: a paid/approved plan, a claimed free trial,
    or slots set by hand from the admin panel. False for a brand-new user."""
    if not u:
        return False
    return bool(
        u.get("plan_expires_at", 0)
        or u.get("free_plan_claimed")
        or u.get("slots_manual")
        or str(u.get("plan", "Free")) not in ("", "Free")
    )


def _slot_limit(u: Optional[Dict]) -> int:
    """Total slots a user really has: 0 for a new user, otherwise the actual stored slots."""
    if not _has_plan(u):
        return 0
    try:
        return max(0, int(u.get("slots", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _plan_active(u: Optional[Dict]) -> bool:
    """True only while the user has a running plan (or slots granted by hand by the admin)."""
    if not u:
        return False
    try:
        exp = int(u.get("plan_expires_at", 0) or 0)
    except (TypeError, ValueError):
        exp = 0
    if exp > 0:
        return exp > time.time()
    return bool(u.get("slots_manual")) and _slot_limit(u) > 0


def _recompute_blocks():
    """Work out which owners / accounts must NOT run right now (expired plan, deleted user,
    or more accounts than the plan has slots). The bot workers read bot_state.blocked_*."""
    users = {u["id"]: u for u in user_store.all_users()}
    blocked_owners = {uid_ for uid_, u in users.items() if not _plan_active(u)}
    entries = _read_accounts()
    per_owner: Dict[str, List[Dict]] = {}
    for e in entries:
        oid = str(e.get("owner_user_id", "") or "")
        if not oid or oid == "admin":
            continue
        if oid not in users:
            blocked_owners.add(oid)
            continue
        per_owner.setdefault(oid, []).append(e)
    blocked_ids: set = set()
    for oid, items in per_owner.items():
        if oid in blocked_owners:
            continue
        limit = _slot_limit(users[oid])
        for e in items[limit:]:
            blocked_ids |= _collect_identities(str(e.get("uid", "") or ""), str(e.get("token", "") or ""), entries)
    bot_state.blocked_owners = blocked_owners
    bot_state.blocked_ids = blocked_ids
    for game_uid, acc in list(bot_state.accounts.items()):
        if bot_state.is_blocked(acc.get("owner_user_id"), game_uid):
            if acc.get("status") not in ("EXPIRED",):
                bot_state.update_status(game_uid, "EXPIRED", 0)


async def plan_enforcer():
    """Every few seconds: stop expired / over-limit accounts, resume renewed ones."""
    tick = 0
    while True:
        try:
            _recompute_blocks()
            tick += 1
            if tick % 30 == 0:
                rate_limiter.cleanup()
        except Exception as e:
            print(f"[ENFORCER] error: {e}")
        await asyncio.sleep(PLAN_ENFORCE_INTERVAL)


def _public_user(u: Dict) -> Dict:
    return {
        "id": u["id"],
        "username": u["username"],
        "email": u.get("email", ""),
        "display_name": u.get("display_name", u["username"]),
        "role": u.get("role", "user"),
        "plan": u.get("plan", "Free"),
        "slots": _slot_limit(u),
        "plan_expires_at": u.get("plan_expires_at", 0),
        "free_plan_claimed": u.get("free_plan_claimed", False),
    }


# ==================== USER HANDLERS ====================
async def handle_user_register(request: web.Request):
    try:
        if _limited(request, "register", 10, 3600):
            return _too_many()
        data = await request.json()
        username = str(data.get("username", "")).strip().lower()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        display_name = str(data.get("display_name", "")).strip()[:40]
        if len(username) < 3 or len(username) > 32 or not username.replace("_", "").isalnum():
            return _json({"status": "error", "error": "Username must be 3-32 letters, numbers, or underscore"})
        if not email.endswith("@gmail.com") or len(email) > 120:
            return _json({"status": "error", "error": "Use a valid Gmail address"})
        if len(password) < 8:
            return _json({"status": "error", "error": "Password must be at least 8 chars"})
        if len(password) > 128:
            return _json({"status": "error", "error": "Password too long"})
        if user_store.find(username) or user_store.find(email):
            return _json({"status": "error", "error": "Username or Gmail already registered"})
        ip = _client_ip(request)
        recent = sum(1 for u in user_store.all_users()
                     if u.get("reg_ip") == ip and time.time() - int(u.get("created_at", 0) or 0) < 86400)
        if recent >= MAX_USERS_PER_IP_PER_DAY:
            return _json({"status": "error", "error": "Too many accounts created from this network today. Try again tomorrow."}, 429)
        pw_hash = await asyncio.to_thread(_hash_pw, password)      # slow hash off the event loop
        user = user_store.create(username, email, pw_hash, display_name, reg_ip=ip)
        if not user:
            return _json({"status": "error", "error": "Username or Gmail already registered"})
        token = user_store.issue_token(username)
        bot_state.log(f"New user registered: {username}", "success")
        return _json({"status": "ok", "token": token, "user": _public_user(user)})
    except Exception:
        return _json({"status": "error", "error": "Could not create user"})


async def handle_user_login(request: web.Request):
    try:
        if _limited(request, "login-ip", 30, 300):
            return _too_many()
        data = await request.json()
        identity = str(data.get("identity", data.get("username", ""))).strip().lower()
        password = str(data.get("password", ""))
        if len(identity) > 120 or len(password) > 128:
            return _json({"status": "error", "error": "Invalid Gmail/username or password"})
        # per (network + account): an outsider cannot lock the real owner out of his own account
        if _limited(request, "login-id", 8, 300, who=f"{_client_ip(request)}|{identity}"):
            return _too_many()
        user = await asyncio.to_thread(user_store.verify, identity, password)   # slow hash off the event loop
        if not user:
            return _json({"status": "error", "error": "Invalid Gmail/username or password"})
        user_store.update(user["username"], last_login=int(time.time()))
        token = user_store.issue_token(user["username"])
        return _json({"status": "ok", "token": token, "user": _public_user(user)})
    except Exception:
        return _json({"status": "error", "error": "Login failed"})


async def handle_user_logout(request: web.Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user_store.revoke_token(auth[7:].strip())
    return _json({"status": "ok"})


async def handle_user_me(request: web.Request):
    user = _get_user(request)
    if not user:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    return _json({"status": "ok", "user": _public_user(user)})


# ==================== STATS ====================
async def handle_get_stats(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)

    if is_admin:
        visible_accounts = list(bot_state.accounts.values())
    else:
        visible_accounts = [a for a in bot_state.accounts.values() if a.get("owner_user_id") == user["id"]]

    accounts_data = []
    for acc in visible_accounts:
        item = dict(acc)
        item["is_paused"] = bot_state.is_paused(item.get("uid", ""))
        item["is_blocked"] = bool(bot_state.is_blocked(item.get("owner_user_id"), item.get("uid", "")))
        if item["is_blocked"]:
            item["status"] = "EXPIRED"
        accounts_data.append(item)
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)

    plan_name = "Admin" if is_admin else (user.get("plan", "Free") if user else "Free")
    user_slots = 9999 if is_admin else _slot_limit(user)
    used_slots = len(accounts_data)
    available_slots = max(0, user_slots - used_slots)

    plan_expired = False
    remaining_seconds = 0
    if not is_admin and user:
        exp_ts = user.get("plan_expires_at", 0)
        if exp_ts > 0:
            remaining_seconds = max(0, exp_ts - int(time.time()))
            plan_expired = remaining_seconds <= 0
        else:
            plan_expired = plan_name == "Free"

    total_matches = sum(a.get("matches_played", 0) for a in accounts_data)
    total_exp = sum(a.get("gained_exp", 0) for a in accounts_data)

    if is_admin:
        logs = bot_state.logs[-60:]
    else:
        # users only see log lines that belong to their own accounts (no other users' names / orders)
        owned_uids = {str(a.get("uid")) for a in accounts_data if a.get("uid")}
        logs = [l for l in list(bot_state.logs)
                if (l.get("uid") and str(l.get("uid")) in owned_uids)
                or (owned_uids and any(u in str(l.get("message", "")) for u in owned_uids))][-60:]

    return _json({
        "status": "ok",
        "total_accounts": len(accounts_data),
        "total_matches": total_matches,
        "total_gained_exp": total_exp,
        "accounts": accounts_data,
        "logs": logs,
        "uptime": int(time.time() - bot_state.start_time),
        "plan_name": plan_name,
        "user_slots": user_slots,
        "available_slots": available_slots,
        "remaining_seconds": remaining_seconds,
        "plan_expired": plan_expired,
    })


# ==================== PLANS HANDLERS ====================
def _activate_paid_order(order: Dict, gateway_data: Dict):
    """Give the user the plan. Safe to call twice: an order is only ever activated once."""
    if order.get("activated_at"):
        return False
    username = str(order.get("username", "")).strip()
    plan_id = str(order.get("plan_id", "")).strip()
    plan = next((p for p in load_plans() if str(p.get("id")) == plan_id), None)
    if plan:
        slots, hours, name = int(plan["slots"]), int(plan["duration_hours"]), plan["name"]
    elif order.get("slots") and order.get("duration_hours"):      # order made under an older plan list
        slots, hours, name = int(order["slots"]), int(order["duration_hours"]), str(order.get("plan_name", "Plan"))
    else:
        return False
    if not username:
        return False
    user = user_store.find(username)
    if not user:
        return False
    now = int(time.time())
    current_expiry = int(user.get("plan_expires_at", 0) or 0)
    start_at = max(now, current_expiry)
    user_store.update(
        username,
        plan=name,
        slots=slots,
        plan_expires_at=start_at + hours * 3600,
        last_payment_order_id=order.get("id"),
        last_payment_txn_id=(gateway_data or {}).get("txn_id") or (gateway_data or {}).get("transaction_id") or "",
    )
    order["activated_at"] = now
    _recompute_blocks()
    return True


_GATEWAY_OK = {"success", "successful", "paid", "completed"}
_GATEWAY_BAD = {"failed", "failure", "cancelled", "canceled", "expired"}
ZAPUPI_STATUS_URL = os.environ.get("ZAPUPI_STATUS_URL", "").strip()   # optional: lets us double-check legacy callbacks


async def _gateway_confirms_paid(order_id: str) -> bool:
    """Ask the gateway directly (only if ZAPUPI_STATUS_URL is configured)."""
    if not ZAPUPI_STATUS_URL or not ZAPUPI_API_KEY:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(ZAPUPI_STATUS_URL, json={"zap_key": ZAPUPI_API_KEY, "order_id": order_id}) as resp:
                info = await resp.json(content_type=None)
        if not isinstance(info, dict):
            return False
        inner = info.get("data") if isinstance(info.get("data"), dict) else info
        status = str(inner.get("status", "")).strip().lower()
        return status in _GATEWAY_OK
    except Exception:
        return False


async def handle_zapupi_webhook(request: web.Request):
    """Payment callback. Only a callback whose URL carries the right signature (created by us
    together with the order) can activate a plan; a plain call to the old URL can only be
    activated after the gateway itself confirms the payment, otherwise it waits for admin approval."""
    try:
        order_id = str(request.match_info.get("order_id", "")).strip()
        sig = str(request.match_info.get("sig", "")).strip()
        try:
            data = await request.json()
        except Exception:
            try:
                data = dict(await request.post())
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        body_order = str(data.get("order_id", "")).strip()
        signed = bool(order_id and sig and hmac.compare_digest(sig, _webhook_sig(order_id)))
        if not order_id:
            order_id = body_order            # legacy URL
        elif body_order and body_order != order_id:
            return _json({"status": "ok"})
        if (order_id and sig) and not signed:
            return _json({"status": "error", "error": "bad signature"}, 403)
        if not order_id:
            return _json({"status": "ok"})

        orders = _load_json(ORDERS_FILE, [])
        order = next((o for o in orders if o.get("id") == order_id), None)
        if not order or order.get("status") in ("paid", "approved"):
            return _json({"status": "ok"})          # unknown order, or already handled (replay)

        gateway_status = str(data.get("status", "")).strip().lower()
        order["gateway_status"] = data.get("status", "")
        order["txn_id"] = data.get("txn_id") or data.get("transaction_id") or ""
        order["utr"] = data.get("utr", "")
        if gateway_status in _GATEWAY_OK:
            if not signed and not await _gateway_confirms_paid(order_id):
                order["status"] = "pending"
                order["note"] = "payment callback not verified - approve manually after checking your bank"
                bot_state.log(f"Unverified payment callback for {order_id} -> waiting for admin approval", "warning")
            else:
                try:
                    paid_amount = float(data.get("amount")) if data.get("amount") not in (None, "") else None
                except (TypeError, ValueError):
                    paid_amount = None
                if paid_amount is not None and paid_amount + 0.5 < float(order.get("price_inr", 0) or 0):
                    order["status"] = "pending"
                    order["note"] = f"amount mismatch (paid {paid_amount}, price {order.get('price_inr')})"
                    bot_state.log(f"Order {order_id}: amount mismatch, not activated", "warning")
                elif _activate_paid_order(order, data):
                    order["status"] = "paid"
                    bot_state.log(f"Payment received: {order.get('username')} -> {order.get('plan_name')}", "success")
                else:
                    order["status"] = "pending"
                    order["note"] = "paid but could not activate - approve manually"
        elif gateway_status in _GATEWAY_BAD:
            order["status"] = "failed"
        _save_json(ORDERS_FILE, orders)
        await user_store.flush_now()
        return _json({"status": "ok"})
    except Exception as e:
        print(f"[WEBHOOK] error: {e}")
        return _json({"status": "ok"})


async def handle_plans_list(request: web.Request):
    return _json({"status": "ok", "plans": load_plans()})


async def handle_zapupi_order(request: web.Request):
    user = _get_user(request)
    if not user:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    if not ZAPUPI_API_KEY:
        return _json({"status": "error", "error": "UPI gateway is not configured"}, 503)
    if _limited(request, "order", 10, 3600, who=user["id"]):
        return _too_many()
    try:
        data = await request.json()
        plan = next((p for p in load_plans() if p.get("id") == str(data.get("plan_id", ""))), None)
        if not plan:
            return _json({"status": "error", "error": "Unknown plan"}, 400)
        order_id = "EXE" + secrets.token_hex(8).upper()
        payload = {
            "zap_key": ZAPUPI_API_KEY,
            "order_id": order_id,
            "amount": float(plan["price_inr"]),          # price always comes from the server, never the client
            "customer_mobile": re.sub(r"[^0-9+]", "", str(data.get("customer_mobile", "")))[:15],
            "remark": f"{plan['name']} | {user['username']}",
            "success_url": f"{APP_URL}/#home",
            "failed_url": f"{APP_URL}/#home",
            "timeout_url": f"{APP_URL}/#home",
        }
        payload = {k: v for k, v in payload.items() if v not in ("", None)}
        webhook_base = (ZAPUPI_WEBHOOK_URL or f"{APP_URL}/api/payments/zapupi/webhook").rstrip("/")
        payload["webhook_url"] = f"{webhook_base}/{order_id}/{_webhook_sig(order_id)}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(ZAPUPI_CREATE_ORDER_URL, json=payload, headers=headers) as response:
                gateway_data = await response.json(content_type=None)
        if not isinstance(gateway_data, dict) or response.status >= 400:
            detail = gateway_data.get("message") or gateway_data.get("error") if isinstance(gateway_data, dict) else ""
            return _json({"status": "error", "error": detail or f"UPI gateway rejected the order ({response.status})"}, 502)
        gateway_payload = gateway_data.get("data") if isinstance(gateway_data.get("data"), dict) else gateway_data
        payment_url = gateway_payload.get("payment_url") or gateway_payload.get("paymentUrl") or gateway_payload.get("checkout_url") or gateway_payload.get("redirect_url")
        if not payment_url:
            return _json({"status": "error", "error": gateway_data.get("message") or "ZapUPI did not return payment_url"}, 502)
        # never send the gateway's raw reply (it may contain our key / signed webhook) back to the browser
        safe_gateway = {"payment_url": payment_url}
        orders = _load_json(ORDERS_FILE, [])
        orders.append({"id": order_id, "user_id": user["id"], "username": user["username"], "plan_id": plan["id"],
                       "plan_name": plan["name"], "price_inr": plan["price_inr"], "price_usd": plan.get("price_usd"),
                       "slots": plan["slots"], "duration_hours": plan["duration_hours"],
                       "payment_method": "ZapUPI", "status": "pending", "created_at": int(time.time()),
                       "user_telegram": str(data.get("user_telegram", "")).strip()[:64],
                       "gateway": safe_gateway})
        _save_json(ORDERS_FILE, orders)
        return _json({"status": "ok", "order_id": order_id, "payment_url": payment_url, "gateway": safe_gateway})
    except Exception:
        return _json({"status": "error", "error": "Unable to create UPI order"}, 502)


async def handle_plan_order(request: web.Request):
    user = _get_user(request)
    if not user:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    if _limited(request, "order", 10, 3600, who=user["id"]):
        return _too_many()
    try:
        data = await request.json()
        plan_id = str(data.get("plan_id", "")).strip()
        txn = str(data.get("transaction_id", "")).strip()[:64]
        tg = str(data.get("user_telegram", "")).strip()[:64]
        method = str(data.get("payment_method", "UPI")).strip()[:20]
        if not plan_id or not txn:
            return _json({"status": "error", "error": "plan_id and transaction_id required"})
        plan = next((p for p in load_plans() if p["id"] == plan_id), None)
        if not plan:
            return _json({"status": "error", "error": "Unknown plan"})
        orders = _load_json(ORDERS_FILE, [])
        # one payment reference can only ever be used for one order
        if any(str(o.get("transaction_id", "")).strip().lower() == txn.lower()
               and o.get("status") in ("pending", "approved", "paid") for o in orders):
            return _json({"status": "error", "error": "This transaction ID was already submitted"})
        order = {
            "id": "ORD-" + secrets.token_hex(4).upper(),
            "user_id": user["id"],
            "username": user["username"],
            "plan_id": plan["id"],
            "plan_name": plan["name"],
            "price_inr": plan["price_inr"],
            "price_usd": plan["price_usd"],
            "slots": plan["slots"],
            "duration_hours": plan["duration_hours"],
            "transaction_id": txn,
            "user_telegram": tg,
            "payment_method": method,
            "status": "pending",
            "created_at": int(time.time()),
            "approved_at": None,
        }
        orders.append(order)
        _save_json(ORDERS_FILE, orders)
        bot_state.log(f"Order {order['id']} from {user['username']} ({plan['name']})", "info")
        return _json({"status": "ok", "order_id": order["id"]})
    except Exception:
        return _json({"status": "error", "error": "Could not create order"})


async def handle_my_orders(request: web.Request):
    user = _get_user(request)
    if not user:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    orders = _load_json(ORDERS_FILE, [])
    mine = [o for o in orders if o.get("user_id") == user["id"]]
    mine.sort(key=lambda o: o.get("created_at", 0), reverse=True)
    return _json({"status": "ok", "orders": mine})


# ==================== FREE PLAN HANDLERS ====================
async def handle_free_plan_status(request: web.Request):
    user = _get_user(request)
    if not user:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    reqs = _load_json(FREE_PLAN_FILE, [])
    mine = [r for r in reqs if r.get("user_id") == user["id"]]
    pending = next((r for r in mine if r.get("status") == "pending"), None)
    approved = next((r for r in mine if r.get("status") == "approved"), None)

    is_active = False
    remaining_seconds = 0
    if approved:
        started = approved.get("approved_at", 0)
        expires = started + (FREE_PLAN_HOURS * 3600)
        remaining_seconds = max(0, expires - int(time.time()))
        is_active = remaining_seconds > 0

    return _json({
        "status": "ok",
        "is_active": is_active,
        "remaining_seconds": remaining_seconds,
        "slots": FREE_PLAN_SLOTS,
        "has_pending": bool(pending),
        "already_used": bool(user.get("free_plan_claimed")),
    })


async def handle_free_plan_request(request: web.Request):
    user = _get_user(request)
    if not user:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        if _limited(request, "freeplan", 5, 86400):
            return _too_many()
        if user.get("free_plan_claimed"):
            return _json({"status": "error", "error": "Free plan already used"})
        data = await request.json()
        tg = str(data.get("telegram", "")).strip()[:64]
        reason = str(data.get("reason", "")).strip()[:300]
        tg_norm = _norm_tg(tg)
        if len(tg_norm) < 3:
            return _json({"status": "error", "error": "Telegram required"})
        reqs = _load_json(FREE_PLAN_FILE, [])
        if any(r.get("user_id") == user["id"] and r.get("status") == "pending" for r in reqs):
            return _json({"status": "error", "error": "You already have a pending request"})
        # one free trial per Telegram handle (stops the same person making many accounts)
        if any(_norm_tg(r.get("telegram", "")) == tg_norm and r.get("status") in ("pending", "approved") for r in reqs):
            return _json({"status": "error", "error": "This Telegram username already requested the free plan"})
        req = {
            "id": "FP-" + secrets.token_hex(4).upper(),
            "user_id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name", user["username"]),
            "telegram": tg,
            "reason": reason,
            "ip": _client_ip(request),
            "status": "pending",
            "created_at": int(time.time()),
            "approved_at": None,
        }
        reqs.append(req)
        _save_json(FREE_PLAN_FILE, reqs)
        bot_state.log(f"Free plan request from {user['username']}", "info")
        return _json({"status": "ok", "message": "Request sent to admin"})
    except Exception:
        return _json({"status": "error", "error": "Could not send request"})


# ==================== ACCOUNT HANDLERS ====================
def _read_accounts() -> List[Dict]:
    return _load_json(ACCOUNTS_FILE, [])


def _write_accounts(accounts: List[Dict]):
    _save_json(ACCOUNTS_FILE, accounts)


def token_key(token) -> str:
    """Same identifier as Main.token_key: derived from the whole token, never from a prefix."""
    return "tok_" + hashlib.sha1(str(token).encode("utf-8")).hexdigest()[:20]


def _token_ids(token: str) -> set:
    token = str(token or "").strip()
    if not token:
        return set()
    return {token, token_key(token)}


def _credential_ids(key: str, cred: Dict) -> set:
    ids = {str(key), str(cred.get("account_id", "") or ""), str(cred.get("auth_uid", "") or "")}
    ids |= _token_ids(cred.get("auth_token", ""))
    ids.discard("")
    return ids


def _entry_ids(entry: Dict) -> set:
    ids = _token_ids(entry.get("token", ""))
    entry_uid = str(entry.get("uid", "") or "").strip()
    if entry_uid:
        ids.add(entry_uid)
    return ids


def _collect_identities(uid: str = "", token: str = "", file_accounts: Optional[List[Dict]] = None) -> set:
    """Expand a game account id / guest uid / token into every identifier the bot uses for it."""
    ids = set()
    if uid:
        ids.add(str(uid).strip())
    ids |= _token_ids(token)
    ids.discard("")
    if not ids:
        return ids
    entries = file_accounts if file_accounts is not None else _read_accounts()
    while True:
        size = len(ids)
        for key, cred in list(bot_state.account_credentials.items()):
            cand = _credential_ids(key, cred)
            if cand & ids:
                ids |= cand
        for entry in entries:
            cand = _entry_ids(entry)
            if cand & ids:
                ids |= cand
        if len(ids) == size:
            return ids


def _user_owns_any(user: Optional[Dict], ids: set) -> bool:
    if not user:
        return False
    for i in ids:
        live = bot_state.accounts.get(i)
        if live and live.get("owner_user_id") == user["id"]:
            return True
    for entry in _read_accounts():
        if _entry_ids(entry) & ids and entry.get("owner_user_id") == user["id"]:
            return True
    return False


async def _purge_account(ids: set):
    """Stop every task for the account and erase all of its stored traces."""
    if not ids:
        return
    bot_state.mark_deleted(ids)

    _write_accounts([a for a in _read_accounts() if not (_entry_ids(a) & ids)])

    workers = []
    for key, worker in list(bot_state.account_workers.items()):
        if key in ids:
            bot_state.account_workers.pop(key, None)
            if worker and not worker.done():
                worker.cancel()
                workers.append(worker)
    if workers:
        await asyncio.wait(workers, timeout=10)

    for key, cred in list(bot_state.account_credentials.items()):
        if _credential_ids(key, cred) & ids:
            bot_state.account_credentials.pop(key, None)
    for i in ids:
        bot_state.accounts.pop(i, None)
        bot_state._paused.pop(i, None)
    bot_state.logs = [l for l in bot_state.logs if l.get("uid") not in ids]
    bot_state.recalc_totals()

    callback = bot_state.refresh_callbacks.get("on_account_deleted")
    if callback:
        try:
            await callback(ids)
        except Exception as e:
            print(f"[DELETE] cleanup callback failed: {e}")


def _owned_entry_count(entries: List[Dict], owner_id: str) -> int:
    return sum(1 for a in entries if a.get("owner_user_id") == owner_id)


def _owns_uid(user: Optional[Dict], is_admin: bool, uid: str) -> bool:
    if is_admin:
        return True
    return _user_owns_any(user, _collect_identities(uid, ""))


async def handle_account_add(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        data = await request.json()
        mode = str(data.get("mode", "auto")).strip().lower()
        if mode not in ("auto", "br", "lw"):
            mode = "auto"

        if not is_admin:
            if _limited(request, "acc-add", 60, 60, who=user["id"]):
                return _too_many()
            if not _plan_active(user):
                return _json({"status": "error", "error": "Your plan has expired. Buy a plan to add accounts."})

        owner_id = user["id"] if user else "admin"
        owner_name = user["username"] if user else "admin"

        accounts = _read_accounts()
        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return _json({"status": "error", "error": "UID and Password required"})
            if len(uid) > 64 or len(pwd) > 128:
                return _json({"status": "error", "error": "UID or password too long"})
            existing = next((a for a in accounts if str(a.get("uid")) == uid), None)
            if existing and not is_admin and existing.get("owner_user_id") != owner_id:
                return _json({"status": "error", "error": "This account is already registered by another user"})
            others = [a for a in accounts if str(a.get("uid")) != uid]
            if not is_admin and _owned_entry_count(others, owner_id) >= _slot_limit(user):
                return _json({"status": "error", "error": "No slots left. Buy a plan."})
            bot_state.undelete(uid)
            accounts = others
            accounts.append({"uid": uid, "password": pwd, "mode": mode,
                             "owner_user_id": owner_id, "owner_username": owner_name})
        elif "token" in data:
            tok = str(data["token"]).strip()
            if not tok:
                return _json({"status": "error", "error": "Token required"})
            if len(tok) > 4096:
                return _json({"status": "error", "error": "Token too long"})
            existing = next((a for a in accounts if a.get("token") == tok), None)
            if existing and not is_admin and existing.get("owner_user_id") != owner_id:
                return _json({"status": "error", "error": "This account is already registered by another user"})
            others = [a for a in accounts if a.get("token") != tok]
            if not is_admin and _owned_entry_count(others, owner_id) >= _slot_limit(user):
                return _json({"status": "error", "error": "No slots left. Buy a plan."})
            bot_state.undelete(*_token_ids(tok))
            accounts = others
            accounts.append({"token": tok, "mode": mode,
                             "owner_user_id": owner_id, "owner_username": owner_name})
        else:
            return _json({"status": "error", "error": "Invalid payload"})
        _write_accounts(accounts)
        bot_state.log(f"Node injected by {owner_name} (mode: {mode})", "success")

        if "on_account_added" in bot_state.refresh_callbacks:
            payload = dict(data)
            payload["owner_user_id"] = owner_id
            payload["owner_username"] = owner_name
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](payload))
        return _json({"status": "ok"})
    except Exception:
        return _json({"status": "error", "error": "Could not add account"})


async def handle_account_bulk_add(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        data = await request.json()
        incoming = data.get("accounts", [])
        if not isinstance(incoming, list) or not incoming:
            return _json({"status": "error", "error": "accounts array required"})
        if len(incoming) > MAX_ACCOUNTS_PER_REQUEST:
            return _json({"status": "error", "error": f"Max {MAX_ACCOUNTS_PER_REQUEST} accounts per request"})

        if not is_admin:
            if _limited(request, "acc-bulk", 10, 60, who=user["id"]):
                return _too_many()
            if not _plan_active(user):
                return _json({"status": "error", "error": "Your plan has expired. Buy a plan to add accounts."})

        owner_id = user["id"] if user else "admin"
        owner_name = user["username"] if user else "admin"
        limit = None if is_admin else _slot_limit(user)

        accounts = _read_accounts()
        added = 0
        failed = 0
        over_limit = 0
        added_items = []

        for item in incoming:
            if not isinstance(item, dict):
                failed += 1
                continue
            uid = str(item.get("uid", "")).strip()
            pwd = str(item.get("password", "")).strip()
            mode = str(item.get("mode", "auto")).strip().lower()
            if mode not in ("auto", "br", "lw"):
                mode = "auto"
            if not uid or not pwd or len(uid) > 64 or len(pwd) > 128:
                failed += 1
                continue
            existing = next((a for a in accounts if str(a.get("uid")) == uid), None)
            if existing and not is_admin and existing.get("owner_user_id") != owner_id:
                failed += 1                       # belongs to somebody else
                continue
            others = [a for a in accounts if str(a.get("uid")) != uid]
            if limit is not None and _owned_entry_count(others, owner_id) >= limit:
                over_limit += 1                   # slot limit reached
                continue
            bot_state.undelete(uid)
            accounts = others
            accounts.append({"uid": uid, "password": pwd, "mode": mode,
                             "owner_user_id": owner_id, "owner_username": owner_name})
            added_items.append({"uid": uid, "password": pwd, "mode": mode,
                                "owner_user_id": owner_id, "owner_username": owner_name})
            added += 1
        _write_accounts(accounts)
        bot_state.log(f"Bulk add: {added} nodes by {owner_name}", "success")

        if "on_account_added" in bot_state.refresh_callbacks:
            for payload in added_items:
                asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](payload))
        result = {"status": "ok", "added": added, "failed": failed + over_limit}
        if over_limit:
            result["limit_reached"] = True
            result["message"] = f"{over_limit} account(s) skipped: no slots left. Buy a plan for more."
        return _json(result)
    except Exception:
        return _json({"status": "error", "error": "Bulk add failed"})


async def handle_account_delete(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        token = str(data.get("token", "")).strip()
        if not uid and not token:
            return _json({"status": "error", "error": "uid or token required"})

        ids = _collect_identities(uid, token)
        if not is_admin and not _user_owns_any(user, ids):
            return _json({"status": "error", "error": "Not your account"}, 403)

        await _purge_account(ids)
        bot_state.log(f"Node {uid or token[:10]} removed permanently", "warning", uid)
        return _json({"status": "ok"})
    except Exception as e:
        return _json({"status": "error", "error": str(e)})


async def handle_account_refresh_all(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        if _limited(request, "refresh-all", 3, 30, who=(user["id"] if user else "admin")):
            return _too_many()
        accounts = [a for a in bot_state.accounts.values() if is_admin or a.get("owner_user_id") == user["id"]]
        callback = bot_state.refresh_callbacks.get("on_refresh_account")
        if callback:
            asyncio.create_task(_refresh_many(callback, [str(a.get("uid")) for a in accounts]))
        return _json({"status": "ok", "refreshed": len(accounts)})
    except Exception:
        return _json({"status": "error", "error": "Refresh failed"})


async def _refresh_many(callback, uids: List[str]):
    # runs in the background (the browser no longer waits for every account) and in small batches
    for i in range(0, len(uids), 20):
        await asyncio.gather(*(callback(u) for u in uids[i:i + 20]), return_exceptions=True)


async def handle_account_refresh(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return _json({"status": "error", "error": "uid required"})
        if not _owns_uid(user, is_admin, uid):
            return _json({"status": "error", "error": "Not your account"}, 403)
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return _json({"status": "ok"})
    except Exception:
        return _json({"status": "error", "error": "Refresh failed"})


async def handle_account_restart(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return _json({"status": "error", "error": "uid required"})
        if not _owns_uid(user, is_admin, uid):
            return _json({"status": "error", "error": "Not your account"}, 403)
        if uid in bot_state.account_workers:
            bot_state.account_workers[uid].cancel()
        bot_state.log(f"Node {uid} restart requested", "warning", uid)
        return _json({"status": "ok"})
    except Exception:
        return _json({"status": "error", "error": "Restart failed"})


async def handle_account_pause(request: web.Request):
    user = _get_user(request)
    is_admin = _get_admin(request)
    if not user and not is_admin:
        return _json({"status": "error", "error": "Not authenticated"}, 401)
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return _json({"status": "error", "error": "uid required"})
        if not _owns_uid(user, is_admin, uid):
            return _json({"status": "error", "error": "Not your account"}, 403)
        acc = bot_state.accounts.get(uid) or {}
        if not is_admin and bot_state.is_blocked(acc.get("owner_user_id"), uid):
            return _json({"status": "error", "error": "Plan expired. Buy a plan to run this account."})
        new_state = not bot_state.is_paused(uid)
        bot_state.set_paused(uid, new_state)
        bot_state.update_status(uid, "PAUSED" if new_state else "ONLINE", 0)
        bot_state.log(f"Node {uid} {'PAUSED' if new_state else 'RESUMED'}", "warning", uid)
        return _json({"status": "ok", "paused": new_state, "message": "Paused" if new_state else "Resumed"})
    except Exception:
        return _json({"status": "error", "error": "Pause failed"})


# ==================== ADMIN HANDLERS ====================
async def handle_admin_login(request: web.Request):
    try:
        if _limited(request, "admin-login", 5, 900):
            return _too_many()
        data = await request.json()
        pw = str(data.get("password", "")).strip()
        if not hmac.compare_digest(pw.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
            await asyncio.sleep(1.0)
            return _json({"status": "error", "error": "Wrong password"})
        tok = _admin_new_token()
        bot_state.log("Admin logged in", "warning")
        return _json({"status": "ok", "token": tok, "expires_in": ADMIN_TOKEN_TTL})
    except Exception:
        return _json({"status": "error", "error": "Login failed"})


async def handle_admin_orders(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    orders = _load_json(ORDERS_FILE, [])
    orders.sort(key=lambda o: o.get("created_at", 0), reverse=True)
    return _json({"status": "ok", "orders": orders})


async def handle_admin_orders_action(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    try:
        data = await request.json()
        order_id = str(data.get("order_id", "")).strip()
        action = str(data.get("action", "")).strip().lower()
        if action not in ("approve", "reject"):
            return _json({"status": "error", "error": "action must be approve/reject"})
        orders = _load_json(ORDERS_FILE, [])
        order = next((o for o in orders if o["id"] == order_id), None)
        if not order:
            return _json({"status": "error", "error": "Order not found"})
        if order.get("status") != "pending":
            return _json({"status": "error", "error": "Order already " + order.get("status", "?")})

        if action == "approve":
            if not _activate_paid_order(order, {}):
                return _json({"status": "error", "error": "Could not activate (user or plan missing). Use Users -> add hours."})
            order["status"] = "approved"
            bot_state.log(f"Order {order_id} approved -> {order['username']} plan {order['plan_name']}", "success")
        else:
            order["status"] = "rejected"
            bot_state.log(f"Order {order_id} rejected", "warning")
        order["approved_at"] = int(time.time())
        _save_json(ORDERS_FILE, orders)
        await user_store.flush_now()
        return _json({"status": "ok", "message": f"Order {action}d"})
    except Exception as e:
        return _json({"status": "error", "error": str(e)})


async def handle_admin_overview(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    return _json({
        "status": "ok",
        "overview": {
            "total_users": len(user_store.all_users()),
            "total_active_accounts": len(bot_state.accounts),
            "total_gained_exp": bot_state.total_gained_exp,
            "total_matches": bot_state.total_matches,
            "uptime": int(time.time() - bot_state.start_time),
        }
    })


async def handle_admin_users(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    users = user_store.all_users()
    enriched = []
    for u in users:
        owned = [a for a in bot_state.accounts.values() if a.get("owner_user_id") == u["id"]]
        enriched.append({
            "id": u["id"],
            "username": u["username"],
            "display_name": u.get("display_name", u["username"]),
            "role": u.get("role", "user"),
            "plan": u.get("plan", "Free"),
            "plan_expires_at": u.get("plan_expires_at", 0),
            "slots": _slot_limit(u),
            "slots_used": len(owned),
            "free_plan_claimed": u.get("free_plan_claimed", False),
            "total_gained_exp": sum(a.get("gained_exp", 0) for a in owned),
            "created_at": u.get("created_at", 0),
        })
    return _json({"status": "ok", "users": enriched})


async def handle_admin_users_update(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    try:
        data = await request.json()
        user_id = str(data.get("user_id", "")).strip()
        slots = data.get("slots")
        add_hours = data.get("add_hours", 0)

        user = next((u for u in user_store.all_users() if u["id"] == user_id), None)
        if not user:
            return _json({"status": "error", "error": "User not found"})

        fields = {}
        if slots is not None:
            try:
                fields["slots"] = int(slots)
                fields["slots_manual"] = True   # admin set this on purpose, so it counts even without a plan
            except: pass
        if add_hours:
            now = int(time.time())
            cur = user.get("plan_expires_at", 0)
            base = cur if cur > now else now
            fields["plan_expires_at"] = base + int(add_hours) * 3600
        if fields:
            user_store.update(user["username"], **fields)
            _recompute_blocks()
        return _json({"status": "ok"})
    except Exception as e:
        return _json({"status": "error", "error": str(e)})


async def handle_admin_users_delete(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    try:
        data = await request.json()
        user_id = str(data.get("user_id", "")).strip()
        user = next((u for u in user_store.all_users() if u["id"] == user_id), None)
        if not user:
            return _json({"status": "error", "error": "User not found"})
        file_accounts = _read_accounts()
        groups = []
        for a in file_accounts:
            if a.get("owner_user_id") == user_id:
                groups.append(_collect_identities(a.get("uid", ""), a.get("token", ""), file_accounts))
        for live_uid, live in list(bot_state.accounts.items()):
            if live.get("owner_user_id") == user_id:
                groups.append(_collect_identities(live_uid, "", file_accounts))
        all_ids = set().union(*groups) if groups else set()
        removed = sum(1 for a in file_accounts if a.get("owner_user_id") == user_id)
        await _purge_account(all_ids)
        user_store.delete(user["username"])
        bot_state.log(f"User {user['username']} deleted ({removed} accounts removed)", "warning")
        return _json({"status": "ok"})
    except Exception as e:
        return _json({"status": "error", "error": str(e)})


async def handle_admin_credentials(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    file_accounts = _read_accounts()
    out = []
    covered_live = set()

    def _find_credential(ids: set) -> Dict:
        for i in ids:
            if i in bot_state.account_credentials:
                return bot_state.account_credentials[i]
        return {}

    def _row(game_uid: str, login_uid: str, password: str, token: str, owner: str, ids: set) -> Dict:
        live = bot_state.accounts.get(game_uid, {}) if game_uid else {}
        credential = _find_credential(ids)
        access_token = token or credential.get("access_token", "") or credential.get("auth_token", "")
        return {
            "uid": game_uid or login_uid,
            "login_uid": login_uid,
            "nickname": live.get("nickname") or credential.get("nickname", ""),
            "password": password or credential.get("auth_password", ""),
            "token": access_token,
            "has_token": bool(access_token),
            "owner_username": owner or live.get("owner_username", "") or credential.get("owner_username", ""),
            "status": live.get("status", "OFFLINE"),
            "level": live.get("level", credential.get("level", 0)),
            "current_exp": live.get("current_exp", credential.get("exp", 0)),
            "gained_exp": live.get("gained_exp", 0),
            "last_updated_at": live.get("last_updated_at", 0),
        }

    for a in file_accounts:
        login_uid = str(a.get("uid", "") or "").strip()
        token = str(a.get("token", "") or "").strip()
        ids = _collect_identities(login_uid, token, file_accounts)
        if not ids or bot_state.is_deleted(*ids):
            continue
        game_uid = next((i for i in ids if i in bot_state.accounts), "")
        if game_uid:
            covered_live.add(game_uid)
        out.append(_row(game_uid, login_uid, str(a.get("password", "") or ""), token,
                        str(a.get("owner_username", "") or ""), ids))

    for game_uid in list(bot_state.accounts):
        if game_uid in covered_live:
            continue
        ids = _collect_identities(game_uid, "", file_accounts)
        credential = _find_credential(ids)
        out.append(_row(game_uid, str(credential.get("auth_uid", "") or ""), "", "", "", ids))

    return _json({"status": "ok", "credentials": out})


async def handle_admin_accounts(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    accounts = []
    for a in bot_state.accounts.values():
        accounts.append({
            "uid": a.get("uid"),
            "nickname": a.get("nickname"),
            "region": a.get("region"),
            "level": a.get("level"),
            "gained_exp": a.get("gained_exp", 0),
            "owner_username": a.get("owner_username", ""),
            "status": a.get("status", "OFFLINE"),
        })
    accounts.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    return _json({"status": "ok", "accounts": accounts})


async def handle_admin_free_plan_requests(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    reqs = _load_json(FREE_PLAN_FILE, [])
    reqs.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return _json({"status": "ok", "requests": reqs})


async def handle_admin_free_plan_action(request: web.Request):
    if not _get_admin(request):
        return _json({"status": "error", "error": "Admin only"}, 403)
    try:
        data = await request.json()
        req_id = str(data.get("request_id", "")).strip()
        action = str(data.get("action", "")).strip().lower()
        if action not in ("approve", "reject"):
            return _json({"status": "error", "error": "action must be approve/reject"})
        reqs = _load_json(FREE_PLAN_FILE, [])
        req = next((r for r in reqs if r["id"] == req_id), None)
        if not req:
            return _json({"status": "error", "error": "Request not found"})
        if req.get("status") != "pending":
            return _json({"status": "error", "error": "Already " + req.get("status", "?")})

        req["status"] = "approved" if action == "approve" else "rejected"
        req["approved_at"] = int(time.time())
        _save_json(FREE_PLAN_FILE, reqs)

        if action == "approve":
            user = user_store.find(req["username"])
            if user:
                now = int(time.time())
                cur = user.get("plan_expires_at", 0)
                base = cur if cur > now else now
                user_store.update(req["username"],
                                  slots=FREE_PLAN_SLOTS,
                                  plan="Free Trial",
                                  plan_expires_at=base + FREE_PLAN_HOURS * 3600,
                                  free_plan_claimed=True)
                bot_state.log(f"Free plan approved for {req['username']}", "success")
                _recompute_blocks()
        else:
            bot_state.log(f"Free plan rejected for {req['username']}", "warning")
        return _json({"status": "ok", "message": f"Free plan {action}d"})
    except Exception as e:
        return _json({"status": "error", "error": str(e)})


async def handle_admin_download_credentials(request: web.Request):
    if not _get_admin(request):
        return web.Response(text="admin only", status=403)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in (ACCOUNTS_FILE, DEVICES_FILE, TOKEN_CACHE_FILE):
            if os.path.exists(path):
                z.write(path, arcname=os.path.basename(path))
    buf.seek(0)
    return web.Response(
        body=buf.read(),
        content_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=nxc_credentials.zip"},
    )


# ==================== INDEX ====================
_INDEX_CACHE: Dict[str, Any] = {"mtime": 0, "html": ""}


async def handle_index(request: web.Request):
    try:
        mtime = os.path.getmtime(TEMPLATE_PATH)
        if mtime != _INDEX_CACHE["mtime"]:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                _INDEX_CACHE["html"] = f.read()
            _INDEX_CACHE["mtime"] = mtime
        content = _INDEX_CACHE["html"]
    except Exception:
        content = "<h1>templates/index.html not found!</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_config(request: web.Request):
    """Public links shown in the panel (kept on the server so there is one place to change them)."""
    return _json({"status": "ok", "channel_1": TG_CHANNEL_1, "channel_2": TG_CHANNEL_2, "support": TG_SUPPORT})


@web.middleware
async def security_middleware(request: web.Request, handler):
    resp = await handler(request)
    try:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
    except Exception:
        pass
    return resp


# ==================== START ====================
async def start_web_dashboard(host: str = "0.0.0.0", port: int = None):
    if port is None:
        port = int(os.environ.get("PORT", 20333))

    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[security_middleware])
    r = app.router

    r.add_get("/", handle_index)
    r.add_get("/api/config", handle_config)

    r.add_post("/api/user/register", handle_user_register)
    r.add_post("/api/user/login", handle_user_login)
    r.add_post("/api/user/logout", handle_user_logout)
    r.add_get("/api/user/me", handle_user_me)

    r.add_get("/api/stats", handle_get_stats)

    r.add_get("/api/plans", handle_plans_list)
    r.add_post("/api/plans/order", handle_plan_order)
    r.add_post("/api/payments/zapupi/order", handle_zapupi_order)
    r.add_post("/api/payments/zapupi/webhook", handle_zapupi_webhook)                       # legacy (needs gateway confirmation)
    r.add_post("/api/payments/zapupi/webhook/{order_id}/{sig}", handle_zapupi_webhook)      # signed
    r.add_get("/api/plans/my-orders", handle_my_orders)

    r.add_get("/api/free-plan/status", handle_free_plan_status)
    r.add_post("/api/free-plan/request", handle_free_plan_request)

    r.add_post("/api/account/add", handle_account_add)
    r.add_post("/api/account/bulk-add", handle_account_bulk_add)
    r.add_post("/api/account/delete", handle_account_delete)
    r.add_post("/api/account/refresh", handle_account_refresh)
    r.add_post("/api/account/refresh-all", handle_account_refresh_all)
    r.add_post("/api/account/restart", handle_account_restart)
    r.add_post("/api/account/pause", handle_account_pause)

    r.add_post("/api/admin/login", handle_admin_login)
    r.add_get("/api/admin/orders", handle_admin_orders)
    r.add_post("/api/admin/orders/action", handle_admin_orders_action)
    r.add_get("/api/admin/overview", handle_admin_overview)
    r.add_get("/api/admin/users", handle_admin_users)
    r.add_post("/api/admin/users/update", handle_admin_users_update)
    r.add_post("/api/admin/users/delete", handle_admin_users_delete)
    r.add_get("/api/admin/credentials", handle_admin_credentials)
    r.add_get("/api/admin/accounts", handle_admin_accounts)
    r.add_get("/api/admin/free-plan-requests", handle_admin_free_plan_requests)
    r.add_post("/api/admin/free-plan-action", handle_admin_free_plan_action)
    r.add_get("/api/admin/download-credentials", handle_admin_download_credentials)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _recompute_blocks()                      # expired plans are blocked BEFORE any worker starts
    bot_state._bg_tasks = [
        asyncio.create_task(plan_enforcer()),
        asyncio.create_task(user_store.flusher()),
    ]

    print("\033[92m" + "=" * 60 + "\033[0m")
    print(f"\033[92m[+] EXE Web Dashboard running on http://{host}:{port}\033[0m")
    if ADMIN_PASSWORD_GENERATED:
        print("\033[93m[!] ADMIN_PASSWORD is not set (or is weak / the old default).\033[0m")
        print(f"\033[93m[!] Temporary admin password for this run only: {ADMIN_PASSWORD}\033[0m")
        print("\033[93m[!] Set a strong ADMIN_PASSWORD environment variable (8+ chars).\033[0m")
    print("\033[92m" + "=" * 60 + "\033[0m")
