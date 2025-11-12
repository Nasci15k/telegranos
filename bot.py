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
        if r.isdigit() and len(r) >= 10:
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

# ---------------- API Endpoints ----------------
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

    # Serpro / apiradar (placa, chassi, cnh)
    "placa_serpro":      "https://apiradar.onrender.com/api/placa?query={valor}&token=KeyBesh",
    "chassi_serpro":     "https://apiradar.onrender.com/api/chassi?query={valor}&token=KeyBesh",
    "cnh_serpro":        "https://apiradar.onrender.com/api/cnh?query={valor}&token=KeyBesh",

    # IP and MAC
    "ip_api":            "http://ip-api.com/json/{valor}",
    "mac_api":           "https://api.macvendors.com/{valor}",
}

# ---------------- State ----------------
API_STATUS: Dict[str, Dict[str, Any]] = {}
LAST_EPHEMERAL: Dict[int, int] = {}
HTTP_CLIENT = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

FIELDS_TO_REMOVE = {"status", "message", "mensagem", "source", "token", "timestamp", "limit", "success", "code", "error"}
PHRASES_TO_REMOVE = [
    r"sou\s+o\s+don[oa]", r"eu\s+sou\s+o\s+don[oa]", r"consultado\s+por", 
    r"consulta\s+realizada\s+por", r"feito\s+por", r"criado\s+por",
    r"owner", r"created\s+by", r"consulted\s+by"
]

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
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return {"_raw": r.text}
            else:
                last_exc = f"HTTP {r.status_code}"
        except Exception as e:
            last_exc = e
            wait = HTTP_BACKOFF[min(attempt, len(HTTP_BACKOFF)-1)]
            logger.warning(f"RequestException {e} for {url} (attempt {attempt+1}) - sleeping {wait}s")
            await asyncio.sleep(wait)
    return {"status": "ERROR", "message": f"Falha ao acessar API ({url}): {last_exc}"}

def remove_phrases(text: str) -> str:
    if not isinstance(text, str):
        return text
    for phrase in PHRASES_TO_REMOVE:
        text = re.sub(phrase, "", text, flags=re.IGNORECASE)
    # Remove espaços extras e limpa o texto
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def clean_api_data(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if not k: continue
            if k.lower() in FIELDS_TO_REMOVE: continue
            
            cleaned = clean_api_data(v)
            
            # Skip empty values
            if cleaned is None: continue
            if cleaned == "": continue
            if cleaned == "null": continue
            if cleaned == "None": continue
            if cleaned == []: continue
            if cleaned == {}: continue
            
            # Remove phrases from string values
            if isinstance(cleaned, str):
                cleaned = remove_phrases(cleaned)
                if not cleaned: continue
            
            out[k] = cleaned
        return out if out else None
    
    if isinstance(data, list):
        cleaned_list = []
        for i in data:
            cleaned = clean_api_data(i)
            if cleaned is None: continue
            if cleaned == "": continue
            if cleaned == "null": continue
            if cleaned == "None": continue
            if cleaned == []: continue
            if cleaned == {}: continue
            
            if isinstance(cleaned, str):
                cleaned = remove_phrases(cleaned)
                if not cleaned: continue
            
            # Avoid duplicates
            if cleaned not in cleaned_list:
                cleaned_list.append(cleaned)
        return cleaned_list if cleaned_list else None
    
    if isinstance(data, str):
        data = remove_phrases(data)
        return data if data else None
    
    return data

def format_txt(data: Any, indent: int = 0) -> str:
    """Formata dados para arquivo .txt com recuos e organização"""
    lines: List[str] = []
    pref = " " * indent
    
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).replace("_", " ").title()
            if isinstance(v, (dict, list)):
                lines.append(f"{pref}{key}:")
                lines.append(format_txt(v, indent + 4))
            else:
                lines.append(f"{pref}{key}: {v}")
        # Add blank line between major sections
        if indent == 0 and lines:
            lines.append("")
            
    elif isinstance(data, list):
        for i, it in enumerate(data, 1):
            lines.append(f"{pref}- Item {i}:")
            lines.append(format_txt(it, indent + 2))
        if indent == 0 and lines:
            lines.append("")
    else:
        lines.append(f"{pref}{data}")
        
    return "\n".join(lines)

