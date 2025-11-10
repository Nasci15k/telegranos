'''
Bot de Consultas Profissional para Telegram
Funcionalidades: Menus Inline, Consolidação SPC, Exportação para PDF, Variáveis de Ambiente.
'''
import logging
import json
import requests
import io
import time
import os
import asyncio # Adicionado para o modo Webhook/Render Gratuito
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7564936099:AAGXt1WXFA2j_rgZGHdGZo696Hq6v-0WW3w")
FETCHBRASIL_TOKEN = os.environ.get("FETCHBRASIL_TOKEN", "FB-E6D2-0330-1561-8E5E")
BASE_URL_APIS_BRASIL = os.environ.get("BASE_URL_APIS_BRASIL", "https://apis-brasil.shop/apis/")
BASE_URL_FETCHBRASIL = os.environ.get("BASE_URL_FETCHBRASIL", "https://api.fetchbrasil.com.br/")

# --- Funções Auxiliares ---

def format_json_to_markdown(data, indent=0):
    """Formata um objeto JSON (dict ou list) em uma string Markdown minimalista."""
    if not isinstance(data, (dict, list)) or not data:
        return ""

    markdown_text = ""
    # Indentação minimalista (2 espaços por nível)
    indent_str = "  " * indent

    if isinstance(data, dict):
        for key, value in data.items():
            key_title = key.replace('_', ' ').strip().title()
            # Verifica se o valor é vazio ou nulo
            is_empty = value in [None, "", "null"] or (isinstance(value, (list, dict)) and not value)
            
            if isinstance(value, (dict, list)) and value:
                # Chave para o objeto/lista aninhada
                markdown_text += f"{indent_str}*{key_title}*:\n{format_json_to_markdown(value, indent + 1)}"
            elif not is_empty:
                # Par Chave: Valor
                value_str = str(value)
                markdown_text += f"{indent_str}*{key_title}*: `{value_str}`\n"
            # Se for 'is_empty', não exibe a linha (minimalista)

    elif isinstance(data, list):
        # Para listas, usar um hífen simples para cada item
        for item in data:
            if isinstance(item, (dict, list)):
                # Se o item for um objeto, introduz com hífen e indenta o conteúdo
                markdown_text += f"{indent_str}-\n{format_json_to_markdown(item, indent + 1)}"
            else:
                # Se for um valor simples na lista
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
        # Para listas, cria uma tabela ou lista simples
        if all(isinstance(item, dict) for item in data) and data:
            # Tenta criar uma tabela se todos os itens forem dicionários
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
            elements.append(Spacer(1, 0.2*inch)) # Espaçamento após a tabela
        
        else:
            # Lista simples para itens não-dicionário ou misturados
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

    # Título
    elements.append(Paragraph(f"<b>Relatório de Consulta: {title}</b>", styles['h1']))
    elements.append(Spacer(1, 0.3 * inch))

    # Data e Hora
    elements.append(Paragraph(f"Data da Geração: {time.strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
    elements.append(Spacer(1, 0.2 * inch))

    # Conteúdo da API
    format_json_to_pdf(data, styles, elements, doc)
    
    doc.build(elements)
    buffer.seek(0)
    return buffer

# --- Funções de Requisição de API ---

def fetch_api(url, params=None):
    """Função genérica para requisitar qualquer URL de API e retornar o JSON."""
    logger.info(f"Requisitando API: {url} com params: {params}")
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status() # Lança HTTPError para 4xx/5xx
        return response.json()
    except requests.exceptions.HTTPError as errh:
        logger.error(f"HTTP Error: {errh}")
        return {"status": "ERROR", "message": f"Erro HTTP: O servidor retornou o status {response.status_code}. A API pode estar fora do ar ou o token expirou."}
    except requests.exceptions.ConnectionError as errc:
        logger.error(f"Error Connecting: {errc}")
        return {"status": "ERROR", "message": "Erro de Conexão: Não foi possível se conectar à API."}
    except requests.exceptions.Timeout as errt:
        logger.error(f"Timeout Error: {errt}")
        return {"status": "ERROR", "message": "Erro de Tempo Limite: A API demorou muito para responder (30s)."}
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
    if not FETCHBRASIL_TOKEN:
        return {"status": "ERROR", "message": "Token FETCHBRASIL_TOKEN não configurado."}
    
    url = f"{BASE_URL_FETCHBRASIL}{endpoint}.php"
    params = {"token": FETCHBRASIL_TOKEN, "chave": query}
    return fetch_api(url, params)

# Mapeamento de API para funções de consulta
api_map = {
    # Serasa/APIS BRASIL
    "api_serasacpf": (lambda q: fetch_apis_brasil("apiserasacpf2025.php", "cpf", q), "Serasa CPF"),
    "api_serasanome": (lambda q: fetch_apis_brasil("apiserasanome2025.php", "nome", q), "Serasa Nome"),
    "api_serasaemail": (lambda q: fetch_apis_brasil("apiserasaemail2025.php", "email", q), "Serasa Email"),
    "api_serpro_placa": (lambda q: fetch_apis_brasil("apiserpro.php", "placa", q), "Serpro Placa"),

    # SPC/APIS BRASIL (Exemplo: SPC Consolidado)
    "api_spc": (lambda q: fetch_apis_brasil("apicpf27spc.php", "cpf", q), "SPC Consolidado"), # Usando 27spc como consolidado

    # Outras APIS BRASIL
    "api_datasuscpf": (lambda q: fetch_apis_brasil("apicpfdatasus.php", "cpf", q), "Datasus CPF"),
    "api_credilinkcpf": (lambda q: fetch_apis_brasil("apicpfcredilink2025.php", "cpf", q), "Credilink CPF"),
    "api_bigdatacpf": (lambda q: fetch_apis_brasil("apicpfbigdata2025.php", "CPF", q), "BigData CPF"),
    "api_asseccpf": (lambda q: fetch_apis_brasil("apiassecc2025.php", "cpf", q), "Assec CPF"),
    "api_credilinktel": (lambda q: fetch_apis_brasil("apitelcredilink2025.php", "telefone", q), "Credilink Telefone"),
    
    # FetchBrasil
    "api_fetchbrasil_cpf": (lambda q: fetch_fetchbrasil_api("cpf_basico", q), "FetchBrasil CPF"),
    "api_fetchbrasil_nome": (lambda q: fetch_fetchbrasil_api("nome_basico", q), "FetchBrasil Nome"),
    "api_fetchbrasil_placa": (lambda q: fetch_fetchbrasil_api("placa_basico", q), "FetchBrasil Placa"),
}

# --- Handlers de Comandos ---

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
            await update.message.reply_text(f"⚠️ Por favor, informe o {title}. Exemplo: /{context.args[0]} `<{title}>`")
            return

        context.user_data['last_query'] = query
        
        keyboard = []
        row = []
        for i, (text, callback_data) in enumerate(buttons_data):
            # O callback_data precisa ser único para o handler
            row.append(InlineKeyboardButton(text, callback_data=callback_data))
            if len(row) == 3 or i == len(buttons_data) - 1: # 3 botões por linha
                keyboard.append(row)
                row = []

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Selecione a API para consultar o {title} **`{query}`**:", reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Erro no menu_query_handler: {e}")
        await update.message.reply_text("Ocorreu um erro ao preparar o menu. Tente novamente.")

async def simple_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, fetch_func, title: str, api_name: str) -> None:
    """Handler genérico para comandos que não exigem menu inline."""
    try:
        query = extract_query(update.message.text)
        if not query:
            await update.message.reply_text(f"⚠️ Por favor, informe o {title}. Exemplo: /{context.args[0]} `<{title}>`")
            return

        await update.message.reply_text(f"⏳ Consultando {api_name} para **`{query}`**...", parse_mode='Markdown')

        data = await handle_api_call(query, fetch_func, title, api_name, update)
        
        # Se os dados vieram, enviamos a resposta e o PDF.
        if data:
            markdown_output = format_json_to_markdown(data)
            await update.message.reply_text(f"✅ *Resultado da Consulta - {api_name}*\n\n{markdown_output}", parse_mode='Markdown')
            
            # Gera e envia o PDF
            pdf_buffer = generate_pdf(f"{title} - {query} ({api_name})", data)
            await update.message.reply_document(
                document=pdf_buffer.getvalue(),
                filename=f"{title}_{query}_{api_name}.pdf",
                caption=f"PDF da consulta {api_name} para {query}"
            )

    except Exception as e:
        logger.error(f"Erro no simple_query_handler para {api_name}: {e}")
        await update.message.reply_text("Ocorreu um erro ao processar sua consulta. Tente novamente.")

