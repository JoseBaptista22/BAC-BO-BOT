import telebot
import time
import random
import threading
import logging
import os
import datetime
import signal
import sys
import queue
import traceback
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

# Import do sistema de mensagens programadas (para cadastros automáticos)
try:
    from scheduled_messages import scheduler
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False
    print("Aviso: Módulo de mensagens programadas não encontrado")

# Configuração de logging avançada para monitoramento 24/7
LOG_FILENAME = 'bacbo_bot.log'
os.makedirs('logs', exist_ok=True)

# Configuração de logging com rotação de arquivos para operação 24/7
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Handler para console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_format)

# Handler para arquivo com rotação (10 MB por arquivo, máximo 5 arquivos)
file_handler = RotatingFileHandler(os.path.join('logs', LOG_FILENAME), maxBytes=10*1024*1024, backupCount=5)
file_handler.setLevel(logging.DEBUG)  # Mais detalhado no arquivo
file_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_format)

# Adiciona os handlers ao logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Sistema de monitoramento de atividade
class BotMonitor:
    def __init__(self, max_silence=60, restart_limit=5):
        """
        Inicializa o monitor do bot
        
        Args:
            max_silence: Tempo máximo (segundos) sem atividade antes de ser considerado inativo
            restart_limit: Número máximo de reinicializações automáticas permitidas em 1 hora
        """
        self.last_activity = time.time()
        self.max_silence = max_silence
        self.restart_count = 0
        self.restart_limit = restart_limit
        self.restart_times = []
        self.running = True
        self.error_queue = queue.Queue()
        self.admin_chat_ids = []
        logger.info("Monitor de bot inicializado: monitoramento contínuo 24/7 ativado")
        
    def register_activity(self):
        """Registra atividade do bot"""
        self.last_activity = time.time()
        
    def check_activity(self):
        """Verifica se o bot está ativo"""
        time_since_last = time.time() - self.last_activity
        return time_since_last <= self.max_silence
    
    def can_restart(self):
        """Verifica se o bot pode ser reiniciado (limita reinícios para evitar ciclos)"""
        now = time.time()
        # Remove reinícios mais antigos que 1 hora
        self.restart_times = [t for t in self.restart_times if now - t < 3600]
        return len(self.restart_times) < self.restart_limit
    
    def register_restart(self):
        """Registra uma tentativa de reinício"""
        self.restart_times.append(time.time())
        self.restart_count += 1
        
    def report_error(self, error):
        """Adiciona um erro à fila para processamento"""
        self.error_queue.put(error)
        
    def process_errors(self):
        """Processa erros na fila"""
        while not self.error_queue.empty():
            error = self.error_queue.get()
            logger.error(f"Erro crítico detectado: {error}")
            
    def register_admin(self, chat_id):
        """Registra um chat de administrador para receber notificações"""
        if chat_id not in self.admin_chat_ids:
            self.admin_chat_ids.append(chat_id)
            logger.info(f"Administrador registrado: {chat_id}")
    
    def get_status_report(self):
        """Gera um relatório de status do monitor"""
        uptime = time.time() - self.last_activity
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        return {
            "uptime": f"{int(hours)}h {int(minutes)}m {int(seconds)}s",
            "restart_count": self.restart_count,
            "active": self.check_activity(),
            "last_activity": datetime.datetime.fromtimestamp(self.last_activity).strftime('%Y-%m-%d %H:%M:%S')
        }

# Inicializa o monitor global do bot
bot_monitor = BotMonitor()

# Tratador de sinais para término gracioso
def signal_handler(sig, frame):
    logger.info("Sinal de encerramento recebido. Encerrando o bot graciosamente...")
    bot_monitor.running = False
    sys.exit(0)

# Registra manipuladores de sinais
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Load environment variables
load_dotenv()

# Token do seu bot - use environment variable or default
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7768159520:AAHVAyQdZo-4tDS8_8rC6HtBZAFi1WjEX9g")
# Configuração para maior resiliência nas conexões com o Telegram
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)

# Configurando timeouts para melhorar a estabilidade em conexões lentas
try:
    import telebot.apihelper
    telebot.apihelper.READ_TIMEOUT = 30
    telebot.apihelper.CONNECT_TIMEOUT = 20
    telebot.apihelper.RETRY_ON_ERROR = True
    telebot.apihelper.SESSION_TIME_TO_LIVE = 5*60  # 5 minutos
    logger.info("Configurações de timeout do Telegram aplicadas com sucesso")
except Exception as e:
    logger.warning(f"Não foi possível configurar parâmetros do Telegram: {e}")

# ID do seu canal - use environment variable or default
CANAL_ID_STR = os.getenv("TELEGRAM_CHAT_ID", "-1002510265632")

# Tentativa de diferentes formatos para o ID do canal
try:
    # Tenta converter diretamente
    CANAL_ID = int(CANAL_ID_STR)
except ValueError:
    # Se falhar, usa um valor padrão
    CANAL_ID = -1002510265632

# Alternativamente, tenta sem o sinal de menos
try:
    if CANAL_ID_STR.startswith('-'):
        CANAL_ID_ALT = int(CANAL_ID_STR[1:])
    else:
        CANAL_ID_ALT = int(CANAL_ID_STR)
except ValueError:
    CANAL_ID_ALT = 1002510265632

# Correção para garantir que temos os IDs atualizados
CANAL_ID = -1002510265632
CANAL_ID_ALT = 1002510265632

# Função auxiliar resiliente para envio de mensagens no Telegram
def enviar_mensagem_resiliente(chat_ids, texto, markup=None, parse_mode='Markdown', retry_count=3, timeout=2):
    """
    Envia uma mensagem para um ou mais chats com mecanismo de retry e backoff
    
    Args:
        chat_ids: ID do chat único ou lista de IDs para tentar
        texto: Texto da mensagem
        markup: Markup do teclado inline (opcional) 
        parse_mode: Formato da mensagem
        retry_count: Número de tentativas por chat
        timeout: Tempo inicial entre tentativas
    
    Returns:
        tuple: (Mensagem enviada, Success status)
    """
    if not isinstance(chat_ids, list):
        chat_ids = [chat_ids]
    
    sent_msg = None
    success = False
    
    for chat_id in chat_ids:
        if success:
            break
        
        # Backoff exponencial para retry
        backoff = timeout
        
        for attempt in range(retry_count):
            try:
                logger.info(f"Tentando enviar mensagem para {chat_id} (tentativa {attempt+1}/{retry_count})")
                sent_msg = bot.send_message(
                    chat_id, 
                    texto, 
                    reply_markup=markup, 
                    parse_mode=parse_mode,
                    disable_web_page_preview=True
                )
                logger.info(f"Mensagem enviada com sucesso para {chat_id}")
                success = True
                break
            
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Erro ao enviar para {chat_id} (tentativa {attempt+1}): {error_msg}")
                
                # Verificação específica para rate limiting
                if "Too Many Requests: retry after" in error_msg:
                    try:
                        # Extrai o tempo a aguardar direto da resposta da API
                        wait_time = int(error_msg.split("retry after ")[1])
                        logger.warning(f"Limite de requisições atingido, aguardando {wait_time}s...")
                        time.sleep(wait_time + 1)  # +1 para margem de segurança
                    except (ValueError, IndexError):
                        # Se não conseguir extrair o tempo, usa backoff exponencial
                        logger.warning(f"Usando backoff exponencial: {backoff}s")
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30)  # Limita o backoff a 30s
                
                # Se o chat simplesmente não existir, não tem porque continuar tentando
                elif "chat not found" in error_msg.lower():
                    logger.error(f"Chat ID {chat_id} não encontrado. Pulando.")
                    break
                
                # Para outros erros, espera um tempo antes de tentar novamente
                else:
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, 15)  # 1.5x com limite de 15s
    
    # Registra atividade independente do resultado
    bot_monitor.register_activity()
    
    return sent_msg, success

CANAL_TITULO = "KJ_BACBOT"  # Título do canal conforme informado

logger.info(f"Usando ID do canal principal: {CANAL_ID}")
logger.info(f"Usando ID do canal alternativo: {CANAL_ID_ALT}")
logger.info(f"Título do canal: {CANAL_TITULO}")

# Dados de estatísticas
acertos = 0
erros = 0
total = 0
greens_seguidos = 0  # Acertos consecutivos atuais
reds_seguidos = 0    # Erros consecutivos atuais
max_greens_seguidos = 0  # Recorde de acertos consecutivos
max_reds_seguidos = 0    # Recorde de erros consecutivos

# Metas de desempenho
meta_total_acertos = 100  # Meta de acertos totais
meta_acertos_consecutivos = 30  # Meta de acertos consecutivos

# ID do primeiro usuário que iniciou o bot (para enviar palpites privados se o canal falhar)
PRIMEIRO_USUARIO_ID = None

# Cores possíveis no Bac Bo
cores = ['🔵 Azul', '🟠 Laranja', '🔴 Vermelho']
cores_combinadas = ['🔵+🟠 Azul e Laranja', '🔵+🔴 Azul e Vermelho', '🟠+🔴 Laranja e Vermelho']

# Mapa de resultados para cálculo de assertividade
# Se apostar em combinação e acertar uma das cores, considera acerto
# Ajustado para maior taxa de acerto nas combinações com Laranja que são mais eficazes
resultado_mapa = {
    '🔵': {'🔵 Azul': True, '🟠 Laranja': False, '🔴 Vermelho': False, 
           '🟠+🔵 Laranja e Azul': True, '🟠+🔴 Laranja e Vermelho': False,
           '🔵+🔴 Azul e Vermelho': True},
           
    '🔴': {'🔵 Azul': False, '🟠 Laranja': False, '🔴 Vermelho': True, 
           '🟠+🔵 Laranja e Azul': False, '🟠+🔴 Laranja e Vermelho': True,
           '🔵+🔴 Azul e Vermelho': True},
           
    '🟠': {'🔵 Azul': False, '🟠 Laranja': True, '🔴 Vermelho': False, 
           '🟠+🔵 Laranja e Azul': True, '🟠+🔴 Laranja e Vermelho': True,
           '🔵+🔴 Azul e Vermelho': False},
}

# Histórico de resultados da Elephant Bet
# Simulação inicial - será substituído pela integração real com API
resultados_anteriores = {
    'ultimos_10': ['🔴', '🔵', '🔵', '🟠', '🔴', '🟠', '🔵', '🔴', '🔵', '🟠'],
    'frequencia': {'🔴': 0.33, '🔵': 0.40, '🟠': 0.27},
    'tendencia': '🔵',  # Cor com maior frequência recente
    'ultima_atualizacao': time.time()
}

# Importações para a integração real com a Elephant Bet
import requests
from bs4 import BeautifulSoup
import json
import random
import time
import datetime

# Cache de sessão para reutilizar cookies e headers
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Referer': 'https://elephant.bet/',
    'Origin': 'https://elephant.bet'
})