def format_html(data: Any, indent: int = 0) -> str:
    """Formata dados para mensagens Telegram com HTML"""
    lines: List[str] = []
    
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).replace("_", " ").title()
            if isinstance(v, (dict, list)):
                lines.append(f"<b>{key}:</b>")
                lines.append(format_html(v, indent + 1))
            else:
                # Para valores simples, formata em uma linha
                lines.append(f"<b>{key}:</b> {v}")
        # Add blank line between major sections
        if indent == 0 and lines:
            lines.append("")
            
    elif isinstance(data, list):
        for i, it in enumerate(data, 1):
            lines.append(f"<b>• Item {i}:</b>")
            lines.append(format_html(it, indent + 1))
        if indent == 0 and lines:
            lines.append("")
    else:
        lines.append(str(data))
        
    return "\n".join(lines)

def generate_txt_bytes(title: str, data: Any, username: str) -> bytes:
    """Gera arquivo .txt bem formatado"""
    cleaned = clean_api_data(data)
    formatted = format_txt(cleaned)
    
    header = f"📊 RELATÓRIO DE CONSULTA — {title.upper()}\n"
    header += "=" * 60 + "\n"
    header += f"📅 Data: {datetime.utcnow().strftime('%d/%m/%Y %H:%M:%S')} UTC\n"
    header += f"👤 Usuário: @{username if username else 'usuario'}\n"
    header += "=" * 60 + "\n\n"
    
    footer = "\n" + "=" * 60 + "\n"
    footer += f"🤖 {BOT_DISPLAY_NAME}\n"
    footer += f"💬 Suporte: {SUPORTE_USERNAME}\n"
    footer += "=" * 60
    
    if formatted and formatted.strip():
        body = formatted
    else:
        body = "📭 Nenhum dado relevante encontrado na consulta."
    
    content = header + body + footer
    return content.encode("utf-8")

