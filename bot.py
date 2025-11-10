'''
Bot de Consultas Profissional para Telegram
Funcionalidades: Menus Inline, Consolidação SPC, Exportação para PDF, Variáveis de Ambiente, Modo Webhook Estável com Gunicorn/Uvicorn.
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

# Configuração de Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configurações e Tokens (Lendo de Variáveis de Ambiente) ---
# O Render irá ler estes valores. Se não existirem, usará os valores padrão.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TELEGRAM_TOKEN_AQUI")
FETCHBRASIL_TOKEN = os.environ.get("FETCHBRASIL_TOKEN", "SEU_FETCHBRASIL_TOKEN_AQUI")
BASE_URL_APIS_BRASIL = os.environ.get("BASE_URL_APIS_BRASIL", "https://apis-brasil.shop/apis/")
BASE_URL_FETCHBRASIL = os.environ.get("BASE_URL_FETCHBRASIL", "https://api.fetchbrasil.com.br/")

# --- Funções Auxiliares (Formatação Minimalista e PDF) ---

def format_json_to_markdown(data, indent=0):
    """Formata um objeto JSON em uma string Markdown minimalista."""
    if not isinstance(data, (dict, list)) or not data:
        return ""

    markdown_text = ""
    indent_str = "  " * indent

    if isinstance(data, dict):
        for key, value in data.items():
            key_title = key.replace('_', ' ').strip().title()
            is_empty = value in [None, "", "null"] or (isinstance(value, (list, dict)) and not value)
            
            if isinstance(value, (dict, list)) and value:
                markdown_text += f"{indent_str}*{key_title}*:\n{format_json_to_markdown(value, indent + 1)}"
            elif not is_empty:
                value_str = str(value)
                markdown_text += f"{indent_str}*{key_title}*: `{value_str}`\n"

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                markdown_text += f"{indent_str}-\n{format_json_to_markdown(item, indent + 1)}"
            else:
                markdown_text += f"{indent_str}- `{str(item)}`\n"

    return markdown_text

def format_json_to_pdf(data, styles, elements, doc):
    """Formata JSON para elementos ReportLab PDF."""
    if isinstance(data, dict):
        for key, value in data.items():
            key_title = key.replace('_', ' ').strip().title()
            is_empty = value in [None, "", "null"] or (isinstance(value, (list, dict)) and not value)

            if isinstance(value, (dict, list)) and value:
                elements.append(Paragraph(f"<b>{key_title}:</b>", styles['Normal']))
                format_json_to_pdf(value, styles, elements, doc)
            elif not is_empty:
                elements.append(Paragraph(f"<b>{key_title}:</b> {str(value)}", styles['Normal']))
    
    elif isinstance(data, list):
        if all(isinstance(item, dict) for item in data) and data:
            headers = set()
            for item in data:
                headers.update(item.keys())
            
            header_list = [h.replace('_', ' ').title() for h in sorted(list(headers))]
            table_data = [header_list]
            
            for item in data:
                row = [str(item.get(h, '')) for h in sorted(list(headers))]
                table_data.append(row)

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
            elements.append(Spacer(1, 0.2*inch))
        else:
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    elements.append(Paragraph(f"Item {i+1}:", styles['Normal']))
                    format_json_to_pdf(item, styles, elements, doc)
                else:
                    elements.append(Paragraph(f"- {str(item)}", styles['Normal']))
            elements.append(Spacer(1, 0.1*inch))

def generate_pdf(title, data):
    """Gera um PDF a partir dos dados formatados."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(f"<b>Relatório de Consulta: {title}</b>", styles['h1']))
    elements.append(Spacer(1, 0.3 * inch))
    elements.append(Paragraph(f"Data da Geração: {time.strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
    elements.append(Spacer(1, 0.2 * inch))

    format_json_to_pdf(data, styles, elements, doc)
    
    doc.build(elements)
    buffer.seek(0)
    return buffer

# --- Funções de Requisição de API (Síncronas) ---

def fetch_api(url, params=None):
    """Função genérica para requisitar qualquer URL de API e retornar o JSON."""
    logger.info(f"Requisitando API: {url} com params: {params}")
    try:
        response = requests.get(url, params=params, timeout=40)
        response.raise_for_status() 
        return response.json()
    except requests.exceptions.HTTPError as errh:
        logger.error(f"HTTP Error: {errh}")
        return {"status": "ERROR", "message": f"Erro HTTP: O servidor retornou o status {response.status_code}. A API pode estar fora do ar ou o token expirou."}
    except requests.exceptions.RequestException as err:
        logger.error(f"API Request Error: {err}")
        return {"status": "ERROR", "message": f"Erro Desconhecido na Requisição: {err}"}
    except json.JSONDecodeError:
        logger.error("JSON Decode Error: A API não retornou JSON válido.")
        return {"status": "ERROR", "message": "Erro de Parsing: A API não retornou dados JSON válidos."}

def fetch_apis_brasil(endpoint, param_name, query):
    """Requisições para a base apis-brasil.shop."""
    url = f"{BASE_URL_APIS_BRASIL}{endpoint}"
    params = {param_name: query}
    return fetch_api(url, params)

def fetch_fetchbrasil_api(endpoint, query):
    """Requisições para a base api.fetchbrasil.com.br."""
    if not FETCHBRASIL_TOKEN or FETCHBRASIL_TOKEN == "SEU_FETCHBRASIL_TOKEN_AQUI":
        return {"status": "ERROR", "message": "Token FETCHBRASIL_TOKEN não configurado."}
    
    url = f"{BASE_URL_FETCHBRASIL}{endpoint}.php"
    params = {"token": FETCHBRASIL_TOKEN, "chave": query}
    return fetch_api(url, params)

# Mapeamento de API
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

# --- Handlers de Comandos e Callbacks ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia uma mensagem de boas-vindas."""
    help_message = (
        "Olá! Eu sou o Bot de Consultas Profissional. "
        "Use os comandos abaixo para iniciar uma consulta:\n\n"
        "Comandos com Menu:\n"
        "/cpf `<número>` - Consulta opções de CPF.\n"
        "/nome_completo `<nome>` - Consulta opções de Nome Completo.\n"
        "/placa `<placa>` - Consulta opções de Placa.\n"
        "/email `<email>` - Consulta Email.\n"
        "/telefone `<telefone>` - Consulta Telefone.\n\n"
        "Comandos Simples:\n"
        "/cnh `<cpf>` - Consulta CNH (FetchBrasil).\n"
        "/ip `<ip>` - Consulta IP.\n"
        "/mac `<mac>` - Consulta MAC Address.\n\n"
        "Exemplo: `/cpf 12345678901`"
    )
    await update.message.reply_text(help_message)

def extract_query(text: str) -> str:
    """Extrai o argumento (query) do comando."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""

async def menu_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str, buttons_data: list) -> None:
    """Handler genérico para comandos que exigem um menu inline."""
    try:
        query = extract_query(update.message.text)
        if not query:
            command_name = update.message.text.split()[0].split('@')[0]
            await update.message.reply_text(f"⚠️ Por favor, informe o {title}. Exemplo: `{command_name} <{title}>`", parse_mode='Markdown')
            return

        context.user_data['last_query'] = query
        context.user_data['last_query_title'] = title
        
        keyboard = []
        row = []
        for i, (text, callback_data) in enumerate(buttons_data):
            row.append(InlineKeyboardButton(text, callback_data=callback_data))
            if len(row) == 3 or i == len(buttons_data) - 1:
                keyboard.append(row)
                row = []

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Selecione a API para consultar o {title} **`{query}`**:", reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Erro no menu_query_handler: {e}")
        await update.message.reply_text("Ocorreu um erro ao preparar o menu. Tente novamente.")

async def handle_api_call(query: str, fetch_func, title: str, api_name: str, update: Update) -> dict:
    """Função para chamar a API e tratar erros de forma unificada."""
    
    # Usa asyncio.to_thread para executar a chamada síncrona (requests) em um thread separado
    data = await asyncio.to_thread(fetch_func, query)
    
    if data.get("status") == "ERROR" or ('message' in data and 'Erro' in data['message']) or ('code' in data and data['code'] in [203, 404]):
        error_message = data.get("message", "Detalhe de erro desconhecido.")
        
        if data.get("status") == "ERROR":
            await update.effective_message.reply_text(f"❌ *Erro na Consulta - {api_name}*\n\nDetalhes: `{error_message}`", parse_mode='Markdown')
        elif data.get("code") in [203, 404]:
            await update.effective_message.reply_text(f"⚠️ *Consulta - {api_name}*\n\nNenhum resultado encontrado para `{query}`.", parse_mode='Markdown')
        else:
            await update.effective_message.reply_text(f"❌ *Erro na Consulta - {api_name}*\n\nDetalhes: `{error_message}`", parse_mode='Markdown')
        return None

    return data

async def simple_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, fetch_func, title: str, api_name: str) -> None:
    """Handler genérico para comandos que não exigem menu inline."""
    try:
        query = extract_query(update.message.text)
        if not query:
            command_name = update.message.text.split()[0].split('@')[0]
            await update.message.reply_text(f"⚠️ Por favor, informe o {title}. Exemplo: `{command_name} <{title}>`", parse_mode='Markdown')
            return

        await update.message.reply_text(f"⏳ Consultando {api_name} para **`{query}`**...", parse_mode='Markdown')

        data = await handle_api_call(query, fetch_func, title, api_name, update)
        
        if data:
            markdown_output = format_json_to_markdown(data)
            
            # Gera e envia o PDF em sequência
            pdf_buffer = generate_pdf(f"{title} - {query} ({api_name})", data)
            await update.message.reply_document(
                document=pdf_buffer.getvalue(),
                filename=f"{title}_{query}_{api_name}.pdf",
                caption=f"✅ *Resultado da Consulta - {api_name}*\n\n{markdown_output}",
                parse_mode='Markdown'
            )

    except Exception as e:
        logger.error(f"Erro no simple_query_handler para {api_name}: {e}")
        await update.message.reply_text("Ocorreu um erro ao processar sua consulta. Tente novamente.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trata cliques nos botões inline de consulta."""
    query_obj = update.callback_query
    await query_obj.answer()
    
    callback_data = query_obj.data
    api_info = api_map.get(callback_data)

    query = context.user_data.get('last_query')
    query_title = context.user_data.get('last_query_title', "Consulta")
    
    if not api_info or not query:
        await query_obj.edit_message_text(text="❌ API desconhecida ou a sessão expirou. Por favor, reinicie a consulta com o comando (ex: /cpf).")
        return

    fetch_func, api_name = api_info

    # Edita a mensagem para mostrar o status de carregamento
    await query_obj.edit_message_text(f"⏳ Consultando {api_name} para **`{query}`**...", parse_mode='Markdown')

    # Chama a função de API
    data = await handle_api_call(query, fetch_func, query_title, api_name, update)
    
    if data:
        # Salva o último resultado para o PDF (otimização)
        context.user_data[f'result_{callback_data}'] = data
        
        markdown_output = format_json_to_markdown(data)
        
        # Cria a mensagem final com o botão de PDF
        keyboard = [[InlineKeyboardButton("📥 Gerar PDF", callback_data=f"pdf_{callback_data}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Atualiza a mensagem
        await query_obj.edit_message_text(
            text=f"✅ *Resultado da Consulta - {api_name}*\n\n{markdown_output}",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    else:
        await query_obj.edit_message_text(f"❌ Ocorreu um erro ao consultar {api_name}. Verifique o erro detalhado acima ou tente outra API.")

async def pdf_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trata o clique no botão 'Gerar PDF'."""
    query_obj = update.callback_query
    await query_obj.answer("Gerando PDF...")

    callback_data_api = query_obj.data.replace("pdf_", "")
    api_info = api_map.get(callback_data_api)
    query = context.user_data.get('last_query')
    query_title = context.user_data.get('last_query_title', "Consulta")
    data = context.user_data.get(f'result_{callback_data_api}')
    
    if not api_info or not query:
        await query_obj.message.reply_text("❌ Não foi possível gerar o PDF. Sessão expirada ou dados ausentes.")
        return

    fetch_func, api_name = api_info

    if not data:
        # Reconsultar se os dados expiraram da sessão
        await query_obj.edit_message_text(f"⏳ Dados não encontrados na sessão. Reconsultando {api_name}...", parse_mode='Markdown')
        data = await handle_api_call(query, fetch_func, query_title, api_name, update)
        if not data: return
        context.user_data[f'result_{callback_data_api}'] = data

    await query_obj.edit_message_text(f"⏳ Gerando PDF para {api_name}...", parse_mode='Markdown')
    
    pdf_buffer = generate_pdf(f"Consulta {api_name} - {query}", data)
    await query_obj.message.reply_document(
        document=pdf_buffer.getvalue(),
        filename=f"{api_name.replace(' ', '_')}_{query}.pdf",
        caption=f"✅ PDF gerado com sucesso para {query}"
    )
    
    markdown_output = format_json_to_markdown(data)
    keyboard = [[InlineKeyboardButton("📥 Gerar PDF", callback_data=f"pdf_{callback_data_api}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query_obj.edit_message_text(
        text=f"✅ *Resultado da Consulta - {api_name}*\n\n{markdown_output}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# --- Funções de Setup Webhook (Executadas uma vez) ---

async def set_webhook_on_render(application: Application, token: str) -> None:
    """Define o Webhook no Telegram usando as variáveis de ambiente do Render."""
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
    
    if not RENDER_EXTERNAL_URL:
        # Isso só deve acontecer se for executado localmente sem a variável Render
        logger.warning("RENDER_EXTERNAL_URL não encontrada. O Webhook não será configurado.")
        return 

    webhook_path = f"/{token}"
    webhook_url = f"{RENDER_EXTERNAL_URL}{webhook_path}"

    # Adiciona getMe para garantir que o bot está vivo antes de configurar o webhook
    await application.bot.get_me() 
    logger.info(f"Configurando Webhook. URL: {webhook_url}")
    
    # Define a URL do Webhook no Telegram
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    logger.info("Webhook configurado com sucesso.")

def register_handlers(application: Application) -> None:
    """Registra todos os handlers no bot."""
    # Comandos Simples (sem menu inline)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("cnh", lambda u, c: simple_query_handler(u, c, lambda q: fetch_fetchbrasil_api("cnh_df", q), "CNH", "FetchBrasil CNH")))
    application.add_handler(CommandHandler("ip", lambda u, c: simple_query_handler(u, c, lambda q: fetch_api(f"http://ip-api.com/json/{q}"), "IP", "IP-API")))
    application.add_handler(CommandHandler("mac", lambda u, c: simple_query_handler(u, c, lambda q: fetch_api(f"https://api.macvendors.com/{q}"), "MAC", "MAC Vendors")))

    # Menus Inline
    application.add_handler(CommandHandler("cpf", lambda u, c: menu_query_handler(u, c, "CPF", [
        ("Serasa", "api_serasacpf"), ("Datasus", "api_datasuscpf"), ("Credilink", "api_credilinkcpf"),
        ("BigData", "api_bigdatacpf"), ("Assec", "api_asseccpf"), ("FetchBrasil", "api_fetchbrasil_cpf"),
        ("SPC Consolidado", "api_spc")
    ])))
    application.add_handler(CommandHandler("nome_completo", lambda u, c: menu_query_handler(u, c, "Nome", [
        ("Serasa", "api_serasanome"), ("FetchBrasil", "api_fetchbrasil_nome")
    ])))
    application.add_handler(CommandHandler("placa", lambda u, c: menu_query_handler(u, c, "Placa", [
        ("FetchBrasil", "api_fetchbrasil_placa"), ("Serpro", "api_serpro_placa")
    ])))
    application.add_handler(CommandHandler("email", lambda u, c: menu_query_handler(u, c, "Email", [("Serasa", "api_serasaemail")])))
    application.add_handler(CommandHandler("telefone", lambda u, c: menu_query_handler(u, c, "Telefone", [("Credilink", "api_credilinktel")])))

    # Callback Handler para botões de API e PDF
    application.add_handler(CallbackQueryHandler(pdf_callback, pattern='^pdf_'))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Handlers registrados com sucesso.")

# --- Instância Global da Aplicação (Entry Point do Gunicorn) ---

# A instância `application` é criada globalmente para ser importada pelo Gunicorn/Uvicorn.
application = Application.builder().token(TELEGRAM_TOKEN).build()
register_handlers(application)

# 🚨 CORREÇÃO CRÍTICA PARA GUNICORN/UVICORN NO RENDER
# Criamos um atalho (webhook_app) que aponta para o servidor ASGI interno do python-telegram-bot.
# Isso resolve o erro "Failed to parse 'application.webserver'" do Gunicorn.
webhook_app = application.webserver # <--- ESSA LINHA É A CORREÇÃO

# --- Execução Local (Polling) ---

async def start_local_polling() -> None:
    """Inicia o bot em modo Polling para testes locais."""
    if not os.environ.get("RENDER_EXTERNAL_URL"):
        logger.warning("RENDER_EXTERNAL_URL não encontrada. Iniciando em modo POLLING.")
        await application.run_polling(poll_interval=1.0, stop_signals=None)

if __name__ == "__main__":
    try:
        asyncio.run(start_local_polling())
    except Exception as e:
        logger.error(f"Erro na execução local: {e}")