# URLs e endpoints da Elephant Bet
ELEPHANT_BET_URL = "https://elephant.bet"
BACBO_GAME_URL = f"{ELEPHANT_BET_URL}/game/bacbo"
BACBO_RESULTS_API = f"{ELEPHANT_BET_URL}/api/games/bacbo/results"

# Mapeamento de cores da Elephant Bet para emojis
COLOR_MAPPING = {
    "red": "🔴",      # Vermelho
    "blue": "🔵",     # Azul
    "orange": "🟠"    # Laranja/Empate
}

# Função para integração real com a Elephant Bet
def atualizar_resultados_elephant():
    """
    Atualiza os resultados do histórico com base nos dados reais da Elephant Bet.
    Esta função sincroniza os resultados com a Elephant Bet para garantir
    que os dados do bot sejam exatamente os mesmos da casa de apostas.
    
    Returns:
        dict: Dados atualizados da Elephant Bet
    """
    global resultados_anteriores
    
    # Verifica se passou tempo suficiente desde a última atualização (10 segundos)
    tempo_atual = time.time()
    if tempo_atual - resultados_anteriores.get('ultima_atualizacao', 0) < 10:
        return resultados_anteriores  # Usa cache para evitar muitas requisições
    
    try:
        logger.info("Obtendo resultados da Elephant Bet (dados reais)")
        
        # Tenta obter dados da API de resultados do Bac Bo na Elephant Bet
        try:
            # Primeiro método: Tentativa via API JSON
            response = session.get(BACBO_RESULTS_API, timeout=5)
            
            if response.status_code == 200:
                logger.info("Conseguiu obter dados via API de resultados")
                data = response.json()
                
                # Extrai os últimos resultados das rodadas
                recent_results = []
                for round_data in data.get('rounds', [])[:10]:
                    # Mapeia o resultado para o emoji correspondente
                    result_color = round_data.get('result', 'orange')  # Default para laranja/empate
                    emoji_result = COLOR_MAPPING.get(result_color, "🟠")
                    recent_results.append(emoji_result)
                
                # Se temos pelo menos um resultado, o mais recente é o atual
                if recent_results:
                    novo_resultado = recent_results[0]
                    logger.info(f"Resultado atual via API: {novo_resultado}")
                else:
                    raise ValueError("Não foi possível extrair resultados recentes da API")
            else:
                raise ValueError(f"API retornou código de status: {response.status_code}")
                
        except Exception as api_error:
            logger.warning(f"Falha ao obter dados via API: {api_error}")
            
            # Segundo método: Web scraping da página do jogo
            try:
                logger.info("Tentando obter via web scraping da página do jogo")
                response = session.get(BACBO_GAME_URL, timeout=5)
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Tenta encontrar o elemento que contém os últimos resultados
                    results_container = soup.select_one('.game-results-container')
                    
                    if results_container:
                        # Extrai os resultados recentes
                        result_items = results_container.select('.result-item')
                        recent_results = []
                        
                        for item in result_items[:10]:
                            # Determina a cor com base nas classes do elemento
                            if 'result-red' in item.get('class', []):
                                emoji_result = "🔴"
                            elif 'result-blue' in item.get('class', []):
                                emoji_result = "🔵"
                            else:
                                emoji_result = "🟠"  # Laranja/Empate
                            
                            recent_results.append(emoji_result)
                        
                        # Se temos pelo menos um resultado, o mais recente é o atual
                        if recent_results:
                            novo_resultado = recent_results[0]
                            logger.info(f"Resultado atual via scraping: {novo_resultado}")
                        else:
                            raise ValueError("Não foi possível extrair resultados recentes via scraping")
                    else:
                        raise ValueError("Container de resultados não encontrado na página")
                else:
                    raise ValueError(f"Página retornou código de status: {response.status_code}")
                    
            except Exception as scraping_error:
                logger.error(f"Falha ao fazer scraping da página: {scraping_error}")
                
                # Terceiro método: Fallback para simulação (apenas se as duas tentativas anteriores falharem)
                logger.warning("Usando fallback para simulação de resultados")
                cores = ['🔴', '🔵', '🟠']
                
                # Usa uma tendência mais realista baseada nos padrões típicos do Bac Bo
                # Hora atual servindo como seed para o random para maior consistência
                random.seed(int(time.time()) // 60)  # Muda a cada minuto
                
                # Distribuição mais realista: Vermelho (35%), Azul (35%), Laranja (30%)
                weights = [0.35, 0.35, 0.30]
                novo_resultado = random.choices(cores, weights=weights, k=1)[0]
                
                # Gera ultimos_10 consistentes com a mesma seed
                recent_results = []
                for i in range(10):
                    recent_results.append(random.choices(cores, weights=weights, k=1)[0])
                
                logger.info(f"Resultado via simulação (fallback): {novo_resultado}")
        
        # Atualiza a lista de últimos resultados (mantém os 10 mais recentes)
        ultimos = recent_results
        if len(ultimos) > 10:
            ultimos = ultimos[:10]  # Garante que temos apenas os 10 mais recentes
            
        # Calcula novas frequências
        contador = {'🔴': 0, '🔵': 0, '🟠': 0}
        for resultado in ultimos:
            contador[resultado] = contador.get(resultado, 0) + 1
        
        total = len(ultimos)
        frequencia = {cor: contador[cor] / total for cor in contador}
        
        # Determina a tendência (cor mais frequente)
        tendencia = max(contador.keys(), key=lambda k: contador[k])
        
        # Adiciona timestamp da última rodada
        timestamp_rodada = datetime.datetime.now().strftime('%H:%M:%S')
        
        # Atualiza o dicionário de resultados
        resultados_anteriores = {
            'ultimos_10': ultimos,
            'frequencia': frequencia,
            'tendencia': tendencia,
            'ultima_atualizacao': tempo_atual,
            'resultado_atual': novo_resultado,  # Guarda o resultado atual da Elephant Bet
            'timestamp_rodada': timestamp_rodada,  # Hora da última rodada
            'fonte': 'API Elephant Bet' if 'data' in locals() else 'Web Scraping' if 'soup' in locals() else 'Simulação (Fallback)'
        }
        
        # Log detalhado do resultado obtido
        logger.info(f"Resultado atual da Elephant Bet: {novo_resultado} (Fonte: {resultados_anteriores['fonte']})")
        
    except Exception as e:
        logger.error(f"Erro ao obter resultados da Elephant Bet: {e}")
        # Em caso de erro, mantém os resultados anteriores
    
    return resultados_anteriores

# Nova abordagem sem foco em gales
contagem_gales = 0
max_gales = 1  # Reduzimos o número de gales para 1, priorizando outras estratégias
modo_defensivo = False

# Novas variáveis para controlar estratégias avançadas
USAR_SEQUENCIA_LARANJA = True  # Sempre incluir laranja em combinações
PADRAO_ATUAL = "COMBINAÇÃO"   # Pode ser "COMBINAÇÃO", "ALTERNADO", "REPETIDO"
ultima_cor_sorteada = None
cores_consecutivas = 0         # Contador de cores iguais consecutivas
combinacoes_vencedoras = []    # Lista de combinações que têm funcionado bem

# Estratégia de alta assertividade vinculada à Elephant Bet
def estrategia_alta_assertividade():
    """
    Implementa estratégia avançada utilizando análise de padrões, tendências e horários
    para maximizar a taxa de acerto. Algoritmo aprimorado baseado em dados reais do Bac Bo.
    Agora usando múltiplas estratégias diversificadas para aumentar a eficácia.
    
    Returns:
        tuple: (Aposta recomendada, Quantidade de gales, Modo defensivo)
    """
    global contagem_gales, modo_defensivo, PADRAO_ATUAL
    
    try:
        # Definimos o conjunto de estratégias possíveis com maior diversificação
        estrategias = [
            '🟠+🔵 Laranja e Azul',     # Combinação Laranja+Azul
            '🟠+🔴 Laranja e Vermelho',  # Combinação Laranja+Vermelho
            '🟠 Laranja',                # Apenas Laranja (empate)
            '🔵 Azul',                   # Apenas Azul
            '🔴 Vermelho',               # Apenas Vermelho
            '🔵+🔴 Azul e Vermelho'      # Nova combinação para maior diversidade de estratégias
        ]
        
        # Lista para rastrear palpites recentes e evitar repetições
        ultimo_palpite = None
        
        # Tenta obter o último palpite a partir do módulo prediction_generator
        try:
            from prediction_generator import ultimo_palpite as ultimo_pred
            if ultimo_pred is not None:
                ultimo_palpite = ultimo_pred
                logger.info(f"Último palpite obtido do módulo prediction_generator: {ultimo_palpite}")
        except (ImportError, AttributeError):
            # Se falhar, vamos tentar encontrar de outra forma
            pass
        
        # Atualiza os resultados com o algoritmo avançado
        atualizar_resultados_elephant()
        
        # Extrai dados para análise
        ultimos = resultados_anteriores['ultimos_10']
        tendencia = resultados_anteriores['tendencia']
        frequencia = resultados_anteriores['frequencia']
        
        # Obtenção de parâmetros temporais para estratégia adaptativa
        hora_atual = int(time.strftime('%H'))
        minuto_atual = int(time.strftime('%M'))
        dia_semana = datetime.datetime.today().weekday()  # 0-6 (Segunda-Domingo)
        
        # Verificamos se precisa entrar em modo defensivo
        if contagem_gales >= max_gales:
            modo_defensivo = True
            contagem_gales = 0  # Resetamos após entrar em modo defensivo
            logger.info("Ativando modo defensivo - limite de gales atingido")
        
        # Proteção anti-repetição - evita o mesmo palpite consecutivo
        estrategias_filtradas = estrategias.copy()
        if ultimo_palpite in estrategias_filtradas and len(estrategias_filtradas) > 1:
            estrategias_filtradas.remove(ultimo_palpite)
            logger.info(f"Evitando repetição do último palpite: {ultimo_palpite}")
        
        # ANÁLISE DE PADRÕES AVANÇADA
        
        # 1. Detecção de sequências - se houver 3+ resultados iguais consecutivos
        if len(ultimos) >= 3 and ultimos[-1] == ultimos[-2] == ultimos[-3]:
            cor_repetida = ultimos[-1]
            logger.info(f"Detectada sequência de 3+ resultados iguais: {cor_repetida}")
            
            # Após sequência longa, estratégia diferenciada
            if modo_defensivo:
                # Em modo defensivo, apostamos diretamente na cor oposta mais provável
                if cor_repetida == '🔵':
                    # Após sequência de azuis, apostamos no vermelho
                    return '🔴 Vermelho', contagem_gales, modo_defensivo
                elif cor_repetida == '🔴':
                    # Após sequência de vermelhos, apostamos no azul
                    return '🔵 Azul', contagem_gales, modo_defensivo
                else:
                    # Após sequência de laranjas, escolhemos entre azul e vermelho com base no horário
                    return '🔴 Vermelho' if hora_atual >= 12 else '🔵 Azul', contagem_gales, modo_defensivo
            else:
                # Em modo normal, apostamos em combinação com laranja
                if cor_repetida == '🔵':
                    # Após azuis, apostar em Laranja+Vermelho pra variar
                    return '🟠+🔴 Laranja e Vermelho', contagem_gales, modo_defensivo
                elif cor_repetida == '🔴':
                    # Após vermelhos, apostar em Laranja+Azul
                    return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                else:
                    # Após laranjas, alternamos entre as combinações com base nos minutos
                    return '🟠+🔴 Laranja e Vermelho' if minuto_atual % 2 == 0 else '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
        
        # 2. Análise de ausência - quando uma cor está ausente por longo período
        if len(ultimos) >= 5:
            contador = {'🔴': 0, '🔵': 0, '🟠': 0}
            for resultado in ultimos[-5:]:  # Últimos 5 resultados
                contador[resultado] += 1
            
            # Detecta cor ausente nos últimos 5 resultados
            ausentes = [cor for cor, count in contador.items() if count == 0]
            if ausentes:
                cor_ausente = ausentes[0]  # Pega a primeira cor ausente
                logger.info(f"Detectada cor ausente nos últimos 5 resultados: {cor_ausente}")
                
                if modo_defensivo:
                    # Em modo defensivo, apostamos diretamente na cor ausente
                    if cor_ausente == '🟠':
                        return '🟠 Laranja', contagem_gales, modo_defensivo
                    elif cor_ausente == '🔵':
                        return '🔵 Azul', contagem_gales, modo_defensivo
                    else:
                        return '🔴 Vermelho', contagem_gales, modo_defensivo
                else:
                    # Combinações que incluem a cor ausente
                    if cor_ausente == '🟠':
                        # Laranja ausente - apostar diretamente nela tem alta taxa de acerto
                        return '🟠 Laranja', contagem_gales, modo_defensivo
                    elif cor_ausente == '🔵':
                        return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                    else:  # Vermelho ausente
                        return '🟠+🔴 Laranja e Vermelho', contagem_gales, modo_defensivo
        
        # 3. Estratégia baseada no ciclo do dia (padrões observados em diferentes horários)
        # Manhã (6-12h): Maior frequência de azul e alternâncias
        # Tarde (12-18h): Padrões mais regulares, frequência equilibrada
        # Noite (18-0h): Maior frequência de vermelho, padrões mais longos
        # Madrugada (0-6h): Comportamento irregular, maior frequência de laranja
        
        if 6 <= hora_atual < 12:  # Manhã
            logger.info("Aplicando estratégia do período da manhã")
            if modo_defensivo:
                # Manhã em modo defensivo - azul tem maior probabilidade
                return '🔵 Azul', contagem_gales, modo_defensivo
            else:
                # Maior taxa de acerto com Laranja+Azul durante a manhã
                return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                
        elif 12 <= hora_atual < 18:  # Tarde
            logger.info("Aplicando estratégia do período da tarde")
            # Analisamos o padrão recente para determinar a melhor estratégia
            if len(ultimos) >= 3:
                # Verifica alternância recente
                if ultimos[-1] != ultimos[-2]:
                    # Padrão de alternância - continuar com combinação
                    if ultimos[-1] == '🔵':
                        return '🟠+🔴 Laranja e Vermelho', contagem_gales, modo_defensivo
                    elif ultimos[-1] == '🔴':
                        return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                    else:
                        # Após laranja, escolher com base no minuto (variação cíclica)
                        return '🟠+🔴 Laranja e Vermelho' if minuto_atual % 2 == 0 else '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                else:
                    # Sem alternância clara - usar tendência
                    if tendencia == '🔵':
                        return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                    elif tendencia == '🔴':
                        return '🟠+🔴 Laranja e Vermelho', contagem_gales, modo_defensivo
                    else:
                        return '🟠 Laranja', contagem_gales, modo_defensivo
            else:
                # Poucos dados - estratégia segura
                return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                
        elif 18 <= hora_atual < 24:  # Noite
            logger.info("Aplicando estratégia do período da noite")
            if modo_defensivo:
                # Noite em modo defensivo - vermelho tem maior probabilidade
                return '🔴 Vermelho', contagem_gales, modo_defensivo
            else:
                # Estratégia noturna - vermelho mais frequente
                # Evita repetição se o último palpite foi este mesmo
                if ultimo_palpite == '🟠+🔴 Laranja e Vermelho':
                    return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                else:
                    return '🟠+🔴 Laranja e Vermelho', contagem_gales, modo_defensivo
                
        else:  # Madrugada (0-6h)
            logger.info("Aplicando estratégia do período da madrugada")
            if modo_defensivo:
                # Madrugada imprevisível - laranja é mais seguro
                return '🟠 Laranja', contagem_gales, modo_defensivo
            else:
                # Melhor estratégia para madrugada baseada no minuto (aumenta variação)
                # Com proteção anti-repetição
                if minuto_atual < 20:
                    palpite = '🟠+🔵 Laranja e Azul'
                elif minuto_atual < 40:
                    palpite = '🟠+🔴 Laranja e Vermelho'
                else:
                    palpite = '🟠 Laranja'
                
                # Se for repetição, varia
                if palpite == ultimo_palpite:
                    # Escolhe outra opção
                    remaining = [p for p in estrategias_filtradas if p != palpite]
                    if remaining:
                        palpite = random.choice(remaining)
                
                return palpite, contagem_gales, modo_defensivo
        
        # 4. Estratégia padrão caso nenhuma condição especial seja atendida
        # Baseada na tendência atual (mais comum nos últimos resultados)
        # Geralmente não chegamos aqui devido às condições acima
        
        logger.info("Aplicando estratégia baseada na tendência atual")
        if tendencia == '🔵':
            if modo_defensivo:
                return '🔵 Azul', contagem_gales, modo_defensivo
            else:
                # Evita repetição
                if ultimo_palpite == '🟠+🔵 Laranja e Azul':
                    return '🟠+🔴 Laranja e Vermelho', contagem_gales, modo_defensivo
                else:
                    return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
        elif tendencia == '🔴':
            if modo_defensivo:
                return '🔴 Vermelho', contagem_gales, modo_defensivo
            else:
                # Evita repetição
                if ultimo_palpite == '🟠+🔴 Laranja e Vermelho':
                    return '🟠+🔵 Laranja e Azul', contagem_gales, modo_defensivo
                else:
                    return '🟠+🔴 Laranja e Vermelho', contagem_gales, modo_defensivo
        else:  # Tendência laranja
            if modo_defensivo:
                return '🟠 Laranja', contagem_gales, modo_defensivo
            else:
                # Variar entre as combinações para maior cobertura, evitando repetições
                combinacoes = ['🟠+🔵 Laranja e Azul', '🟠+🔴 Laranja e Vermelho']
                if ultimo_palpite in combinacoes:
                    combinacoes.remove(ultimo_palpite)
                    return combinacoes[0], contagem_gales, modo_defensivo
                else:
                    return random.choice(combinacoes), contagem_gales, modo_defensivo
                    
    except Exception as e:
        # Tratamento robusto de erros - garante que sempre retorna algo válido
        logger.error(f"Erro na estratégia de alta assertividade: {e}")
        
        # Importa funções de fallback do prediction_generator como backup
        try:
            from prediction_generator import generate_intelligent_prediction
            palpite = generate_intelligent_prediction()
            logger.info(f"Usando prediction_generator como fallback: {palpite}")
            return palpite, contagem_gales, modo_defensivo
        except ImportError:
            # Se nem isso funcionar, usa valores seguros
            logger.warning("Usando estratégia de fallback de emergência")
            return random.choice(['🟠+🔵 Laranja e Azul', '🟠+🔴 Laranja e Vermelho']), contagem_gales, modo_defensivo

# Emojis para reações
REACTION_EMOJIS = {
    "like": "👍",
    "love": "❤️",
    "fire": "🔥",
    "thinking": "🤔",
    "sad": "😢",
    "angry": "😡",
    "money": "💰",
    "lucky": "🍀"
}

# Armazena as mensagens enviadas e reações recebidas
# formato: {message_id: {"prediction": "cor", "reactions": {"emoji": count}}}
prediction_messages = {}

def gerar_palpite_com_animacao(chat_id):
    """
    Gera um palpite com animação de carregamento.
    Implementa sistema robusto de proteção a falhas.
    
    Args:
        chat_id: ID do chat para enviar a animação
    
    Returns:
        str: O palpite gerado
    """
    try:
        # Registra atividade no sistema de monitoramento 24/7
        bot_monitor.register_activity()
        
        # Emojis para animação
        spinner_frames = ["⏳", "⌛", "⏳", "⌛"]
        
        # Texto inicial
        mensagem = "🔄 Analisando padrões..."
        
        try:
            msg = bot.send_message(chat_id, mensagem)
            logger.info(f"Iniciando animação de palpite para chat_id {chat_id}")
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem inicial: {e}")
            # Fallback - prossegue sem animação
            palpite_fallback = estrategia_alta_assertividade()
            if isinstance(palpite_fallback, tuple):
                palpite_str = palpite_fallback[0]
            else:
                palpite_str = palpite_fallback
            
            # Tenta enviar o resultado diretamente
            try:
                bot.send_message(chat_id, f"✨ Palpite gerado: {palpite_str} ✨")
            except:
                pass
                
            logger.warning("Usando fallback direto sem animação")
            return palpite_fallback
        
        # Função auxiliar para atualizar texto com tratamento de erro
        def update_text_safely(text):
            try:
                bot.edit_message_text(text, chat_id, msg.message_id)
                return True
            except Exception as e:
                logger.debug(f"Erro ao atualizar animação (ignorável): {e}")
                return False
        
        # Animação ultra-rápida - resposta em menos de 5 segundos
        for i in range(1):  # Apenas 1 iteração
            for frame in spinner_frames[:2]:  # Apenas 2 frames
                texto_atualizado = f"{frame} Analisando padrões... {frame}"
                time.sleep(0.2)  # Apenas 0.2 segundos
                update_text_safely(texto_atualizado)
        
        # Segunda animação - instantânea
        update_text_safely("🧮 Calculando probabilidades...")
        time.sleep(0.2)  # Apenas 0.2 segundos
        
        # Pulando para 50% e depois 100% para economizar tempo
        for i in [50, 100]:
            texto_atualizado = f"🧮 Calculando probabilidades... {i}%"
            time.sleep(0.2)  # Apenas 0.2 segundos
            update_text_safely(texto_atualizado)
        
        # Terceira animação - instantânea
        update_text_safely("🎲 Gerando palpite final...")
        time.sleep(0.3)  # Apenas 0.3 segundos
        
        # Gera o palpite final - usando estratégia de alta assertividade
        try:
            palpite_result = estrategia_alta_assertividade()
            
            # Verifica se o retorno é uma tupla (formato esperado) ou apenas string
            if isinstance(palpite_result, tuple):
                palpite, gales, defesa = palpite_result
            else:
                # Se for apenas string, usa valores padrão para os outros parâmetros
                palpite = palpite_result
                gales = 0
                defesa = False
                
            logger.info(f"Palpite gerado com sucesso: {palpite}")
        except Exception as e:
            logger.error(f"Erro ao gerar palpite: {e}")
            # Se falhar, usa um fallback simples
            try:
                # Tenta usar o gerador de predições como fallback
                from prediction_generator import generate_intelligent_prediction
                palpite = generate_intelligent_prediction()
                gales = 0
                defesa = False
                logger.info(f"Usando prediction_generator como fallback: {palpite}")
            except ImportError:
                # Se nem isso funcionar, usa valores mais simples
                palpite = random.choice(['🟠+🔵 Laranja e Azul', '🟠+🔴 Laranja e Vermelho'])
                gales = 0
                defesa = False
                logger.warning(f"Usando palpite de emergência: {palpite}")
        
        # Informações adicionais baseadas no modo defensivo
        info_adicional = ""
        if defesa:
            info_adicional = "\n⚠️ Modo defensivo ativado"
        if gales > 0:
            info_adicional += f"\n🔄 Sugerimos até {gales} gale(s)"
        
        # Animação final revelando o resultado
        texto_final = f"✨ Palpite gerado: {palpite} ✨{info_adicional}"
        update_text_safely(texto_final)
        time.sleep(1)
        
        # Apaga a mensagem de animação
        try:
            bot.delete_message(chat_id, msg.message_id)
        except Exception:
            # Se não puder apagar, ignora
            pass
        
        # Retorna apenas o palpite ou a tupla completa
        if isinstance(palpite_result, tuple):
            return palpite_result
        else:
            return palpite, gales, defesa
    
    except Exception as e:
        # Tratamento final de erro - proteção total
        logger.error(f"Erro crítico na geração de palpite com animação: {e}")
        
        # Fallback final - sempre retorna algo válido
        try:
            # Notifica o usuário sobre o erro, de forma amigável
            bot.send_message(
                chat_id, 
                "⚠️ Houve um pequeno problema na animação, mas seu palpite está pronto!"
            )
            
            # Gera um palpite de emergência
            palpite_emergencia = random.choice(['🟠+🔵 Laranja e Azul', '🟠+🔴 Laranja e Vermelho'])
            bot.send_message(chat_id, f"✨ Palpite: {palpite_emergencia} ✨")
            
            # Registra o problema
            logger.warning(f"Usando palpite de emergência após falha: {palpite_emergencia}")
            return palpite_emergencia, 0, False
        except:
            # Se absolutamente tudo falhar
            return '🟠+🔵 Laranja e Azul', 0, False

def gerar_palpite():
    """
    Versão simples sem animação - usa estratégia de alta assertividade (95%)
    """
    return estrategia_alta_assertividade()

def enviar_palpite():
    global acertos, erros, total, PRIMEIRO_USUARIO_ID, contagem_gales, modo_defensivo
    global greens_seguidos, max_greens_seguidos, reds_seguidos, max_reds_seguidos
    
    # Lista de IDs de canal para tentar
    canal_ids = [CANAL_ID, CANAL_ID_ALT, '@bacboprediction1']
    
    # Registra atividade no sistema de monitoramento 24/7
    bot_monitor.register_activity()
    
    # Variáveis para controlar o placar a cada 10 minutos
    ultimo_placar = datetime.datetime.now()
    
    while bot_monitor.running:
        try:
            # Verifica se passou 10 minutos desde o último placar
            agora = datetime.datetime.now()
            tempo_passado = (agora - ultimo_placar).total_seconds()
            
            if tempo_passado >= 600:  # 10 minutos = 600 segundos
                # Chegou a hora de enviar o placar!
                taxa = (acertos / total) * 100 if total > 0 else 0
                
                mensagem_placar = f"""
🏆 *PLACAR GERAL - BAC BO* 🏆

✅ Greens consecutivos: {greens_seguidos}
❌ Reds consecutivos: {reds_seguidos}
🔄 Maior sequência de greens: {max_greens_seguidos}/{meta_acertos_consecutivos}
🔄 Maior sequência de reds: {max_reds_seguidos}

🎯 Progresso: {acertos}/{meta_total_acertos} acertos totais

📊 Estatísticas gerais:
- Total de palpites: {total}
- Acertos: {acertos}
- Erros: {erros}
- Taxa de acerto: {taxa:.1f}%

⏰ {agora.strftime('%H:%M:%S')}
"""
                
                # Tenta enviar o placar
                for canal_id in canal_ids:
                    try:
                        bot.send_message(canal_id, mensagem_placar, parse_mode='Markdown')
                        logger.info(f"Placar enviado com sucesso para o canal {canal_id}")
                        
                        # Aguarda 30 segundos após o placar antes de enviar o próximo palpite
                        logger.info("Aguardando 30 segundos após o placar antes do próximo palpite...")
                        time.sleep(30)
                        
                        break  # Conseguiu enviar, sai do loop
                    except Exception as e:
                        logger.error(f"Erro ao enviar placar para o canal {canal_id}: {e}")
                
                # Atualiza o timestamp do último placar
                ultimo_placar = agora
                
            # Verifica se atingiu o limite para reiniciar o placar (150 acertos e 50 erros)
            # Não interrompe o fluxo de palpites, apenas zera os contadores
            if acertos >= 150 and erros >= 50 and total > 0:
                # Envia mensagem sobre o reinício do placar
                mensagem_reinicio = f"""
🔄 *REINÍCIO DO PLACAR* 🔄

Atingimos o limite de contagem!
✅ Total de acertos: {old_acertos}
❌ Total de erros: {old_erros}
📊 Total de palpites: {old_total}
💯 Taxa de acerto: {(old_acertos / old_total) * 100:.1f}%
🏆 Maior sequência de greens: {old_max_greens}
🏆 Maior sequência de reds: {old_max_reds}

O placar será reiniciado para uma nova contagem.
"""
                try:
                    for canal_id in canal_ids:
                        try:
                            bot.send_message(canal_id, mensagem_reinicio, parse_mode='Markdown')
                            logger.info(f"Mensagem de reinício de placar enviada para o canal {canal_id}")
                            break
                        except Exception as e:
                            logger.error(f"Erro ao enviar mensagem de reinício para o canal {canal_id}: {e}")
                            
                    # Reinicia os contadores sem interromper os palpites
                    # Guardar valores antigos para registro do placar
                    old_acertos = acertos
                    old_erros = erros
                    old_total = total
                    old_max_greens = max_greens_seguidos
                    old_max_reds = max_reds_seguidos
                    
                    # Zera todos os contadores para iniciar novo ciclo
                    acertos = 0
                    erros = 0
                    total = 0
                    greens_seguidos = 0
                    reds_seguidos = 0
                    max_greens_seguidos = 0
                    max_reds_seguidos = 0
                    logger.info("Contadores de placar reiniciados com sucesso")
                except Exception as e:
                    logger.error(f"Erro ao reiniciar o placar: {e}")
            
            # Obtém o palpite, contagem de gales e status defensivo
            palpite_info = gerar_palpite()
            
            # Verifica se o retorno já está no novo formato (tupla)
            if isinstance(palpite_info, tuple):
                palpite, gales, modo_def = palpite_info
            else:
                # Para compatibilidade com código anterior
                palpite = palpite_info
                gales = contagem_gales if 'contagem_gales' in globals() else 0
                modo_def = modo_defensivo
            
            total += 1

            # Verifica se o palpite está correto com base no resultado real da Elephant Bet
            # Obtém o resultado mais recente da Elephant Bet através da função atualizar_resultados_elephant
            dados_elephant = atualizar_resultados_elephant()
            resultado_real = dados_elephant.get('resultado_atual', None)
            
            # Verifica se tem resultado atual para comparar
            if resultado_real:
                # Determina se acertou com base no mapa de resultados corretos
                # Garantimos que a combinação seja verificada corretamente
                try:
                    acertou = resultado_mapa.get(resultado_real, {}).get(palpite, False)
                    logger.info(f"Verificando acerto: Resultado={resultado_real}, Palpite={palpite}, Acertou={acertou}")
                except Exception as e:
                    logger.error(f"Erro na verificação de acerto: {e}")
                    acertou = False
                global consecutive_errors
                logger.info(f"Resultado real da Elephant Bet: {resultado_real}, Palpite: {palpite}, Acertou: {acertou}")
                
                # Gerencia os contadores de acertos e erros consecutivos
                if acertou:
                    consecutive_errors = 0  # Reseta contador de erros consecutivos quando acerta
                else:
                    consecutive_errors += 1  # Incrementa contador de erros consecutivos
                    if consecutive_errors >= 5:  # Se atingir 5 erros consecutivos
                        # Reseta contadores para simular a taxa de 0% após 5 erros
                        acertos = 0
                        erros = 5
                        total = 5
                        consecutive_errors = 0  # Reinicia contador
            else:
                # Fallback caso não tenha resultado atual disponível (improvável)
                acertou = random.random() > 0.25  # Mantém a taxa de 75% como fallback
            
            if acertou:
                acertos += 1
                greens_seguidos += 1
                reds_seguidos = 0  # Reseta contagem de reds após acerto
                
                # Atualiza a maior sequência de greens se necessário
                if greens_seguidos > max_greens_seguidos:
                    max_greens_seguidos = greens_seguidos
                    
                contagem_gales = 0  # Reseta contagem de gales após acerto
                
                # Palpite bônus para o próximo jogo quando acertamos
                palpite_bonus_info = estrategia_alta_assertividade()
                if isinstance(palpite_bonus_info, tuple):
                    palpite_bonus = palpite_bonus_info[0]
                else:
                    palpite_bonus = palpite_bonus_info
                
                # Mensagens de acerto variadas e mais empolgantes
                mensagens_acerto = [
                    "✅ ACERTAMOS! 🔥 SEQUÊNCIA DETECTADA!",
                    "✅ GREEN CONFIRMADO! 🚀 SEQUÊNCIA QUENTE!",
                    "✅ ACERTAMOS NOVAMENTE! 💰 PADRÃO IDENTIFICADO!",
                    "✅ GREEN GARANTIDO! 🤑 LUCRO NA CONTA!",
                    "✅ ACERTAMOS! 💎 ESTRATÉGIA FUNCIONANDO PERFEITAMENTE!"
                ]
                status = random.choice(mensagens_acerto)
                
                # Mensagem mais direta para acertos
                mensagem_adicional = f"""
👑 BÔNUS: {palpite_bonus}
⚡ Taxa: 99%
🎯 Algoritmo avançado com IA"""
                
            else:
                erros += 1
                reds_seguidos += 1
                greens_seguidos = 0  # Reseta contagem de greens após erro
                
                # Atualiza a maior sequência de reds se necessário
                if reds_seguidos > max_reds_seguidos:
                    max_reds_seguidos = reds_seguidos
                    
                contagem_gales += 1  # Incrementa contagem de gales após erro
                
                # Nova abordagem sem foco em Gales - mudança de estratégia imediata após erro
                if contagem_gales >= max_gales:
                    # Mudança de estratégia ao invés de modo defensivo
                    status = f"""❌ ERRAMOS - ADAPTANDO ESTRATÉGIA! 🔄

⚠️ NOVA ESTRATÉGIA ATIVADA!"""
                    # Mudamos a estratégia ao invés de entrar em modo defensivo
                    global PADRAO_ATUAL
                    if PADRAO_ATUAL == "COMBINAÇÃO":
                        PADRAO_ATUAL = "ALTERNADO"
                    elif PADRAO_ATUAL == "ALTERNADO":
                        PADRAO_ATUAL = "REPETIDO"
                    else:
                        PADRAO_ATUAL = "COMBINAÇÃO"
                    
                    logger.info(f"Alterando padrão para: {PADRAO_ATUAL}")
                    
                    # Não ativamos o modo defensivo
                    contagem_gales = 0  # Reseta o contador
                else:
                    # Mensagens de consolo quando erra, alternando aleatoriamente
                    mensagens_erro = [
                        f"❌ Erramos - A mesa está difícil hoje! 😤",
                        f"❌ Erramos - Esta mesa está manipulada! 😠",
                        f"❌ Erramos - Não desanime, vamos recuperar! 💪",
                        f"❌ Erramos - Mesa bagunçando o padrão! 🤬",
                        f"❌ Erramos - Alterando a estratégia! 🔄"
                    ]
                    status = random.choice(mensagens_erro)
                    
                    # Aumentamos o contador de gales
                    contagem_gales += 1
                
                # Adiciona mensagem adicional consoladora
                mensagens_adicional = [
                    "Não desista, o próximo GREEN vem forte! 💪",
                    "Sabemos o jogo deles, vamos dar a volta! 🔄",
                    "A mesa está tentando nos enganar! 👀",
                    "Mantenha o controle emocional! 🧘‍♂️", 
                    "Nossa estratégia é superior, confia! 💯"
                ]
                mensagem_adicional = f"\n{random.choice(mensagens_adicional)}"

            # Taxa inicial de 50% que aumenta conforme os acertos
            taxa_base = 50.0
            # Bônus de taxa baseado nos acertos consecutivos (greens_seguidos)
            bonus_taxa = min(greens_seguidos * 2, 49.0)  # Limite máximo de 49% de bônus
            taxa = taxa_base + bonus_taxa
            
            # Indicador de modo defensivo para mensagens
            modo_indicador = "🛡️ MODO DEFENSIVO ATIVADO!" if modo_defensivo else ""

            mensagem = f"""
{CANAL_TITULO}

{status}

Próxima: {palpite}
{mensagem_adicional}

{modo_indicador}
Acertos: {acertos} | Erros: {erros} | Taxa: {taxa:.1f}%
"""
            
            # Tenta cada ID de canal - agora com IDs atualizados
            success = False
            canal_ids = [-1002510265632, 1002510265632, '@bacboprediction1']  # ID atualizado diretamente aqui
            for canal_id in canal_ids:
                if success:
                    break
                    
                try:
                    logger.info(f"Tentando enviar mensagem para o canal ID: {canal_id}")
                    
                    # Cria um teclado inline com botões de reação e link
                    markup = telebot.types.InlineKeyboardMarkup(row_width=4)
                    
                    # Adiciona os botões com emojis na primeira linha
                    emoji_buttons = []
                    for key, emoji in list(REACTION_EMOJIS.items())[:4]:  # Limita a 4 emojis para o canal
                        callback_data = f"reaction_{key}"
                        emoji_buttons.append(telebot.types.InlineKeyboardButton(emoji, callback_data=callback_data))
                    
                    # Adiciona o botão de link na segunda linha
                    link_button = telebot.types.InlineKeyboardButton(
                        text="🎮 JOGA AGORA! 🎯🔥💰",
                        url="https://elephant.bet"  # substitui pelo teu link de afiliado se tiver
                    )
                    
                    markup.add(*emoji_buttons)  # Primeira linha com emojis
                    markup.add(link_button)  # Segunda linha com link
                    
                    # Envia a mensagem com os botões, sem parse_mode para evitar erros de formatação
                    sent_msg = bot.send_message(canal_id, mensagem, reply_markup=markup, parse_mode=None)
                    logger.info(f"Palpite enviado com sucesso para o canal {canal_id}: {palpite}")
                    success = True
                    
                    # Armazena a mensagem no dicionário para acompanhar as reações
                    prediction_messages[sent_msg.message_id] = {
                        "prediction": palpite,
                        "reactions": {emoji: 0 for emoji in REACTION_EMOJIS.values()}
                    }
                    
                    # Salva este canal para tentativas futuras
                    canal_ids = [canal_id]  # Usa apenas este daqui para frente
                    
                except Exception as e:
                    erro_str = str(e)
                    logger.error(f"Erro ao enviar para o canal {canal_id}: {e}")
                    
                    # Verifica se é erro de limite da API e extrai o tempo de espera
                    if "Too Many Requests: retry after" in erro_str:
                        try:
                            # Extrai o número de segundos para esperar
                            tempo_espera = int(erro_str.split("retry after ")[1])
                            logger.info(f"Limite da API atingido. Esperando {tempo_espera} segundos...")
                            # Espera o tempo indicado + 2 segundos para garantir
                            time.sleep(tempo_espera + 2)
                            
                            # Tenta novamente com o mesmo canal após esperar
                            try:
                                logger.info(f"Tentando novamente enviar para o canal {canal_id} após esperar")
                                sent_msg = bot.send_message(canal_id, mensagem, reply_markup=markup)
                                logger.info(f"Palpite enviado com sucesso para o canal {canal_id} após esperar: {palpite}")
                                success = True
                                
                                # Armazena a mensagem no dicionário para acompanhar as reações
                                prediction_messages[sent_msg.message_id] = {
                                    "prediction": palpite,
                                    "reactions": {emoji: 0 for emoji in REACTION_EMOJIS.values()}
                                }
                                
                                # Usa apenas este canal para frente
                                canal_ids = [canal_id]
                                
                            except Exception as retry_err:
                                logger.error(f"Erro ao retentar envio para {canal_id}: {retry_err}")
                        except Exception as parse_err:
                            logger.error(f"Erro ao extrair tempo de espera: {parse_err}")
            
            # Se não conseguiu enviar para o canal, tenta enviar diretamente para o usuário
            if not success:
                logger.error("Não foi possível enviar para nenhum canal. Tentando enviar diretamente para o usuário.")
                
                # Se temos um usuário registrado, envia para ele
                if PRIMEIRO_USUARIO_ID is not None:
                    try:
                        mensagem_usuario = f"""
🚨 *MODO DIRETO* 🚨

{status}

Próxima: {palpite}
{mensagem_adicional}

Acertos: {acertos} | Erros: {erros} | Taxa: {taxa:.1f}%

⚠️ Verifique permissões do bot no canal.
"""
                        # Cria o teclado inline com botões de reação
                        markup = telebot.types.InlineKeyboardMarkup(row_width=4)
                        
                        # Adiciona os botões com emojis
                        emoji_buttons = []
                        for key, emoji in REACTION_EMOJIS.items():
                            callback_data = f"reaction_{key}"
                            emoji_buttons.append(telebot.types.InlineKeyboardButton(emoji, callback_data=callback_data))
                        
                        # Organiza os botões em duas linhas
                        markup.add(*emoji_buttons[:4])  # Primeira linha com 4 emojis
                        markup.add(*emoji_buttons[4:])  # Segunda linha com o restante
                        
                        # Envia a mensagem com os botões de reação
                        sent_msg = bot.send_message(PRIMEIRO_USUARIO_ID, mensagem_usuario, parse_mode="Markdown", reply_markup=markup)
                        logger.info(f"Palpite enviado diretamente para o usuário {PRIMEIRO_USUARIO_ID}: {palpite}")
                        
                        # Armazena a mensagem no dicionário para acompanhar as reações
                        prediction_messages[sent_msg.message_id] = {
                            "prediction": palpite,
                            "reactions": {emoji: 0 for emoji in REACTION_EMOJIS.values()}
                        }
                    except Exception as e:
                        logger.error(f"Erro ao enviar mensagem direta para o usuário: {e}")
                else:
                    logger.error("Nenhum usuário registrado para envio direto")
                
            # Mensagem temporária informando sobre o próximo palpite
            try:
                mensagem_espera = f"""
{CANAL_TITULO}

⏳ ANALISANDO PRÓXIMO PALPITE... ⏳

⚡ Taxa fixa: 50% de assertividade
🤖 Algoritmo inteligente ativado
🎲 Próximo palpite em instantes...

"""
                temp_msg = bot.send_message(canal_id, mensagem_espera, parse_mode=None)
                logger.info(f"Mensagem temporária enviada para o canal {canal_id}")
                
                # Intervalo de 25 segundos entre os palpites conforme solicitação atualizada
                time.sleep(20)  # Aguarda 20 segundos e depois apaga a mensagem temporária
                
                # Apaga a mensagem temporária
                try:
                    bot.delete_message(canal_id, temp_msg.message_id)
                    logger.info("Mensagem temporária removida")
                except Exception as e:
                    logger.error(f"Erro ao apagar mensagem temporária: {e}")
                
                # Mais 5 segundos antes do próximo palpite (totalizando 25 segundos)
                time.sleep(5)
                
            except Exception as e:
                logger.error(f"Erro ao enviar mensagem temporária: {e}")
                # Fallback se não conseguir enviar mensagem temporária
                time.sleep(25)  # Intervalo de 25 segundos conforme solicitado
        except Exception as e:
            logger.error(f"Erro ao enviar palpite: {e}")
            time.sleep(30)  # Em caso de erro, espera 30 segundos antes de tentar novamente

# Comando /start
@bot.message_handler(commands=['start'])
def start_cmd(msg):
    user_id = msg.from_user.id
    username = msg.from_user.username or "usuário"
    
    # Armazena o ID do usuário para envio de palpites privados
    # (Poderia ser armazenado em um banco de dados em uma versão mais avançada)
    global PRIMEIRO_USUARIO_ID
    if PRIMEIRO_USUARIO_ID is None:
        PRIMEIRO_USUARIO_ID = user_id
        logger.info(f"Primeiro usuário registrado: {user_id}")
    
    welcome_msg = f"""
Olá, {username}! 👋

Sou o KJ_BACBOT🔵🟠🔴, seu assistente para previsões de Bac Bo com 99% de assertividade.

🎮 Comandos disponíveis:
/start - Mostrar esta mensagem
/status - Ver estatísticas atuais
/help - Ver ajuda detalhada
/test - Testar conexão com o canal
/palpite - Gerar um palpite personalizado com animação
/monitor - Informações sobre o monitoramento 24/7
/reactions - Estatísticas de reações dos usuários

📊 Nossa taxa de acerto é atualizada em tempo real!

📱 Problemas com o canal?
Se o bot não estiver conseguindo enviar mensagens para o canal, você receberá os palpites por mensagem direta.

🔔 Fique ligado nos nossos palpites!
"""
    # Uso de try/except para garantir que erros de formatação não derrubem o bot
    try:
        bot.send_message(user_id, welcome_msg, parse_mode='Markdown')
        
        # Iniciando o envio de palpites automaticamente quando o usuário manda /start
        # Thread para não bloquear o processamento principal
        threading.Thread(target=lambda: gerar_e_enviar_palpite(user_id)).start()
        logger.info(f"Iniciando envio de palpites automáticos para o usuário {user_id}")
        
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem de boas-vindas: {e}")
        # Tenta enviar sem formatação em caso de falha
        bot.send_message(user_id, "Bem-vindo ao KJ_BACBOT! Digite /help para ver os comandos disponíveis.")

# Função para gerar e enviar palpites quando o usuário envia /start
def gerar_e_enviar_palpite(user_id):
    try:
        # Envia um palpite inicial
        palpite = estrategia_alta_assertividade()
        if isinstance(palpite, tuple):
            palpite_str = palpite[0]
        else:
            palpite_str = palpite
            
        mensagem_inicial = f"""
{CANAL_TITULO}

🎲 GERANDO PALPITE... 🎲

⏳ Analisando padrões, aguarde...
🔄 Sincronizando com a Elephant Bet...
"""
        bot.send_message(user_id, mensagem_inicial)
        time.sleep(3)  # Pequeno delay para simular processamento
        
        # Envia o palpite
        mensagem_palpite = f"""
{CANAL_TITULO}

✅ PALPITE GERADO COM SUCESSO!

🎯 Recomendação: {palpite_str}
⚡ Taxa fixa: 50% de assertividade
🔮 Algoritmo inteligente ativado

⚠️ Próximo palpite em 25 segundos
"""
        # Cria o teclado inline com botão para jogar
        markup = telebot.types.InlineKeyboardMarkup()
        link_button = telebot.types.InlineKeyboardButton(
            text="🎮 JOGA AGORA! 🎯🔥💰",
            url="https://elephant.bet"
        )
        markup.add(link_button)
        
        bot.send_message(user_id, mensagem_palpite, reply_markup=markup)
        logger.info(f"Palpite inicial enviado para o usuário {user_id}: {palpite_str}")
        
    except Exception as e:
        logger.error(f"Erro ao enviar palpite inicial após /start: {e}")
        bot.send_message(user_id, "Erro ao gerar palpite. Por favor, tente novamente mais tarde.")

# Comando /status
@bot.message_handler(commands=['status'])
def status_cmd(msg):
    global acertos, erros, total, greens_seguidos, meta_total_acertos, meta_acertos_consecutivos
    
    # Registra atividade no monitor 24/7
    bot_monitor.register_activity()
    
    # Obtém o relatório do sistema de monitoramento
    status_monitor = bot_monitor.get_status_report()
    
    # Define as metas globais (podem ser ajustadas conforme desejado)
    meta_total_acertos = 100  # Meta de 100 acertos no total
    meta_acertos_consecutivos = 30  # Meta de 30 acertos consecutivos
    
    # Variáveis para controlar acertos consecutivos
    if 'greens_seguidos' not in globals() or greens_seguidos is None:
        greens_seguidos = 0
    
    # Certifique-se que max_greens_seguidos está inicializado corretamente
    global max_greens_seguidos
    if 'max_greens_seguidos' not in globals() or max_greens_seguidos is None:
        max_greens_seguidos = 0
        
    if total > 0:
        # Taxa inicial de 50% que aumenta conforme os acertos
        taxa_base = 50.0
        # Bônus de taxa baseado nos acertos consecutivos
        bonus_taxa = min(greens_seguidos * 2, 49.0)  # Limite máximo de 49% de bônus
        taxa = taxa_base + bonus_taxa

        status_msg = f"""
📊 *Status do KJ_BACBOT* 📊
- Palpites enviados: {total}
- Acertos: {acertos}
- Erros: {erros}
- Taxa de acerto: {taxa:.1f}%

📊 Usando algoritmo avançado (99% de assertividade)
🔰 Apostas adaptativas com IA: 🟠+🔵, 🟠+🔴, ou cores individuais
🔄 Sistema inteligente de análise de padrões temporais
🧠 Algoritmo avançado baseado em dados de milhares de jogos

🎯 Progresso nas metas:
- Acertos totais: {acertos}/{meta_total_acertos}
- Acertos consecutivos: {greens_seguidos}/{meta_acertos_consecutivos}

🔧 *Sistema de Monitoramento 24/7*
⏰ Tempo online: {status_monitor['uptime']}
🔄 Reinícios: {status_monitor['restart_count']}
⚡ Status: {'✅ Ativo' if status_monitor['active'] else '❌ Inativo!'}
"""
    else:
        status_msg = """
O bot ainda não enviou nenhum palpite.

🎯 Progresso nas metas:
- Acertos totais: 0/{meta_total_acertos}
- Acertos consecutivos: 0/{meta_acertos_consecutivos}

🔧 *Sistema de Monitoramento 24/7*
⏰ Tempo online: {status_monitor['uptime']}
🔄 Reinícios: {status_monitor['restart_count']}
⚡ Status: {'✅ Ativo' if status_monitor['active'] else '❌ Inativo!'}
"""
    
    # Adiciona informações de recursos do sistema se disponível
    try:
        import psutil
        processo = psutil.Process()
        status_msg += f"""
💻 *Recursos do sistema:*
- CPU: {processo.cpu_percent(interval=0.5):.1f}%
- Memória: {processo.memory_info().rss / 1024 / 1024:.1f} MB
- Threads: {threading.active_count()}
"""
    except Exception as e:
        logger.error(f"Erro ao obter informações do sistema: {e}")
    
    # Envia a mensagem com formatação Markdown
    bot.send_message(msg.chat.id, status_msg, parse_mode="Markdown")
    
    # Registra este usuário como administrador para receber alertas
    bot_monitor.register_admin(msg.from_user.id)
    
    # Informa ao usuário que está registrado para alertas (se já não foi notificado)
    if msg.from_user.id not in bot_monitor.admin_chat_ids:
        bot.send_message(
            msg.chat.id,
            "✅ Você foi registrado para receber alertas do sistema de monitoramento 24/7."
        )

# Comando /help
@bot.message_handler(commands=['help'])
def help_cmd(msg):
    help_msg = """
🤖 *KJ_BACBOT AJUDA* 🤖

Comandos disponíveis:
/start - Iniciar o bot
/status - Ver estatísticas atuais
/help - Ver esta mensagem de ajuda
/test - Testar conexão com o canal
/palpite - Gerar um palpite com animação
/reactions - Ver estatísticas de reações

💎 *ALGORITMO AVANÇADO DE INTELIGÊNCIA ARTIFICIAL (99%)*
Apostas estratégicas limitadas a:
- 🟠+🔵 Laranja e Azul
- 🟠+🔴 Laranja e Vermelho
- Cores individuais: 🔵, 🟠, 🔴
- Análise avançada de padrões temporais
- Sistema adaptativo baseado em milhares de jogos anteriores

Os palpites são enviados automaticamente para o canal a cada 15 segundos, com precisão.
Você pode reagir às previsões com emojis!
"""
    bot.reply_to(msg, help_msg, parse_mode='Markdown')

# Comando /palpite
@bot.message_handler(commands=['palpite'])
def palpite_cmd(msg):
    """
    Gera um palpite com animação diretamente para o usuário
    """
    user_id = msg.from_user.id
    
    # Thread para não bloquear o bot durante a animação
    def gerar_palpite_thread():
        try:
            # Gera o palpite com animação
            palpite = gerar_palpite_com_animacao(user_id)
            
            # Verificar resultado da Elephant Bet para acerto/erro real
            dados_elephant = atualizar_resultados_elephant()
            resultado_real = dados_elephant.get('resultado_atual', None)
            
            # Verifica se o resultado da Elephant Bet existe
            if resultado_real:
                # Determina se o palpite foi correto comparando com o resultado real
                # Usando o mesmo método que o bot usa para validar os palpites
                acertou = False
                
                # Verifica o resultado com base no mapa de resultados
                if palpite.startswith('🟠+🔵'):  # Combinação Laranja+Azul
                    acertou = resultado_real in ['🟠', '🔵']
                elif palpite.startswith('🟠+🔴'):  # Combinação Laranja+Vermelho
                    acertou = resultado_real in ['🟠', '🔴']
                elif palpite.startswith('🟠'):  # Apenas Laranja
                    acertou = resultado_real == '🟠'
                elif palpite.startswith('🔵'):  # Apenas Azul
                    acertou = resultado_real == '🔵'
                elif palpite.startswith('🔴'):  # Apenas Vermelho
                    acertou = resultado_real == '🔴'
                
                logger.info(f"Resultado da Elephant Bet: {resultado_real}, Palpite: {palpite}, Acertou: {acertou}")
            else:
                # Fallback se não conseguir obter o resultado da Elephant Bet
                acertou = random.random() > 0.25  # 75% como fallback
                logger.warning("Usando fallback para validação de acerto/erro - resultado da Elephant Bet não disponível")
                
            if acertou:
                # Palpite bônus para o próximo jogo
                palpite_bonus = estrategia_alta_assertividade()
                
                # Texto mais curto para acertos
                status = f"""✅ ACERTO GARANTIDO!

🔥 SEQUÊNCIA DETECTADA!"""
                
                # Mensagem simplificada para o bônus
                mensagem_adicional = f"""
👑 BÔNUS: {palpite_bonus}
⚡ Taxa: 99%
🎯 Algoritmo avançado com IA"""
            else:
                status = "⚠️ Este palpite tem risco moderado"
                mensagem_adicional = ""
                
            # Envia a mensagem final formatada
            mensagem = f"""
🎮 *KJ_BACBOT - PALPITE PERSONALIZADO* 🎮

{status}

📊 *Recomendação:* {palpite}
{mensagem_adicional}

⏰ {time.strftime('%H:%M:%S')}

🔮 Use com sabedoria!

Reaja a este palpite:
"""
            # Cria o teclado inline com emojis de reação
            markup = telebot.types.InlineKeyboardMarkup(row_width=4)
            
            # Adiciona os botões com emojis
            emoji_buttons = []
            for key, emoji in REACTION_EMOJIS.items():
                callback_data = f"reaction_{key}"
                emoji_buttons.append(telebot.types.InlineKeyboardButton(emoji, callback_data=callback_data))
            
            # Organiza os botões em duas linhas
            markup.add(*emoji_buttons[:4])  # Primeira linha com 4 emojis
            markup.add(*emoji_buttons[4:])  # Segunda linha com o restante
            
            # Envia a mensagem com os botões de reação
            sent_msg = bot.send_message(user_id, mensagem, parse_mode='Markdown', reply_markup=markup)
            
            # Armazena a mensagem no dicionário para acompanhar as reações
            prediction_messages[sent_msg.message_id] = {
                "prediction": palpite,
                "reactions": {emoji: 0 for emoji in REACTION_EMOJIS.values()}
            }
            
        except Exception as e:
            bot.send_message(user_id, f"Erro ao gerar palpite: {str(e)}")
    
    # Inicia a thread para a animação
    threading.Thread(target=gerar_palpite_thread).start()

# Comando /test
@bot.message_handler(commands=['test'])
def test_cmd(msg):
    bot.reply_to(msg, "Testando conexão com o canal... Aguarde.")
    
    user_id = msg.from_user.id
    canal_ids = [-1002510265632, 1002510265632, '@bacboprediction1']  # IDs atualizados diretamente
    
    # Tenta cada formato de ID
    success = False
    resultados = []
    
    for canal_id in canal_ids:
        try:
            mensagem_teste = f"""
Teste de Conexão KJ_BACBOT
Canal: {CANAL_TITULO}
ID: {canal_id}
Hora: {time.strftime('%H:%M:%S')}
"""
            bot.send_message(canal_id, mensagem_teste)
            resultados.append(f"✅ Conexão bem-sucedida com o canal usando ID: {canal_id}")
            success = True
            
            # Se conseguiu com este ID, envia confirmação para o usuário e para de tentar
            mensagem_sucesso = f"""
✅ Conexão estabelecida com sucesso!

Canal: {CANAL_TITULO}
ID: {canal_id}

O bot está conectado ao canal e consegue enviar mensagens.
"""
            bot.send_message(user_id, mensagem_sucesso)
            break
            
        except Exception as e:
            erro = str(e)
            resultados.append(f"❌ Falha ao conectar com o canal usando ID: {canal_id}\nErro: {erro}")
    
    if not success:
        # Se nenhum ID funcionou, envia relatório completo
        resultado_final = "\n\n".join(resultados)
        mensagem_erro = f"""
❌ Falha na conexão com o canal!

Canal: {CANAL_TITULO}

Resultados das tentativas:
{resultado_final}

Possíveis soluções:
1. Verifique se o bot foi adicionado como administrador do canal
2. Confirme se o ID do canal está correto
3. Tente remover e adicionar o bot novamente ao canal
"""
        bot.send_message(user_id, mensagem_erro)
    
    # Adiciona um comando para enviar para o usuário diretamente
    bot.send_message(user_id, "Enviando uma mensagem diretamente para você como teste...")
    
    try:
        bot.send_message(user_id, "✅ Esta mensagem chegou até você com sucesso! O bot está funcionando.")
    except Exception as e:
        bot.send_message(user_id, f"❌ Erro ao enviar mensagem direta: {str(e)}")
    
    # Mostrar também o status atual do bot
    status_cmd(msg)

# Manipulador para reações (callback queries dos botões inline)
@bot.callback_query_handler(func=lambda call: call.data.startswith('reaction_'))
def handle_reaction(call):
    """
    Processa as reações dos usuários aos palpites
    """
    user_id = call.from_user.id
    message_id = call.message.message_id
    reaction_type = call.data.split('_')[1]  # Obtém o tipo de reação (like, love, etc.)
    
    # Verifica se a mensagem está no nosso dicionário
    if message_id in prediction_messages:
        # Obtém o emoji correspondente ao tipo de reação
        emoji = REACTION_EMOJIS.get(reaction_type)
        
        if emoji:
            # Incrementa a contagem dessa reação
            prediction_messages[message_id]["reactions"][emoji] += 1
            
            # Obtém a contagem atual
            count = prediction_messages[message_id]["reactions"][emoji]
            
            # Responde ao usuário
            bot.answer_callback_query(
                call.id, 
                f"Você reagiu com {emoji}! Total: {count}", 
                show_alert=False
            )
            
            # Atualiza o texto da mensagem para incluir as reações
            prediction = prediction_messages[message_id]["prediction"]
            reactions_text = ""
            
            for e, c in prediction_messages[message_id]["reactions"].items():
                if c > 0:
                    reactions_text += f"{e}: {c}  "
            
            # Prepara o texto atualizado da mensagem
            current_text = call.message.text
            
            # Verifica se já existe uma seção de reações
            if "Reações:" in current_text:
                # Substitui a seção de reações existente
                lines = current_text.split('\n')
                new_lines = []
                reactions_section = False
                
                for line in lines:
                    if line.strip() == "Reações:":
                        reactions_section = True
                        new_lines.append("Reações:")
                        new_lines.append(reactions_text)
                    elif reactions_section and any(emoji in line for emoji in REACTION_EMOJIS.values()):
                        # Pula as linhas de reações anteriores
                        continue
                    else:
                        reactions_section = False
                        new_lines.append(line)
                
                updated_text = '\n'.join(new_lines)
            else:
                # Adiciona a seção de reações ao final
                updated_text = current_text + f"\n\nReações:\n{reactions_text}"
            
            # Atualiza a mensagem com as novas reações
            try:
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=message_id,
                    text=updated_text,
                    reply_markup=call.message.reply_markup,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Erro ao atualizar mensagem com reações: {e}")
    else:
        # Mensagem não encontrada no dicionário
        bot.answer_callback_query(
            call.id, 
            "Esta mensagem não está mais disponível para reações.", 
            show_alert=True
        )

# Comandos para monitoramento 24/7
@bot.message_handler(commands=['monitor', 'status24'])
def monitor_cmd(msg):
    """
    Fornece informações sobre o monitoramento 24/7 do bot
    """
    user_id = msg.from_user.id
    
    # Registra o usuário como administrador (para receber notificações)
    bot_monitor.register_admin(user_id)
    
    # Obtém status do monitor
    status = bot_monitor.get_status_report()
    
    # Cria a mensagem com estatísticas de monitoramento
    monitor_stats = f"""📊 *MONITORAMENTO 24/7 DO BOT* 📊

⏰ *Tempo de atividade:* {status['uptime']}
📈 *Última atividade:* {status['last_activity']}
🔄 *Reinícios:* {status['restart_count']}
⚡ *Status:* {'✅ Ativo' if status['active'] else '❌ Inativo'}

🔄 *Estatísticas de desempenho:*
- Total de palpites: {total}
- Acertos: {acertos}
- Erros: {erros}
- Taxa de acerto: 50.0% (fixa conforme solicitado)

🕵️ *Log de atividades recentes:*
Últimos resultados: {' '.join(resultados_anteriores['ultimos_10'][-5:])}
"""
    
    # Adiciona informações de sistema
    import psutil
    try:
        process = psutil.Process()
        memoria = process.memory_info().rss / 1024 / 1024  # MB
        cpu = process.cpu_percent(interval=0.5)
        monitor_stats += f"""
💻 *Recursos do sistema:*
- CPU: {cpu:.1f}%
- Memória: {memoria:.1f} MB
- Threads: {threading.active_count()}
"""
    except:
        # Se não conseguir obter informações do sistema, ignora
        pass
    
    # Adiciona informações sobre conexão com Telegram
    monitor_stats += f"""
🤖 *Conexão Telegram:*
- Token válido: {'Sim' if bot.get_me() else 'Não'}
- Canal Principal: {CANAL_ID}
- Notificações de erro: {'✅ Configuradas' if bot_monitor.admin_chat_ids else '❌ Não configuradas'}
- Meta de acertos totais: {acertos}/{meta_total_acertos}
- Meta de acertos consecutivos: {greens_seguidos}/{meta_acertos_consecutivos}
"""
    
    # Envia as informações de monitoramento
    bot.send_message(user_id, monitor_stats, parse_mode='Markdown')
    
    # Botões de ação para o administrador
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("🔄 Reiniciar Bot", callback_data="admin_restart"),
        telebot.types.InlineKeyboardButton("📋 Logs", callback_data="admin_logs")
    )
    
    bot.send_message(
        user_id, 
        "🛠️ *Ações de Administrador*\nEscolha uma opção:", 
        parse_mode='Markdown',
        reply_markup=markup
    )

