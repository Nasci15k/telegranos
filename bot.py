# bot.py
# Icsan Search Bot - versão final entregue
# Requer: python-telegram-bot, fastapi, httpx, gunicorn, uvicorn
# Env vars required: TELEGRAM_TOKEN, (optional) LOG_CHANNEL_ID, UPDATE_CHANNEL_ID, RENDER_EXTERNAL_URL

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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- Config ----------------
BOT_DISPLAY_NAME = "Icsan Search Bot"
BOT_USERNAME = "@IcsanSearchBot"
SUPORTE_USERNAME = "@astrahvhdev"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TOKEN_AQUI")
RAW_LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "1003027034402")
RAW_UPDATE_CHANNEL_ID = os.environ.get("UPDATE_CHANNEL_ID", "1003027034402")

def normalize_chat_id(raw: str) -> int:
    try:
        r = raw.strip()
        if r.startswith("-100"):
            return int(r)
        # if comes like 1003027034402 -> turn into -100... if missing prefix
        if r.isdigit() and len(r) >= 10:
            # if already has 13+ digits assume proper group id; prefix -100 if missing
            return int(f"-100{r[-10:]}")
        return int(r)
    except Exception:
        try:
            return int(raw)
        except Exception:
            return 0

LOG_CHANNEL_ID = normalize_chat_id(RAW_LOG_CHANNEL_ID)
UPDATE_CHANNEL_ID = normalize_chat_id(RAW_UPDATE_CHANNEL_ID)

HTTP_TIMEOUT = 20.0
HTTP_RETRIES = 2
HTTP_BACKOFF = [1, 2, 4]

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("icsan_bot")

# ---------------- API Endpoints (coloquei as URLs que você citou) ----------------
# Mantenha {valor} no lugar que será substituído pelo CPF/placa/ip/etc.
API_ENDPOINTS: Dict[str, str] = {
    # CPF
    "cpf_serasa":        "https://apis-brasil.shop/apis/apiserasacpf2025.php?cpf={valor}",
    "cpf_assec":         "https://apis-brasil.shop/apis/apiassecc2025.php?cpf={valor}",
    "cpf_bigdata":       "https://apis-brasil.shop/apis/apicpfbigdata2025.php?CPF={valor}",
    "cpf_datasus":       "https://apis-brasil.shop/apis/apicpfdatasus.php?cpf={valor}",
    "cpf_credilink":     "https://apis-brasil.shop/apis/apicpfcredilink2025.php?cpf={valor}",
    "cpf_spc":           "https://apis-brasil.shop/apis/apispccpf2025.php?cpf={valor}",

    # Nome, email, telefone
    "nome_serasa":       "https://apis-brasil.shop/apis/apiserasanome2025.php?nome={valor}",
    "email_serasa":      "https://apis-brasil.shop/apis/apiserasaemail2025.php?email={valor}",
    "telefone_credilink":"https://apis-brasil.shop/apis/apitelcredilink2025.php?telefone={valor}",

    # Serpro / apiradar (placa, chassi, cnh)  <-- token pode estar embutido na URL se necessário
    "placa_serpro":      "https://apiradar.onrender.com/api/placa?query={valor}&token=KeyBesh",
    "chassi_serpro":     "https://apiradar.onrender.com/api/chassi?query={valor}&token=KeyBesh",
    "cnh_serpro":        "https://apiradar.onrender.com/api/cnh?query={valor}&token=KeyBesh",

    # IP and MAC
    "ip_api":            "http://ip-api.com/json/{valor}",
    "mac_api":           "https://api.macvendors.com/{valor}",
}

# ---------------- State ----------------
API_STATUS: Dict[str, Dict[str, Any]] = {}
LAST_EPHEMERAL: Dict[int, int] = {}  # chat_id -> message_id (ephemeral messages only)
HTTP_CLIENT = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

FIELDS_TO_REMOVE = {"status", "message", "mensagem", "source", "token", "timestamp", "limit", "success", "code", "error"}

# ---------------- Helpers ----------------
def classify_rt(rt: Optional[float]) -> str:
    if rt is None:
        return "🔴"
    if rt < 2.0:
        return "🟢"
    if rt < 6.0:
        return "🟡"
    return "🔴"

async def fetch_with_retries(url: str, retries: int = HTTP_RETRIES) -> Any:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = await HTTP_CLIENT.get(url, timeout=HTTP_TIMEOUT)
            text = r.text
            try:
                return r.json()
            except Exception:
                return {"_raw": text}
        except Exception as e:
            last_exc = e
            wait = HTTP_BACKOFF[min(attempt, len(HTTP_BACKOFF)-1)]
            logger.warning(f"RequestException {e} for {url} (attempt {attempt+1}) - sleeping {wait}s")
            await asyncio.sleep(wait)
    return {"status": "ERROR", "message": f"Falha ao acessar API ({url}): {last_exc}"}

