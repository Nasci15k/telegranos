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
# ATENÇÃO: Defina estas variáveis no seu ambiente de hospedagem (ex: Render)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7564936099:AAGXt1WXFA2j_rgZGHdGZo696Hq6v-0WW3w") # Token de teste
FETCHBRASIL_TOKEN = os.environ.get("FETCHBRASIL_TOKEN", "FB-E6D2-0330-1561-8E5E") # Token de teste
BASE_URL_APIS_BRASIL = "https://apis-brasil.shop/apis/"

# --- Mapeamento de APIs ---
APIS_BRASIL_MAP = {
    "serasacpf": ("apiserasacpf2025.php", "cpf"),
    "serasanome": ("apiserasanome2025.php", "nome"),
    "serasaemail": ("apiserasaemail2025.php", "email"),
    "asseccpf": ("apiassecc2025.php", "cpf"),
    "bigdatacpf": ("apicpfbigdata2025.php", "CPF"),
    "datasuscpf": ("apicpfdatasus.php", "cpf"),
    "credilinkcpf": ("apicpfcredilink2025.php", "cpf"),
    "credilinktel": ("apitelcredilink2025.php", "telefone"),
}

SPC_APIS = {
    "spccpf": ("apicpfspc.php", "doc"), "spccpf1": ("apicpf1spc.php", "doc"),
    "spccpf2": ("apicpf2spc.php", "doc"), "spccpf3": ("apicpf3spc.php", "doc"),
    "spccpf4": ("apicpf4spc.php", "cpf"), "spccpf5": ("apicpf5spc.php", "cpf"),
    "spccpf6": ("apicpf6spc.php", "cpf"), "spccpf7": ("apicpf7spc.php", "cpf"),
    "spccpf8": ("apicpf8spc.php", "cpf"), "spccpf9": ("apicpf9spc.php", "cpf"),
    "spccpf10": ("apicpf10spc.php", "cpf"), "spccpf11": ("apicpf11spc.php", "cpf"),
    "spccpf12": ("apicpf12spc.php", "cpf"), "spccpf13": ("apicpf13spc.php", "cpf"),
    "spccpf14": ("apicpf14spc.php", "cpf"), "spccpf15": ("apicpf15spc.php", "cpf"),
    "spccpf16": ("apicpf16spc.php", "cpf"), "spccpf17": ("apicpf17spc.php", "cpf"),
    "spccpf18": ("apicpf18spc.php", "cpf"), "spccpf19": ("apicpf19spc.php", "cpf"),
    "spccpf20": ("apicpf20spc.php", "cpf"), "spccpf21": ("apicpf21spc.php", "cpf"),
    "spccpf23": ("apicpf23spc.php", "cpf"), "spccpf24": ("apicpf24spc.php", "cpf"),
    "spccpf26": ("apicpf26spc.php", "cpf"), "spccpf27": ("apicpf27spc.php", "cpf"),
    "spccpf28": ("apicpf28spc.php", "cpf"), "spccpf29": ("apicpf29spc.php", "cpf"),
    "spccpf30": ("apicpf30spc.php", "cpf"), "spccpf31": ("apicpf31spc.php", "cpf"),
    "spccpf32": ("apicpf32spc.php", "cpf"), "spccpf33": ("apicpf33spc.php", "cpf"),
    "spccpf34": ("apicpf34spc.php", "cpf"), "spccpf35": ("apicpf35spc.php", "cpf"),
}

# --- Funções de Formatação e Geração de PDF ---