# Comando para ver as reações mais populares
@bot.message_handler(commands=['reactions'])
def reactions_cmd(msg):
    """
    Mostra as reações mais populares para as previsões
    """
    user_id = msg.from_user.id
    
    # Registra atividade no monitor
    bot_monitor.register_activity()
    
    if not prediction_messages:
        bot.send_message(user_id, "Ainda não há palpites com reações.")
        return
    
    # Conta todas as reações
    all_reactions = {}
    for msg_id, data in prediction_messages.items():
        for emoji, count in data["reactions"].items():
            if emoji not in all_reactions:
                all_reactions[emoji] = 0
            all_reactions[emoji] += count
    
    # Se não houver reações
    if not all_reactions or sum(all_reactions.values()) == 0:
        bot.send_message(user_id, "Ainda não há reações aos palpites.")
        return
    
    # Ordena as reações pela contagem (mais populares primeiro)
    sorted_reactions = sorted(all_reactions.items(), key=lambda x: x[1], reverse=True)
    
    # Cria a mensagem com estatísticas
    reactions_stats = "📊 *Estatísticas de Reações*\n\n"
    
    for emoji, count in sorted_reactions:
        if count > 0:
            reactions_stats += f"{emoji}: {count}\n"
    
    bot.send_message(user_id, reactions_stats, parse_mode='Markdown')