def clean_api_data(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if not k: continue
            if k.lower() in FIELDS_TO_REMOVE: continue
            cleaned = clean_api_data(v)
            if cleaned in (None, "", [], {}): continue
            out[k] = cleaned
        return out
    if isinstance(data, list):
        return [clean_api_data(i) for i in data if i not in (None, "", [], {})]
    return data

def format_txt(data: Any, indent: int = 0) -> str:
    lines: List[str] = []
    pref = " " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).replace("_", " ").capitalize()
            if isinstance(v, (dict, list)):
                lines.append(f"{pref}{key}:")
                lines.append(format_txt(v, indent + 4))
            else:
                lines.append(f"{pref}{key}: {v}")
    elif isinstance(data, list):
        for i, it in enumerate(data, 1):
            lines.append(f"{pref}- Item {i}:")
            lines.append(format_txt(it, indent + 2))
    else:
        lines.append(f"{pref}{data}")
    return "\n".join(lines)

def generate_txt_bytes(title: str, data: Any, username: str) -> bytes:
    cleaned = clean_api_data(data)
    formatted = format_txt(cleaned)
    header = f"Relatório de Consulta — {title}\nData: {datetime.utcnow().isoformat()} UTC\n\n"
    footer = f"\n\n🤖 {BOT_DISPLAY_NAME}\n👤 @{username if username else 'usuario'}\n"
    body = formatted if formatted.strip() else "(sem campos relevantes)"
    return (header + body + footer).encode("utf-8")

# ---------------- Healthcheck ----------------
async def check_api_health():
    for key, template in API_ENDPOINTS.items():
        test_val = "00000000000"
        url = template.format(valor=test_val)
        start = time.time()
        try:
            r = await HTTP_CLIENT.get(url, timeout=HTTP_TIMEOUT)
            rt = time.time() - start
            API_STATUS[key] = {"icon": classify_rt(rt), "rt": rt}
        except Exception:
            API_STATUS[key] = {"icon": "🔴", "rt": None}
    logger.info(f"API_STATUS: {API_STATUS}")

# ---------------- Telegram helpers ----------------
def track_ephemeral(chat_id: int, message_id: int):
    LAST_EPHEMERAL[chat_id] = message_id

async def delete_ephemeral(app: Application, chat_id: int):
    mid = LAST_EPHEMERAL.get(chat_id)
    if not mid:
        return
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=mid)
    except Exception as e:
        logger.debug(f"Could not delete ephemeral {mid} in {chat_id}: {e}")
    LAST_EPHEMERAL.pop(chat_id, None)

async def send_log(app: Application, text: str):
    try:
        await app.bot.send_message(chat_id=LOG_CHANNEL_ID, text=text)
    except Exception as e:
        logger.warning(f"Failed to send log to {LOG_CHANNEL_ID}: {e}")

# ---------------- Data detection ----------------
def detect_type(s: str) -> str:
    t = s.strip()
    if re.fullmatch(r"\d{11}", re.sub(r"\D", "", t)):
        return "cpf"
    if re.fullmatch(r"[A-Za-z]{3}\d{4}", t.replace("-","").upper()):
        return "placa"
    if re.fullmatch(r"[A-Za-z0-9]{17}", t):
        return "chassi"
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", t):
        return "ip"
    if "@" in t and "." in t:
        return "email"
    return ""

# ---------------- Menu and callbacks ----------------
def status_icon(key: str) -> str:
    s = API_STATUS.get(key)
    return s.get("icon", "🔴") if s else "🔴"