# --- Handler de Callback (Botões Inline) ---

async def handle_api_call(query: str, fetch_func, title: str, api_name: str, update: Update) -> dict:
    """Função para chamar a API e tratar erros de forma unificada."""
    
    # Chamada de API Sincrona (usando time.sleep para evitar problemas de concorrência)
    # Em produção, um Executor ou asyncio.to_thread seria mais adequado.
    # Para o escopo deste bot, a chamada síncrona dentro da coroutine é aceitável.
    
    start_time = time.time()
    data = fetch_func(query)
    end_time = time.time()

    logger.info(f"API {api_name} para {query} finalizada em {end_time - start_time:.2f}s.")

    if data.get("status") == "ERROR" or 'message' in data.keys() and 'Erro' in data['message']:
        error_message = data.get("message", "Detalhe de erro desconhecido.")
        await update.effective_message.reply_text(f"❌ *Erro na Consulta - {api_name}*\n\nDetalhes: `{error_message}`", parse_mode='Markdown')
        return None
    
    # Se o FetchBrasil retornar 'nenhum resultado' ou similar
    if data.get("code") == 203 or data.get("status") == 404:
        await update.effective_message.reply_text(f"⚠️ *Consulta - {api_name}*\n\nNenhum resultado encontrado para `{query}`.", parse_mode='Markdown')
        return None

    return data

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trata cliques nos botões inline."""
    query_obj = update.callback_query
    await query_obj.answer() # Fecha o pop-up de carregamento no Telegram
    
    callback_data = query_obj.data
    api_info = api_map.get(callback_data)

    # Verifica se a query anterior está salva
    query = context.user_data.get('last_query')
    
    if not api_info or not query:
        await query_obj.edit_message_text(text="❌ API desconhecida ou a sessão expirou. Por favor, reinicie a consulta com o comando (ex: /cpf).")
        return

    fetch_func, api_name = api_info

    # Edita a mensagem para mostrar o status de carregamento
    await query_obj.edit_message_text(f"⏳ Consultando {api_name} para **`{query}`**...", parse_mode='Markdown')

    # Chama a função de API
    data = await handle_api_call(query, fetch_func, "Consulta", api_name, update)
    
    # Se os dados vieram, enviamos a resposta e o PDF.
    if data:
        # Formata o resultado
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

    elif not data:
        # Se houve erro e handle_api_call enviou a mensagem de erro, 
        # apenas atualiza a mensagem original para evitar loop
        await query_obj.edit_message_text(text=f"❌ Erro ao consultar {api_name}. Veja a mensagem de erro acima.")


# --- Handler de Geração de PDF ---

async def pdf_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trata o clique no botão 'Gerar PDF'."""
    query_obj = update.callback_query
    await query_obj.answer("Gerando PDF...")

    callback_data = query_obj.data.replace("pdf_", "")
    api_info = api_map.get(callback_data)
    query = context.user_data.get('last_query')

    if not api_info or not query:
        await query_obj.message.reply_text("❌ Não foi possível gerar o PDF. Sessão expirada ou dados ausentes.")
        return

    fetch_func, api_name = api_info
    
    # Edita a mensagem para mostrar o status
    await query_obj.edit_message_text(f"⏳ Gerando PDF para {api_name}...", parse_mode='Markdown')
    
    # Requisita a API novamente (necessário se o resultado não foi salvo)
    # *Em um ambiente de produção ideal, o resultado da API seria salvo no user_data*
    data = fetch_func(query) 

    if data.get("status") == "ERROR":
        await query_obj.message.reply_text(f"❌ Não foi possível obter os dados novamente para o PDF. Erro: {data.get('message')}")
        return

    # Gera e envia o PDF
    pdf_buffer = generate_pdf(f"Consulta {api_name} - {query}", data)
    await query_obj.message.reply_document(
        document=pdf_buffer.getvalue(),
        filename=f"{api_name.replace(' ', '_')}_{query}.pdf",
        caption=f"PDF da consulta {api_name} para {query}"
    )
    
    # Volta a mensagem original
    markdown_output = format_json_to_markdown(data)
    keyboard = [[InlineKeyboardButton("📥 Gerar PDF", callback_data=f"pdf_{callback_data}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query_obj.edit_message_text(
        text=f"✅ *Resultado da Consulta - {api_name}*\n\n{markdown_output}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# --- Registro de Handlers e Main (Modo Webhook/Polling) ---

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

async def main() -> None:
    """Inicia o bot (Modo Webhook para Render Gratuito ou Polling Local)."""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN não configurado. O bot não pode ser iniciado.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Registra os comandos
    register_handlers(application)

    # --- Configuração do Webhook para Render ---
    PORT = int(os.environ.get("PORT", 8080))
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

    if not RENDER_EXTERNAL_URL:
        # Se a URL não estiver definida, rode em modo polling (para teste local)
        logger.warning("RENDER_EXTERNAL_URL não encontrada. Iniciando em modo POLLING (para testes locais).")
        await application.run_polling(poll_interval=1.0) # Adicionado poll_interval para estabilizar
    else:
        # Se a URL ESTIVER definida, rode em modo Webhook (para o Render)
        webhook_path = f"/{TELEGRAM_TOKEN}"
        webhook_url = f"{RENDER_EXTERNAL_URL}{webhook_path}"

        logger.info(f"Iniciando bot em modo Webhook. URL: {webhook_url}")

        # Diz ao Telegram qual é a nossa URL
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)

        # Inicia o servidor web interno do bot
        await application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot desligado pelo usuário.")
    except Exception as e:
        logger.error(f"Erro fatal no bot: {e}")
