# bot.py
"""
Icsan Search Bot - Versão 2.0 (final)
Inclui:
- /cpf_full (todas as 6 APIs de CPF) -> gera .txt consolidado
- inline menu inteligente (detecta tipo do dado e sugere comandos)
- logs automáticos em canal (LOG_CHANNEL_ID)
- animação de mensagens (consultando -> processando -> resultado)
- anúncio automático de atualização no canal (UPDATE_CHANNEL_ID)
- remoção de FetchBrasil, SERPRO com retries/backoff
- tratamento de respostas não-JSON (Expecting value)
- cache simples, healthcheck de APIs no startup
- limpeza de mensagens de usuário (se permitido)
- webhook FastAPI para Render
"""

import os
import io
import time
import json
import logging
import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple

import requests
from fastapi import FastAPI, Request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------------- CONFIG ----------------
BOT_NAME = "Icsan Search"
BOT_USERNAME = "@IcsanSearchBot"
SUPORTE_USERNAME = "@astrahvhdev"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TELEGRAM_TOKEN_AQUI")
PORT = int(os.environ.get("PORT", 8000))

# use provided group id as default; you can override via env vars
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "1003027034402"))
UPDATE_CHANNEL_ID = int(os.environ.get("UPDATE_CHANNEL_ID", "1003027034402"))

BASE_URL_APIS_BRASIL = "https://apis-brasil.shop/apis/"
BASE_URL_SERPRO = "https://apiradar.onrender.com"

# CPF APIs list (name, full_url, param_name)
CPF_APIS = [
    ("api_serasacpf", f"{BASE_URL_APIS_BRASIL}apiserasacpf2025.php", "cpf"),
    ("api_asseccpf", f"{BASE_URL_APIS_BRASIL}apiassecc2025.php", "cpf"),
    ("api_bigdatacpf", f"{BASE_URL_APIS_BRASIL}apicpfbigdata2025.php", "CPF"),
    ("api_datasuscpf", f"{BASE_URL_APIS_BRASIL}apicpfdatasus.php", "cpf"),
    ("api_credilinkcpf", f"{BASE_URL_APIS_BRASIL}apicpfcredilink2025.php", "cpf"),
    ("api_spc", f"{BASE_URL_APIS_BRASIL}apicpf27spc.php", "cpf"),
]

API_ENDPOINTS = {
    "serasanome": (f"{BASE_URL_APIS_BRASIL}apiserasanome2025.php", "nome"),
    "serasaemail": (f"{BASE_URL_APIS_BRASIL}apiserasaemail2025.php", "email"),
    "credilinktel": (f"{BASE_URL_APIS_BRASIL}apitelcredilink2025.php", "telefone"),
    "ip_api": ("http://ip-api.com/json/{q}", None),
    "mac_api": ("https://api.macvendors.com/{q}", None),
}

# ---------------- LOGGING ----------------
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("icsan_bot")

# ---------------- GLOBALS ----------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "IcsanSearchBot/2.0"})

CACHE: Dict[str, Tuple[float, Any]] = {}  # key -> (expiry_timestamp, value)
CACHE_TTL = 600  # 10 minutes

API_STATUS: Dict[str, bool] = {}
FIELDS_TO_REMOVE = {"status", "message", "mensagem", "source", "token", "timestamp", "limit", "success", "code", "error"}

# ---------------- UTILITIES ----------------
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

def safe_json(response: requests.Response) -> Any:
    text = response.text.strip()
    if not text:
        return {"status": "ERROR", "message": "A API não retornou dados (resposta vazia)."}
    if text.startswith("<"):
        return {"status": "ERROR", "message": "A API retornou HTML (erro do servidor)."}
    try:
        return response.json()
    except ValueError:
        snippet = text[:1000]
        return {"status": "ERROR", "message": f"Resposta inesperada (não JSON): {snippet}"}