def build_menu_buttons(options: List[Tuple[str,str]]):
    kb = []
    for label, key in options:
        kb.append([InlineKeyboardButton(f"{status_icon(key)} {label}", callback_data=key)])
    return InlineKeyboardMarkup(kb)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    api_key = q.data
    user = update.effective_user
    query_value = context.user_data.get("last_query")
    if not query_value:
        await q.edit_message_text("Sessão expirada. Envie o comando novamente.")
        return

    # Clean value (remove $ or other artifacts)
    query_value = str(query_value).replace("$", "").strip()

    # delete previous ephemeral
    await delete_ephemeral(context.application, update.effective_chat.id)

    # send ephemeral status and track
    ep = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Consultando <code>{query_value}</code> — fonte: {api_key}", parse_mode="HTML")
    track_ephemeral(update.effective_chat.id, ep.message_id)
    await asyncio.sleep(0.4)
    try:
        await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text="📊 Processando...", parse_mode="HTML")
    except Exception:
        pass

    start = time.time()
    # route to endpoint
    if api_key not in API_ENDPOINTS:
        await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text="Fonte não configurada.")
        return

    url = API_ENDPOINTS[api_key].format(valor=query_value)
    result = await fetch_with_retries(url)
    elapsed = time.time() - start

    # handle error
    if isinstance(result, dict) and result.get("status") == "ERROR":
        await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text=f"❌ Erro ao consultar a API: tente novamente mais tarde.")
        await send_log(context.application, f"[ERRO] {user.username} {api_key} {query_value} -> {result.get('message')}")
        return

    cleaned = clean_api_data(result)
    textified = format_txt(cleaned)
    username_for_file = user.username or user.first_name or "usuario"
    summary = f"✅ Consulta concluída — tempo: {elapsed:.2f}s"

    # if not too long, send as message; else send .txt
    if textified and len(textified) <= 3500:
        final_text = f"{summary}\n\n{textified}\n\n🤖 {BOT_DISPLAY_NAME}\n👤 @{username_for_file}"
        # replace ephemeral content with final (do not delete file message later)
        try:
            await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text=final_text, parse_mode="HTML")
            # track ephemeral (so next ephemeral will delete it) — but we must NOT delete file messages
            track_ephemeral(ep.chat_id, ep.message_id)
            await send_log(context.application, f"[OK] {username_for_file} {api_key} {query_value} ({elapsed:.2f}s)")
        except Exception:
            m = await context.application.bot.send_message(chat_id=ep.chat_id, text=final_text, parse_mode="HTML")
            track_ephemeral(m.chat_id, m.message_id)
            await send_log(context.application, f"[OK send] {username_for_file} {api_key} {query_value} ({elapsed:.2f}s)")
    else:
        # send file: delete ephemeral first, then send file (file must be preserved)
        try:
            await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text=f"{summary}\n📄 Resultado extenso — enviando arquivo .txt...")
        except Exception:
            pass
        txt_bytes = generate_txt_bytes(f"{api_key}_{query_value}", cleaned, username_for_file)
        bio = io.BytesIO(txt_bytes)
        bio.name = f"{api_key}_{query_value}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.txt"
        # delete ephemeral BEFORE sending final file (per your instructions)
        await delete_ephemeral(context.application, ep.chat_id)
        try:
            sent = await context.application.bot.send_document(chat_id=update.effective_chat.id, document=bio, filename=bio.name,
                                                             caption=f"✅ Resultado — {query_value}\n\n🤖 {BOT_DISPLAY_NAME}\n👤 @{username_for_file}")
            # IMPORTANT: do NOT delete file messages later. But we still store last ephemeral as this id
            # so next ephemeral deletion will remove it if desired. To follow your instruction we MUST NOT delete file messages.
            # so we will not use LAST_EPHEMERAL to delete this message.
            await send_log(context.application, f"[OK file] {username_for_file} {api_key} {query_value} ({elapsed:.2f}s) file:{bio.name}")
        except Exception as e:
            await send_log(context.application, f"[ERRO_SEND_FILE] {username_for_file} {api_key} {query_value} -> {e}")
            await context.application.bot.send_message(chat_id=update.effective_chat.id, text="❌ Erro ao enviar arquivo: tente novamente.")

