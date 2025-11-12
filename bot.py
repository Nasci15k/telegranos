# bot.py
"""
Icsan Search - Final build (placeholder-safe)
- Timeout 20s, retries/backoff
- Logs sent as plain text to group -1003027034402
- /start with inline support button
- /status shows 🟢/🟡/🔴 plus response times
- Inline detection for CPF, placa, chassi, IP, email
- /cpf (choose API), /cpf_full (all APIs), /nome, /telefone, /email, /placa, /cnh, /chassi, /ip, /mac
- Temporary bot messages (consulting/processing/summary) are deleted before final file message is sent.
- Final .txt files are never deleted by the bot.
- IMPORTANT: Replace API_ENDPOINTS placeholders with your actual URLs (see comments below).
"""

import os
import io
import re
import time
import json
import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

import httpx
from fastapi import FastAPI, Request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- CONFIG --------------------
BOT_NAME = "Icsan Search"
BOT_USERNAME = "@IcsanSearchBot"
SUPORTE_USERNAME = "@astrahvhdev"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TELEGRAM_TOKEN_AQUI")
PORT = int(os.environ.get("PORT", 8000))

# The user-provided group id; code prefixes -100 automatically for supergroups/channels
RAW_LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "1003027034402")
RAW_UPDATE_CHANNEL_ID = os.environ.get("UPDATE_CHANNEL_ID", "1003027034402")

def normalize_chat_id(raw_id: str) -> int:
    """If id length looks like a supergroup id without -100 prefix, add -100."""
    try:
        rid = raw_id.strip()
        # numeric only
        rid_num = int(rid)
        s = rid
        if len(rid) >= 13 and not rid.startswith("-100"):
            return int("-100" + rid[-10:])  # fallback
        # if already negative or proper, return numeric
        return int(rid)
    except Exception:
        # fallback to given raw (might error later)
        return int(raw_id)

LOG_CHANNEL_ID = normalize_chat_id(RAW_LOG_CHANNEL_ID)
UPDATE_CHANNEL_ID = normalize_chat_id(RAW_UPDATE_CHANNEL_ID)

# HTTP settings
HTTP_TIMEOUT = 20.0  # seconds (as requested)
HTTP_RETRIES = 2
HTTP_BACKOFF = [1, 2, 4]

# Cache TTL
CACHE_TTL = 15 * 60  # 15 minutes

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("icsan_bot")

# -------------------- PLACEHOLDER API ENDPOINTS --------------------
# >>> REPLACE THE STRINGS BELOW with your actual API URLs.
# Keep the placeholder token {valor} where the search value (cpf/placa/ip/etc.) should be inserted.
#
# Examples (FOR YOU to replace):
# "cpf_serasa": "https://apis-brasil.shop/apis/apiserasacpf2025.php?cpf={valor}"
# "placa_serpro": "https://apiradar.onrender.com/?placa={valor}"
#
API_ENDPOINTS: Dict[str, str] = {
    # CPF sources (CPF_FULL will use all of these)
    "cpf_serasa":        "https://apis-brasil.shop/apis/apiserasacpf2025.php?cpf={valor}",
    "cpf_assec":         "https://apis-brasil.shop/apis/apiassecc2025.php?cpf={valor}",
    "cpf_bigdata":       "https://apis-brasil.shop/apis/apicpfbigdata2025.php?CPF={valor}",
    "cpf_datasus":       "https://apis-brasil.shop/apis/apicpfdatasus.php?cpf={valor}",
    "cpf_credilink":     "https://apis-brasil.shop/apis/apicpfcredilink2025.php?cpf={valor}",
    "cpf_spc":           "REPLACE_WITH_CPF_SPC_URL?cpf={valor}",

    # Name, email, telefone (examples)
    "nome_serasa":       "https://apis-brasil.shop/apis/apiserasanome2025.php?nome={valor}",
    "email_serasa":      "https://apis-brasil.shop/apis/apiserasaemail2025.php?email={valor}",
    "telefone_credilink":"https://apis-brasil.shop/apis/apitelcredilink2025.php?telefone={valor}",

    # Serpro / apiradar (placa, chassi, cnh)
    "placa_serpro":      "https://apiradar.onrender.com/api/placa?query=${valor}&token=KeyBesh",
    "chassi_serpro":     "https://apiradar.onrender.com/api/placa?query=${valor}&token=KeyBesh",
    "cnh_serpro":        "https://apiradar.onrender.com/api/placa?query=${valor}&token=KeyBesh",

    # IP and MAC
    "ip_api":            "http://ip-api.com/json/{valor}",
    "mac_api":           "https://api.macvendors.com/{valor}",
}

