'''
Bot de Consultas Profissional para Telegram
Funcionalidades: Menus Inline, Consolidação SPC, Exportação para PDF, Variáveis de Ambiente,
Modo Webhook Estável (Render) com FastAPI + Gunicorn + Uvicorn.
'''

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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors

# --- Configuração de Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configurações e Tokens (Variáveis de Ambiente) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TELEGRAM_TOKEN_AQUI")
FETCHBRASIL_TOKEN = os.environ.get("FETCHBRASIL_TOKEN", "SEU_FETCHBRASIL_TOKEN_AQUI")
BASE_URL_APIS_BRASIL = os.environ.get("BASE_URL_APIS_BRASIL", "https://apis-brasil.shop/apis/")
BASE_URL_FETCHBRASIL = os.environ.get("BASE_URL_FETCHBRASIL", "https://api.fetchbrasil.com.br/")
PORT = int(os.environ.get("PORT", 8000))

# --- Funções Auxiliares de Formatação ---

def format_json_to_markdown(data, indent=0):
    """Formata JSON em texto Markdown legível."""
    if not isinstance(data, (dict, list)) or not data:
        return ""
    markdown_text = ""
    indent_str = "  " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            key_title = key.replace('_', ' ').title()
            is_empty = value in [None, "", "null"] or (isinstance(value, (list, dict)) and not value)
            if isinstance(value, (dict, list)) and value:
                markdown_text += f"{indent_str}*{key_title}*:\n{format_json_to_markdown(value, indent + 1)}"
            elif not is_empty:
                markdown_text += f"{indent_str}*{key_title}*: `{value}`\n"
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                markdown_text += f"{indent_str}-\n{format_json_to_markdown(item, indent + 1)}"
            else:
                markdown_text += f"{indent_str}- `{item}`\n"
    return markdown_text


def format_json_to_pdf(data, styles, elements, doc):
    """Formata JSON para PDF (ReportLab)."""
    if isinstance(data, dict):
        for key, value in data.items():
            key_title = key.replace('_', ' ').title()
            is_empty = value in [None, "", "null"] or (isinstance(value, (list, dict)) and not value)
            if isinstance(value, (dict, list)) and value:
                elements.append(Paragraph(f"<b>{key_title}:</b>", styles['Normal']))
                format_json_to_pdf(value, styles, elements, doc)
            elif not is_empty:
                elements.append(Paragraph(f"<b>{key_title}:</b> {value}", styles['Normal']))
    elif isinstance(data, list):
        if all(isinstance(item, dict) for item in data):
            headers = sorted({key for d in data for key in d})
            table_data = [[h.replace('_', ' ').title() for h in headers]]
            for item in data:
                table_data.append([str(item.get(h, '')) for h in headers])
            table = Table(table_data)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            elements.append(table)
            elements.append(Spacer(1, 0.2 * inch))
        else:
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    elements.append(Paragraph(f"Item {i+1}:", styles['Normal']))
                    format_json_to_pdf(item, styles, elements, doc)
                else:
                    elements.append(Paragraph(f"- {item}", styles['Normal']))
            elements.append(Spacer(1, 0.1 * inch))