def format_json_to_markdown(data, indent=0):
    """Formata um objeto JSON (dict ou list) em uma string Markdown legível."""
    if not isinstance(data, (dict, list)) or not data:
        return ""

    markdown_text = ""
    indent_str = "    " * indent

    if isinstance(data, dict):
        for key, value in data.items():
            key_title = key.replace('_', ' ').strip().title()
            # Verifica se o valor é vazio ou nulo
            is_empty = value in [None, "", "null"] or (isinstance(value, (list, dict)) and not value)
            
            if isinstance(value, (dict, list)) and value:
                markdown_text += f"{indent_str}🔹 *{key_title}*:\n{format_json_to_markdown(value, indent + 1)}"
            elif not is_empty:
                value_str = str(value)
                markdown_text += f"{indent_str}▪️ *{key_title}*: `{value_str}`\n"
    elif isinstance(data, list):
        for i, item in enumerate(data):
            markdown_text += f"{indent_str}--- Item {i+1} ---\n{format_json_to_markdown(item, indent + 1)}"

    return markdown_text

def generate_pdf_report(api_name, query, result_data):
    """Gera um relatório PDF a partir dos dados da consulta."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=18)
    styles = getSampleStyleSheet()
    story = []

    # Título
    story.append(Paragraph(f"Relatório de Consulta - {api_name}", styles['Title']))
    story.append(Spacer(1, 0.2 * inch))

    # Informações da Consulta
    story.append(Paragraph(f"<b>Consulta:</b> {api_name}", styles['Normal']))
    story.append(Paragraph(f"<b>Parâmetro:</b> {query}", styles['Normal']))
    story.append(Paragraph(f"<b>Data/Hora:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
    story.append(Spacer(1, 0.2 * inch))

    # Conteúdo da Resposta (JSON formatado)
    story.append(Paragraph("<b>Dados da Resposta da API:</b>", styles['h2']))
    story.append(Spacer(1, 0.1 * inch))

    # Função auxiliar para formatar o JSON para o PDF (usando ReportLab)
    def format_for_pdf(data, indent=0):
        elements = []
        indent_str = "&nbsp;" * 4 * indent
        
        if isinstance(data, dict):
            for key, value in data.items():
                key_title = key.replace('_', ' ').title()
                is_empty = value in [None, "", "null"] or (isinstance(value, (list, dict)) and not value)
                
                if isinstance(value, (dict, list)) and value:
                    elements.append(Paragraph(f"{indent_str}<b>{key_title}</b>:", styles['Normal']))
                    elements.extend(format_for_pdf(value, indent + 1))
                elif not is_empty:
                    value_str = str(value)
                    elements.append(Paragraph(f"{indent_str}<b>{key_title}:</b> {value_str}", styles['Normal']))
        
        elif isinstance(data, list):
            for i, item in enumerate(data):
                elements.append(Paragraph(f"{indent_str}--- <b>Item {i+1}</b> ---", styles['Normal']))
                elements.extend(format_for_pdf(item, indent + 1))
        
        return elements

    story.extend(format_for_pdf(result_data, indent=0))

    doc.build(story)
    buffer.seek(0)
    return buffer

# --- Funções de Consulta de API ---

def fetch_api(url, params=None):
    """Função genérica para fazer requisições HTTP e retornar o JSON/Texto."""
    try:
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"raw_response": response.text, "status": "OK", "message": "Resposta não é JSON."}
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro na requisição para {url}: {e}")
        return {"status": "ERROR", "message": str(e)}

def fetch_fetchbrasil_api(api_name, query):
    """Função genérica para as APIs FetchBrasil."""
    return fetch_api("https://api.fetchbrasil.pro/", {"token": FETCHBRASIL_TOKEN, "api": api_name, "query": query})

def fetch_cpf_fetchbrasil(cpf):
    """Consulta CPF Básico na FetchBrasil."""
    return fetch_fetchbrasil_api("cpf_basica", cpf)

def fetch_placa_serpro(placa):
    """Consulta Placa Serpro na apiradar.onrender.com."""
    return fetch_api(f"https://apiradar.onrender.com/?placa={placa}")

def fetch_apis_brasil(endpoint_path, param_name, query):
    """Função genérica para as APIs apis-brasil.shop."""
    return fetch_api(f"{BASE_URL_APIS_BRASIL}{endpoint_path}", {param_name: query})

def fetch_all_spc(query):
    """Consulta todas as APIs SPC e consolida os resultados."""
    results = {api_name.upper(): fetch_apis_brasil(path, param, query) for api_name, (path, param) in SPC_APIS.items()}
    return results

# --- Handlers do Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia uma mensagem quando o comando /start é emitido."""
    help_text = (
        "👋 *Bem-vindo ao Bot de Consultas Profissional!*\n\n"
        "Use os comandos abaixo para iniciar uma consulta. Após a resposta, você terá a opção de baixar um relatório em PDF.\n\n"
        "🔹 */cpf <número>*: Consulta CPF em várias fontes.\n"
        "🔹 */nome_completo <nome>*: Consulta Nome em várias fontes.\n"
        "🔹 */placa <placa>*: Consulta Placa Veicular em várias fontes.\n"
        "🔹 */email <endereço>*: Consulta E-mail.\n"
        "🔹 */telefone <número>*: Consulta Telefone.\n"
        "🔹 */cnh <número>*: Consulta CNH (FetchBrasil).\n"
        "🔹 */ip <endereço_ip>*: Consulta informações de IP.\n"
        "🔹 */mac <endereço_mac>*: Consulta fabricante de MAC Address.\n"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_api_call(message, context, api_name, query, result):
    """Trata o resultado da API, envia a resposta e adiciona o botão PDF."""
    if isinstance(result, dict) and result.get("status") == "ERROR":
        response_text = f"❌ *Erro na Consulta {api_name}*:\n`{result['message']}`"
        await message.edit_text(response_text, parse_mode='Markdown')
    else:
        # Armazena o resultado no user_data para geração de PDF
        key = f"pdf_data_{message.chat_id}_{message.message_id}"
        context.user_data[key] = {"api_name": api_name, "query": query, "result": result}
        
        # Formata a resposta em Markdown
        response_text = f"✅ *Resultado da Consulta: {api_name}*\n\n{format_json_to_markdown(result)}"
        
        # Limita o tamanho da mensagem
        if len(response_text) > 4096:
            response_text = response_text[:4000] + "\n\n... (Resposta truncada devido ao limite de 4096 caracteres do Telegram)"
        
        # Adiciona o botão inline para download do PDF
        keyboard = [[InlineKeyboardButton("⬇️ Baixar Relatório em PDF", callback_data=f"download_pdf_{key}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await message.edit_text(response_text, parse_mode='Markdown', reply_markup=reply_markup)

async def simple_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, api_func, query_type, api_name):
    """Handler para comandos simples que não usam menu inline (IP, MAC, CNH)."""
    if not context.args:
        await update.message.reply_text(f"Uso: `/{update.message.text.split()[0][1:]} <{query_type}>`", parse_mode='Markdown')
        return
    query = " ".join(context.args)
    message = await update.message.reply_text(f"⏳ Consultando *{api_name}* para `{query}`...", parse_mode='Markdown')
    result = api_func(query)
    await handle_api_call(message, context, api_name, query, result)

async def menu_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, query_type: str, buttons: list):
    """Handler para comandos que exibem um menu inline."""
    if not context.args:
        await update.message.reply_text(f"Uso: `/{update.message.text.split()[0][1:]} <{query_type}>`", parse_mode='Markdown')
        return
    query = " ".join(context.args)
    
    # Cria os botões inline
    keyboard = [[InlineKeyboardButton(text, callback_data=f"{cb_data}_{query}")] for text, cb_data in buttons]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(f"Selecione a fonte para consultar `{query}`:", reply_markup=reply_markup, parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trata os cliques nos botões inline (Seleção de API ou Download de PDF)."""
    query_callback = update.callback_query
    await query_callback.answer()

    data = query_callback.data

    # --- Lógica para Download de PDF ---
    if data.startswith("download_pdf_"):
        key = data.replace("download_pdf_", "")
        pdf_data = context.user_data.get(key)
        
        if not pdf_data:
            await query_callback.message.reply_text("❌ Erro: Dados da consulta expiraram ou não foram encontrados.")
            return

        # Remove o botão PDF da mensagem original para evitar cliques duplicados
        await query_callback.edit_message_reply_markup(reply_markup=None)
        
        # Envia mensagem de status
        message = await query_callback.message.reply_text(f"⏳ Gerando relatório PDF para *{pdf_data['api_name']}*...", parse_mode='Markdown')

        try:
            pdf_buffer = generate_pdf_report(pdf_data['api_name'], pdf_data['query'], pdf_data['result'])
            filename = f"Relatorio_{pdf_data['api_name'].replace(' ', '_')}_{pdf_data['query']}_{int(time.time())}.pdf"
            
            await context.bot.send_document(
                chat_id=query_callback.message.chat_id,
                document=pdf_buffer,
                filename=filename,
                caption=f"✅ *Download Concluído*:\nRelatório de consulta *{pdf_data['api_name']}* para `{pdf_data['query']}`.",
                parse_mode='Markdown'
            )
            await message.delete() # Remove a mensagem de status
        except Exception as e:
            logger.error(f"Erro ao gerar ou enviar PDF: {e}")
            await message.edit_text(f"❌ Erro ao gerar ou enviar o PDF: {e}")
        
        return

    # --- Lógica para Seleção de API ---
    
    try:
        # O formato do callback_data é: api_<nome_api>_<query>
        _, api_key, query = data.split('_', 2)
    except ValueError:
        await query_callback.edit_message_text("❌ Erro ao processar a requisição. Formato de dados inválido.")
        return

    # Edita a mensagem de menu para indicar que a consulta está em andamento
    message = await query_callback.edit_message_text(f"⏳ *Consulta em andamento* para `{api_key.upper()}` com `{query}`...", parse_mode='Markdown')

    api_map = {
        "serasacpf": (lambda q: fetch_apis_brasil("apiserasacpf2025.php", "cpf", q), "Serasa CPF"),
        "datasuscpf": (lambda q: fetch_apis_brasil("apicpfdatasus.php", "cpf", q), "Datasus CPF"),
        "credilinkcpf": (lambda q: fetch_apis_brasil("apicpfcredilink2025.php", "cpf", q), "Credilink CPF"),
        "bigdatacpf": (lambda q: fetch_apis_brasil("apicpfbigdata2025.php", "CPF", q), "BigData CPF"),
        "asseccpf": (lambda q: fetch_apis_brasil("apiassecc2025.php", "cpf", q), "Assec CPF"),
        "fetchbrasil_cpf": (fetch_cpf_fetchbrasil, "FetchBrasil CPF"),
        "spc": (fetch_all_spc, "SPC Consolidado"),
        "serasanome": (lambda q: fetch_apis_brasil("apiserasanome2025.php", "nome", q), "Serasa Nome"),
        "fetchbrasil_nome": (lambda q: fetch_fetchbrasil_api("nome_basico", q), "FetchBrasil Nome"),
        "fetchbrasil_placa": (lambda q: fetch_fetchbrasil_api("placa_df", q), "FetchBrasil Placa"),
        "serpro_placa": (fetch_placa_serpro, "Serpro Placa"),
        "serasaemail": (lambda q: fetch_apis_brasil("apiserasaemail2025.php", "email", q), "Serasa Email"),
        "credilinktel": (lambda q: fetch_apis_brasil("apitelcredilink2025.php", "telefone", q), "Credilink Telefone"),
    }

    if api_key in api_map:
        api_func, api_name = api_map[api_key]
        result = api_func(query)
        await handle_api_call(message, context, api_name, query, result)
    else:
        await message.edit_text("❌ API desconhecida.")

# --- Main Function ---
def main() -> None:
    """Inicia o bot."""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN não configurado. O bot não pode ser iniciado.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

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

    # Callback Handler
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot iniciado...")
    application.run_polling()

if __name__ == "__main__":
    main()