# -------------------- STATE, CACHE --------------------
CACHE: Dict[str, Tuple[float, Any]] = {}  # key -> (expiry_ts, value)
API_STATUS: Dict[str, Dict[str, Any]] = {}  # name -> {"icon": "🟢", "rt": float|None}
LAST_BOT_MESSAGE: Dict[int, int] = {}  # chat_id -> message_id (so we delete only bot messages)
HTTP_CLIENT = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

# Fields to drop from API responses when cleaning
FIELDS_TO_REMOVE = {"status", "message", "mensagem", "source", "token", "timestamp", "limit", "success", "code", "error"}

# -------------------- UTIL FUNCTIONS --------------------
def cache_get(key: str):
    item = CACHE.get(key)
    if not item:
        return None
    expiry, val = item
    if time.time() > expiry:
        del CACHE[key]
        return None
    return val

def cache_set(key: str, value: Any, ttl: int = CACHE_TTL):
    CACHE[key] = (time.time() + ttl, value)

def classify_response_time(rt_seconds: Optional[float]) -> str:
    if rt_seconds is None:
        return "🔴"
    if rt_seconds < 2.0:
        return "🟢"
    if rt_seconds < 6.0:
        return "🟡"
    return "🔴"

async def fetch_with_retries(url: str, params: dict = None, retries: int = HTTP_RETRIES) -> Any:
    """
    Generic GET with retries/backoff.
    Returns JSON if possible, else text wrapped in dict message.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = await HTTP_CLIENT.get(url, params=params)
            text = resp.text
            # try parse json
            try:
                return resp.json()
            except Exception:
                # not json: return as text container
                return {"_raw": text}
        except Exception as e:
            last_exc = e
            wait = HTTP_BACKOFF[min(attempt, len(HTTP_BACKOFF)-1)]
            logger.warning(f"RequestException {e} for {url} (attempt {attempt+1}), sleeping {wait}s")
            await asyncio.sleep(wait)
    return {"status": "ERROR", "message": f"Falha ao acessar API ({url}): {last_exc}"}

def clean_api_data(data: Any) -> Any:
    """Remove irrelevant fields and empty values recursively."""
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if not k:
                continue
            if k.lower() in FIELDS_TO_REMOVE:
                continue
            if v in [None, "", [], {}]:
                continue
            cleaned = clean_api_data(v)
            if cleaned in [None, "", [], {}]:
                continue
            out[k] = cleaned
        return out
    elif isinstance(data, list):
        lst = [clean_api_data(i) for i in data]
        return [i for i in lst if i not in (None, "", [], {})]
    else:
        return data

def format_txt(data: Any, indent: int = 0) -> str:
    """Pretty-format dict/list into readable txt."""
    lines: List[str] = []
    prefix = " " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).replace("_", " ").capitalize()
            if isinstance(v, dict):
                lines.append(f"{prefix}{key}:")
                lines.append(format_txt(v, indent + 4))
            elif isinstance(v, list):
                if all(not isinstance(x, (dict, list)) for x in v):
                    lines.append(f"{prefix}{key}: {' | '.join(map(str, v))}")
                else:
                    lines.append(f"{prefix}{key}:")
                    for i, item in enumerate(v, 1):
                        lines.append(f"{prefix}  - Item {i}:")
                        lines.append(format_txt(item, indent + 6))
            else:
                lines.append(f"{prefix}{key}: {v}")
    elif isinstance(data, list):
        for i, item in enumerate(data, 1):
            lines.append(f"{prefix}- Item {i}:")
            lines.append(format_txt(item, indent + 2))
    else:
        lines.append(f"{prefix}{data}")
    return "\n".join(lines)

def generate_txt_bytes(title: str, data: Any, username: str) -> bytes:
    cleaned = clean_api_data(data)
    formatted = format_txt(cleaned)
    header = f"Relatório de Consulta — {title}\nData: {datetime.utcnow().isoformat()} UTC\n\n"
    footer = f"\n\n🤖 {BOT_USERNAME}\n👤 @{username if username else 'usuario'}\n"
    final = header + (formatted if formatted.strip() else "(sem campos relevantes)") + footer
    return final.encode("utf-8")

# -------------------- HEALTHCHECK --------------------
async def check_api_health():
    """Ping each configured endpoint lightly and record icon + rt."""
    for key, url_template in API_ENDPOINTS.items():
        test_value = "00000000000"  # safe dummy (for CPF endpoints)
        url = url_template.format(valor=test_value)
        start = time.time()
        try:
            r = await HTTP_CLIENT.get(url, timeout=HTTP_TIMEOUT)
            rt = time.time() - start
            API_STATUS[key] = {"icon": classify_response_time(rt), "rt": rt}
        except Exception as e:
            logger.debug(f"Health check failed for {key}: {e}")
            API_STATUS[key] = {"icon": "🔴", "rt": None}
    logger.info(f"API_STATUS: {API_STATUS}")

# -------------------- TELEGRAM HELPERS --------------------
def _set_last_bot_message(chat_id: int, message_id: int):
    LAST_BOT_MESSAGE[chat_id] = message_id

async def _delete_last_bot_message(application: Application, chat_id: int):
    mid = LAST_BOT_MESSAGE.get(chat_id)
    if not mid:
        return
    try:
        await application.bot.delete_message(chat_id=chat_id, message_id=mid)
        LAST_BOT_MESSAGE.pop(chat_id, None)
    except Exception as e:
        logger.debug(f"Could not delete last bot message in {chat_id}: {e}")

async def _send_and_track(application: Application, chat_id: int, text: str, **kwargs):
    msg = await application.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    _set_last_bot_message(chat_id, msg.message_id)
    return msg

async def notify_log_channel_text(application: Application, text: str):
    """Send plain text logs to the log channel (no Markdown)."""
    try:
        await application.bot.send_message(chat_id=LOG_CHANNEL_ID, text=text)
    except Exception as e:
        logger.warning(f"Failed to send log to {LOG_CHANNEL_ID}: {e}")

async def announce_update(application: Application):
    text = f"Icsan Search reiniciado / atualizado em {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    try:
        await application.bot.send_message(chat_id=UPDATE_CHANNEL_ID, text=text)
    except Exception as e:
        logger.warning(f"Failed to announce update: {e}")

# -------------------- DATA TYPE DETECTION --------------------
def detect_data_type(q: str) -> str:
    s = q.strip()
    # CPF: 11 digits
    if re.fullmatch(r"\d{11}", re.sub(r"\D", "", s)):
        return "cpf"
    if re.fullmatch(r"[A-Za-z]{3}\d{4}", s.replace("-", "").upper()):
        return "placa"
    if re.fullmatch(r"[A-Za-z0-9]{17}", s):
        return "chassi"
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", s):
        return "ip"
    if "@" in s and "." in s:
        return "email"
    return ""

# -------------------- MENU HELPERS (style A: icons only) --------------------
def _status_icon_for(api_key: str) -> str:
    s = API_STATUS.get(api_key)
    if s and isinstance(s, dict):
        return s.get("icon", "🔴")
    return "🔴"

def _build_buttons_for(options: List[Tuple[str, str]]) -> InlineKeyboardMarkup:
    keyboard = []
    for label, api_key in options:
        icon = _status_icon_for(api_key)
        keyboard.append([InlineKeyboardButton(f"{icon} {label}", callback_data=api_key)])
    return InlineKeyboardMarkup(keyboard)

# -------------------- CALLBACK HANDLER --------------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    api_key = q.data
    user = update.effective_user
    query_value = context.user_data.get("last_query")
    title = context.user_data.get("last_query_title", "Consulta")

    if not query_value:
        await q.edit_message_text("Sessão expirada. Use o comando novamente.")
        return

    # delete previous bot message for cleanliness
    await _delete_last_bot_message(context.application, update.effective_chat.id)

    # start animated status message
    status_msg = await _send_and_track(context.application, update.effective_chat.id, f"🔍 Consultando `{query_value}` — fonte: {api_key}...")
    await asyncio.sleep(0.8)
    try:
        await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="📊 Processando dados...")
    except Exception:
        pass
    await asyncio.sleep(0.5)

    start_ts = time.time()
    # route to API
    result = None
    if api_key.startswith("cpf_") or api_key in ("cpf_serasa","cpf_assec","cpf_bigdata","cpf_datasus","cpf_credilink","cpf_spc"):
        url = API_ENDPOINTS.get(api_key)
        if url is None:
            await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="Fonte não configurada.")
            return
        # format url with valor
        full_url = url.format(valor=query_value)
        result = await fetch_with_retries(full_url)
    elif api_key in ("placa_serpro", "chassi_serpro", "cnh_serpro"):
        url = API_ENDPOINTS.get(api_key)
        if url is None:
            await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="Fonte não configurada.")
            return
        full_url = url.format(valor=query_value)
        result = await fetch_with_retries(full_url)
    elif api_key == "ip_api":
        full_url = API_ENDPOINTS.get("ip_api").format(valor=query_value)
        result = await fetch_with_retries(full_url)
    elif api_key == "mac_api":
        full_url = API_ENDPOINTS.get("mac_api").format(valor=query_value)
        result = await fetch_with_retries(full_url)
    elif api_key == "nome_serasa":
        full_url = API_ENDPOINTS.get("nome_serasa").format(valor=query_value)
        result = await fetch_with_retries(full_url)
    elif api_key == "email_serasa":
        full_url = API_ENDPOINTS.get("email_serasa").format(valor=query_value)
        result = await fetch_with_retries(full_url)
    elif api_key == "telefone_credilink":
        full_url = API_ENDPOINTS.get("telefone_credilink").format(valor=query_value)
        result = await fetch_with_retries(full_url)
    else:
        await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="API desconhecida.")
        return

    elapsed = time.time() - start_ts

    # handle errors or empty responses
    if not result:
        await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="❌ Erro na consulta (sem resposta).")
        await notify_log_channel_text(context.application, f"ERROR: API {api_key} returned empty for {query_value}")
        return
    if isinstance(result, dict) and result.get("status") == "ERROR":
        await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=f"❌ Erro na consulta: {result.get('message')}")
        await notify_log_channel_text(context.application, f"API {api_key} error for {query_value}: {result.get('message')}")
        return

    # Clean and format
    cleaned = clean_api_data(result)
    cleaned_text = format_txt(cleaned)
    user_display = user.username if user.username else (user.first_name or "usuario")
    summary_line = f"✅ Consulta concluída — tempo: {elapsed:.2f}s"

    # If small enough send as message; otherwise send .txt
    if cleaned_text and len(cleaned_text) <= 3500:
        final_text = f"{summary_line}\n\n{cleaned_text}\n\n🤖 {BOT_USERNAME}\n👤 @{user_display}"
        try:
            await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=final_text)
            # track last bot message (we keep final message tracked)
            _set_last_bot_message(status_msg.chat_id, status_msg.message_id)
            # delete the summary message before sending file? we are sending as message so do nothing
        except Exception:
            # fallback: send new message
            msg = await context.application.bot.send_message(chat_id=status_msg.chat_id, text=final_text)
            _set_last_bot_message(msg.chat_id, msg.message_id)
    else:
        # prepare txt file and send; delete the status message first
        try:
            await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=f"{summary_line}\n📄 Resultado extenso — enviando arquivo .txt...")
        except Exception:
            pass
        txt_bytes = generate_txt_bytes(f"{api_key}_{query_value}", cleaned, user_display)
        bio = io.BytesIO(txt_bytes)
        bio.name = f"{api_key}_{query_value}.txt"
        try:
            # delete the short status message to keep the chat clean, per your instruction
            await _delete_last_bot_message(context.application, status_msg.chat_id)
            sent = await context.application.bot.send_document(chat_id=status_msg.chat_id, document=bio, filename=bio.name,
                                                              caption=f"✅ Resultado completo — {query_value}\n\n🤖 {BOT_USERNAME}\n👤 @{user_display}")
            # Do NOT delete this message in future (we only track last bot message for ephemeral ones)
            # We set last bot msg to this file message so next ephemeral will delete it before sending new ephemeral
            _set_last_bot_message(sent.chat_id, sent.message_id)
        except Exception as e:
            await notify_log_channel_text(context.application, f"Falha ao enviar arquivo para {user_display}: {e}")
            try:
                await context.application.bot.send_message(chat_id=status_msg.chat_id, text=f"❌ Erro ao enviar arquivo: {e}")
            except Exception:
                pass

# -------------------- CPF_FULL Handler --------------------
async def cmd_cpf_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # don't delete user messages; delete last bot ephemeral before starting
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Informe o CPF: /cpf_full 12345678900")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    cpf = re.sub(r"\D", "", parts[1].strip())
    status_msg = await _send_and_track(context.application, update.effective_chat.id, f"🔍 Iniciando CPF_FULL para `{cpf}`...")
    await asyncio.sleep(0.6)
    start_ts = time.time()

    # Kick off all CPF tasks (use the endpoints that start with cpf_)
    tasks = []
    keys = [k for k in API_ENDPOINTS.keys() if k.startswith("cpf_")]
    for k in keys:
        url_template = API_ENDPOINTS.get(k)
        if not url_template:
            continue
        url = url_template.format(valor=cpf)
        tasks.append(fetch_with_retries(url))

    if not tasks:
        await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="⚠️ Nenhuma fonte de CPF configurada.")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid_results: List[Any] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"Exception in cpf_full fetch: {r}")
            continue
        if isinstance(r, dict) and r.get("status") == "ERROR":
            logger.info(f"CPF API returned error: {r.get('message')}")
            continue
        valid_results.append(r)

    if not valid_results:
        await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="❌ Todas as APIs de CPF falharam ou retornaram vazias.")
        await notify_log_channel_text(context.application, f"❌ CPF_FULL todas APIs falharam para {cpf}")
        return

    # merge and dedupe results
    cleaned_list = [clean_api_data(v) for v in valid_results]
    # simple merge: combine dicts into one by keys
    merged: Dict[str, Any] = {}
    for d in cleaned_list:
        if isinstance(d, dict):
            for k, v in d.items():
                if k not in merged:
                    merged[k] = v
                else:
                    if merged[k] != v:
                        # convert to list of unique values
                        existing = merged[k]
                        if not isinstance(existing, list):
                            existing = [existing]
                        if v not in existing:
                            existing.append(v)
                        merged[k] = existing

    elapsed = time.time() - start_ts
    summary = f"✅ CPF_FULL concluído — tempo: {elapsed:.2f}s"
    try:
        await context.application.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=summary)
    except Exception:
        pass

    # send txt file
    txt_bytes = generate_txt_bytes(f"CPF_FULL_{cpf}", merged, update.effective_user.username or update.effective_user.first_name)
    bio = io.BytesIO(txt_bytes)
    bio.name = f"CPF_FULL_{cpf}.txt"
    try:
        # delete ephemeral summary to keep chat clean, then send file (file preserved)
        await _delete_last_bot_message(context.application, update.effective_chat.id)
        sent = await context.application.bot.send_document(chat_id=update.effective_chat.id, document=bio, filename=bio.name,
                                                          caption=f"✅ Resultado CPF_FULL — {cpf}\n\n🤖 {BOT_USERNAME}\n👤 @{update.effective_user.username or update.effective_user.first_name}")
        _set_last_bot_message(sent.chat_id, sent.message_id)
    except Exception as e:
        await notify_log_channel_text(context.application, f"Falha ao enviar CPF_FULL txt: {e}")
        await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Erro ao enviar arquivo: {e}")

# -------------------- SIMPLE COMMANDS --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # delete last ephemeral bot message
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    keyboard = [
        [InlineKeyboardButton("🔍 CPF", callback_data="menu_cpf")],
        [InlineKeyboardButton("📂 CPF FULL", callback_data="menu_cpf_full")],
        [InlineKeyboardButton("🚗 Veículo (placa/chassi)", callback_data="menu_veiculo")],
        [InlineKeyboardButton("🌐 IP / MAC", callback_data="menu_net")],
        [InlineKeyboardButton("💬 Suporte", url=f"https://t.me/{SUPORTE_USERNAME.replace('@','')}")],
    ]
    text = (
        f"👋 Bem-vindo ao *{BOT_NAME}*\n\n"
        "Use os comandos ou escolha uma opção abaixo.\n\n"
        "Comandos principais:\n"
        "• /cpf <cpf>\n"
        "• /cpf_full <cpf>\n"
        "• /nome <nome>\n"
        "• /telefone <telefone>\n"
        "• /email <email>\n"
        "• /placa <placa>\n"
        "• /cnh <numero>\n"
        "• /chassi <chassi>\n"
        "• /ip <ip>\n"
        "• /mac <mac>\n\n"
        f"Suporte: {SUPORTE_USERNAME}"
    )
    # Use MarkdownV2 to avoid parse issues; escape potential offending chars
    safe_text = text.replace("_", r"\_")
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=safe_text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(keyboard))
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🌐 Status das APIs (ícones):"]
    for k, v in API_STATUS.items():
        name = k.replace("cpf_", "").replace("_serpro", "").upper()
        icon = v.get("icon", "🔴")
        rt = v.get("rt")
        rt_str = f"{rt:.2f}s" if (rt is not None) else "–"
        lines.append(f"{icon} {name} ({rt_str})")
    text = "\n".join(lines)
    await context.application.bot.send_message(chat_id=update.effective_chat.id, text=text)

# wrappers to show menus (will set last_query and provide selection buttons)
async def cmd_cpf_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # expects text: /cpf <value>
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /cpf <cpf>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = re.sub(r"\D", "", parts[1].strip())
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "CPF"
    options = [
        ("Serasa", "cpf_serasa"),
        ("Assec", "cpf_assec"),
        ("BigData", "cpf_bigdata"),
        ("Datasus", "cpf_datasus"),
        ("Credilink", "cpf_credilink"),
        ("SPC", "cpf_spc"),
    ]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar CPF `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

# other command wrappers (similar patterns)
async def cmd_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /nome <nome completo>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "Nome"
    options = [("Serasa", "nome_serasa")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar Nome `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /email <email>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "Email"
    options = [("Serasa", "email_serasa")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar Email `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_telefone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /telefone <numero>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = re.sub(r"\D", "", parts[1].strip())
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "Telefone"
    options = [("Credilink", "telefone_credilink")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar Telefone `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_placa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /placa <placa>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "Placa"
    options = [("Serpro", "placa_serpro")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar Placa `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_cnh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /cnh <numero>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = re.sub(r"\D", "", parts[1].strip())
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "CNH"
    options = [("Serpro", "cnh_serpro")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar CNH `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_chassi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /chassi <chassi>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "Chassi"
    options = [("Serpro", "chassi_serpro")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar Chassi `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /ip <ip>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "IP"
    options = [("IP API", "ip_api")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar IP `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

async def cmd_mac(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /mac <mac>")
        _set_last_bot_message(update.effective_chat.id, msg.message_id)
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = "MAC"
    options = [("MAC Vendors", "mac_api")]
    markup = _build_buttons_for(options)
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar MAC `{query}`:", parse_mode="Markdown", reply_markup=markup)
    _set_last_bot_message(update.effective_chat.id, msg.message_id)

# -------------------- INLINE TEXT DETECTION --------------------
async def text_handler_detect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    dtype = detect_data_type(text)
    if not dtype:
        return
    await _delete_last_bot_message(context.application, update.effective_chat.id)
    if dtype == "cpf":
        keyboard = [
            [InlineKeyboardButton("🔎 Consultar CPF", callback_data="cpf_serasa")],
            [InlineKeyboardButton("📂 CPF FULL", callback_data="cpf_full_quick")],
        ]
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei CPF — `{text}`. Escolha uma opção:", parse_mode="Markdown")
        _set_last_bot_message(update.effective_chat.id, sent.message_id)
        context.user_data["last_query"] = re.sub(r"\D", "", text)
        context.user_data["last_query_title"] = "CPF"
    elif dtype == "placa":
        keyboard = [[InlineKeyboardButton("🔎 Consultar Placa", callback_data="placa_serpro")]]
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei Placa — `{text}`. Escolha:", parse_mode="Markdown")
        _set_last_bot_message(update.effective_chat.id, sent.message_id)
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "Placa"
    elif dtype == "chassi":
        keyboard = [[InlineKeyboardButton("🔎 Consultar Chassi", callback_data="chassi_serpro")]]
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei Chassi — `{text}`. Escolha:", parse_mode="Markdown")
        _set_last_bot_message(update.effective_chat.id, sent.message_id)
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "Chassi"
    elif dtype == "ip":
        keyboard = [[InlineKeyboardButton("🔎 Consultar IP", callback_data="ip_api")]]
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei IP — `{text}`. Escolha:", parse_mode="Markdown")
        _set_last_bot_message(update.effective_chat.id, sent.message_id)
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "IP"
    elif dtype == "email":
        keyboard = [[InlineKeyboardButton("🔎 Consultar E-mail", callback_data="email_serasa")]]
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei E-mail — `{text}`. Escolha:", parse_mode="Markdown")
        _set_last_bot_message(update.effective_chat.id, sent.message_id)
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "Email"

# -------------------- HANDLERS REGISTRATION --------------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cpf", cmd_cpf_menu_handler))
    app.add_handler(CommandHandler("cpf_full", cmd_cpf_full))
    app.add_handler(CommandHandler("nome", cmd_nome))
    app.add_handler(CommandHandler("telefone", cmd_telefone))
    app.add_handler(CommandHandler("email", cmd_email))
    app.add_handler(CommandHandler("placa", cmd_placa))
    app.add_handler(CommandHandler("cnh", cmd_cnh))
    app.add_handler(CommandHandler("chassi", cmd_chassi))
    app.add_handler(CommandHandler("ip", cmd_ip))
    app.add_handler(CommandHandler("mac", cmd_mac))

    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler_detect))

    logger.info("Handlers registrados com sucesso.")

# -------------------- FASTAPI WEBHOOK (Render) --------------------
application = Application.builder().token(TELEGRAM_TOKEN).build()
register_handlers(application)

webhook_app = FastAPI()

@webhook_app.on_event("startup")
async def startup_event():
    logger.info("Startup: checking API health and initializing bot")
    await check_api_health()
    try:
        await application.initialize()
        await application.start()
        await announce_update(application)
        await notify_log_channel_text(application, f"Bot iniciado em {datetime.utcnow().isoformat()} UTC")
        logger.info("Bot Telegram iniciado")
    except Exception as e:
        logger.error(f"Erro ao iniciar bot: {e}")
        await notify_log_channel_text(application, f"Erro ao iniciar bot: {e}")

@webhook_app.post(f"/{TELEGRAM_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

async def set_webhook_on_render(application: Application, token: str):
    """Call this from deploy command to set webhook."""
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
    if not RENDER_EXTERNAL_URL:
        logger.warning("RENDER_EXTERNAL_URL não configurada.")
        return
    webhook_url = f"{RENDER_EXTERNAL_URL}/{token}"
    try:
        await application.bot.delete_webhook()
        await application.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook configurado: {webhook_url}")
    except Exception as e:
        logger.warning(f"Failed to set webhook: {e}")

# -------------------- ENTRYPOINT FOR LOCAL TEST --------------------
if __name__ == "__main__":
    # fallback to polling for local dev
    application.run_polling()