# ---------------- CPF_FULL handler ----------------
async def cmd_cpf_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_ephemeral(context.application, update.effective_chat.id)
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        m = await context.application.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Envie: /cpf_full 12345678900")
        track_ephemeral(update.effective_chat.id, m.message_id)
        return
    cpf = re.sub(r"\D", "", parts[1].strip())
    ep = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Iniciando CPF_FULL para {cpf}...", parse_mode="HTML")
    track_ephemeral(update.effective_chat.id, ep.message_id)
    await asyncio.sleep(0.5)
    start = time.time()

    keys = [k for k in API_ENDPOINTS.keys() if k.startswith("cpf_")]
    tasks = []
    for k in keys:
        tpl = API_ENDPOINTS.get(k)
        if tpl:
            tasks.append(fetch_with_retries(tpl.format(valor=cpf)))
    if not tasks:
        await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text="⚠️ Nenhuma fonte de CPF configurada.", parse_mode="HTML")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid = []
    for r in results:
        if isinstance(r, Exception):
            continue
        if isinstance(r, dict) and r.get("status") == "ERROR":
            continue
        valid.append(r)

    if not valid:
        await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text="❌ Todas as fontes falharam ou retornaram vazias.", parse_mode="HTML")
        await send_log(context.application, f"[CPF_FULL FAIL] {cpf}")
        return

    # merge/dedupe
    merged: Dict[str, Any] = {}
    for d in valid:
        if isinstance(d, dict):
            for k, v in d.items():
                if k in FIELDS_TO_REMOVE: continue
                if k not in merged:
                    merged[k] = v
                else:
                    if merged[k] != v:
                        ex = merged[k]
                        if not isinstance(ex, list):
                            ex = [ex]
                        if v not in ex:
                            ex.append(v)
                        merged[k] = ex

    elapsed = time.time() - start
    try:
        await context.application.bot.edit_message_text(chat_id=ep.chat_id, message_id=ep.message_id, text=f"✅ CPF_FULL concluído — tempo: {elapsed:.2f}s", parse_mode="HTML")
    except Exception:
        pass

    # generate file and send (delete ephemeral prior)
    txt_bytes = generate_txt_bytes(f"CPF_FULL_{cpf}", merged, update.effective_user.username or update.effective_user.first_name)
    bio = io.BytesIO(txt_bytes)
    bio.name = f"CPF_FULL_{cpf}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.txt"
    await delete_ephemeral(context.application, update.effective_chat.id)
    try:
        sent = await context.application.bot.send_document(chat_id=update.effective_chat.id, document=bio, filename=bio.name,
                                                         caption=f"✅ Resultado CPF_FULL — {cpf}\n\n🤖 {BOT_DISPLAY_NAME}\n👤 @{update.effective_user.username or update.effective_user.first_name}")
        await send_log(context.application, f"[CPF_FULL OK] {update.effective_user.username or update.effective_user.first_name} {cpf} ({elapsed:.2f}s)")
    except Exception as e:
        await send_log(context.application, f"[CPF_FULL SEND ERROR] {e}")
        await context.application.bot.send_message(chat_id=update.effective_chat.id, text="❌ Erro ao enviar arquivo.")

# ---------------- Other command wrappers ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_ephemeral(context.application, update.effective_chat.id)
    keyboard = [
        [InlineKeyboardButton("🔍 CPF", callback_data="menu_cpf")],
        [InlineKeyboardButton("📂 CPF FULL", callback_data="menu_cpf_full")],
        [InlineKeyboardButton("🚗 Veículo (placa/chassi/cnh)", callback_data="menu_veiculo")],
        [InlineKeyboardButton("🌐 IP / MAC", callback_data="menu_net")],
        [InlineKeyboardButton("💬 Suporte", url=f"https://t.me/{SUPORTE_USERNAME.replace('@','')}")],
    ]
    text = (
        f"👋 <b>Bem-vindo ao {BOT_DISPLAY_NAME}</b>\n\n"
        "Use os comandos ou escolha uma opção abaixo.\n\n"
        "<b>Comandos</b>:\n"
        "• /cpf &lt;cpf&gt;\n"
        "• /cpf_full &lt;cpf&gt;\n"
        "• /nome &lt;nome&gt;\n"
        "• /telefone &lt;telefone&gt;\n"
        "• /email &lt;email&gt;\n"
        "• /placa &lt;placa&gt;\n"
        "• /cnh &lt;numero&gt;\n"
        "• /chassi &lt;chassi&gt;\n"
        "• /ip &lt;ip&gt;\n"
        "• /mac &lt;mac&gt;\n\n"
        f"Suporte: {SUPORTE_USERNAME}"
    )
    msg = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    track_ephemeral(update.effective_chat.id, msg.message_id)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🌐 <b>Status das APIs</b>:"]
    for k, v in API_STATUS.items():
        name = k.replace("cpf_", "").replace("_serpro", "").upper()
        icon = v.get("icon", "🔴")
        rt = v.get("rt")
        rtstr = f"{rt:.2f}s" if rt else "–"
        lines.append(f"{icon} {name} ({rtstr})")
    await context.application.bot.send_message(chat_id=update.effective_chat.id, text="\n".join(lines), parse_mode="HTML")

# Generic menu handlers for commands that expect a value
async def generic_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, options: List[Tuple[str,str]], title: str):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        m = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"⚠️ Envie: /{field} <valor>")
        track_ephemeral(update.effective_chat.id, m.message_id)
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = title
    markup = build_menu_buttons(options)
    await delete_ephemeral(context.application, update.effective_chat.id)
    sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"Selecione a fonte para consultar {title} <code>{query}</code>:", parse_mode="HTML", reply_markup=markup)
    track_ephemeral(update.effective_chat.id, sent.message_id)

