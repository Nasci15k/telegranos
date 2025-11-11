"""
Bot de Consultas Profissional (Telegram)
Compatível com Render (FastAPI + Gunicorn + Uvicorn + Webhook)
Versão: 11/11/2025 — Estável
"""

import logging
import json
import requests
import io
import time
import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

# ===== LOGGING =====
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIGURAÇÕES =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TELEGRAM_TOKEN_AQUI")
FETCHBRASIL_TOKEN = os.environ.get("FETCHBRASIL_TOKEN", "SEU_FETCHBRASIL_TOKEN_AQUI")
BASE_URL_APIS_BRASIL = os.environ.get("BASE_URL_APIS_BRASIL", "https://apis-brasil.shop/apis/")
BASE_URL_FETCHBRASIL = os.environ.get("BASE_URL_FETCHBRASIL", "https://api.fetchbrasil.com.br/")
PORT = int(os.environ.get("PORT", 8000))

# ===== FUNÇÕES AUXILIARES =====
def format_json_to_markdown(data, indent=0):
    if not isinstance(data, (dict, list)) or not data:
        return ""
    markdown = ""
    space = "  " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            title = k.replace("_", " ").title()
            if isinstance(v, (dict, list)):
                markdown += f"{space}*{title}*:\n{format_json_to_markdown(v, indent + 1)}"
            elif v not in [None, "", "null"]:
                markdown += f"{space}*{title}*: `{v}`\n"
    elif isinstance(data, list):
        for i in data:
            markdown += format_json_to_markdown(i, indent)
    return markdown

def format_json_to_pdf(data, styles, elements):
    if isinstance(data, dict):
        for k, v in data.items():
            title = k.replace("_", " ").title()
            if isinstance(v, (dict, list)):
                elements.append(Paragraph(f"<b>{title}:</b>", styles["Normal"]))
                format_json_to_pdf(v, styles, elements)
            elif v not in [None, "", "null"]:
                elements.append(Paragraph(f"<b>{title}:</b> {v}", styles["Normal"]))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            elements.append(Paragraph(f"Item {i+1}:", styles["Normal"]))
            format_json_to_pdf(item, styles, elements)

def generate_pdf(title, data):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"<b>Relatório de Consulta: {title}</b>", styles["h1"]),
        Spacer(1, 0.3 * inch),
        Paragraph(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]
    format_json_to_pdf(data, styles, elements)
    doc.build(elements)
    buffer.seek(0)
    return buffer

# ===== FUNÇÕES DE API =====
def fetch_api(url, params=None):
    try:
        response = requests.get(url, params=params, timeout=40)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}

def fetch_apis_brasil(endpoint, param_name, query):
    return fetch_api(f"{BASE_URL_APIS_BRASIL}{endpoint}", {param_name: query})

def fetch_fetchbrasil_api(endpoint, query):
    if not FETCHBRASIL_TOKEN or FETCHBRASIL_TOKEN == "SEU_FETCHBRASIL_TOKEN_AQUI":
        return {"status": "ERROR", "message": "Token FETCHBRASIL_TOKEN não configurado."}
    return fetch_api(f"{BASE_URL_FETCHBRASIL}{endpoint}.php", {"token": FETCHBRASIL_TOKEN, "chave": query})

# ===== MAPEAMENTO =====
api_map = {
    "api_serasacpf": (lambda q: fetch_apis_brasil("apiserasacpf2025.php", "cpf", q), "Serasa CPF"),
    "api_serasanome": (lambda q: fetch_apis_brasil("apiserasanome2025.php", "nome", q), "Serasa Nome"),
    "api_fetchbrasil_cpf": (lambda q: fetch_fetchbrasil_api("cpf_basico", q), "FetchBrasil CPF"),
    "api_fetchbrasil_nome": (lambda q: fetch_fetchbrasil_api("nome_basico", q), "FetchBrasil Nome"),
    "api_fetchbrasil_placa": (lambda q: fetch_fetchbrasil_api("placa_basico", q), "FetchBrasil Placa"),
}

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Bot de Consultas*\n\n"
        "Comandos disponíveis:\n"
        "/cpf `<número>`\n"
        "/nome_completo `<nome>`\n"
        "/placa `<placa>`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

def extract_query(text):
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""

async def menu_query_handler(update: Update, context, title, buttons):
    query = extract_query(update.message.text)
    if not query:
        await update.message.reply_text(f"⚠️ Informe o {title}. Exemplo: /cpf 12345678901")
        return
    context.user_data["last_query"] = query
    keyboard = [[InlineKeyboardButton(text, callback_data=data)] for text, data in buttons]
    await update.message.reply_text(
        f"Selecione a API para consultar o {title} `{query}`:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def handle_api_call(query, fetch_func, api_name, update):
    data = await asyncio.to_thread(fetch_func, query)
    if data.get("status") == "ERROR":
        await update.effective_message.reply_text(f"❌ Erro na consulta {api_name}:\n`{data['message']}`", parse_mode="Markdown")
        return None
    return data

async def button_callback(update, context):
    q = update.callback_query
    await q.answer()
    data_id = q.data
    api_info = api_map.get(data_id)
    query = context.user_data.get("last_query")
    if not api_info or not query:
        await q.edit_message_text("Sessão expirada. Use o comando novamente.")
        return
    fetch_func, api_name = api_info
    await q.edit_message_text(f"⏳ Consultando {api_name}...")
    data = await handle_api_call(query, fetch_func, api_name, update)
    if data:
        markdown_output = format_json_to_markdown(data)
        await q.edit_message_text(f"✅ *Resultado {api_name}*\n\n{markdown_output}", parse_mode="Markdown")

# ===== REGISTRO =====
def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cpf", lambda u, c: menu_query_handler(u, c, "CPF", [("Serasa", "api_serasacpf"), ("FetchBrasil", "api_fetchbrasil_cpf")])))
    app.add_handler(CommandHandler("nome_completo", lambda u, c: menu_query_handler(u, c, "Nome", [("Serasa", "api_serasanome"), ("FetchBrasil", "api_fetchbrasil_nome")])))
    app.add_handler(CommandHandler("placa", lambda u, c: menu_query_handler(u, c, "Placa", [("FetchBrasil", "api_fetchbrasil_placa")])))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Handlers registrados com sucesso.")

# ===== APLICAÇÃO =====
application = Application.builder().token(TELEGRAM_TOKEN).build()
register_handlers(application)

# ===== FASTAPI PARA WEBHOOK =====
from fastapi import FastAPI, Request
webhook_app = FastAPI()

@webhook_app.on_event("startup")
async def on_startup():
    logger.info("🚀 Iniciando bot no Render...")
    await application.initialize()
    await application.start()
    logger.info("✅ Bot pronto para receber atualizações.")

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

# ===== EXECUÇÃO LOCAL =====
async def start_local_polling():
    if not os.environ.get("RENDER_EXTERNAL_URL"):
        logger.warning("Executando localmente (polling).")
        await application.run_polling()

if __name__ == "__main__":
    asyncio.run(start_local_polling())