def generate_pdf(title, data):
    """Gera PDF do resultado."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"<b>Relatório de Consulta: {title}</b>", styles['h1']),
        Spacer(1, 0.3 * inch),
        Paragraph(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']),
        Spacer(1, 0.2 * inch)
    ]
    format_json_to_pdf(data, styles, elements, doc)
    doc.build(elements)
    buffer.seek(0)
    return buffer

# --- Funções de Requisição ---

def fetch_api(url, params=None):
    """Requisição genérica para APIs JSON."""
    try:
        response = requests.get(url, params=params, timeout=40)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Erro na API: {e}")
        return {"status": "ERROR", "message": str(e)}

def fetch_apis_brasil(endpoint, param_name, query):
    return fetch_api(f"{BASE_URL_APIS_BRASIL}{endpoint}", {param_name: query})

def fetch_fetchbrasil_api(endpoint, query):
    if not FETCHBRASIL_TOKEN or FETCHBRASIL_TOKEN == "SEU_FETCHBRASIL_TOKEN_AQUI":
        return {"status": "ERROR", "message": "Token FETCHBRASIL_TOKEN não configurado."}
    return fetch_api(f"{BASE_URL_FETCHBRASIL}{endpoint}.php", {"token": FETCHBRASIL_TOKEN, "chave": query})

# --- Mapeamento de APIs ---

api_map = {
    "api_serasacpf": (lambda q: fetch_apis_brasil("apiserasacpf2025.php", "cpf", q), "Serasa CPF"),
    "api_serasanome": (lambda q: fetch_apis_brasil("apiserasanome2025.php", "nome", q), "Serasa Nome"),
    "api_serasaemail": (lambda q: fetch_apis_brasil("apiserasaemail2025.php", "email", q), "Serasa Email"),
    "api_serpro_placa": (lambda q: fetch_apis_brasil("apiserpro.php", "placa", q), "Serpro Placa"),
    "api_spc": (lambda q: fetch_apis_brasil("apicpf27spc.php", "cpf", q), "SPC Consolidado"),
    "api_datasuscpf": (lambda q: fetch_apis_brasil("apicpfdatasus.php", "cpf", q), "Datasus CPF"),
    "api_credilinkcpf": (lambda q: fetch_apis_brasil("apicpfcredilink2025.php", "cpf", q), "Credilink CPF"),
    "api_bigdatacpf": (lambda q: fetch_apis_brasil("apicpfbigdata2025.php", "CPF", q), "BigData CPF"),
    "api_asseccpf": (lambda q: fetch_apis_brasil("apiassecc2025.php", "cpf", q), "Assec CPF"),
    "api_credilinktel": (lambda q: fetch_apis_brasil("apitelcredilink2025.php", "telefone", q), "Credilink Telefone"),
    "api_fetchbrasil_cpf": (lambda q: fetch_fetchbrasil_api("cpf_basico", q), "FetchBrasil CPF"),
    "api_fetchbrasil_nome": (lambda q: fetch_fetchbrasil_api("nome_basico", q), "FetchBrasil Nome"),
    "api_fetchbrasil_placa": (lambda q: fetch_fetchbrasil_api("placa_basico", q), "FetchBrasil Placa"),
}

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "🤖 *Bot de Consultas Profissional*\n\n"
        "Use os comandos abaixo:\n"
        "/cpf `<número>`\n/nome_completo `<nome>`\n/placa `<placa>`\n/email `<email>`\n/telefone `<telefone>`"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

def extract_query(text): return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""

async def menu_query_handler(update: Update, context, title, buttons):
    query = extract_query(update.message.text)
    if not query:
        await update.message.reply_text(f"⚠️ Informe o {title}. Exemplo: /cpf 12345678901")
        return
    context.user_data['last_query'] = query
    context.user_data['last_query_title'] = title
    keyboard = [[InlineKeyboardButton(text, callback_data=data)] for text, data in buttons]
    await update.message.reply_text(
        f"Selecione a API para consultar o {title} `{query}`:",
        parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_api_call(query, fetch_func, title, api_name, update):
    data = await asyncio.to_thread(fetch_func, query)
    if data.get("status") == "ERROR":
        await update.effective_message.reply_text(f"❌ Erro na consulta {api_name}:\n`{data['message']}`", parse_mode='Markdown')
        return None
    return data

async def simple_query_handler(update, context, fetch_func, title, api_name):
    query = extract_query(update.message.text)
    if not query:
        await update.message.reply_text(f"⚠️ Informe o {title}. Exemplo: /{title.lower()} valor")
        return
    await update.message.reply_text(f"⏳ Consultando {api_name}...")
    data = await handle_api_call(query, fetch_func, title, api_name, update)
    if data:
        markdown_output = format_json_to_markdown(data)
        pdf_buffer = generate_pdf(f"{title} - {query} ({api_name})", data)
        await update.message.reply_document(
            document=pdf_buffer.getvalue(),
            filename=f"{title}_{query}_{api_name}.pdf",
            caption=f"✅ *Resultado da Consulta - {api_name}*\n\n{markdown_output}",
            parse_mode='Markdown'
        )

async def button_callback(update, context):
    q = update.callback_query
    await q.answer()
    data_id = q.data
    api_info = api_map.get(data_id)
    query = context.user_data.get('last_query')
    if not api_info or not query:
        await q.edit_message_text("Sessão expirada. Use o comando novamente.")
        return
    fetch_func, api_name = api_info
    await q.edit_message_text(f"⏳ Consultando {api_name}...")
    data = await handle_api_call(query, fetch_func, "Consulta", api_name, update)
    if data:
        markdown_output = format_json_to_markdown(data)
        keyboard = [[InlineKeyboardButton("📥 Gerar PDF", callback_data=f"pdf_{data_id}")]]
        context.user_data[f"result_{data_id}"] = data
        await q.edit_message_text(
            f"✅ *Resultado da Consulta - {api_name}*\n\n{markdown_output}",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def pdf_callback(update, context):
    q = update.callback_query
    await q.answer("Gerando PDF...")
    data_id = q.data.replace("pdf_", "")
    api_info = api_map.get(data_id)
    query = context.user_data.get('last_query')
    data = context.user_data.get(f"result_{data_id}")
    if not api_info or not data:
        await q.message.reply_text("Erro: dados não encontrados.")
        return
    fetch_func, api_name = api_info
    pdf_buffer = generate_pdf(f"Consulta {api_name} - {query}", data)
    await q.message.reply_document(pdf_buffer.getvalue(), filename=f"{api_name}_{query}.pdf",
                                   caption=f"✅ PDF gerado com sucesso para {query}")

# --- Registro de Handlers ---

def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cpf", lambda u, c: menu_query_handler(u, c, "CPF",
        [("Serasa", "api_serasacpf"), ("Datasus", "api_datasuscpf"), ("Credilink", "api_credilinkcpf"),
         ("BigData", "api_bigdatacpf"), ("Assec", "api_asseccpf"), ("FetchBrasil", "api_fetchbrasil_cpf"),
         ("SPC Consolidado", "api_spc")])))
    app.add_handler(CommandHandler("nome_completo", lambda u, c: menu_query_handler(u, c, "Nome",
        [("Serasa", "api_serasanome"), ("FetchBrasil", "api_fetchbrasil_nome")])))
    app.add_handler(CommandHandler("placa", lambda u, c: menu_query_handler(u, c, "Placa",
        [("FetchBrasil", "api_fetchbrasil_placa"), ("Serpro", "api_serpro_placa")])))
    app.add_handler(CommandHandler("email", lambda u, c: menu_query_handler(u, c, "Email",
        [("Serasa", "api_serasaemail")])))
    app.add_handler(CommandHandler("telefone", lambda u, c: menu_query_handler(u, c, "Telefone",
        [("Credilink", "api_credilinktel")])))
    app.add_handler(CallbackQueryHandler(pdf_callback, pattern="^pdf_"))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Handlers registrados com sucesso.")

# --- Instância Global (Render + FastAPI) ---

application = Application.builder().token(TELEGRAM_TOKEN).build()
register_handlers(application)

from fastapi import FastAPI, Request
import uvicorn

webhook_app = FastAPI()

@webhook_app.post(f"/{TELEGRAM_TOKEN}")
async def telegram_webhook(request: Request):
    """Recebe atualizações do Telegram e as envia para o bot."""
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return {"status": "ok"}

async def set_webhook_on_render(application: Application, token: str) -> None:
    """Configura o Webhook automaticamente no Render."""
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
    if not RENDER_EXTERNAL_URL:
        logger.warning("RENDER_EXTERNAL_URL não encontrada. Webhook não configurado.")
        return
    webhook_url = f"{RENDER_EXTERNAL_URL}/{token}"
    await application.bot.delete_webhook()
    await application.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook configurado com sucesso: {webhook_url}")

# --- Execução Local (Polling) ---

async def start_local_polling():
    """Executa o bot localmente (modo polling)."""
    if not os.environ.get("RENDER_EXTERNAL_URL"):
        logger.warning("Executando localmente (modo polling).")
        await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(start_local_polling())
    except Exception as e:
        logger.error(f"Erro na execução local: {e}")
