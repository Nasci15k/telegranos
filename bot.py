"""
🤖 ICSAN SEARCH BOT — CONSULTAS PROFISSIONAIS
Bot de Telegram com integração de múltiplas APIs (Serasa, SPC, Serpro, FetchBrasil, Datasus, BigData, etc.)
Compatível com Render (FastAPI + Gunicorn + Uvicorn + Webhook)
Versão Final: 2025-11-12
"""

import logging
import os
import io
import time
import requests
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from fastapi import FastAPI, Request

# ===== CONFIGURAÇÕES =====
BOT_NAME = "Icsan Search"
BOT_USERNAME = "@IcsanSearchBot"
SUPORTE_USERNAME = "@astrahvhdev"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TELEGRAM_TOKEN_AQUI")
FETCHBRASIL_TOKEN = os.environ.get("FETCHBRASIL_TOKEN", "SEU_FETCHBRASIL_TOKEN_AQUI")
PORT = int(os.environ.get("PORT", 8000))

BASE_URL_APIS_BRASIL = "https://apis-brasil.shop/apis/"
BASE_URL_FETCHBRASIL = "https://api.fetchbrasil.com.br/"
BASE_URL_FETCHBRASIL_PRO = "https://api.fetchbrasil.pro/"
BASE_URL_SERPRO = "https://apiradar.onrender.com"

# ===== LOGGING =====
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== FUNÇÕES AUXILIARES =====
def format_json_to_markdown(data, indent=0):
    """Formata JSON em texto Markdown legível para Telegram."""
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