# ---------------- Healthcheck ----------------
async def check_api_health():
    for key, template in API_ENDPOINTS.items():
        test_val = "00000000000" if "cpf" in key else "TEST123"
        url = template.format(valor=test_val)
        start = time.time()
        try:
            r = await HTTP_CLIENT.get(url, timeout=HTTP_TIMEOUT)
            rt = time.time() - start
            if r.status_code == 200:
                API_STATUS[key] = {"icon": classify_rt(rt), "rt": rt}
            else:
                API_STATUS[key] = {"icon": "🔴", "rt": None}
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
    
    if api_key.startswith("menu_"):
        await handle_menu_navigation(update, context, api_key)
        return
        
    if api_key == "cpf_full":
        await handle_cpf_full_from_menu(update, context)
        return
        
    query_value = context.user_data.get("last_query")
    if not query_value:
        await q.edit_message_text("Sessão expirada. Envie o comando novamente.")
        return

    query_value = str(query_value).replace("$", "").strip()
    await delete_ephemeral(context.application, update.effective_chat.id)

    ep = await context.application.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=f"🔍 Consultando <code>{query_value}</code> — fonte: {api_key}", 
        parse_mode="HTML"
    )
    track_ephemeral(update.effective_chat.id, ep.message_id)
    await asyncio.sleep(0.4)
    
    try:
        await context.application.bot.edit_message_text(
            chat_id=ep.chat_id, 
            message_id=ep.message_id, 
            text="📊 Processando...", 
            parse_mode="HTML"
        )
    except Exception:
        pass

    start = time.time()
    if api_key not in API_ENDPOINTS:
        await context.application.bot.edit_message_text(
            chat_id=ep.chat_id, 
            message_id=ep.message_id, 
            text="Fonte não configurada."
        )
        return

    url = API_ENDPOINTS[api_key].format(valor=query_value)
    result = await fetch_with_retries(url)
    elapsed = time.time() - start

    if isinstance(result, dict) and result.get("status") == "ERROR":
        await context.application.bot.edit_message_text(
            chat_id=ep.chat_id, 
            message_id=ep.message_id, 
            text=f"❌ Erro ao consultar a API: tente novamente mais tarde."
        )
        await send_log(context.application, f"[ERRO] {user.username} {api_key} {query_value} -> {result.get('message')}")
        return

    cleaned = clean_api_data(result)
    username_for_file = user.username or user.first_name or "usuario"
    summary = f"✅ <b>Consulta concluída</b> — tempo: {elapsed:.2f}s\n\n"

    # ✅ CORREÇÃO: Formatação HTML para mensagens de texto
    if cleaned and cleaned not in [None, {}, []]:
        textified_html = format_html(cleaned)
        
        if textified_html and len(textified_html) <= 3000 and textified_html.strip():
            final_text = f"{summary}{textified_html}\n\n🤖 {BOT_DISPLAY_NAME}\n👤 @{username_for_file}"
            
            try:
                await context.application.bot.edit_message_text(
                    chat_id=ep.chat_id, 
                    message_id=ep.message_id, 
                    text=final_text, 
                    parse_mode="HTML"
                )
                track_ephemeral(ep.chat_id, ep.message_id)
                await send_log(context.application, f"[OK] {username_for_file} {api_key} {query_value} ({elapsed:.2f}s)")
            except Exception:
                m = await context.application.bot.send_message(
                    chat_id=ep.chat_id, 
                    text=final_text, 
                    parse_mode="HTML"
                )
                track_ephemeral(m.chat_id, m.message_id)
                await send_log(context.application, f"[OK send] {username_for_file} {api_key} {query_value} ({elapsed:.2f}s)")
        else:
            # Resultado muito longo, enviar como arquivo
            try:
                await context.application.bot.edit_message_text(
                    chat_id=ep.chat_id, 
                    message_id=ep.message_id, 
                    text=f"{summary}📄 <b>Resultado extenso</b> — enviando arquivo .txt...", 
                    parse_mode="HTML"
                )
            except Exception:
                pass
            
            # ✅ CORREÇÃO: Formatação TXT para arquivos
            txt_bytes = generate_txt_bytes(f"{api_key}_{query_value}", cleaned, username_for_file)
            bio = io.BytesIO(txt_bytes)
            bio.name = f"consulta_{api_key}_{query_value}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
            
            await delete_ephemeral(context.application, ep.chat_id)
            
            try:
                sent = await context.application.bot.send_document(
                    chat_id=update.effective_chat.id, 
                    document=bio, 
                    filename=bio.name,
                    caption=f"✅ <b>Resultado da consulta</b>\n🔍 {query_value}\n⏱️ {elapsed:.2f}s\n\n🤖 {BOT_DISPLAY_NAME}\n👤 @{username_for_file}",
                    parse_mode="HTML"
                )
                await send_log(context.application, f"[OK file] {username_for_file} {api_key} {query_value} ({elapsed:.2f}s) file:{bio.name}")
            except Exception as e:
                await send_log(context.application, f"[ERRO_SEND_FILE] {username_for_file} {api_key} {query_value} -> {e}")
                await context.application.bot.send_message(
                    chat_id=update.effective_chat.id, 
                    text="❌ Erro ao enviar arquivo: tente novamente."
                )
    else:
        # Nenhum dado retornado
        final_text = f"{summary}📭 <b>Nenhum dado relevante encontrado</b>\n\n🤖 {BOT_DISPLAY_NAME}\n👤 @{username_for_file}"
        try:
            await context.application.bot.edit_message_text(
                chat_id=ep.chat_id, 
                message_id=ep.message_id, 
                text=final_text, 
                parse_mode="HTML"
            )
            track_ephemeral(ep.chat_id, ep.message_id)
        except Exception:
            m = await context.application.bot.send_message(
                chat_id=ep.chat_id, 
                text=final_text, 
                parse_mode="HTML"
            )
            track_ephemeral(m.chat_id, m.message_id)
        await send_log(context.application, f"[OK vazio] {username_for_file} {api_key} {query_value} ({elapsed:.2f}s)")