def fetch_api_with_retries(url: str, params: dict = None, timeout: int = 20, retries: int = 2, backoff: List[int] = [1,3,5]) -> Any:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = backoff[min(attempt, len(backoff)-1)]
                logger.warning(f"429 from {url} - sleeping {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return safe_json(r)
        except requests.RequestException as e:
            last_exc = e
            wait = backoff[min(attempt, len(backoff)-1)] if attempt < len(backoff) else backoff[-1]
            logger.warning(f"RequestException {e} for {url} (attempt {attempt+1}), sleeping {wait}s")
            time.sleep(wait)
    return {"status": "ERROR", "message": f"Falha ao acessar API ({url}): {last_exc}"}

def fetch_serpro(tipo: str, valor: str) -> Any:
    key = f"serpro:{tipo}:{valor}"
    cached = cache_get(key)
    if cached:
        return cached
    params = {tipo: valor}
    res = fetch_api_with_retries(BASE_URL_SERPRO, params=params, retries=3, backoff=[1,2,4])
    cache_set(key, res, ttl=300)
    return res

def fetch_generic_apibrasil(endpoint: str, param_name: str, query: str) -> Any:
    key = f"apibrasil:{endpoint}:{query}"
    cached = cache_get(key)
    if cached:
        return cached
    res = fetch_api_with_retries(endpoint, params={param_name: query}, retries=2, backoff=[1,2])
    cache_set(key, res)
    return res

def fetch_ip_api(q: str) -> Any:
    key = f"ip:{q}"
    cached = cache_get(key)
    if cached:
        return cached
    url = API_ENDPOINTS["ip_api"][0].format(q=q)
    res = fetch_api_with_retries(url, timeout=10)
    cache_set(key, res)
    return res

def fetch_mac_api(q: str) -> Any:
    key = f"mac:{q}"
    cached = cache_get(key)
    if cached:
        return cached
    url = API_ENDPOINTS["mac_api"][0].format(q=q)
    res = fetch_api_with_retries(url, timeout=10)
    cache_set(key, res)
    return res

# ---------------- CLEAN & MERGE ----------------
def clean_api_data(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
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
        cleaned_list = [clean_api_data(item) for item in data]
        return [i for i in cleaned_list if i not in [None, "", [], {}]]
    else:
        return data

def merge_results(list_of_dicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for d in list_of_dicts:
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if k not in result:
                result[k] = v
            else:
                existing = result[k]
                if isinstance(existing, dict) and isinstance(v, dict):
                    result[k] = merge_results([existing, v])
                elif isinstance(existing, list):
                    if isinstance(v, list):
                        for it in v:
                            if it not in existing:
                                existing.append(it)
                    else:
                        if v not in existing:
                            existing.append(v)
                    result[k] = existing
                else:
                    if existing == v:
                        result[k] = existing
                    else:
                        vals = []
                        if isinstance(existing, list):
                            vals = existing
                        else:
                            vals = [existing]
                        if isinstance(v, list):
                            for it in v:
                                if it not in vals:
                                    vals.append(it)
                        else:
                            if v not in vals:
                                vals.append(v)
                        result[k] = vals
    return result

def format_txt(data: Any, indent: int = 0) -> str:
    lines: List[str] = []
    prefix = " " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            display_k = k.replace("_", " ").capitalize()
            if isinstance(v, dict):
                lines.append(f"{prefix}{display_k}:")
                lines.append(format_txt(v, indent + 4))
            elif isinstance(v, list):
                if all(not isinstance(i, (dict, list)) for i in v):
                    joined = " | ".join(map(str, v))
                    lines.append(f"{prefix}{display_k}: {joined}")
                else:
                    lines.append(f"{prefix}{display_k}:")
                    for i, item in enumerate(v, 1):
                        lines.append(f"{prefix}  - Item {i}:")
                        lines.append(format_txt(item, indent + 6))
            else:
                lines.append(f"{prefix}{display_k}: {v}")
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
    full_text = header + (formatted if formatted.strip() else "(sem campos relevantes)") + footer
    return full_text.encode("utf-8")

# ---------------- HEALTHCHECK ----------------
async def check_api_health():
    for name, url, param in CPF_APIS:
        try:
            r = SESSION.get(url, params={param: "00000000000"}, timeout=5)
            API_STATUS[name] = (r.status_code == 200 and bool(r.text.strip()))
        except Exception as e:
            API_STATUS[name] = False
            logger.warning(f"Healthcheck fail for {name}: {e}")

    try:
        r = SESSION.get(BASE_URL_SERPRO, timeout=5)
        API_STATUS["serpro"] = (r.status_code == 200 and bool(r.text.strip()))
    except Exception as e:
        API_STATUS["serpro"] = False
        logger.warning(f"Healthcheck fail for serpro: {e}")

    try:
        r = SESSION.get("http://ip-api.com/json/8.8.8.8", timeout=5)
        API_STATUS["ip_api"] = (r.status_code == 200 and bool(r.text.strip()))
    except Exception:
        API_STATUS["ip_api"] = False

    try:
        r = SESSION.get("https://api.macvendors.com/00:00:00:00:00:00", timeout=5)
        API_STATUS["mac_api"] = (r.status_code in (200, 204) and bool(r.text.strip()))
    except Exception:
        API_STATUS["mac_api"] = False

    for key in ["serasanome", "serasaemail", "credilinktel"]:
        try:
            endpoint, param = API_ENDPOINTS[key]
            r = SESSION.get(endpoint, params={param: "test"}, timeout=5)
            API_STATUS[key] = (r.status_code == 200 and bool(r.text.strip()))
        except Exception:
            API_STATUS[key] = False

    logger.info(f"API status: {API_STATUS}")

# ---------------- TELEGRAM HELPERS ----------------
async def try_delete_message(msg):
    try:
        if msg:
            await msg.delete()
    except Exception as e:
        logger.debug(f"Could not delete message: {e}")

async def notify_log_channel(application: Application, text: str):
    try:
        await application.bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Failed to send log message to channel {LOG_CHANNEL_ID}: {e}")

async def announce_update(application: Application):
    text = (
        f"🚀 *Icsan Search reiniciado / atualizado*\n\n"
        f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"✅ Status: Online\n"
    )
    try:
        await application.bot.send_message(chat_id=UPDATE_CHANNEL_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Failed to send update announcement to channel {UPDATE_CHANNEL_ID}: {e}")

# ---------------- DETECTION (inline menu) ----------------
def detect_data_type(query: str) -> str:
    q = query.strip()
    # CPF: 11 digits (optionally with punctuation)
    cpf_plain = re.sub(r"\D", "", q)
    if re.fullmatch(r"\d{11}", cpf_plain):
        return "cpf"
    # placa brasileiro pattern (7 chars, letters+digits) simplified
    if re.fullmatch(r"[A-Za-z]{3}\d{4}", q.replace("-", "").replace(" ", "").upper()):
        return "placa"
    # chassi: 17 chars alnum
    if re.fullmatch(r"[A-Za-z0-9]{17}", q):
        return "chassi"
    # IP
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", q):
        return "ip"
    # email
    if "@" in q and "." in q:
        return "email"
    # default
    return ""

# ---------------- MENU + BUTTONS ----------------
async def menu_query_handler_generic(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str, options: List[Tuple[str, str]]):
    await try_delete_message(update.message)
    text = update.message.text if update.message else ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await update.effective_message.reply_text(f"⚠️ Por favor informe o {title}. Exemplo: /{title.lower()} 123")
        return
    query = parts[1].strip()
    context.user_data["last_query"] = query
    context.user_data["last_query_title"] = title
    keyboard = []
    for label, api_key in options:
        offline = False
        if api_key.startswith("api_"):
            offline = not API_STATUS.get(api_key.replace("api_", ""), True)
        btn_label = label + (" (offline)" if offline else "")
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=api_key)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text(f"Selecione a fonte para consultar *{title}* `{query}`:", parse_mode="Markdown", reply_markup=reply_markup)

# ---------------- BUTTON CALLBACK ----------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    api_key = q.data
    query = context.user_data.get("last_query")
    title = context.user_data.get("last_query_title", "Consulta")
    user = update.effective_user
    if not query:
        await q.edit_message_text("Sessão expirada. Reinicie com o comando.")
        return

    # Animated message sequence
    await q.edit_message_text("🔍 Consultando...")
    await asyncio.sleep(0.8)
    await q.edit_message_text("📊 Processando dados...")
    await asyncio.sleep(0.6)

    start_ts = time.time()
    data = None

    try:
        if api_key == "api_serasacpf":
            data = await asyncio.to_thread(fetch_generic_apibrasil, CPF_APIS[0][1], CPF_APIS[0][2], query)
        elif api_key == "api_asseccpf":
            data = await asyncio.to_thread(fetch_generic_apibrasil, CPF_APIS[1][1], CPF_APIS[1][2], query)
        elif api_key == "api_bigdatacpf":
            data = await asyncio.to_thread(fetch_generic_apibrasil, CPF_APIS[2][1], CPF_APIS[2][2], query)
        elif api_key == "api_datasuscpf":
            data = await asyncio.to_thread(fetch_generic_apibrasil, CPF_APIS[3][1], CPF_APIS[3][2], query)
        elif api_key == "api_credilinkcpf":
            data = await asyncio.to_thread(fetch_generic_apibrasil, CPF_APIS[4][1], CPF_APIS[4][2], query)
        elif api_key == "api_spc":
            data = await asyncio.to_thread(fetch_generic_apibrasil, CPF_APIS[5][1], CPF_APIS[5][2], query)

        elif api_key == "api_serpro_placa":
            if not API_STATUS.get("serpro", True):
                await q.edit_message_text("⚠️ A API Serpro (Radar) está temporariamente indisponível.")
                return
            data = await asyncio.to_thread(fetch_serpro, "placa", query)
        elif api_key == "api_serpro_cnh":
            if not API_STATUS.get("serpro", True):
                await q.edit_message_text("⚠️ A API Serpro (Radar) está temporariamente indisponível.")
                return
            data = await asyncio.to_thread(fetch_serpro, "cnh", query)
        elif api_key == "api_serpro_chassi":
            if not API_STATUS.get("serpro", True):
                await q.edit_message_text("⚠️ A API Serpro (Radar) está temporariamente indisponível.")
                return
            data = await asyncio.to_thread(fetch_serpro, "chassi", query)

        elif api_key == "api_ip":
            if not API_STATUS.get("ip_api", True):
                await q.edit_message_text("⚠️ A API de IP está temporariamente indisponível.")
                return
            data = await asyncio.to_thread(fetch_ip_api, query)
        elif api_key == "api_mac":
            if not API_STATUS.get("mac_api", True):
                await q.edit_message_text("⚠️ A API de MAC está temporariamente indisponível.")
                return
            data = await asyncio.to_thread(fetch_mac_api, query)

        elif api_key == "api_serasanome":
            endpoint, param = API_ENDPOINTS["serasanome"]
            data = await asyncio.to_thread(fetch_generic_apibrasil, endpoint, param, query)
        elif api_key == "api_serasaemail":
            endpoint, param = API_ENDPOINTS["serasaemail"]
            data = await asyncio.to_thread(fetch_generic_apibrasil, endpoint, param, query)
        elif api_key == "api_credilinktel":
            endpoint, param = API_ENDPOINTS["credilinktel"]
            data = await asyncio.to_thread(fetch_generic_apibrasil, endpoint, param, query)
        else:
            await q.edit_message_text("API desconhecida.")
            return
    except Exception as e:
        await q.edit_message_text(f"❌ Erro interno: {e}")
        await notify_log_channel(context.application, f"❌ Erro interno no button_callback: {e}")
        return

    elapsed = time.time() - start_ts

    if not data:
        await q.edit_message_text("❌ Erro desconhecido ao consultar a API.")
        await notify_log_channel(context.application, f"❌ Erro: API retornou nada para {api_key} / {query}")
        return
    if isinstance(data, dict) and data.get("status") == "ERROR":
        msg = data.get("message", "Erro na API")
        await q.edit_message_text(f"❌ Erro na consulta: {msg}")
        await notify_log_channel(context.application, f"❌ Erro API {api_key} para `{query}`: {msg}")
        return

    # Prepare cleaned output and file
    cleaned = clean_api_data(data)
    cleaned_str = format_txt(cleaned)
    user_display = user.username if user.username else user.first_name
    summary = f"✅ Consulta concluída ({api_key})\n⏱️ Tempo de consulta: {elapsed:.2f}s\n"

    # If small enough, send as message; else send txt file
    if len(cleaned_str) <= 3500 and cleaned_str.strip():
        await q.edit_message_text(f"{summary}\n{cleaned_str}\n\n🤖 {BOT_USERNAME}\n👤 @{user_display}", parse_mode="Markdown")
    else:
        await q.edit_message_text(summary + "\n📄 Resultado extenso. Enviando arquivo .txt...")
        txt_bytes = generate_txt_bytes(f"{title} {query}", cleaned, user_display)
        bio = io.BytesIO(txt_bytes)
        bio.name = f"{title}_{query}.txt"
        try:
            await q.message.reply_document(document=bio, filename=bio.name,
                                           caption=f"✅ Resultado completo — {title} {query}\n\n🤖 {BOT_USERNAME}\n👤 @{user_display}")
        except Exception as e:
            await q.edit_message_text(f"❌ Erro ao enviar arquivo: {e}")
            await notify_log_channel(context.application, f"❌ Falha ao enviar arquivo txt para {user_display}: {e}")

# ---------------- CPF_FULL Handler ----------------
async def cmd_cpf_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await try_delete_message(update.message)
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.effective_message.reply_text("⚠️ Informe o CPF. Exemplo: /cpf_full 12345678900")
        return
    cpf = re.sub(r"\D", "", parts[1].strip())
    user = update.effective_user
    status_msg = await update.effective_message.reply_text(f"🔍 Iniciando CPF_FULL para `{cpf}`...\n⏳ Consultando várias fontes...", parse_mode="Markdown")

    start_ts = time.time()
    tasks = []
    for name, url, param in CPF_APIS:
        if not API_STATUS.get(name, True):
            logger.info(f"Skipping {name} because offline")
            continue
        tasks.append(asyncio.to_thread(fetch_generic_apibrasil, url, param, cpf))

    if not tasks:
        await status_msg.edit_text("⚠️ Nenhuma API de CPF disponível no momento. Tente novamente mais tarde.")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"Exception in cpf_full fetch: {r}")
            continue
        if isinstance(r, dict) and r.get("status") == "ERROR":
            logger.info(f"CPF API returned error: {r.get('message')}")
            continue
        valid.append(r)

    if not valid:
        await status_msg.edit_text("❌ Todas as APIs retornaram erro ou não retornaram dados.")
        await notify_log_channel(context.application, f"❌ CPF_FULL: todas as APIs falharam para {cpf}")
        return

    cleaned_list = [clean_api_data(v) for v in valid if v]
    merged = merge_results(cleaned_list)
    elapsed = time.time() - start_ts

    # short summary
    short = "Resultado consolidado pronto."
    for key in ("nome", "name", "Nome", "Name"):
        if key in merged:
            short = f"Nome: {merged[key]}"
            break
    summary_text = f"✅ CPF_FULL concluído\n⏱️ Tempo de consulta: {elapsed:.2f}s\n{short}"
    await status_msg.edit_text(summary_text)

    txt_bytes = generate_txt_bytes(f"CPF_FULL_{cpf}", merged, user.username if user.username else user.first_name)
    bio = io.BytesIO(txt_bytes)
    bio.name = f"CPF_FULL_{cpf}.txt"
    try:
        await update.effective_message.reply_document(document=bio, filename=bio.name,
            caption=f"✅ Resultado CPF_FULL — {cpf}\n\n🤖 {BOT_USERNAME}\n👤 @{user.username if user.username else user.first_name}")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Erro ao enviar arquivo: {e}")
        await notify_log_channel(context.application, f"❌ Falha ao enviar CPF_FULL txt: {e}")

# ---------------- SIMPLE COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await try_delete_message(update.message)
    keyboard = [[InlineKeyboardButton("💬 Suporte", url=f"https://t.me/{SUPORTE_USERNAME.replace('@','')}")]]
    text = (
        f"👋 Bem-vindo ao *{BOT_NAME}*\n\n"
        "🔍 *Módulos de Consulta:*\n"
        "• /cpf `<número>` — selecione fonte\n"
        "• /cpf_full `<número>` — Todas as fontes (arquivo .txt consolidado)\n"
        "• /nome `<nome>`\n"
        "• /placa `<placa>`\n"
        "• /cnh `<número>`\n"
        "• /chassi `<chassi>`\n"
        "• /ip `<endereço>`\n"
        "• /mac `<endereço>`\n"
        "• /email `<email>`\n"
        "• /telefone `<telefone>`\n\n"
        f"📞 Suporte: {SUPORTE_USERNAME}"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_suporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await try_delete_message(update.message)
    await update.effective_message.reply_text(f"💬 Suporte: {SUPORTE_USERNAME}")

# wrappers to open menu
async def cmd_cpf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [
        ("Serasa", "api_serasacpf"),
        ("Assec", "api_asseccpf"),
        ("BigData", "api_bigdatacpf"),
        ("Datasus", "api_datasuscpf"),
        ("Credilink", "api_credilinkcpf"),
        ("SPC Consolidado", "api_spc"),
    ]
    await menu_query_handler_generic(update, context, "CPF", options)

async def cmd_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Serasa", "api_serasanome")]
    await menu_query_handler_generic(update, context, "Nome", options)

async def cmd_placa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Serpro", "api_serpro_placa")]
    await menu_query_handler_generic(update, context, "Placa", options)

async def cmd_cnh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Serpro", "api_serpro_cnh")]
    await menu_query_handler_generic(update, context, "CNH", options)

async def cmd_chassi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Serpro", "api_serpro_chassi")]
    await menu_query_handler_generic(update, context, "Chassi", options)

async def cmd_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Consulta IP", "api_ip")]
    await menu_query_handler_generic(update, context, "IP", options)

async def cmd_mac(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Consulta MAC", "api_mac")]
    await menu_query_handler_generic(update, context, "MAC", options)

async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Serasa", "api_serasaemail")]
    await menu_query_handler_generic(update, context, "Email", options)

async def cmd_telefone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [("Credilink", "api_credilinktel")]
    await menu_query_handler_generic(update, context, "Telefone", options)

# ---------------- INLINE DETECTION HANDLER ----------------
async def text_handler_detect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if user types plain text (not command), detect and propose actions
    text = (update.message.text or "").strip()
    if not text:
        return
    dtype = detect_data_type(text)
    if not dtype:
        return
    await try_delete_message(update.message)
    user = update.effective_user
    if dtype == "cpf":
        keyboard = [
            [InlineKeyboardButton("🔎 Consultar CPF", callback_data="api_serasacpf")],
            [InlineKeyboardButton("📂 CPF FULL", callback_data="cpf_full_inline")]
        ]
        msg = f"🔍 Detectei um *CPF* — `{text}`. O que deseja fazer?"
        await update.effective_chat.send_message(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        # store last query
        context.user_data["last_query"] = re.sub(r"\D", "", text)
        context.user_data["last_query_title"] = "CPF"
    elif dtype == "placa":
        keyboard = [
            [InlineKeyboardButton("🔎 Consultar Placa", callback_data="api_serpro_placa")]
        ]
        msg = f"🔍 Detectei uma *Placa* — `{text}`. O que deseja fazer?"
        await update.effective_chat.send_message(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "Placa"
    elif dtype == "chassi":
        keyboard = [[InlineKeyboardButton("🔎 Consultar Chassi", callback_data="api_serpro_chassi")]]
        msg = f"🔍 Detectei um *Chassi* — `{text}`. O que deseja fazer?"
        await update.effective_chat.send_message(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "Chassi"
    elif dtype == "ip":
        keyboard = [[InlineKeyboardButton("🔎 Consultar IP", callback_data="api_ip")]]
        await update.effective_chat.send_message(f"🔍 Detectei IP — `{text}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "IP"
    elif dtype == "email":
        keyboard = [[InlineKeyboardButton("🔎 Consultar E-mail", callback_data="api_serasaemail")]]
        await update.effective_chat.send_message(f"🔍 Detectei E-mail — `{text}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["last_query"] = text
        context.user_data["last_query_title"] = "Email"

# ---------------- REGISTER HANDLERS ----------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("suporte", cmd_suporte))
    app.add_handler(CommandHandler("cpf", cmd_cpf))
    app.add_handler(CommandHandler("cpf_full", cmd_cpf_full))
    app.add_handler(CommandHandler("nome", cmd_nome))
    app.add_handler(CommandHandler("placa", cmd_placa))
    app.add_handler(CommandHandler("cnh", cmd_cnh))
    app.add_handler(CommandHandler("chassi", cmd_chassi))
    app.add_handler(CommandHandler("ip", cmd_ip))
    app.add_handler(CommandHandler("mac", cmd_mac))
    app.add_handler(CommandHandler("email", cmd_email))
    app.add_handler(CommandHandler("telefone", cmd_telefone))
    app.add_handler(CallbackQueryHandler(button_callback))
    # detect plain text (not commands)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler_detect))
    logger.info("Handlers registrados com sucesso.")

# ---------------- APPLICATION + WEBHOOK ----------------
application = Application.builder().token(TELEGRAM_TOKEN).build()
register_handlers(application)

webhook_app = FastAPI()

@webhook_app.on_event("startup")
async def startup_event():
    logger.info("Inicializando Icsan Search...")
    # do healthcheck in thread
    await asyncio.to_thread(lambda: logger.info("Starting API healthcheck..."))
    await check_api_health()
    # announce update
    try:
        await application.initialize()
        await application.start()
        await announce_update(application)
        await notify_log_channel(application, f"✅ Bot iniciado com sucesso em {datetime.utcnow().isoformat()} UTC")
        logger.info("Bot Telegram iniciado.")
    except Exception as e:
        logger.error(f"Erro ao iniciar aplicação Telegram: {e}")
        await notify_log_channel(application, f"❌ Erro ao iniciar bot: {e}")

@webhook_app.post(f"/{TELEGRAM_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"status": "ok"}

async def set_webhook_on_render(application: Application, token: str):
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
    if not RENDER_EXTERNAL_URL:
        logger.warning("RENDER_EXTERNAL_URL não configurada.")
        return
    webhook_url = f"{RENDER_EXTERNAL_URL}/{token}"
    await application.bot.delete_webhook()
    await application.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook configurado com sucesso: {webhook_url}")

# ---------------- Entrypoint ----------------
if __name__ == "__main__":
    # local polling fallback
    asyncio.run(application.run_polling())