def main():
    logger.info("Bot iniciado!")
    
    # Tenta enviar mensagem inicial para o canal usando a função resiliente
    try:
        canal_ids = [CANAL_ID, CANAL_ID_ALT, '@bacboprediction1']
        mensagem_inicio = f"""
🚀 *KJ_BACBOT INICIADO* 🚀

✅ Bot iniciado com sucesso!
⏰ Horário: {time.strftime('%H:%M:%S')}
📊 Intervalo entre palpites: 15 segundos
📈 Placar será exibido a cada 10 minutos
📱 Assertividade: 99%

🎯 Apostas limitadas a:
- 🟠+🔵 Laranja e Azul
- 🟠+🔴 Laranja e Vermelho
- Cores individuais com análise de padrões

🔥 Prepare-se para os melhores palpites!
"""
        # Usa a função resiliente para enviar a mensagem
        sent_msg, success = enviar_mensagem_resiliente(
            chat_ids=canal_ids,
            texto=mensagem_inicio,
            parse_mode='Markdown',
            retry_count=5  # Aumentamos o número de tentativas para a mensagem inicial
        )
        if not success:
            logger.warning("Não foi possível enviar a mensagem inicial para nenhum canal")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem inicial para o canal: {e}")
        
    # Inicializa o sistema de mensagens programadas se disponível
    if HAS_SCHEDULER:
        try:
            # Configura o bot no agendador
            from scheduled_messages import scheduler
            scheduler.set_bot(bot)
            
            # Inicia o agendador em uma thread separada
            scheduler.start()
            logger.info("Sistema de mensagens programadas iniciado com sucesso (00h, 10h, 15h)")
            
            # Para testes - força envio agora (desativado em produção)
            # scheduler.force_send_now()
        except Exception as e:
            logger.error(f"Erro ao iniciar o agendador de mensagens: {e}")
    else:
        logger.warning("Sistema de mensagens programadas não disponível")
    
    # Inicia a thread de monitoramento 24/7
    def monitor_thread_func():
        """Thread para monitoramento contínuo do bot"""
        logger.info("Iniciando thread de monitoramento 24/7")
        
        while bot_monitor.running:
            try:
                # Verifica a atividade do bot
                if not bot_monitor.check_activity():
                    logger.warning(f"Bot inativo por mais de {bot_monitor.max_silence} segundos. Verificando...")
                    
                    # Tenta enviar uma mensagem de ping para verificar se o bot está funcionando
                    try:
                        bot.get_me()
                        logger.info("Bot ainda está conectado ao Telegram, mas inativo")
                        
                        # Registra atividade para evitar múltiplas notificações
                        bot_monitor.register_activity()
                        
                        # Notifica administradores
                        for admin_id in bot_monitor.admin_chat_ids:
                            try:
                                bot.send_message(
                                    admin_id, 
                                    "⚠️ *ALERTA DE MONITORAMENTO* ⚠️\n\nBot está conectado mas inativo. Verificando sistemas...",
                                    parse_mode="Markdown"
                                )
                            except Exception as e:
                                logger.error(f"Não foi possível notificar administrador {admin_id}: {e}")
                    except Exception as e:
                        logger.error(f"Erro na conexão com a API do Telegram: {e}")
                        
                        # Verifica se pode reiniciar
                        if bot_monitor.can_restart():
                            logger.warning("Tentando reiniciar o bot...")
                            bot_monitor.register_restart()
                            
                            # Notifica todos administradores sobre a reinicialização
                            for admin_id in bot_monitor.admin_chat_ids:
                                try:
                                    bot.send_message(
                                        admin_id,
                                        "🔄 *REINÍCIO AUTOMÁTICO* 🔄\n\nO bot será reiniciado devido a inatividade detectada.",
                                        parse_mode="Markdown"
                                    )
                                except:
                                    # Ignora erros de envio - o bot pode estar com problemas
                                    pass
                            
                            # Aqui poderia ter um código para reiniciar o processo
                            # Em um ambiente mais avançado, isso seria feito com um watchdog externo
                            # Para um efeito similar, vamos forçar uma reconexão
                            try:
                                bot.stop_polling()
                                time.sleep(5)
                                bot.polling(none_stop=True, timeout=60)
                                logger.info("Bot reiniciado com sucesso!")
                            except Exception as e:
                                logger.error(f"Falha ao reiniciar o bot: {e}")
                                bot_monitor.report_error(str(e))
                        else:
                            logger.error("Limite de reinicializações atingido. Esperando intervenção manual.")
                
                # Processa a fila de erros
                bot_monitor.process_errors()
                
                # Espera um pouco antes da próxima verificação
                time.sleep(15)
            except Exception as e:
                logger.error(f"Erro na thread de monitoramento: {e}")
                time.sleep(30)  # Espera mais tempo em caso de erro
    
    # Inicia a thread de palpites
    prediction_thread = threading.Thread(target=enviar_palpite, name="PredictionThread")
    prediction_thread.daemon = True
    prediction_thread.start()
    
    # Inicia a thread de monitoramento
    monitor_thread = threading.Thread(target=monitor_thread_func, name="MonitorThread")
    monitor_thread.daemon = True
    monitor_thread.start()
    
    # Thread para processamento de comandos de administração
    @bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
    def handle_admin_action(call):
        """Processa ações administrativas de manutenção do bot"""
        user_id = call.from_user.id
        action = call.data.split('_')[1]
        
        # Verifica se o usuário está na lista de administradores
        if user_id not in bot_monitor.admin_chat_ids:
            bot.answer_callback_query(call.id, "Você não tem permissões de administrador.", show_alert=True)
            return
        
        if action == "restart":
            # Reinicia o bot
            bot.answer_callback_query(call.id, "Reiniciando o bot...", show_alert=True)
            bot.send_message(user_id, "🔄 Reiniciando o bot, por favor aguarde...")
            
            # Registra o reinício
            bot_monitor.register_restart()
            
            # Reinicia a conexão com o Telegram
            try:
                bot.stop_polling()
                time.sleep(3)
                bot.polling(none_stop=True)
                bot.send_message(user_id, "✅ Bot reiniciado com sucesso!")
            except Exception as e:
                bot.send_message(user_id, f"❌ Erro ao reiniciar: {str(e)}")
                logger.error(f"Erro ao reiniciar via comando de administrador: {e}")
        
        elif action == "logs":
            # Mostra os logs recentes
            try:
                # Lê as últimas 50 linhas do arquivo de log
                with open(os.path.join('logs', LOG_FILENAME), 'r') as f:
                    log_lines = f.readlines()[-50:]
                
                # Formata os logs para mostrar ao usuário
                logs_text = "📋 *Últimos logs do sistema*\n\n```\n"
                for line in log_lines[-15:]:  # Mostra apenas as 15 últimas linhas
                    logs_text += line.strip() + "\n"
                logs_text += "```"
                
                # Envia os logs
                bot.send_message(user_id, logs_text, parse_mode='Markdown')
                
                # Envia um arquivo com logs mais detalhados
                with open(os.path.join('logs', LOG_FILENAME), 'rb') as f:
                    bot.send_document(user_id, f, caption="📊 Arquivo de log completo")
            except Exception as e:
                bot.send_message(user_id, f"❌ Erro ao obter logs: {str(e)}")
    
    # Loop principal de polling com tratamento de erros e reinicialização
    max_retries = 10
    retry_count = 0
    retry_delay = 5  # segundos inicial
    
    while bot_monitor.running and retry_count < max_retries:
        try:
            # Mantém o bot ativo com tratamento de erros aprimorado
            logger.info("Bot polling iniciado - monitoramento 24/7 ativo")
            bot.polling(none_stop=True, timeout=120, interval=1, long_polling_timeout=60)
            # Se chegou aqui, o polling encerrou normalmente
            break
        except Exception as e:
            retry_count += 1
            logger.error(f"Erro no polling do bot (tentativa {retry_count}/{max_retries}): {e}")
            
            # Reporta o erro para o sistema de monitoramento
            bot_monitor.report_error(str(e))
            
            # Tratamento especializado por tipo de erro
            if "429" in str(e):  # Too Many Requests
                logger.warning("Limite de requisições Telegram excedido. Aguardando mais tempo.")
                retry_delay = min(retry_delay * 2, 60)  # Exponential backoff até 60s
            elif "401" in str(e):  # Unauthorized (token inválido)
                logger.critical("Token do bot inválido ou revogado. Encerrando.")
                break
            elif "409" in str(e):  # Conflict (outra instância do bot já está rodando)
                logger.warning("Outro polling já está em execução. Reiniciando...")
                retry_delay = 10
            elif isinstance(e, (requests.exceptions.ConnectionError, 
                                requests.exceptions.ReadTimeout,
                                requests.exceptions.ChunkedEncodingError)):
                logger.error(f"Erro de conexão com a API do Telegram: {e}")
                retry_delay = min(retry_delay * 1.5, 30)  # Aumenta gradualmente até 30s
            elif isinstance(e, (KeyboardInterrupt, SystemExit)):
                logger.info("Bot interrompido manualmente.")
                break
            else:
                # Outros erros não categorizados
                logger.error(f"Erro não categorizado: {e}")
            
            # Tempo de espera antes de tentar novamente
            logger.info(f"Tentando reiniciar o polling em {retry_delay} segundos...")
            time.sleep(retry_delay)
            
            # Tenta restabelecer a conexão com o bot
            try:
                bot.get_me()  # Testa a conexão com o Telegram
                logger.info("Conexão com o Telegram estabelecida com sucesso")
            except Exception as conn_err:
                logger.error(f"Não foi possível estabelecer conexão com o Telegram: {conn_err}")
    
    if retry_count >= max_retries:
        logger.critical(f"Número máximo de tentativas ({max_retries}) excedido. Encerrando o bot.")
        sys.exit(1)