def generate_pdf(title, data):
    """Gera PDF estruturado para consultas longas."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"<b>Relatório de Consulta — {BOT_NAME}</b>", styles["Title"]),
        Spacer(1, 0.2 * inch),
        Paragraph(f"<b>Tipo:</b> {title}", styles["Normal"]),
        Paragraph(f"<b>Data:</b> {time.strftime('%d/%m/%Y %H:%M:%S')}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]
    add_json_to_pdf(data, styles, elements)
    doc.build(elements)
    buffer.seek(0)
    return buffer


def add_json_to_pdf(data, styles, elements):
    if isinstance(data, dict):
        for k, v in data.items():
            title = k.replace("_", " ").title()
            if isinstance(v, (dict, list)):
                elements.append(Paragraph(f"<b>{title}:</b>", styles["Normal"]))
                add_json_to_pdf(v, styles, elements)
            elif v not in [None, "", "null"]:
                elements.append(Paragraph(f"<b>{title}:</b> {v}", styles["Normal"]))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            elements.append(Paragraph(f"Item {i+1}:", styles["Normal"]))
            add_json_to_pdf(item, styles, elements)

# ===== FUNÇÕES DE API =====
def fetch_api(url, params=None):
    try:
        response = requests.get(url, params=params, timeout=40)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}

def fetch_apis_brasil(endpoint, param, query):
    return fetch_api(f"{BASE_URL_APIS_BRASIL}{endpoint}", {param: query})

def fetch_fetchbrasil(endpoint, query):
    return fetch_api(f"{BASE_URL_FETCHBRASIL}{endpoint}.php", {"token": FETCHBRASIL_TOKEN, "chave": query})

def fetch_fetchbrasil_pro(api, query):
    token = "FB-E6D2-0330-1561-8E5E"
    url = f"{BASE_URL_FETCHBRASIL_PRO}?token={token}&api={api}&query={query}"
    return fetch_api(url)

def fetch_serpro(tipo, valor):
    return fetch_api(BASE_URL_SERPRO, {tipo: valor})

def fetch_ip(ip):
    return fetch_api(f"http://ip-api.com/json/{ip}")

def fetch_mac(mac):
    try:
        r = requests.get(f"https://api.macvendors.com/{mac}", timeout=20)
        if r.status_code == 200:
            return {"MAC": mac, "Fabricante": r.text}
        return {"status": "ERROR", "message": "MAC não encontrado"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}

# ===== MAPA DE APIS =====
api_map = {
    # --- CPF ---
    "api_serasacpf": (lambda q: fetch_apis_brasil("apiserasacpf2025.php", "cpf", q), "Serasa CPF"),
    "api_asseccpf": (lambda q: fetch_apis_brasil("apiassecc2025.php", "cpf", q), "Assec CPF 2025"),
    "api_bigdatacpf": (lambda q: fetch_apis_brasil("apicpfbigdata2025.php", "CPF", q), "BigData CPF 2025"),
    "api_datasuscpf": (lambda q: fetch_apis_brasil("apicpfdatasus.php", "cpf", q), "Datasus CPF 2025"),
    "api_credilinkcpf": (lambda q: fetch_apis_brasil("apicpfcredilink2025.php", "cpf", q), "Credilink CPF 2025"),
    "api_spc": (lambda q: fetch_apis_brasil("apicpf27spc.php", "cpf", q), "SPC Consolidado"),
    "api_fetchbrasil_cpf": (lambda q: fetch_fetchbrasil("cpf_basico", q), "FetchBrasil CPF"),

    # --- NOME ---
    "api_serasanome": (lambda q: fetch_apis_brasil("apiserasanome2025.php", "nome", q), "Serasa Nome"),
    "api_fetchbrasil_nome": (lambda q: fetch_fetchbrasil("nome_basico", q), "FetchBrasil Nome"),

    # --- VEÍCULOS ---
    "api_serpro_placa": (lambda q: fetch_serpro("placa", q), "Serpro Placa"),
    "api_serpro_cnh": (lambda q: fetch_serpro("cnh", q), "Serpro CNH"),
    "api_serpro_chassi": (lambda q: fetch_serpro("chassi", q), "Serpro Chassi"),
    "api_fetchbrasil_placa": (lambda q: fetch_fetchbrasil_pro("placa_df", q), "FetchBrasil PRO Placa"),
    "api_fetchbrasil_cnh": (lambda q: fetch_fetchbrasil_pro("cnh_df", q), "FetchBrasil PRO CNH"),

    # --- REDE ---
    "api_ip": (lambda q: fetch_ip(q), "Consulta IP"),
    "api_mac": (lambda q: fetch_mac(q), "Consulta MAC"),

    # --- OUTROS ---
    "api_serasaemail": (lambda q: fetch_apis_brasil("apiserasaemail2025.php", "email", q), "Serasa Email"),
    "api_credilinktel": (lambda q: fetch_apis_brasil("apitelcredilink2025.php", "telefone", q), "Credilink Telefone"),
}

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("💬 Suporte", url=f"https://t.me/{SUPORTE_USERNAME.replace('@', '')}")]]
    msg = (
        f"👋 Bem-vindo ao *{BOT_NAME}*\n\n"
        "🔍 *Módulos de Consulta:*\n"
        "• /cpf `<número>` — 7 fontes de CPF (Serasa, SPC, Datasus, etc)\n"
        "• /nome `<nome completo>` — Busca por nome\n"
        "• /placa `<placa>` — Dados de veículo (Serpro e FetchBrasil PRO)\n"
        "• /cnh `<número>` — CNH (Serpro e FetchBrasil PRO)\n"
        "• /chassi `<chassi>` — Veículo por chassi (Serpro)\n"
        "• /ip `<endereço>` — Geolocalização e ISP\n"
        "• /mac `<endereço>` — Fabricante de dispositivo\n"
        "• /email `<email>` — Consulta de e-mail\n"
        "• /telefone `<telefone>` — Consulta de telefone\n\n"
        f"🧩 *Versão:* 2025.11 — Integrado via APIs Brasil & FetchBrasil PRO\n"
        f"📞 *Suporte:* {SUPORTE_USERNAME}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def suporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💬 Fale com o suporte: {SUPORTE_USERNAME}")

def extract_query(text):
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""

async def menu_query_handler(update: Update, context, title, buttons):
    query = extract_query(update.message.text)
    if not query:
        await update.message.reply_text(f"⚠️ Informe o {title}. Exemplo: /{title.lower()} valor")
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
        await update.effective_message.reply_text(
            f"❌ Erro na consulta {api_name}:\n`{data['message']}`", parse_mode="Markdown"
        )
        return None
    return data

async def button_callback(update, context):
    q = update.callback_query
    await q.answer()
    data_id = q.data
    api_info = api_map.get(data_id)
    query = context.user_data.get("last_query")
    user = update.effective_user
    if not api_info or not query:
        await q.edit_message_text("Sessão expirada. Use o comando novamente.")
        return
    fetch_func, api_name = api_info
    await q.edit_message_text(f"⏳ Consultando {api_name}...")
    data = await handle_api_call(query, fetch_func, api_name, update)
    if data:
        markdown_output = format_json_to_markdown(data)
        assinatura = f"\n\n🤖 {BOT_USERNAME}\n👤 @{user.username if user.username else user.first_name}"
        if len(markdown_output) > 3500:
            pdf_buffer = generate_pdf(f"{api_name} — {query}", data)
            await q.edit_message_text("📄 Resultado extenso. Enviando PDF...")
            await q.message.reply_document(pdf_buffer.getvalue(), filename=f"{api_name}_{query}.pdf",
                                           caption=f"✅ Resultado completo\n{assinatura}")
        else:
            await q.edit_message_text(f"✅ *Resultado {api_name}*\n\n{markdown_output}{assinatura}", parse_mode="Markdown")

# ===== REGISTRO =====
def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("suporte", suporte))
    app.add_handler(CommandHandler("cpf", lambda u, c: menu_query_handler(u, c, "CPF", [
        ("Serasa", "api_serasacpf"),
        ("Assec", "api_asseccpf"),
        ("BigData", "api_bigdatacpf"),
        ("Datasus", "api_datasuscpf"),
        ("Credilink", "api_credilinkcpf"),
        ("FetchBrasil", "api_fetchbrasil_cpf"),
        ("SPC Consolidado", "api_spc"),
    ])))
    app.add_handler(CommandHandler("nome", lambda u, c: menu_query_handler(u, c, "Nome", [
        ("Serasa", "api_serasanome"),
        ("FetchBrasil", "api_fetchbrasil_nome"),
    ])))
    app.add_handler(CommandHandler("placa", lambda u, c: menu_query_handler(u, c, "Placa", [
        ("Serpro", "api_serpro_placa"),
        ("FetchBrasil PRO", "api_fetchbrasil_placa"),
    ])))
    app.add_handler(CommandHandler("cnh", lambda u, c: menu_query_handler(u, c, "CNH", [
        ("Serpro", "api_serpro_cnh"),
        ("FetchBrasil PRO", "api_fetchbrasil_cnh"),
    ])))
    app.add_handler(CommandHandler("chassi", lambda u, c: menu_query_handler(u, c, "Chassi", [
        ("Serpro", "api_serpro_chassi"),
    ])))
    app.add_handler(CommandHandler("ip", lambda u, c: menu_query_handler(u, c, "IP", [
        ("Consulta IP", "api_ip"),
    ])))
    app.add_handler(CommandHandler("mac", lambda u, c: menu_query_handler(u, c, "MAC", [
        ("Consulta MAC", "api_mac"),
    ])))
    app.add_handler(CommandHandler("email", lambda u, c: menu_query_handler(u, c, "Email", [
        ("Serasa", "api_serasaemail"),
    ])))
    app.add_handler(CommandHandler("telefone", lambda u, c: menu_query_handler(u, c, "Telefone", [
        ("Credilink", "api_credilinktel"),
    ])))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Handlers registrados com sucesso.")

# ===== FASTAPI =====
application = Application.builder().token(TELEGRAM_TOKEN).build()
register_handlers(application)
webhook_app = FastAPI()

@webhook_app.on_event("startup")
async def on_startup():
    logger.info("🚀 Iniciando Icsan Search no Render...")
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

if __name__ == "__main__":
    asyncio.run(application.run_polling())