# ... (o restante do código permanece igual, apenas copiando as funções essenciais)

async def handle_menu_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE, menu_key: str):
    q = update.callback_query
    
    if menu_key == "menu_cpf":
        text = "🔍 <b>Consulta de CPF</b>\n\nDigite o CPF (apenas números):"
        context.user_data["awaiting_input"] = "cpf"
        await q.edit_message_text(text=text, parse_mode="HTML")
        
    elif menu_key == "menu_cpf_full":
        text = "📂 <b>Consulta CPF FULL</b>\n\nDigite o CPF (apenas números):"
        context.user_data["awaiting_input"] = "cpf_full"
        await q.edit_message_text(text=text, parse_mode="HTML")
        
    elif menu_key == "menu_veiculo":
        keyboard = [
            [InlineKeyboardButton("🚗 Placa", callback_data="menu_placa")],
            [InlineKeyboardButton("📄 CNH", callback_data="menu_cnh")],
            [InlineKeyboardButton("🔧 Chassi", callback_data="menu_chassi")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="menu_back")]
        ]
        text = "🚗 <b>Consulta de Veículo</b>\n\nEscolha o tipo de consulta:"
        await q.edit_message_text(text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif menu_key == "menu_placa":
        text = "🚗 <b>Consulta de Placa</b>\n\nDigite a placa do veículo:"
        context.user_data["awaiting_input"] = "placa"
        await q.edit_message_text(text=text, parse_mode="HTML")
        
    elif menu_key == "menu_cnh":
        text = "📄 <b>Consulta de CNH</b>\n\nDigite o número da CNH:"
        context.user_data["awaiting_input"] = "cnh"
        await q.edit_message_text(text=text, parse_mode="HTML")
        
    elif menu_key == "menu_chassi":
        text = "🔧 <b>Consulta de Chassi</b>\n\nDigite o chassi do veículo:"
        context.user_data["awaiting_input"] = "chassi"
        await q.edit_message_text(text=text, parse_mode="HTML")
        
    elif menu_key == "menu_net":
        keyboard = [
            [InlineKeyboardButton("🌐 IP", callback_data="menu_ip")],
            [InlineKeyboardButton("📡 MAC", callback_data="menu_mac")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="menu_back")]
        ]
        text = "🌐 <b>Consulta de Rede</b>\n\nEscolha o tipo de consulta:"
        await q.edit_message_text(text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif menu_key == "menu_ip":
        text = "🌐 <b>Consulta de IP</b>\n\nDigite o endereço IP:"
        context.user_data["awaiting_input"] = "ip"
        await q.edit_message_text(text=text, parse_mode="HTML")
        
    elif menu_key == "menu_mac":
        text = "📡 <b>Consulta de MAC</b>\n\nDigite o endereço MAC:"
        context.user_data["awaiting_input"] = "mac"
        await q.edit_message_text(text=text, parse_mode="HTML")
        
    elif menu_key == "menu_back":
        await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text, 
            parse_mode="HTML", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await context.application.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ... (o restante das funções permanece igual)

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
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_input_handler))

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
        try:
            await application.bot.send_message(chat_id=UPDATE_CHANNEL_ID, text=f"{BOT_DISPLAY_NAME} iniciado em {datetime.utcnow().isoformat()} UTC")
        except Exception as e:
            logger.warning(f"Announce update failed: {e}")
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