async def cmd_cpf_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "cpf", [("Serasa","cpf_serasa"),("Assec","cpf_assec"),("BigData","cpf_bigdata"),("Datasus","cpf_datasus"),("Credilink","cpf_credilink"),("SPC","cpf_spc")], "CPF")

async def cmd_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "nome", [("Serasa","nome_serasa")], "Nome")

async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "email", [("Serasa","email_serasa")], "E-mail")

async def cmd_telefone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "telefone", [("Credilink","telefone_credilink")], "Telefone")

async def cmd_placa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "placa", [("Serpro","placa_serpro")], "Placa")

async def cmd_cnh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "cnh", [("Serpro","cnh_serpro")], "CNH")

async def cmd_chassi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "chassi", [("Serpro","chassi_serpro")], "Chassi")

async def cmd_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "ip", [("IP API","ip_api")], "IP")

async def cmd_mac(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generic_menu_command(update, context, "mac", [("MAC Vendors","mac_api")], "MAC")

# Detect plain messages (auto-detect CPF/placa/ip/email/chassi)
async def text_detect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    dtype = detect_type(text)
    if not dtype:
        return
    await delete_ephemeral(context.application, update.effective_chat.id)
    context.user_data["last_query"] = text
    context.user_data["last_query_title"] = dtype.upper()
    # build quick option
    if dtype == "cpf":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar CPF (Serasa)", callback_data="cpf_serasa")], [InlineKeyboardButton("📂 CPF FULL", callback_data="cpf_full")]])
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei CPF — <code>{text}</code>. Escolha:", parse_mode="HTML", reply_markup=kb)
        track_ephemeral(update.effective_chat.id, sent.message_id)
    elif dtype == "placa":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar Placa", callback_data="placa_serpro")]])
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei Placa — <code>{text}</code>. Escolha:", parse_mode="HTML", reply_markup=kb)
        track_ephemeral(update.effective_chat.id, sent.message_id)
    elif dtype == "chassi":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar Chassi", callback_data="chassi_serpro")]])
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei Chassi — <code>{text}</code>. Escolha:", parse_mode="HTML", reply_markup=kb)
        track_ephemeral(update.effective_chat.id, sent.message_id)
    elif dtype == "ip":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar IP", callback_data="ip_api")]])
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei IP — <code>{text}</code>. Escolha:", parse_mode="HTML", reply_markup=kb)
        track_ephemeral(update.effective_chat.id, sent.message_id)
    elif dtype == "email":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar E-mail", callback_data="email_serasa")]])
        sent = await context.application.bot.send_message(chat_id=update.effective_chat.id, text=f"🔍 Detectei E-mail — <code>{text}</code>. Escolha:", parse_mode="HTML", reply_markup=kb)
        track_ephemeral(update.effective_chat.id, sent.message_id)

# ---------------- Handlers registration ----------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cpf", cmd_cpf_menu))
    app.add_handler(CommandHandler("cpf_full", cmd_cpf_full))
    app.add_handler(CommandHandler("nome", cmd_nome))
    app.add_handler(CommandHandler("email", cmd_email))
    app.add_handler(CommandHandler("telefone", cmd_telefone))
    app.add_handler(CommandHandler("placa", cmd_placa))
    app.add_handler(CommandHandler("cnh", cmd_cnh))
    app.add_handler(CommandHandler("chassi", cmd_chassi))
    app.add_handler(CommandHandler("ip", cmd_ip))
    app.add_handler(CommandHandler("mac", cmd_mac))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_detect_handler))

    logger.info("Handlers registrados com sucesso.")

# ---------------- FastAPI webhook for Render ----------------
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
        # try announce (may fail if topic closed)
        try:
            await application.bot.send_message(chat_id=UPDATE_CHANNEL_ID, text=f"{BOT_DISPLAY_NAME} iniciado em {datetime.utcnow().isoformat()} UTC")
        except Exception as e:
            logger.warning(f"Announce update failed: {e}")
        # log started
        await send_log(application, f"{BOT_DISPLAY_NAME} iniciado em {datetime.utcnow().isoformat()} UTC")
        logger.info("Bot Telegram iniciado")
    except Exception as e:
        logger.error(f"Erro ao iniciar bot: {e}")
        await send_log(application, f"Erro ao iniciar bot: {e}")

@webhook_app.post(f"/{TELEGRAM_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

async def set_webhook_on_render(application: Application, token: str):
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

# ---------------- Local entrypoint (polling) ----------------
if __name__ == "__main__":
    application.run_polling()