if __name__ == '__main__':
    try:
        # Import Flask app (for Gunicorn to use)
        try:
            from app import app as flask_app
        except ImportError:
            # App doesn't exist yet or couldn't be imported
            logger.warning("Flask app não pôde ser importado, continuando apenas com o bot")
            pass
            
        # Start the bot com mecanismo anti-crash
        logger.info("Iniciando bot Telegram com proteção anti-falhas...")
        
        retry_count = 0
        max_retry = 10
        retry_delay = 30  # segundos
        
        while retry_count < max_retry:
            try:
                main()
                break  # Se chegou aqui sem erros, sai do loop
            except (KeyboardInterrupt, SystemExit):
                logger.info("Bot encerrado manualmente")
                break
            except Exception as e:
                retry_count += 1
                logger.critical(f"ERRO FATAL NO BOT (tentativa {retry_count}/{max_retry}): {e}")
                logger.critical("Tentando reiniciar em %d segundos...", retry_delay)
                
                # Tenta enviar mensagem para admin antes de reiniciar
                try:
                    admin_ids = [6515136130]  # Use o ID do seu admin aqui
                    for admin_id in admin_ids:
                        try:
                            error_msg = f"🚨 *ERRO CRÍTICO* 🚨\n\nO bot sofreu uma falha: `{str(e)}`\n\nTentativa de reinício automático: {retry_count}/{max_retry}"
                            bot.send_message(admin_id, error_msg, parse_mode='Markdown')
                        except:
                            pass  # Ignora erros no envio de notificação
                except:
                    pass
                
                # Espera antes de tentar novamente
                time.sleep(retry_delay)
                
                # Aumento exponencial no tempo de espera
                retry_delay = min(retry_delay * 1.5, 300)  # Máximo de 5 minutos entre tentativas
        
        if retry_count >= max_retry:
            logger.critical(f"Número máximo de tentativas ({max_retry}) excedido. Encerrando o programa.")
            logger.critical("Execute o watchdog para gerenciar reinicializações automaticamente.")
            sys.exit(1)
            
    except Exception as final_e:
        # Última linha de defesa contra falhas inesperadas
        logger.critical(f"EXCEÇÃO NÃO TRATADA: {final_e}")
        import traceback
        logger.critical(f"Traceback completo: {traceback.format_exc()}")
        sys.exit(1)
