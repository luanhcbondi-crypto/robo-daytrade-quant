import datetime as dt
import html
import json
import logging
import os
import re
import socket
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ==============================================================================
# 1. BLINDAGEM DE REDE IPv4-FIRST (EVITA [Errno 101] NO GITHUB ACTIONS)
# ==============================================================================
_ORIG_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo_ipv4_first(
    host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> Any:
    res = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    ipv4 = [r for r in res if r[0] == socket.AF_INET]
    return ipv4 if ipv4 else res


socket.getaddrinfo = _getaddrinfo_ipv4_first  # type: ignore[assignment]

import requests
from tradingview_ta import Interval, TA_Handler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("QuantDayTradeBot")

# ==============================================================================
# 2. CONFIGURAÇÕES GERAIS E VARIÁVEIS DE AMBIENTE (GITHUB SECRETS)
# ==============================================================================
TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
TELEGRAM_GROUP_ID = (os.environ.get("TELEGRAM_GROUP_ID") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'")

HISTORICO_PATH = Path(os.environ.get("HISTORICO_ARQUIVO", "historico.json"))
TIMEFRAME_PADRAO = (os.environ.get("DAYTRADE_INTERVAL", "15m") or "15m").strip().lower()
ATR_MULTIPLIER = float(os.environ.get("ATR_MULTIPLIER", "1.5"))
ATR_MULTIPLIER_SWING = float(os.environ.get("ATR_MULTIPLIER_SWING", "2.0"))
NOTA_MINIMA_IA = int(os.environ.get("NOTA_MINIMA_IA", "78"))
COOLDOWN_SINAL_MINUTOS = int(os.environ.get("COOLDOWN_SINAL_MINUTOS", "180"))
MAX_SINAIS_POR_CICLO = int(os.environ.get("MAX_SINAIS_POR_CICLO", "3"))

TZ_BRT = dt.timezone(dt.timedelta(hours=-3))

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODELS_FALLBACK: Tuple[str, ...] = (
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-flash-latest",
)
_MODELOS_GEMINI_EXCLUSAO = (
    "embedding", "imagen", "veo", "tts", "audio", "live", "robotics",
    "computer-use", "deep-research", "aqa", "gemma", "learnlm", "vision",
    "image", "customtools", "thinking-exp", "omni", "transcribe", "translate",
    "nano-banana",
)
_CACHE_MODELOS_ATIVOS: List[str] = []
_MODELO_VENCEDOR_SESSAO: Optional[str] = None
_MODELOS_BLOQUEADOS_SESSAO: Set[str] = set()
_MODELOS_DEGRADADOS_SESSAO: Set[str] = set()

# Cache curto em memória (por ciclo) para evitar chamadas repetidas ao ativo líder (BOVA11/BTCUSDT) e MTF
_CACHE_SNAPSHOT_CICLO: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL_SEGUNDOS = 75.0

MAPA_INTERVALOS_TV: Dict[str, str] = {
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
    "1w": Interval.INTERVAL_1_WEEK,
}


@dataclass(frozen=True)
class AtivoMonitorado:
    simbolo: str
    exchange: str
    screener: str
    moeda: str
    casas_decimais: int = 2
    apenas_pregao_b3: bool = False


ATIVO_LIDER_B3 = AtivoMonitorado("BOVA11", "BMFBOVESPA", "brazil", "R$", 2, True)
ATIVO_LIDER_CRIPTO = AtivoMonitorado("BTCUSDT", "BINANCE", "crypto", "US$", 2, False)

# Watchlist padrão para varredura automática de Day Trade: Blue Chips B3 + Criptoativos Majors 24/7
WATCHLIST_PADRAO: Tuple[AtivoMonitorado, ...] = (
    AtivoMonitorado("PETR4", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("VALE3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("ITUB4", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("BBAS3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("BBDC4", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("PRIO3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("WEGE3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("B3SA3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("BOVA11", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("BTCUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("ETHUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("SOLUSDT", "BINANCE", "crypto", "US$", 2, False),
)

# Carteira setorial expandida para o Scanner de Recomendações de Swing Trade / Oportunidades
CARTEIRA_SWING_B3: Tuple[AtivoMonitorado, ...] = (
    AtivoMonitorado("PETR4", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("VALE3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("ITUB4", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("BBAS3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("BPAC11", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("PRIO3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("WEGE3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("EMBR3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("ELET3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("SBSP3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("RENT3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("GGBR4", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("B3SA3", "BMFBOVESPA", "brazil", "R$", 2, True),
    AtivoMonitorado("SUZB3", "BMFBOVESPA", "brazil", "R$", 2, True),
)

CARTEIRA_SWING_CRIPTO: Tuple[AtivoMonitorado, ...] = (
    AtivoMonitorado("BTCUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("ETHUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("SOLUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("BNBUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("XRPUSDT", "BINANCE", "crypto", "US$", 4, False),
    AtivoMonitorado("AVAXUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("LINKUSDT", "BINANCE", "crypto", "US$", 3, False),
    AtivoMonitorado("SUIUSDT", "BINANCE", "crypto", "US$", 4, False),
)

# Mapa de apelidos e nomes populares para consultas no chat ("o que acha do bitcoin?", "analisa petrobras")
ALIASES_ATIVOS_CHAT: Dict[str, AtivoMonitorado] = {
    "bitcoin": AtivoMonitorado("BTCUSDT", "BINANCE", "crypto", "US$", 2, False),
    "btc": AtivoMonitorado("BTCUSDT", "BINANCE", "crypto", "US$", 2, False),
    "ethereum": AtivoMonitorado("ETHUSDT", "BINANCE", "crypto", "US$", 2, False),
    "ether": AtivoMonitorado("ETHUSDT", "BINANCE", "crypto", "US$", 2, False),
    "eth": AtivoMonitorado("ETHUSDT", "BINANCE", "crypto", "US$", 2, False),
    "solana": AtivoMonitorado("SOLUSDT", "BINANCE", "crypto", "US$", 2, False),
    "sol": AtivoMonitorado("SOLUSDT", "BINANCE", "crypto", "US$", 2, False),
    "bnb": AtivoMonitorado("BNBUSDT", "BINANCE", "crypto", "US$", 2, False),
    "xrp": AtivoMonitorado("XRPUSDT", "BINANCE", "crypto", "US$", 4, False),
    "ripple": AtivoMonitorado("XRPUSDT", "BINANCE", "crypto", "US$", 4, False),
    "cardano": AtivoMonitorado("ADAUSDT", "BINANCE", "crypto", "US$", 4, False),
    "ada": AtivoMonitorado("ADAUSDT", "BINANCE", "crypto", "US$", 4, False),
    "doge": AtivoMonitorado("DOGEUSDT", "BINANCE", "crypto", "US$", 5, False),
    "dogecoin": AtivoMonitorado("DOGEUSDT", "BINANCE", "crypto", "US$", 5, False),
    "avax": AtivoMonitorado("AVAXUSDT", "BINANCE", "crypto", "US$", 2, False),
    "avalanche": AtivoMonitorado("AVAXUSDT", "BINANCE", "crypto", "US$", 2, False),
    "link": AtivoMonitorado("LINKUSDT", "BINANCE", "crypto", "US$", 3, False),
    "chainlink": AtivoMonitorado("LINKUSDT", "BINANCE", "crypto", "US$", 3, False),
    "sui": AtivoMonitorado("SUIUSDT", "BINANCE", "crypto", "US$", 4, False),
    "petrobras": AtivoMonitorado("PETR4", "BMFBOVESPA", "brazil", "R$", 2, True),
    "vale": AtivoMonitorado("VALE3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "itau": AtivoMonitorado("ITUB4", "BMFBOVESPA", "brazil", "R$", 2, True),
    "itaú": AtivoMonitorado("ITUB4", "BMFBOVESPA", "brazil", "R$", 2, True),
    "banco do brasil": AtivoMonitorado("BBAS3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "bradesco": AtivoMonitorado("BBDC4", "BMFBOVESPA", "brazil", "R$", 2, True),
    "prio": AtivoMonitorado("PRIO3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "petrorio": AtivoMonitorado("PRIO3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "weg": AtivoMonitorado("WEGE3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "embraer": AtivoMonitorado("EMBR3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "magalu": AtivoMonitorado("MGLU3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "magazine luiza": AtivoMonitorado("MGLU3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "ambev": AtivoMonitorado("ABEV3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "eletrobras": AtivoMonitorado("ELET3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "sabesp": AtivoMonitorado("SBSP3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "localiza": AtivoMonitorado("RENT3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "suzano": AtivoMonitorado("SUZB3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "klabin": AtivoMonitorado("KLBN11", "BMFBOVESPA", "brazil", "R$", 2, True),
    "gerdau": AtivoMonitorado("GGBR4", "BMFBOVESPA", "brazil", "R$", 2, True),
    "csn": AtivoMonitorado("CSNA3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "usiminas": AtivoMonitorado("USIM5", "BMFBOVESPA", "brazil", "R$", 2, True),
    "btg": AtivoMonitorado("BPAC11", "BMFBOVESPA", "brazil", "R$", 2, True),
    "santander": AtivoMonitorado("SANB11", "BMFBOVESPA", "brazil", "R$", 2, True),
    "b3": AtivoMonitorado("B3SA3", "BMFBOVESPA", "brazil", "R$", 2, True),
    "ibovespa": AtivoMonitorado("BOVA11", "BMFBOVESPA", "brazil", "R$", 2, True),
    "ibov": AtivoMonitorado("BOVA11", "BMFBOVESPA", "brazil", "R$", 2, True),
    "nvidia": AtivoMonitorado("NVDC34", "BMFBOVESPA", "brazil", "R$", 2, True),
    "apple": AtivoMonitorado("AAPL34", "BMFBOVESPA", "brazil", "R$", 2, True),
    "tesla": AtivoMonitorado("TSLA34", "BMFBOVESPA", "brazil", "R$", 2, True),
    "microsoft": AtivoMonitorado("MSFT34", "BMFBOVESPA", "brazil", "R$", 2, True),
    "dolar": AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),
    "dólar": AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),
    "usdbrl": AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),
    "ouro": AtivoMonitorado("XAUUSD", "OANDA", "cfd", "US$", 2, False),
    "xauusd": AtivoMonitorado("XAUUSD", "OANDA", "cfd", "US$", 2, False),
}

REGEX_TICKER_B3 = re.compile(r"\b([A-Z]{4}(?:3|4|5|6|11|31|32|33|34|35|39))\b")
REGEX_PAR_CRIPTO = re.compile(r"\b([A-Z0-9]{2,10}USDT)\b")
REGEX_COMANDO_CHAT = re.compile(r"^/(?:analisar|analise|grafico|ativo|sinal|avaliar)\b", flags=re.IGNORECASE)
REGEX_COMANDO_RANKING = re.compile(
    r"^/(?:swing|swingtrade|daytrade|oportunidades|recomendacao|recomendar|top|top3|calls|radar)\b",
    flags=re.IGNORECASE,
)
REGEX_INTENCAO_PERGUNTA = re.compile(
    r"(?:o\s*que\s+(?:vc\s+|voc[êe]\s+)?acha|oque\s+acha|analis[ae]|an[áa]lise|avalia|avaliar|"
    r"como\s+(?:est[áa]|t[áa]|ta)\b|gr[áa]fico|setup|vale\s+a\s+pena|compra\s+ou\s+venda|"
    r"entrar\s+em|opini[ãa]o\s+sobre|tend[êe]ncia\s+d[eo]|alvo\s+para|stop\s+para|swing\s*trade)",
    flags=re.IGNORECASE,
)
REGEX_PEDIDO_RECOMENDACAO = re.compile(
    r"(?:qual\s+(?:a\s+)?(?:melhor\s+)?(?:a[çc][ãa]o|ativo|cripto\w*|moeda|papel)|"
    r"quais\s+(?:as\s+)?(?:melhores\s+)?(?:a[çc][õo]es|ativos|cripto\w*|moedas|pap[ée]is)|"
    r"(?:quero|fazer|busco|procuro|tem|algum\w*|melhor(?:es)?|d[áa]\s+um|me\s+d[áa])\s+(?:um\s+|uma\s+)?(?:call|oportunidade\w*|recomenda[çc][ãa]o|indica[çc][ãa]o|setup|swing\s*trade|day\s*trade)|"
    r"(?:recomenda\w*|indica\w*|sugere\w*)\s+(?:um\s+|uma\s+|algum\w*\s+|boas?\s+|bons?\s+)?(?:a[çc][ãa]o|a[çc][õo]es|ativo\w*|cripto\w*|moeda\w*|papel|swing\s*trade|day\s*trade|call)|"
    r"o\s+que\s+(?:comprar|operar|vender)\s+(?:hoje|agora|nessa\s+semana|no\s+swing|no\s+day))",
    flags=re.IGNORECASE,
)


@dataclass
class SnapshotIndicadores:
    simbolo: str
    exchange: str
    screener: str
    intervalo: str
    moeda: str
    casas_decimais: int
    preco_atual: float
    abertura: float
    maxima: float
    minima: float
    ema_9: float
    ema_21: float
    sma_200: float
    rsi: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    volume: float
    atr: float
    adx: float
    recomendacao_tv: str
    direcao_preliminar: str
    # --- 6 CAMADAS INSTITUCIONAIS + SUPER-SKILLS DE MESA PROPRIETÁRIA ---
    vwma: float = 0.0
    di_plus: float = 20.0
    di_minus: float = 20.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    stoch_k: float = 50.0
    stoch_d: float = 50.0
    pivot_p: float = 0.0
    pivot_r1: float = 0.0
    pivot_s1: float = 0.0
    variacao_pct: float = 0.0
    alpha_vs_lider_pct: float = 0.0
    tf_superior: str = "1h"
    tendencia_mtf_superior: str = "NEUTRA"
    tf_inferior: str = "5m"
    confirmacao_mtf_inferior: str = "FAVORAVEL"
    ativo_lider: str = ""
    vies_ativo_lider: str = "NEUTRO"
    score_quant_local: int = 50
    tipo_setup: str = "CONFLUÊNCIA PADRÃO"
    preco_pullback_ideal: float = 0.0


@dataclass
class PlanoRisco:
    direcao: str
    entrada: float
    stop_loss: float
    risco_unitario: float
    risco_percentual: float
    alvo_1: float  # 1:1.5 (Parcial 50% + Gatilho Break-Even 0x0)
    alvo_2: float  # 1:2.0 (Parcial 30%)
    alvo_3: float  # 1:3.0 (Runner 20% com Trailing EMA 9)
    gatilho_breakeven: float = 0.0
    modalidade: str = "DAY TRADE"
    horizonte_estimado: str = "Intradiário (encerrar no mesmo pregão)"
    capital_referencia: float = 10000.0
    risco_max_1pct_capital: float = 100.0
    quantidade_sugerida_1pct: float = 0.0
    exposicao_total_sugerida: float = 0.0
    regra_execucao: str = (
        "Realizar 50% no Alvo 1 e mover Stop Loss para a Entrada (0x0 Break-Even); "
        "30% no Alvo 2; 20% final no Alvo 3."
    )


@dataclass
class AuditoriaIA:
    aprovado: bool
    nota_confluencia: int
    regime_mercado: str
    racional_tecnico: str
    alerta_risco: str
    motivo_veto: str = ""
    modelo_utilizado: str = ""


# ==============================================================================
# 3. EXTRAÇÃO DE DADOS DO TRADINGVIEW + MTF + FORÇA RELATIVA (ALPHA)
# ==============================================================================
def pregao_b3_aberto(agora_utc: Optional[dt.datetime] = None) -> bool:
    agora_brt = (agora_utc or dt.datetime.now(dt.timezone.utc)).astimezone(TZ_BRT)
    if agora_brt.weekday() >= 5:
        return False
    minutos_dia = agora_brt.hour * 60 + agora_brt.minute
    return (10 * 60 + 5) <= minutos_dia <= (17 * 60 + 50)


def _num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        f = float(val)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def obter_pares_mtf(intervalo_operacional: str) -> Tuple[str, str]:
    """Retorna (timeframe_ancora_superior, timeframe_gatilho_inferior) para confirmação fractal."""
    tf = (intervalo_operacional or "15m").lower().strip()
    mapa = {
        "5m": ("15m", "5m"),
        "15m": ("1h", "5m"),
        "30m": ("1h", "15m"),
        "1h": ("4h", "15m"),
        "4h": ("1d", "1h"),
        "1d": ("1w", "4h"),
        "1w": ("1w", "1d"),
    }
    return mapa.get(tf, ("1h", "5m"))


def classificar_tipo_setup(snap: SnapshotIndicadores, direcao: str) -> Tuple[str, float]:
    """Classifica a 'jogada institucional' do gráfico (Pullback de Desconto vs Ignição vs Esticado)."""
    preco_pullback = round(snap.ema_9, snap.casas_decimais)
    if snap.atr <= 0 or snap.preco_atual <= 0:
        return "INDEFINIDO", preco_pullback

    dist_ema9_atr = abs(snap.preco_atual - snap.ema_9) / snap.atr
    spread_emas_pct = abs(snap.ema_9 - snap.ema_21) / snap.preco_atual

    if spread_emas_pct < 0.0010 or (0 < snap.adx < 18.0):
        return "CONSOLIDAÇÃO LATERAL (AGUARDAR ROMPIMENTO)", preco_pullback

    if direcao == "COMPRA":
        if snap.preco_atual >= snap.ema_9 > snap.ema_21 and dist_ema9_atr <= 0.85 and 50.0 <= snap.rsi <= 64.0:
            return "PULLBACK INSTITUCIONAL NA EMA 9 (DESCONTO IDEAL)", preco_pullback
        if dist_ema9_atr > 1.75 or snap.rsi > 70.0:
            return f"PREÇO ESTICADO (AGUARDAR RECUO ATÉ EMA 9 EM {snap.moeda} {preco_pullback:.2f})", preco_pullback
        return "IGNIÇÃO DE TENDÊNCIA & FLUXO COMPRADOR", preco_pullback

    if direcao == "VENDA":
        if snap.preco_atual <= snap.ema_9 < snap.ema_21 and dist_ema9_atr <= 0.85 and 36.0 <= snap.rsi <= 50.0:
            return "PULLBACK VENDEDOR NA EMA 9 (PONTO DE VENDA IDEAL)", preco_pullback
        if dist_ema9_atr > 1.75 or snap.rsi < 30.0:
            return f"SOBREVENDA ESTICADA (AGUARDAR REPIQUE ATÉ EMA 9 EM {snap.moeda} {preco_pullback:.2f})", preco_pullback
        return "IGNIÇÃO DE QUEDA & FLUXO VENDEDOR", preco_pullback

    return "TRANSICÃO DE TENDÊNCIA", preco_pullback


def _extrair_snapshot_base(ativo: AtivoMonitorado, intervalo_str: str) -> Optional[SnapshotIndicadores]:
    chave_cache = f"{ativo.exchange}:{ativo.simbolo}:{intervalo_str}"
    agora_mono = time.monotonic()
    if chave_cache in _CACHE_SNAPSHOT_CICLO:
        ts_salvo, snap_salvo = _CACHE_SNAPSHOT_CICLO[chave_cache]
        if (agora_mono - ts_salvo) <= _CACHE_TTL_SEGUNDOS and snap_salvo is not None:
            return snap_salvo

    tv_interval = MAPA_INTERVALOS_TV.get(intervalo_str, Interval.INTERVAL_15_MINUTES)
    try:
        handler = TA_Handler(
            symbol=ativo.simbolo,
            screener=ativo.screener,
            exchange=ativo.exchange,
            interval=tv_interval,
            timeout=12,
        )
        try:
            handler.add_indicators(["EMA9", "EMA21", "ATR"])
        except Exception:
            pass

        analise = handler.get_analysis()
        if not analise or not getattr(analise, "indicators", None):
            return None

        ind: Dict[str, Any] = analise.indicators
        summary: Dict[str, Any] = getattr(analise, "summary", {}) or {}

        close = _num(ind.get("close"))
        if close <= 0:
            return None

        high = _num(ind.get("high"), close)
        low = _num(ind.get("low"), close)
        open_p = _num(ind.get("open"), close)
        change_pct = _num(ind.get("change"), ((close - open_p) / open_p * 100.0) if open_p > 0 else 0.0)

        ema_9 = _num(ind.get("EMA9"), _num(ind.get("EMA10"), close))
        ema_21 = _num(ind.get("EMA21"), _num(ind.get("EMA20"), close))
        sma_200 = _num(ind.get("SMA200"), _num(ind.get("EMA200"), close))
        vwma = _num(ind.get("VWMA"), ema_21)

        rsi = _num(ind.get("RSI"), 50.0)
        macd_line = _num(ind.get("MACD.macd"))
        macd_signal = _num(ind.get("MACD.signal"))
        macd_hist = macd_line - macd_signal
        volume = _num(ind.get("volume"))
        adx = _num(ind.get("ADX"), 20.0)
        di_plus = _num(ind.get("ADX+DI"), 22.0 if close >= ema_21 else 18.0)
        di_minus = _num(ind.get("ADX-DI"), 18.0 if close >= ema_21 else 22.0)

        bb_up = _num(ind.get("BB.upper"))
        bb_low = _num(ind.get("BB.lower"))
        stoch_k = _num(ind.get("Stoch.K"), 50.0)
        stoch_d = _num(ind.get("Stoch.D"), 50.0)

        pivot_p = _num(ind.get("Pivot.M.Classic.Middle"), close)
        pivot_r1 = _num(ind.get("Pivot.M.Classic.R1"), 0.0)
        pivot_s1 = _num(ind.get("Pivot.M.Classic.S1"), 0.0)

        atr_raw = _num(ind.get("ATR"))
        if atr_raw <= 0:
            amplitude_vela = abs(high - low)
            amplitude_bb = abs(bb_up - bb_low) / 4.0 if (bb_up > bb_low > 0) else 0.0
            atr_raw = max(amplitude_vela, amplitude_bb, close * 0.005)

        rec_tv = str(summary.get("RECOMMENDATION") or "NEUTRAL").upper()
        casas = 4 if (close < 1.0 and ativo.casas_decimais == 2) else ativo.casas_decimais
        tf_sup, tf_inf = obter_pares_mtf(intervalo_str)

        snap = SnapshotIndicadores(
            simbolo=ativo.simbolo,
            exchange=ativo.exchange,
            screener=ativo.screener,
            intervalo=intervalo_str,
            moeda=ativo.moeda,
            casas_decimais=casas,
            preco_atual=close,
            abertura=open_p,
            maxima=high,
            minima=low,
            ema_9=ema_9,
            ema_21=ema_21,
            sma_200=sma_200,
            rsi=rsi,
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            volume=volume,
            atr=atr_raw,
            adx=adx,
            recomendacao_tv=rec_tv,
            direcao_preliminar="NEUTRO",
            vwma=vwma,
            di_plus=di_plus,
            di_minus=di_minus,
            bb_upper=bb_up,
            bb_lower=bb_low,
            stoch_k=stoch_k,
            stoch_d=stoch_d,
            pivot_p=pivot_p,
            pivot_r1=pivot_r1,
            pivot_s1=pivot_s1,
            variacao_pct=change_pct,
            tf_superior=tf_sup,
            tf_inferior=tf_inf,
            preco_pullback_ideal=round(ema_9, casas),
        )
        _CACHE_SNAPSHOT_CICLO[chave_cache] = (agora_mono, snap)
        return snap
    except Exception as exc:
        logger.warning("Falha ao extrair TradingView para %s:%s (%s)", ativo.exchange, ativo.simbolo, str(exc)[:120])
        return None


def _classificar_tendencia_macro(snap_macro: Optional[SnapshotIndicadores]) -> str:
    if not snap_macro:
        return "NEUTRA"
    alta = snap_macro.preco_atual >= snap_macro.ema_21 and snap_macro.macd_line >= snap_macro.macd_signal
    baixa = snap_macro.preco_atual <= snap_macro.ema_21 and snap_macro.macd_line <= snap_macro.macd_signal
    if alta and snap_macro.rsi >= 48.0:
        return "ALTA"
    if baixa and snap_macro.rsi <= 52.0:
        return "BAIXA"
    return "LATERAL"


def _classificar_gatilho_curto(snap_curto: Optional[SnapshotIndicadores]) -> str:
    if not snap_curto:
        return "FAVORAVEL"
    if snap_curto.rsi > 78.0 or (snap_curto.stoch_k > 92.0 and snap_curto.stoch_k < snap_curto.stoch_d):
        return "EXAUSTAO_ALTA"
    if snap_curto.rsi < 22.0 or (snap_curto.stoch_k < 8.0 and snap_curto.stoch_k > snap_curto.stoch_d):
        return "EXAUSTAO_BAIXA"
    if snap_curto.preco_atual < snap_curto.ema_21 and snap_curto.macd_hist < 0 and snap_curto.rsi < 43.0:
        return "CONTRA_COMPRA"
    if snap_curto.preco_atual > snap_curto.ema_21 and snap_curto.macd_hist > 0 and snap_curto.rsi > 57.0:
        return "CONTRA_VENDA"
    return "FAVORAVEL"


def _avaliar_ativo_lider(ativo: AtivoMonitorado, intervalo_str: str) -> Tuple[str, str, float]:
    if ativo.screener == "brazil":
        if ativo.simbolo == ATIVO_LIDER_B3.simbolo:
            return "BOVA11", "PROPRIO", 0.0
        snap_lider = _extrair_snapshot_base(ATIVO_LIDER_B3, intervalo_str)
        nome_lider = "BOVA11"
    elif ativo.screener == "crypto":
        if ativo.simbolo == ATIVO_LIDER_CRIPTO.simbolo:
            return "BTCUSDT", "PROPRIO", 0.0
        snap_lider = _extrair_snapshot_base(ATIVO_LIDER_CRIPTO, intervalo_str)
        nome_lider = "BTCUSDT"
    else:
        return ativo.simbolo, "PROPRIO", 0.0

    if not snap_lider:
        return nome_lider, "NEUTRO", 0.0

    var_lider = snap_lider.variacao_pct
    if (
        snap_lider.preco_atual > snap_lider.ema_21
        and snap_lider.ema_9 >= snap_lider.ema_21
        and snap_lider.macd_line > snap_lider.macd_signal
        and snap_lider.rsi >= 54.0
    ):
        return nome_lider, "ALTA_FORTE", var_lider
    if (
        snap_lider.preco_atual < snap_lider.ema_21
        and snap_lider.ema_9 <= snap_lider.ema_21
        and snap_lider.macd_line < snap_lider.macd_signal
        and snap_lider.rsi <= 46.0
    ):
        return nome_lider, "BAIXA_FORTE", var_lider
    return nome_lider, "NEUTRO", var_lider


def calcular_score_quant_local(snap: SnapshotIndicadores, direcao_alvo: str = "NEUTRO") -> int:
    """Pontua objetivamente (0 a 100) a confluência das 6 camadas institucionais."""
    dir_eval = direcao_alvo if direcao_alvo in ("COMPRA", "VENDA") else (
        "COMPRA" if snap.preco_atual >= snap.ema_21 else "VENDA"
    )
    pontos = 0
    risco_r = max(snap.atr * ATR_MULTIPLIER, snap.preco_atual * 0.0025, 1e-9)

    if dir_eval == "COMPRA":
        # 1. Alinhamento de Tendência (EMA 9 > EMA 21 + SMA 200) -> até 25 pts
        if snap.preco_atual > snap.ema_9 > snap.ema_21:
            pontos += 15
        if snap.preco_atual > snap.sma_200:
            pontos += 10
        # 2. Fluxo Institucional Ponderado por Volume (VWMA) -> até 15 pts
        if snap.vwma <= 0 or snap.preco_atual >= snap.vwma:
            pontos += 15
        # 3. Força e Dominância Direcional (ADX + DI+ > DI-) -> até 15 pts
        if snap.di_plus >= snap.di_minus:
            pontos += 8
        if snap.adx >= 23.0:
            pontos += 7
        elif snap.adx >= 18.0:
            pontos += 4
        # 4. Momentum Limpo (MACD + RSI em zona nobre sem sobrecompra extrema) -> até 15 pts
        if snap.macd_line > snap.macd_signal:
            pontos += 8
        if 52.0 <= snap.rsi <= 70.0:
            pontos += 7
        elif 48.0 <= snap.rsi <= 73.0:
            pontos += 3
        # 5. Alinhamento Multi-Timeframe (MTF Superior + Inferior) -> até 15 pts
        if snap.tendencia_mtf_superior in ("ALTA", "NEUTRA"):
            pontos += 10 if snap.tendencia_mtf_superior == "ALTA" else 6
        if snap.confirmacao_mtf_inferior not in ("EXAUSTAO_ALTA", "CONTRA_COMPRA"):
            pontos += 5
        # 6. Vento a Favor (Ativo Líder + Caminho Livre até Pivot R1) -> até 15 pts
        if snap.vies_ativo_lider != "BAIXA_FORTE":
            pontos += 8
        if snap.pivot_r1 <= snap.preco_atual or (snap.pivot_r1 - snap.preco_atual) >= (0.60 * risco_r):
            pontos += 7
    else:
        # 1. Alinhamento de Tendência (EMA 9 < EMA 21 + SMA 200) -> até 25 pts
        if snap.preco_atual < snap.ema_9 < snap.ema_21:
            pontos += 15
        if snap.preco_atual < snap.sma_200:
            pontos += 10
        # 2. Fluxo Institucional Ponderado por Volume (VWMA) -> até 15 pts
        if snap.vwma <= 0 or snap.preco_atual <= snap.vwma:
            pontos += 15
        # 3. Força e Dominância Direcional (ADX + DI- > DI+) -> até 15 pts
        if snap.di_minus >= snap.di_plus:
            pontos += 8
        if snap.adx >= 23.0:
            pontos += 7
        elif snap.adx >= 18.0:
            pontos += 4
        # 4. Momentum Limpo (MACD + RSI) -> até 15 pts
        if snap.macd_line < snap.macd_signal:
            pontos += 8
        if 30.0 <= snap.rsi <= 48.0:
            pontos += 7
        elif 27.0 <= snap.rsi <= 52.0:
            pontos += 3
        # 5. Alinhamento Multi-Timeframe (MTF Superior + Inferior) -> até 15 pts
        if snap.tendencia_mtf_superior in ("BAIXA", "NEUTRA"):
            pontos += 10 if snap.tendencia_mtf_superior == "BAIXA" else 6
        if snap.confirmacao_mtf_inferior not in ("EXAUSTAO_BAIXA", "CONTRA_VENDA"):
            pontos += 5
        # 6. Vento a Favor (Ativo Líder + Caminho Livre até Pivot S1) -> até 15 pts
        if snap.vies_ativo_lider != "ALTA_FORTE":
            pontos += 8
        if snap.pivot_s1 <= 0 or snap.pivot_s1 >= snap.preco_atual or (snap.preco_atual - snap.pivot_s1) >= (0.60 * risco_r):
            pontos += 7

    return max(0, min(100, pontos))


def extrair_dados_tradingview(
    ativo: AtivoMonitorado,
    intervalo_str: str = TIMEFRAME_PADRAO,
    enriquecer_mtf: bool = True,
) -> Optional[SnapshotIndicadores]:
    snap = _extrair_snapshot_base(ativo, intervalo_str)
    if not snap:
        return None

    if enriquecer_mtf:
        tf_sup, tf_inf = obter_pares_mtf(intervalo_str)
        snap.tf_superior = tf_sup
        snap.tf_inferior = tf_inf

        snap_sup = _extrair_snapshot_base(ativo, tf_sup) if tf_sup != intervalo_str else snap
        snap.tendencia_mtf_superior = _classificar_tendencia_macro(snap_sup)

        snap_inf = _extrair_snapshot_base(ativo, tf_inf) if tf_inf != intervalo_str else snap
        snap.confirmacao_mtf_inferior = _classificar_gatilho_curto(snap_inf)

        nome_lider, vies_lider, var_lider = _avaliar_ativo_lider(ativo, intervalo_str)
        snap.ativo_lider = nome_lider
        snap.vies_ativo_lider = vies_lider
        snap.alpha_vs_lider_pct = round(snap.variacao_pct - var_lider, 2) if vies_lider != "PROPRIO" else 0.0

    dir_estimada = "COMPRA" if snap.preco_atual >= snap.ema_21 else "VENDA"
    snap.score_quant_local = calcular_score_quant_local(snap, dir_estimada)
    snap.tipo_setup, snap.preco_pullback_ideal = classificar_tipo_setup(snap, dir_estimada)
    return snap


def pre_filtrar_confluencia_quant(snap: SnapshotIndicadores) -> Tuple[bool, str]:
    """Aplica as 6 Camadas Quantitativas Institucionais antes de acionar a IA."""
    if snap.volume <= 0 and snap.screener not in ("forex", "cfd"):
        return False, "Sem volume negociado no período."
    if snap.atr <= 0 or snap.preco_atual <= 0:
        return False, "ATR ou preço inválido."

    # 1. Filtro Anti-Consolidação (Spread de EMAs + ADX mínimo)
    spread_emas_pct = abs(snap.ema_9 - snap.ema_21) / snap.preco_atual
    if spread_emas_pct < 0.0010:
        return False, f"Consolidação: EMA9 e EMA21 emboladas (spread {spread_emas_pct * 100:.3f}%)."

    if 0 < snap.adx < 18.0:
        return False, f"Consolidação: tendência sem força direcional (ADX {snap.adx:.1f} < 18)."

    # 2. Filtro Anti-Violinada (Distância máxima da EMA 9 pelo ATR)
    dist_ema9_atr = abs(snap.preco_atual - snap.ema_9) / snap.atr
    if dist_ema9_atr > 2.2:
        return False, f"Preço esticado demais em relação à EMA9 ({dist_ema9_atr:.1f}x ATR — risco de violinada)."

    mult_atr = ATR_MULTIPLIER_SWING if snap.intervalo in ("4h", "1d", "1w") else ATR_MULTIPLIER
    risco_r = max(snap.atr * mult_atr, snap.preco_atual * 0.0025)

    # 3. Verificação dos Pilares Primários (EMA 9/21 + SMA 200 + MACD + RSI)
    tendencia_alta = snap.preco_atual > snap.ema_9 and snap.ema_9 > snap.ema_21 and snap.preco_atual > snap.sma_200
    momentum_alta = snap.macd_line > snap.macd_signal and 52.0 <= snap.rsi <= 72.0

    tendencia_baixa = snap.preco_atual < snap.ema_9 and snap.ema_9 < snap.ema_21 and snap.preco_atual < snap.sma_200
    momentum_baixa = snap.macd_line < snap.macd_signal and 28.0 <= snap.rsi <= 48.0

    if tendencia_alta and momentum_alta:
        # Camada 3: Fluxo Institucional (VWMA)
        if snap.vwma > 0 and snap.preco_atual < snap.vwma:
            return False, f"Fluxo institucional negativo: preço ({snap.preco_atual:.2f}) abaixo da VWMA ({snap.vwma:.2f})."
        # Camada 5: Dominância Direcional (DI+ vs DI-)
        if snap.di_plus < snap.di_minus:
            return False, f"Sem dominância compradora: DI+ ({snap.di_plus:.1f}) menor que DI- ({snap.di_minus:.1f})."
        # Camada 4: Envelope de Bollinger e Barreira de Pivot R1
        if snap.bb_upper > 0 and snap.preco_atual > (snap.bb_upper * 1.002) and snap.rsi > 68.0:
            return False, "Exaustão de topo: preço rompendo fora da Banda Superior de Bollinger com RSI alto."
        if snap.pivot_r1 > snap.preco_atual and (snap.pivot_r1 - snap.preco_atual) < (0.55 * risco_r):
            return False, f"Resistência Pivot R1 ({snap.pivot_r1:.2f}) bloqueando o caminho até o Alvo 1."
        # Camada 1: Multi-Timeframe (Âncora Superior e Gatilho Inferior)
        if snap.tendencia_mtf_superior == "BAIXA":
            return False, f"Conflito Multi-Timeframe: gráfico superior ({snap.tf_superior.upper()}) está em tendência de BAIXA."
        if snap.confirmacao_mtf_inferior in ("EXAUSTAO_ALTA", "CONTRA_COMPRA"):
            return False, f"Gatilho curto ({snap.tf_inferior.upper()}) em exaustão ou perda de força ({snap.confirmacao_mtf_inferior})."
        # Camada 2: Correlação com Ativo Líder (BOVA11 / BTCUSDT)
        if snap.vies_ativo_lider == "BAIXA_FORTE":
            return False, f"Vento contra do mercado: ativo líder ({snap.ativo_lider}) em queda forte."

        snap.direcao_preliminar = "COMPRA"
        snap.score_quant_local = calcular_score_quant_local(snap, "COMPRA")
        snap.tipo_setup, snap.preco_pullback_ideal = classificar_tipo_setup(snap, "COMPRA")
        return True, f"Confluência institucional de COMPRA confirmada (Score Quant {snap.score_quant_local}/100)."

    if tendencia_baixa and momentum_baixa:
        # Camada 3: Fluxo Institucional (VWMA)
        if snap.vwma > 0 and snap.preco_atual > snap.vwma:
            return False, f"Fluxo institucional comprador: preço ({snap.preco_atual:.2f}) acima da VWMA ({snap.vwma:.2f})."
        # Camada 5: Dominância Direcional (DI- vs DI+)
        if snap.di_minus < snap.di_plus:
            return False, f"Sem dominância vendedora: DI- ({snap.di_minus:.1f}) menor que DI+ ({snap.di_plus:.1f})."
        # Camada 4: Envelope de Bollinger e Suporte Pivot S1
        if snap.bb_lower > 0 and snap.preco_atual < (snap.bb_lower * 0.998) and snap.rsi < 32.0:
            return False, "Exaustão de fundo: preço esticado abaixo da Banda Inferior de Bollinger com RSI baixo."
        if 0 < snap.pivot_s1 < snap.preco_atual and (snap.preco_atual - snap.pivot_s1) < (0.55 * risco_r):
            return False, f"Suporte Pivot S1 ({snap.pivot_s1:.2f}) bloqueando o caminho até o Alvo 1."
        # Camada 1: Multi-Timeframe (Âncora Superior e Gatilho Inferior)
        if snap.tendencia_mtf_superior == "ALTA":
            return False, f"Conflito Multi-Timeframe: gráfico superior ({snap.tf_superior.upper()}) está em tendência de ALTA."
        if snap.confirmacao_mtf_inferior in ("EXAUSTAO_BAIXA", "CONTRA_VENDA"):
            return False, f"Gatilho curto ({snap.tf_inferior.upper()}) em exaustão vendedora ({snap.confirmacao_mtf_inferior})."
        # Camada 2: Correlação com Ativo Líder (BOVA11 / BTCUSDT)
        if snap.vies_ativo_lider == "ALTA_FORTE":
            return False, f"Vento contra do mercado: ativo líder ({snap.ativo_lider}) em alta forte."

        snap.direcao_preliminar = "VENDA"
        snap.score_quant_local = calcular_score_quant_local(snap, "VENDA")
        snap.tipo_setup, snap.preco_pullback_ideal = classificar_tipo_setup(snap, "VENDA")
        return True, f"Confluência institucional de VENDA confirmada (Score Quant {snap.score_quant_local}/100)."

    return False, "Sem alinhamento simultâneo de tendência (EMA9/21 + SMA200) e momentum (MACD + RSI)."


def diagnosticar_direcao_sob_demanda(snap: SnapshotIndicadores) -> Tuple[str, str]:
    """Para consultas no chat, classifica o viés técnico atual mesmo que o ativo esteja lateral."""
    ok_confluencia, _ = pre_filtrar_confluencia_quant(snap)
    if ok_confluencia and snap.direcao_preliminar in ("COMPRA", "VENDA"):
        return snap.direcao_preliminar, "CONFLUÊNCIA INSTITUCIONAL ATIVA"

    pontos_alta = 0
    pontos_baixa = 0
    if snap.preco_atual > snap.ema_9:
        pontos_alta += 1
    else:
        pontos_baixa += 1
    if snap.ema_9 > snap.ema_21:
        pontos_alta += 1
    else:
        pontos_baixa += 1
    if snap.preco_atual > snap.sma_200:
        pontos_alta += 1
    else:
        pontos_baixa += 1
    if snap.vwma > 0:
        if snap.preco_atual >= snap.vwma:
            pontos_alta += 1
        else:
            pontos_baixa += 1
    if snap.macd_line > snap.macd_signal:
        pontos_alta += 1
    else:
        pontos_baixa += 1
    if snap.di_plus >= snap.di_minus:
        pontos_alta += 1
    else:
        pontos_baixa += 1
    if snap.rsi >= 53.0:
        pontos_alta += 1
    elif snap.rsi <= 47.0:
        pontos_baixa += 1

    vies = "COMPRA" if pontos_alta >= pontos_baixa else "VENDA"
    snap.score_quant_local = calcular_score_quant_local(snap, vies)
    snap.tipo_setup, snap.preco_pullback_ideal = classificar_tipo_setup(snap, vies)

    spread_emas_pct = abs(snap.ema_9 - snap.ema_21) / max(snap.preco_atual, 1e-9)
    if spread_emas_pct < 0.0010 or (0 < snap.adx < 18.0):
        return vies, "CONSOLIDAÇÃO / AGUARDAR ROMPIMENTO"

    if vies == "COMPRA" and snap.tendencia_mtf_superior == "BAIXA":
        return "COMPRA", f"CONTRA TENDÊNCIA MACRO ({snap.tf_superior.upper()} EM BAIXA)"
    if vies == "VENDA" and snap.tendencia_mtf_superior == "ALTA":
        return "VENDA", f"CONTRA TENDÊNCIA MACRO ({snap.tf_superior.upper()} EM ALTA)"

    if pontos_alta > pontos_baixa:
        return "COMPRA", "VIÉS COMPRADOR (PARCIAL)"
    return "VENDA", "VIÉS VENDEDOR (PARCIAL)"


# ==============================================================================
# 4. GESTÃO MATEMÁTICA DE RISCO + BREAK-EVEN + DIMENSIONAMENTO DE LOTE (1% RISCO)
# ==============================================================================
def calcular_gestao_risco(
    preco_entrada: float,
    atr: float,
    direcao: str,
    multiplicador_atr: float = ATR_MULTIPLIER,
    casas_decimais: int = 2,
    modalidade: str = "DAY TRADE",
    moeda: str = "R$",
) -> PlanoRisco:
    if preco_entrada <= 0 or atr <= 0:
        raise ValueError("Preço de entrada e ATR devem ser maiores que zero.")

    dir_norm = direcao.strip().upper()
    if dir_norm not in ("COMPRA", "VENDA"):
        raise ValueError(f"Direção inválida para cálculo de risco: {direcao}")

    mod_norm = modalidade.strip().upper()
    eh_swing = "SWING" in mod_norm
    piso_pct = 0.0080 if eh_swing else 0.0025
    risco_r = max(atr * multiplicador_atr, preco_entrada * piso_pct)
    risco_pct = (risco_r / preco_entrada) * 100.0

    if dir_norm == "COMPRA":
        stop_loss = preco_entrada - risco_r
        alvo_1 = preco_entrada + (1.5 * risco_r)
        alvo_2 = preco_entrada + (2.0 * risco_r)
        alvo_3 = preco_entrada + (3.0 * risco_r)
    else:
        stop_loss = preco_entrada + risco_r
        alvo_1 = preco_entrada - (1.5 * risco_r)
        alvo_2 = preco_entrada - (2.0 * risco_r)
        alvo_3 = preco_entrada - (3.0 * risco_r)

    # Dimensionamento Institucional de Posição (Regra de Ouro: arriscar no máximo 1% do capital por operação)
    capital_ref = 1000.0 if moeda == "US$" else 10000.0
    risco_max_1pct = capital_ref * 0.01  # R$ 100 para cada R$ 10.000 (ou US$ 10 para cada US$ 1.000)
    qtd_bruta = risco_max_1pct / max(risco_r, 1e-9)
    if moeda == "R$" and preco_entrada >= 2.0:
        qtd_sugerida = float(max(1, int(round(qtd_bruta))))
    else:
        qtd_sugerida = round(qtd_bruta, 3 if preco_entrada >= 10 else 2)
    exposicao_sugerida = round(qtd_sugerida * preco_entrada, 2)

    horizonte = (
        "3 a 15 dias úteis (Swing Trade de Tendência)"
        if eh_swing
        else "Intradiário (encerrar no mesmo pregão)"
    )

    alvo_1_round = round(alvo_1, casas_decimais)
    return PlanoRisco(
        direcao=dir_norm,
        entrada=round(preco_entrada, casas_decimais),
        stop_loss=round(stop_loss, casas_decimais),
        risco_unitario=round(risco_r, casas_decimais),
        risco_percentual=round(risco_pct, 2),
        alvo_1=alvo_1_round,
        alvo_2=round(alvo_2, casas_decimais),
        alvo_3=round(alvo_3, casas_decimais),
        gatilho_breakeven=alvo_1_round,
        modalidade="SWING TRADE" if eh_swing else "DAY TRADE",
        horizonte_estimado=horizonte,
        capital_referencia=capital_ref,
        risco_max_1pct_capital=round(risco_max_1pct, 2),
        quantidade_sugerida_1pct=qtd_sugerida,
        exposicao_total_sugerida=exposicao_sugerida,
    )


# ==============================================================================
# 5. AUDITORIA QUANTITATIVA VIA GEMINI API (FREE TIER + CIRCUIT BREAKER)
# ==============================================================================
def _pontuar_modelo_gemini(nome: str) -> int:
    n = (nome or "").lower().strip()
    if not n.startswith("gemini-") or "flash" not in n or any(x in n for x in _MODELOS_GEMINI_EXCLUSAO):
        return -1
    m_ver = re.search(r"gemini-(\d+)(?:\.(\d+))?", n)
    major = int(m_ver.group(1)) if m_ver else 0
    minor = int(m_ver.group(2) or 0) if m_ver else 0
    if m_ver and (major, minor) < (2, 5):
        return -1
    base_familia = 2000 if "flash-lite" in n else 1000
    penalidade = (-15 if ("preview" in n or "exp" in n) else 0) + (-20 if "latest" in n else 0)
    return base_familia + (major * 100 + minor * 10) + penalidade


def descobrir_modelos_gemini_gratuitos(api_key: str) -> List[str]:
    global _MODELO_VENCEDOR_SESSAO
    if not _CACHE_MODELOS_ATIVOS:
        descobertos: List[str] = []
        try:
            resp = requests.get(
                f"{GEMINI_API_BASE}?pageSize=100",
                headers={"Accept": "application/json", "x-goog-api-key": api_key},
                timeout=12,
            )
            if resp.status_code == 200:
                modelos = resp.json().get("models", [])
                ranqueados: List[Tuple[int, str]] = []
                for m in modelos:
                    if not isinstance(m, dict):
                        continue
                    if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                        continue
                    nome = str(m.get("name") or "").replace("models/", "").strip()
                    sc = _pontuar_modelo_gemini(nome)
                    if sc > 0:
                        ranqueados.append((sc, nome))
                ranqueados.sort(key=lambda x: x[0], reverse=True)
                descobertos = [nome for _, nome in ranqueados[:12]]
        except Exception as exc:
            logger.warning("Aviso ao listar modelos Gemini (usando lista padrão): %s", str(exc)[:100])

        combinados = list(dict.fromkeys([*(descobertos or []), *GEMINI_MODELS_FALLBACK]))
        _CACHE_MODELOS_ATIVOS.extend(combinados)

    ordenados: List[str] = []
    if (
        _MODELO_VENCEDOR_SESSAO
        and _MODELO_VENCEDOR_SESSAO in _CACHE_MODELOS_ATIVOS
        and _MODELO_VENCEDOR_SESSAO not in _MODELOS_BLOQUEADOS_SESSAO
        and _MODELO_VENCEDOR_SESSAO not in _MODELOS_DEGRADADOS_SESSAO
    ):
        ordenados.append(_MODELO_VENCEDOR_SESSAO)
    for m in _CACHE_MODELOS_ATIVOS:
        if m not in _MODELOS_BLOQUEADOS_SESSAO and m not in _MODELOS_DEGRADADOS_SESSAO and m not in ordenados:
            ordenados.append(m)
    for m in _CACHE_MODELOS_ATIVOS:
        if m not in _MODELOS_BLOQUEADOS_SESSAO and m in _MODELOS_DEGRADADOS_SESSAO and m not in ordenados:
            ordenados.append(m)
    return ordenados


def _extrair_json_resposta(texto: str) -> Optional[Dict[str, Any]]:
    if not texto:
        return None
    s = texto.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        s = m.group(1)
    else:
        i, f = s.find("{"), s.rfind("}")
        if i != -1 and f > i:
            s = s[i : f + 1]
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def construir_prompt_auditoria(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    pergunta_usuario: str = "",
) -> Tuple[str, str]:
    system_instruction = (
        "Você é um Quant Trader Sênior e Gestor de Risco Institucional (Day Trade e Swing Trade) com foco em setups de alta taxa de acerto (75%+). "
        "Sua função é auditar o JSON multi-timeframe extraído do TradingView e VETAR qualquer operação com ruído, "
        "consolidação lateral, fluxo institucional contrário (VWMA), falta de dominância direcional (DI+ vs DI-), "
        "resistência/suporte Pivot bloqueando o Alvo 1, ou divergência com o gráfico superior (MTF) e com o ativo líder (BOVA11/BTC). "
        "Nunca invente valores fora do JSON fornecido e responda estritamente em JSON válido."
    )

    payload_indicadores = {
        "ativo": f"{snap.exchange}:{snap.simbolo}",
        "modalidade_operacional": plano.modalidade,
        "timeframe_operacional": snap.intervalo,
        "direcao_avaliada": plano.direcao,
        "score_quant_local_6_camadas": snap.score_quant_local,
        "classificacao_setup": snap.tipo_setup,
        "preco_desconto_pullback_ema9": snap.preco_pullback_ideal,
        "cotacao": {
            "abertura": round(snap.abertura, snap.casas_decimais),
            "maxima": round(snap.maxima, snap.casas_decimais),
            "minima": round(snap.minima, snap.casas_decimais),
            "fechamento_atual": round(snap.preco_atual, snap.casas_decimais),
            "variacao_periodo_pct": round(snap.variacao_pct, 2),
            "alpha_forca_relativa_vs_lider_pct": round(snap.alpha_vs_lider_pct, 2),
            "volume": round(snap.volume, 2),
        },
        "indicadores_tendencia_e_fluxo": {
            "ema_9": round(snap.ema_9, snap.casas_decimais),
            "ema_21": round(snap.ema_21, snap.casas_decimais),
            "sma_200": round(snap.sma_200, snap.casas_decimais),
            "vwma_institucional": round(snap.vwma, snap.casas_decimais),
            "adx_14": round(snap.adx, 2),
            "di_plus": round(snap.di_plus, 2),
            "di_minus": round(snap.di_minus, 2),
        },
        "indicadores_momentum_e_volatilidade": {
            "rsi_14": round(snap.rsi, 2),
            "stoch_k": round(snap.stoch_k, 2),
            "stoch_d": round(snap.stoch_d, 2),
            "macd_line": round(snap.macd_line, 4),
            "macd_signal": round(snap.macd_signal, 4),
            "macd_histograma": round(snap.macd_hist, 4),
            "atr_14": round(snap.atr, snap.casas_decimais),
            "bollinger_upper": round(snap.bb_upper, snap.casas_decimais),
            "bollinger_lower": round(snap.bb_lower, snap.casas_decimais),
        },
        "mapa_institucional_mtf_e_pivots": {
            "pivot_suporte_s1": round(snap.pivot_s1, snap.casas_decimais),
            "pivot_central_p": round(snap.pivot_p, snap.casas_decimais),
            "pivot_resistencia_r1": round(snap.pivot_r1, snap.casas_decimais),
            f"tendencia_ancora_{snap.tf_superior}": snap.tendencia_mtf_superior,
            f"gatilho_curto_{snap.tf_inferior}": snap.confirmacao_mtf_inferior,
            "ativo_lider_mercado": snap.ativo_lider or snap.simbolo,
            "vies_ativo_lider": snap.vies_ativo_lider,
            "consenso_tradingview": snap.recomendacao_tv,
        },
        "plano_risco_algoritmico": asdict(plano),
    }

    bloco_pergunta = (
        f"\nPERGUNTA DO INVESTIDOR NO CHAT: \"{pergunta_usuario.strip()}\"\n"
        "Responda diretamente à dúvida do investidor dentro do campo 'racional_tecnico', "
        "explicando com clareza se as 6 camadas institucionais (Médias, VWMA, ADX/DI, MTF, Pivots e Líder) "
        "autorizam entrada agora (ou entrada por ordem limite no Pullback da EMA 9) ou se é melhor ficar de fora.\n"
        if pergunta_usuario.strip()
        else ""
    )

    user_prompt = (
        "Audite o seguinte JSON de indicadores institucionais em tempo real do TradingView:\n\n"
        f"{json.dumps(payload_indicadores, ensure_ascii=False, indent=2)}\n"
        f"{bloco_pergunta}\n"
        "REGRAS DE AUDITORIA QUANTITATIVA (6 CAMADAS):\n"
        "1. VETE (aprovado=false) se notar consolidação/lateralização (EMA9 embolada com EMA21, ADX < 18 ou DI+ e DI- sem dominância clara).\n"
        "2. VETE (aprovado=false) se o preço operar contra o fluxo institucional (abaixo da VWMA na COMPRA ou acima da VWMA na VENDA).\n"
        "3. VETE (aprovado=false) se houver conflito Multi-Timeframe (tendencia_ancora contrária à operação) ou vento contra forte do ativo líder (BOVA11/BTC).\n"
        "4. VETE (aprovado=false) se o Pivot R1 (na COMPRA) ou Pivot S1 (na VENDA) estiver bloqueando o caminho antes do Alvo 1, ou se o preço já estiver em exaustão fora das Bandas de Bollinger.\n"
        "5. APROVE (aprovado=true) somente se houver sinergia limpa entre as 6 camadas e relação risco/retorno favorável com proteção de Break-Even no Alvo 1.\n\n"
        "Retorne APENAS um objeto JSON com as chaves exatas:\n"
        "{\n"
        '  "aprovado": boolean,\n'
        '  "nota_confluencia": integer (0 a 100),\n'
        '  "regime_mercado": "TENDENCIA_FORTE" | "TENDENCIA_MODERADA" | "CONSOLIDACAO" | "EXAUSTAO",\n'
        '  "racional_tecnico": "string em português (2 a 3 frases diretas citando EMA9/21, SMA200, VWMA, RSI, ADX/DI e alinhamento MTF)",\n'
        '  "alerta_risco": "string curta (1 frase objetiva sobre o Pivot R1/S1, Pullback na EMA 9, Break-Even no Alvo 1 ou gatilho a aguardar)",\n'
        '  "motivo_veto": "string curta caso aprovado=false, ou vazio caso aprovado=true"\n'
        "}"
    )
    return system_instruction, user_prompt


def gerar_auditoria_quant_fallback(snap: SnapshotIndicadores, plano: PlanoRisco) -> AuditoriaIA:
    """Motor Quantitativo Local (6 Camadas) — garante auditoria precisa mesmo se a API do Gemini
    sofrer pico temporário de HTTP 503 nos servidores da Google.
    """
    ok_pre, motivo = pre_filtrar_confluencia_quant(snap)
    m, c = snap.moeda, snap.casas_decimais
    pos_sma = "acima" if snap.preco_atual >= snap.sma_200 else "abaixo"
    pos_vwma = "acima" if (snap.vwma <= 0 or snap.preco_atual >= snap.vwma) else "abaixo"
    pos_macd = "positivo" if snap.macd_hist >= 0 else "negativo"
    nota = calcular_score_quant_local(snap, plano.direcao)

    if ok_pre and nota >= NOTA_MINIMA_IA:
        regime = "TENDENCIA_FORTE" if snap.adx >= 25 else "TENDENCIA_MODERADA"
        racional = (
            f"Para {plano.modalidade}, {snap.simbolo} opera a {_fmt_preco(snap.preco_atual, m, c)} ({snap.tipo_setup}), "
            f"alinhado com EMA 9 ({_fmt_preco(snap.ema_9, m, c)}), EMA 21 ({_fmt_preco(snap.ema_21, m, c)}), {pos_vwma} da VWMA "
            f"institucional ({_fmt_preco(snap.vwma or snap.ema_21, m, c)}) e {pos_sma} da SMA 200 "
            f"({_fmt_preco(snap.sma_200, m, c)}). O ADX em {snap.adx:.1f} (DI+ {snap.di_plus:.1f} / DI- {snap.di_minus:.1f}), "
            f"RSI em {snap.rsi:.1f}, MACD {pos_macd} ({snap.macd_hist:+.4f}) e MTF {snap.tf_superior.upper()} ({snap.tendencia_mtf_superior}) confirmam a direção."
        )
        alerta = (
            f"Ao atingir o Alvo 1 ({_fmt_preco(plano.alvo_1, m, c)}), realizar 50% da posição e mover o Stop Loss "
            f"para o Break-Even ({_fmt_preco(plano.entrada, m, c)})."
        )
        return AuditoriaIA(True, nota, regime, racional, alerta, "", "Motor Quant Institucional")

    regime = (
        "CONSOLIDACAO"
        if (0 < snap.adx < 18.0 or abs(snap.ema_9 - snap.ema_21) / max(snap.preco_atual, 1e-9) < 0.0012)
        else "INDEFINIDO"
    )
    racional = (
        f"No gráfico de {snap.intervalo.upper()} ({plano.modalidade}), {snap.simbolo} negocia a {_fmt_preco(snap.preco_atual, m, c)} "
        f"(EMA 9 em {_fmt_preco(snap.ema_9, m, c)}, EMA 21 em {_fmt_preco(snap.ema_21, m, c)}, VWMA em "
        f"{_fmt_preco(snap.vwma or snap.ema_21, m, c)} e SMA 200 em {_fmt_preco(snap.sma_200, m, c)}), "
        f"com RSI em {snap.rsi:.1f}, ADX em {snap.adx:.1f} (DI+ {snap.di_plus:.1f} / DI- {snap.di_minus:.1f}) "
        f"e tendência {snap.tf_superior.upper()} {snap.tendencia_mtf_superior}. "
        f"Diagnóstico das 6 camadas: {motivo}"
    )
    alerta = (
        f"Proteja seu capital: aguarde alinhamento das médias ou recuo institucional até a região da EMA 9 "
        f"({_fmt_preco(snap.preco_pullback_ideal or snap.ema_9, m, c)}) com entrada de volume."
    )
    return AuditoriaIA(False, min(nota, 68), regime, racional, alerta, motivo, "Motor Quant Institucional")


def auditar_sinal_com_gemini(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    api_key: str = GEMINI_API_KEY,
    pergunta_usuario: str = "",
) -> Optional[AuditoriaIA]:
    global _MODELO_VENCEDOR_SESSAO
    if not api_key:
        logger.error("GEMINI_API_KEY não configurada.")
        return None

    modelos = descobrir_modelos_gemini_gratuitos(api_key)
    if not modelos:
        logger.error("Nenhum modelo Gemini gratuito disponível nesta sessão.")
        return None

    sys_inst, usr_prompt = construir_prompt_auditoria(snap, plano, pergunta_usuario=pergunta_usuario)
    payload = {
        "systemInstruction": {"parts": [{"text": sys_inst}]},
        "contents": [{"role": "user", "parts": [{"text": usr_prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 1200,
            "responseMimeType": "application/json",
        },
    }

    for modelo in modelos:
        url = f"{GEMINI_API_BASE}/{modelo}:generateContent"
        try:
            resp = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-goog-api-key": api_key,
                },
                json=payload,
                timeout=15,
            )
            if resp.status_code == 200:
                body = resp.json()
                cand0 = (body.get("candidates") or [{}])[0]
                partes = cand0.get("content", {}).get("parts", [])
                texto = "".join(p.get("text", "") for p in partes if isinstance(p, dict) and not p.get("thought"))
                if not texto:
                    texto = "".join(p.get("text", "") for p in partes if isinstance(p, dict))
                dados = _extrair_json_resposta(texto)
                if dados and "aprovado" in dados:
                    _MODELO_VENCEDOR_SESSAO = modelo
                    _MODELOS_DEGRADADOS_SESSAO.discard(modelo)
                    nota = int(_num(dados.get("nota_confluencia"), 0))
                    aprovado = bool(dados.get("aprovado")) and nota >= NOTA_MINIMA_IA
                    return AuditoriaIA(
                        aprovado=aprovado,
                        nota_confluencia=nota,
                        regime_mercado=str(dados.get("regime_mercado") or "INDEFINIDO").strip(),
                        racional_tecnico=str(dados.get("racional_tecnico") or "").strip(),
                        alerta_risco=str(dados.get("alerta_risco") or "").strip(),
                        motivo_veto=str(dados.get("motivo_veto") or ("Nota abaixo do corte mínimo" if not aprovado else "")).strip(),
                        modelo_utilizado=modelo,
                    )
                _MODELOS_DEGRADADOS_SESSAO.add(modelo)
                logger.warning("Gemini (%s) retornou JSON incompleto; tentando próximo modelo.", modelo)
            elif resp.status_code in (404, 429):
                _MODELOS_BLOQUEADOS_SESSAO.add(modelo)
                if _MODELO_VENCEDOR_SESSAO == modelo:
                    _MODELO_VENCEDOR_SESSAO = None
                logger.warning("Gemini (%s) sem cota/indisponível (HTTP %s); bloqueando modelo na sessão.", modelo, resp.status_code)
            elif resp.status_code in (400, 500, 503):
                _MODELOS_DEGRADADOS_SESSAO.add(modelo)
                if _MODELO_VENCEDOR_SESSAO == modelo:
                    _MODELO_VENCEDOR_SESSAO = None
                logger.warning("Gemini (%s) instável/sobrecarregado (HTTP %s); rebaixando na fila.", modelo, resp.status_code)
            elif resp.status_code in (401, 403):
                logger.error("GEMINI_API_KEY inválida ou sem permissão (HTTP %s).", resp.status_code)
                return None
            else:
                logger.warning("Gemini (%s) erro HTTP %s; tentando próximo modelo.", modelo, resp.status_code)
        except Exception as exc:
            _MODELOS_DEGRADADOS_SESSAO.add(modelo)
            if _MODELO_VENCEDOR_SESSAO == modelo:
                _MODELO_VENCEDOR_SESSAO = None
            logger.warning("Timeout/erro ao consultar Gemini (%s): %s", modelo, str(exc)[:100])

    return None


# ==============================================================================
# 6. ATENDIMENTO INTERATIVO NO CHAT + SCANNER DE SWING TRADE & OPORTUNIDADES
# ==============================================================================
def detectar_modalidade_na_mensagem(texto: str) -> str:
    t = (texto or "").lower()
    if re.search(r"\b(?:swing\s*trade|swing|m[ée]dio\s*prazo|semanal|carteira|dias|1\s*d|di[áa]rio|4\s*h|h4)\b", t):
        return "SWING TRADE"
    return "DAY TRADE"


def extrair_intervalo_da_mensagem(texto: str) -> str:
    t = (texto or "").lower()
    for padrao, tf in (
        (r"\b(?:5\s*m|5\s*min|m5)\b", "5m"),
        (r"\b(?:15\s*m|15\s*min|m15)\b", "15m"),
        (r"\b(?:30\s*m|30\s*min|m30)\b", "30m"),
        (r"\b(?:1\s*h|60\s*m|60\s*min|h1)\b", "1h"),
        (r"\b(?:4\s*h|h4)\b", "4h"),
        (r"\b(?:1\s*d|di[áa]rio|d1)\b", "1d"),
        (r"\b(?:1\s*w|semanal|w1)\b", "1w"),
    ):
        if re.search(padrao, t):
            return tf
    if detectar_modalidade_na_mensagem(t) == "SWING TRADE":
        return "1d"
    return TIMEFRAME_PADRAO


def identificar_pedido_recomendacao_ranking(texto: str, bot_username: str = "") -> Optional[Tuple[str, str]]:
    """Detecta quando o trader pede recomendações/calls gerais sem especificar um único ativo
    (ex.: 'quero fazer swing trade, qual ação recomenda?', '/swing', 'qual a melhor cripto pra hoje?').
    Retorna (modalidade, segmento):
      - modalidade: 'SWING TRADE' | 'DAY TRADE'
      - segmento: 'B3' | 'CRIPTO' | 'TODOS'
    """
    if not texto:
        return None
    t_low = texto.lower().strip()

    eh_cmd_rank = bool(REGEX_COMANDO_RANKING.search(t_low))
    tem_pedido_rec = bool(REGEX_PEDIDO_RECOMENDACAO.search(t_low))

    if not eh_cmd_rank and not tem_pedido_rec:
        return None

    # Determina modalidade (Swing Trade no gráfico Diário vs Day Trade no 15m)
    if re.search(r"\b(?:swing|swingtrade|m[ée]dio\s*prazo|semanal|dias|di[áa]rio|1d)\b", t_low):
        modalidade = "SWING TRADE"
    elif re.search(r"\b(?:day\s*trade|daytrade|intradi[áa]rio|scalp|15m|5m)\b", t_low):
        modalidade = "DAY TRADE"
    else:
        modalidade = "SWING TRADE" if "recomenda" in t_low or "indica" in t_low else "DAY TRADE"

    # Determina segmento (Ações B3 vs Cripto vs Todos)
    if re.search(r"\b(?:a[çc][ãa]o|a[çc][õo]es|b3|bolsa|ibov|papel|pap[ée]is)\b", t_low):
        segmento = "B3"
    elif re.search(r"\b(?:cripto|criptomoeda|criptos|moeda|altcoin|binance)\b", t_low):
        segmento = "CRIPTO"
    else:
        segmento = "TODOS"

    return modalidade, segmento


def identificar_ativo_na_pergunta(texto: str, bot_username: str = "") -> Optional[AtivoMonitorado]:
    if not texto:
        return None

    t_up = texto.upper()
    t_low = texto.lower()

    eh_comando = bool(REGEX_COMANDO_CHAT.search(t_low))
    mencionou_bot = bool(bot_username and f"@{bot_username.lower()}" in t_low)
    tem_intencao = eh_comando or mencionou_bot or bool(REGEX_INTENCAO_PERGUNTA.search(t_low))

    # 1. Procura par cripto explícito (ex.: BTCUSDT, SOLUSDT, PEPEUSDT)
    m_cripto = REGEX_PAR_CRIPTO.search(t_up)
    if m_cripto and (tem_intencao or len(t_up.split()) <= 4):
        simb = m_cripto.group(1)
        for a in (*WATCHLIST_PADRAO, *CARTEIRA_SWING_CRIPTO):
            if a.simbolo == simb:
                return a
        return AtivoMonitorado(simb, "BINANCE", "crypto", "US$", 2, False)

    # 2. Procura ticker padrão B3 (ex.: PETR4, VALE3, KLBN11, HGLG11, NVDC34, SMFT3)
    m_b3 = REGEX_TICKER_B3.search(t_up)
    if m_b3 and (tem_intencao or len(t_up.split()) <= 4):
        simb = m_b3.group(1)
        for a in (*WATCHLIST_PADRAO, *CARTEIRA_SWING_B3):
            if a.simbolo == simb:
                return a
        return AtivoMonitorado(simb, "BMFBOVESPA", "brazil", "R$", 2, True)

    # 3. Procura nomes/apelidos conhecidos (ex.: "o que acha do bitcoin?", "analisa petrobras", "swing trade em vale")
    if tem_intencao or len(t_low.split()) <= 3:
        for apelido in sorted(ALIASES_ATIVOS_CHAT.keys(), key=len, reverse=True):
            if re.search(r"(?<!\w)" + re.escape(apelido) + r"(?!\w)", t_low):
                return ALIASES_ATIVOS_CHAT[apelido]

    # 4. Se usou comando explícito (/analisar AAPL ou /analisar SUI), tenta resolver cripto/ação
    if eh_comando:
        limpo = REGEX_COMANDO_CHAT.sub("", t_up).strip()
        if bot_username:
            limpo = re.sub(rf"@{re.escape(bot_username.upper())}\b", "", limpo).strip()
        tokens = [
            tok for tok in re.findall(r"[A-Z0-9]{2,10}", limpo)
            if tok not in {"5M", "15M", "30M", "1H", "4H", "1D", "1W", "SWING"}
        ]
        if tokens:
            tk = tokens[0]
            if tk.lower() in ALIASES_ATIVOS_CHAT:
                return ALIASES_ATIVOS_CHAT[tk.lower()]
            if not tk.endswith("USDT") and len(tk) <= 5:
                return AtivoMonitorado(f"{tk}USDT", "BINANCE", "crypto", "US$", 2, False)

    return None


def _fmt_preco(valor: float, moeda: str, casas: int) -> str:
    s = f"{valor:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{moeda} {s}"


def _formatar_linha_pivots(snap: SnapshotIndicadores) -> str:
    m, c = snap.moeda, snap.casas_decimais
    s1 = _fmt_preco(snap.pivot_s1, m, c) if snap.pivot_s1 > 0 else "N/D"
    p = _fmt_preco(snap.pivot_p, m, c) if snap.pivot_p > 0 else "N/D"
    r1 = _fmt_preco(snap.pivot_r1, m, c) if snap.pivot_r1 > 0 else "N/D"
    return f"S1 <code>{s1}</code>  |  P <code>{p}</code>  |  R1 <code>{r1}</code>"


def _formatar_linha_lote_1pct(plano: PlanoRisco, moeda: str) -> str:
    cap_fmt = _fmt_preco(plano.capital_referencia, moeda, 0)
    risco_fmt = _fmt_preco(plano.risco_max_1pct_capital, moeda, 0)
    exp_fmt = _fmt_preco(plano.exposicao_total_sugerida, moeda, 2)
    qtd_str = f"{int(plano.quantidade_sugerida_1pct)}" if plano.quantidade_sugerida_1pct == int(plano.quantidade_sugerida_1pct) else f"{plano.quantidade_sugerida_1pct}"
    unidade = "ações/cotas" if moeda == "R$" else "unid."
    return (
        f"Para banca de <code>{cap_fmt}</code> (Risco 1% = <code>{risco_fmt}</code>) 👉 "
        f"<b>{qtd_str} {unidade}</b> (Exposição: <code>{exp_fmt}</code>)"
    )


def escanear_melhores_oportunidades(
    modalidade: str = "SWING TRADE",
    segmento: str = "TODOS",
    top_n: int = 3,
) -> List[Tuple[SnapshotIndicadores, PlanoRisco, bool, str]]:
    """Varre a carteira institucional de Ações B3 e/ou Cripto no timeframe adequado
    (1D para Swing Trade com Stop 2.0x ATR; 15m para Day Trade com Stop 1.5x ATR),
    ranqueia pelas 6 Camadas Quant + Força Relativa (Alpha) e retorna os Top N setups.
    """
    eh_swing = "SWING" in modalidade.upper()
    tf = "1d" if eh_swing else TIMEFRAME_PADRAO
    mult_atr = ATR_MULTIPLIER_SWING if eh_swing else ATR_MULTIPLIER

    if segmento == "B3":
        universo: Tuple[AtivoMonitorado, ...] = CARTEIRA_SWING_B3
    elif segmento == "CRIPTO":
        universo = CARTEIRA_SWING_CRIPTO
    else:
        universo = (*CARTEIRA_SWING_B3[:8], *CARTEIRA_SWING_CRIPTO[:4])

    candidatos: List[Tuple[int, float, SnapshotIndicadores, PlanoRisco, bool, str]] = []
    for ativo in universo:
        snap = extrair_dados_tradingview(ativo, tf, enriquecer_mtf=True)
        if not snap:
            continue
        aprovado_pre, motivo = pre_filtrar_confluencia_quant(snap)
        direcao, status_vies = diagnosticar_direcao_sob_demanda(snap)
        plano = calcular_gestao_risco(
            preco_entrada=snap.preco_atual,
            atr=snap.atr,
            direcao=direcao,
            multiplicador_atr=mult_atr,
            casas_decimais=snap.casas_decimais,
            modalidade="SWING TRADE" if eh_swing else "DAY TRADE",
            moeda=snap.moeda,
        )
        # Bônus de ranqueamento para setups aprovados nas 6 camadas e com Pullback na EMA 9
        bonus = (25 if aprovado_pre else 0) + (8 if "PULLBACK" in snap.tipo_setup else 0)
        alpha_favoravel = snap.alpha_vs_lider_pct if direcao == "COMPRA" else -snap.alpha_vs_lider_pct
        nota_rank = snap.score_quant_local + bonus
        candidatos.append((nota_rank, alpha_favoravel, snap, plano, aprovado_pre, status_vies if not aprovado_pre else "SINAL APROVADO (6 CAMADAS)"))

    candidatos.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(s, p, ap, st) for _, _, s, p, ap, st in candidatos[:top_n]]


def formatar_painel_recomendacoes_html(
    modalidade: str,
    segmento: str,
    oportunidades: List[Tuple[SnapshotIndicadores, PlanoRisco, bool, str]],
) -> str:
    agora_str = dt.datetime.now(TZ_BRT).strftime("%d/%m/%Y às %H:%M BRT")
    eh_swing = "SWING" in modalidade.upper()
    tf_label = "DIÁRIO (1D) + 4H" if eh_swing else f"INTRADIÁRIO ({TIMEFRAME_PADRAO.upper()}) + 1H"
    mult_atr = ATR_MULTIPLIER_SWING if eh_swing else ATR_MULTIPLIER
    seg_label = {"B3": "AÇÕES B3", "CRIPTO": "CRIPTOMOEDAS", "TODOS": "B3 & CRIPTO"}.get(segmento, "GERAL")

    if not oportunidades:
        return (
            f"<blockquote>📡 <b>RADAR DE OPORTUNIDADES {html.escape(modalidade)}</b></blockquote>\n\n"
            "⚠️ Não foi possível extrair cotações do TradingView neste instante. Tente novamente em alguns segundos."
        )

    aprovados_qtd = sum(1 for _, _, ap, _ in oportunidades if ap)
    if aprovados_qtd > 0:
        cabecalho_status = (
            f"✅ <b>{aprovados_qtd} CALL(S) COM CONFLUÊNCIA INSTITUCIONAL ATIVA!</b>\n"
            f"<i>Selecionados pelo algoritmo de 6 Camadas + Força Relativa (Alpha).</i>"
        )
    else:
        cabecalho_status = (
            "🛡️ <b>MODO PRESERVAÇÃO DE CAPITAL (NENHUM ATIVO 100% ALINHADO AGORA)</b>\n"
            "<i>Ficar de fora também é posição! Abaixo estão os 3 ativos mais fortes do radar e o gatilho exato para aguardar:</i>"
        )

    blocos: List[str] = []
    medalhas = ("🥇", "🥈", "🥉")
    for idx, (snap, plano, aprovado, status_txt) in enumerate(oportunidades):
        med = medalhas[idx] if idx < len(medalhas) else "🔹"
        m, c = snap.moeda, snap.casas_decimais
        icone_dir = "🟢 COMPRA" if plano.direcao == "COMPRA" else "🔴 VENDA"
        badge = "✅ <b>CALL APROVADO</b>" if aprovado else f"⏳ <b>{html.escape(status_txt)}</b>"
        alpha_str = f"{snap.alpha_vs_lider_pct:+.2f}% vs {snap.ativo_lider}" if snap.ativo_lider and snap.vies_ativo_lider != "PROPRIO" else "Ativo Líder"

        blocos.append(
            f"{med} <b>#{html.escape(snap.simbolo)}</b> ({html.escape(snap.exchange)}) — <b>{icone_dir}</b>  |  {badge}\n"
            f"• <b>Score Quant:</b> <code>{snap.score_quant_local}/100</code>  |  <b>Alpha:</b> <code>{html.escape(alpha_str)}</code>  |  <b>MTF {html.escape(snap.tf_superior.upper())}:</b> <code>{html.escape(snap.tendencia_mtf_superior)}</code>\n"
            f"• <b>Jogada Gráfica:</b> <i>{html.escape(snap.tipo_setup)}</i>\n"
            f"• <b>Entrada Atual:</b> <code>{_fmt_preco(plano.entrada, m, c)}</code>  |  <b>Desconto EMA 9:</b> <code>{_fmt_preco(snap.preco_pullback_ideal, m, c)}</code>\n"
            f"• <b>Stop Técnico ({mult_atr:.1f}x ATR):</b> <code>{_fmt_preco(plano.stop_loss, m, c)}</code> <i>(-{plano.risco_percentual:.2f}%)</i>\n"
            f"• <b>Alvo 1 (50% + Stop 0x0):</b> <code>{_fmt_preco(plano.alvo_1, m, c)}</code>  |  <b>Alvo 2:</b> <code>{_fmt_preco(plano.alvo_2, m, c)}</code>  |  <b>Alvo 3:</b> <code>{_fmt_preco(plano.alvo_3, m, c)}</code>\n"
            f"• <b>Gestão de Lote (1% Risco):</b> {_formatar_linha_lote_1pct(plano, m)}"
        )

    corpo_cards = "\n\n".join(blocos)
    return (
        f"<blockquote>🏆 <b>TOP {len(oportunidades)} RECOMENDAÇÕES — {html.escape(modalidade)} ({html.escape(seg_label)})</b>\n"
        f"⏱️ <b>Timeframe:</b> {html.escape(tf_label)}  ·  <b>Stop ATR:</b> {mult_atr:.1f}x</blockquote>\n\n"
        f"{cabecalho_status}\n\n"
        f"{corpo_cards}\n\n"
        f"💡 <b>Dica de Mesa Proprietária:</b> Priorize entradas próximas ao <b>Desconto EMA 9</b> e mova o Stop Loss para o <b>Break-Even (0x0)</b> assim que atingir o Alvo 1.\n"
        f"<i>🕒 {agora_str} · Para ver o raio-x completo de um ativo, envie: <code>/analisar {html.escape(oportunidades[0][0].simbolo)} {'1d' if eh_swing else '15m'}</code></i>"
    )


def formatar_resposta_analise_chat_html(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    auditoria: AuditoriaIA,
    status_vies: str,
) -> str:
    m, c = snap.moeda, snap.casas_decimais
    agora_str = dt.datetime.now(TZ_BRT).strftime("%d/%m/%Y às %H:%M BRT")
    vwma_val = snap.vwma if snap.vwma > 0 else snap.ema_21
    lider_str = f"{snap.ativo_lider or snap.simbolo} ({snap.vies_ativo_lider})"
    alpha_str = f"{snap.alpha_vs_lider_pct:+.2f}%" if snap.vies_ativo_lider != "PROPRIO" else "Líder"
    mult_usado = ATR_MULTIPLIER_SWING if plano.modalidade == "SWING TRADE" else ATR_MULTIPLIER

    if auditoria.aprovado:
        badge_status = (
            f"🟢 <b>CALL DE {html.escape(plano.modalidade)} APROVADO — COMPRA (LONG)</b>"
            if plano.direcao == "COMPRA"
            else f"🔴 <b>CALL DE {html.escape(plano.modalidade)} APROVADO — VENDA (SHORT)</b>"
        )
        titulo_niveis = f"🎯 <b>PLANO DE {html.escape(plano.modalidade)} ({plano.direcao} · ATR {mult_usado:.1f}x)</b>"
    else:
        icone_vies = "🟡" if "CONSOLIDAÇÃO" in status_vies else ("🟢" if plano.direcao == "COMPRA" else "🔴")
        badge_status = f"{icone_vies} <b>{html.escape(status_vies)} — SEM ENTRADA AGORA</b>"
        titulo_niveis = f"📐 <b>NÍVEIS DE REFERÊNCIA ({plano.modalidade} · {plano.direcao} SE CONFIRMAR)</b>"

    racional_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.racional_tecnico))
    alerta_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.alerta_risco))
    regime_limpo = html.escape(auditoria.regime_mercado.replace("_", " "))

    return (
        f"<blockquote>🔎 <b>ANÁLISE QUANT ({html.escape(plano.modalidade)})</b>  ·  <code>#{html.escape(snap.simbolo)}</code>  ·  <b>{html.escape(snap.intervalo.upper())}</b></blockquote>\n\n"
        f"{badge_status}\n"
        f"🏛️ <b>Bolsa:</b> {html.escape(snap.exchange)}  |  📊 <b>Score Quant:</b> <code>{snap.score_quant_local}/100</code>  |  <b>IA:</b> <code>{auditoria.nota_confluencia}/100</code> ({regime_limpo})\n"
        f"♟️ <b>Setup Gráfico:</b> <i>{html.escape(snap.tipo_setup)}</i>\n\n"
        f"🧠 <b>PARECER DO ANALISTA QUANT ({html.escape(auditoria.modelo_utilizado)})</b>\n"
        f"{racional_limpo}\n\n"
        f"{titulo_niveis}\n"
        f"• <b>Preço Atual / Pullback EMA 9:</b> <code>{_fmt_preco(plano.entrada, m, c)}</code> / <code>{_fmt_preco(snap.preco_pullback_ideal, m, c)}</code>\n"
        f"• <b>Stop Técnico ({mult_usado:.1f}x ATR):</b> <code>{_fmt_preco(plano.stop_loss, m, c)}</code> <i>(-{plano.risco_percentual:.2f}%)</i>\n"
        f"• <b>Alvo 1 (1:1.5 · 50% + Break-Even 0x0):</b> <code>{_fmt_preco(plano.alvo_1, m, c)}</code>\n"
        f"• <b>Alvo 2 (1:2.0 · Parcial 30%):</b> <code>{_fmt_preco(plano.alvo_2, m, c)}</code>\n"
        f"• <b>Alvo 3 (1:3.0 · Runner 20%):</b> <code>{_fmt_preco(plano.alvo_3, m, c)}</code>\n"
        f"• <b>Horizonte:</b> <i>{html.escape(plano.horizonte_estimado)}</i>\n"
        f"• <b>Gestão de Lote (1% Risco):</b> {_formatar_linha_lote_1pct(plano, m)}\n\n"
        f"🧭 <b>CONFLUÊNCIA MULTI-TIMEFRAME &amp; FLUXO INSTITUCIONAL</b>\n"
        f"• <b>Âncora Macro ({html.escape(snap.tf_superior.upper())}):</b> <code>{html.escape(snap.tendencia_mtf_superior)}</code>  |  <b>Gatilho ({html.escape(snap.tf_inferior.upper())}):</b> <code>{html.escape(snap.confirmacao_mtf_inferior)}</code>\n"
        f"• <b>Ativo Líder:</b> <code>{html.escape(lider_str)}</code>  |  <b>Alpha vs Líder:</b> <code>{html.escape(alpha_str)}</code>\n"
        f"• <b>EMA 9 / EMA 21:</b> <code>{_fmt_preco(snap.ema_9, m, c)}</code> / <code>{_fmt_preco(snap.ema_21, m, c)}</code>\n"
        f"• <b>VWMA (Volume) / SMA 200:</b> <code>{_fmt_preco(vwma_val, m, c)}</code> / <code>{_fmt_preco(snap.sma_200, m, c)}</code>\n"
        f"• <b>ADX / Dominância:</b> <code>{snap.adx:.1f}</code> (<b>DI+</b> <code>{snap.di_plus:.1f}</code> vs <b>DI-</b> <code>{snap.di_minus:.1f}</code>)\n"
        f"• <b>RSI (14):</b> <code>{snap.rsi:.1f}</code>  |  <b>MACD Hist:</b> <code>{snap.macd_hist:+.4f}</code>  |  <b>ATR:</b> <code>{_fmt_preco(snap.atr, m, c)}</code>\n"
        f"• <b>Pivots:</b> {_formatar_linha_pivots(snap)}\n\n"
        f"⚠️ <b>Atenção:</b> {alerta_limpo}\n"
        f"<i>🕒 {agora_str} · Análise quantitativa educacional (não é recomendação de investimento).</i>"
    )


# ==============================================================================
# 7. FORMATAÇÃO DE SINAIS AUTOMÁTICOS, HISTÓRICO E INTEGRAÇÃO TELEGRAM
# ==============================================================================
def formatar_mensagem_sinal_html(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    auditoria: AuditoriaIA,
) -> str:
    icone_dir = "🟢 <b>COMPRA (LONG)</b>" if plano.direcao == "COMPRA" else "🔴 <b>VENDA (SHORT)</b>"
    agora_str = dt.datetime.now(TZ_BRT).strftime("%d/%m/%Y às %H:%M BRT")
    m, c = snap.moeda, snap.casas_decimais
    vwma_val = snap.vwma if snap.vwma > 0 else snap.ema_21
    lider_str = f"{snap.ativo_lider or snap.simbolo} ({snap.vies_ativo_lider})"
    alpha_str = f"{snap.alpha_vs_lider_pct:+.2f}%" if snap.vies_ativo_lider != "PROPRIO" else "Líder"

    racional_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.racional_tecnico))
    alerta_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.alerta_risco))
    regime_limpo = html.escape(auditoria.regime_mercado.replace("_", " "))

    return (
        f"<blockquote>⚡ <b>RADAR QUANT {html.escape(plano.modalidade)} (6 CAMADAS)</b>  ·  <code>#{html.escape(snap.simbolo)}</code>  ·  <b>{html.escape(snap.intervalo.upper())}</b></blockquote>\n\n"
        f"{icone_dir}  ·  <i>{html.escape(snap.tipo_setup)}</i>\n"
        f"🏛️ <b>Bolsa:</b> {html.escape(snap.exchange)}  |  📊 <b>Score Quant:</b> <code>{snap.score_quant_local}/100</code>  |  <b>IA:</b> <code>{auditoria.nota_confluencia}/100</code> ({regime_limpo})\n\n"
        f"🎯 <b>GESTÃO MATEMÁTICA DE RISCO (ATR {ATR_MULTIPLIER:.1f}x + BREAK-EVEN)</b>\n"
        f"• <b>Entrada / Desconto EMA 9:</b> <code>{_fmt_preco(plano.entrada, m, c)}</code> / <code>{_fmt_preco(snap.preco_pullback_ideal, m, c)}</code>\n"
        f"• <b>Stop Loss Técnico:</b> <code>{_fmt_preco(plano.stop_loss, m, c)}</code> <i>(-{plano.risco_percentual:.2f}%)</i>\n"
        f"• <b>Alvo 1 (1:1.5 · Parcial 50% + Stop no 0x0):</b> <code>{_fmt_preco(plano.alvo_1, m, c)}</code>\n"
        f"• <b>Alvo 2 (1:2.0 · Parcial 30%):</b> <code>{_fmt_preco(plano.alvo_2, m, c)}</code>\n"
        f"• <b>Alvo 3 (1:3.0 · Runner 20%):</b> <code>{_fmt_preco(plano.alvo_3, m, c)}</code>\n"
        f"• <b>Gestão de Lote (1% Risco):</b> {_formatar_linha_lote_1pct(plano, m)}\n"
        f"• <b>Regra de Proteção:</b> <i>Bateu Alvo 1 ({_fmt_preco(plano.gatilho_breakeven, m, c)}), embolse 50% e traga o Stop para a Entrada ({_fmt_preco(plano.entrada, m, c)}).</i>\n\n"
        f"🧭 <b>INDICADORES INSTITUCIONAIS &amp; MTF (TRADINGVIEW)</b>\n"
        f"• <b>MTF ({html.escape(snap.tf_superior.upper())} / {html.escape(snap.intervalo.upper())} / {html.escape(snap.tf_inferior.upper())}):</b> <code>{html.escape(snap.tendencia_mtf_superior)}</code> / <code>{html.escape(plano.direcao)}</code> / <code>{html.escape(snap.confirmacao_mtf_inferior)}</code>\n"
        f"• <b>Vento de Mercado (Líder):</b> <code>{html.escape(lider_str)}</code>  |  <b>Alpha:</b> <code>{html.escape(alpha_str)}</code>\n"
        f"• <b>EMA 9 / EMA 21:</b> <code>{_fmt_preco(snap.ema_9, m, c)}</code> / <code>{_fmt_preco(snap.ema_21, m, c)}</code>\n"
        f"• <b>VWMA (Fluxo) / SMA 200:</b> <code>{_fmt_preco(vwma_val, m, c)}</code> / <code>{_fmt_preco(snap.sma_200, m, c)}</code>\n"
        f"• <b>ADX / Dominância:</b> <code>{snap.adx:.1f}</code> (<b>DI+</b> <code>{snap.di_plus:.1f}</code> vs <b>DI-</b> <code>{snap.di_minus:.1f}</code>)\n"
        f"• <b>RSI (14):</b> <code>{snap.rsi:.1f}</code>  |  <b>MACD / Sinal:</b> <code>{snap.macd_line:.4f}</code> / <code>{snap.macd_signal:.4f}</code>\n"
        f"• <b>Pivots Intradiários:</b> {_formatar_linha_pivots(snap)}\n"
        f"• <b>ATR (14):</b> <code>{_fmt_preco(snap.atr, m, c)}</code>  |  <b>Volume:</b> <code>{snap.volume:,.0f}</code>\n\n"
        f"🧠 <b>AUDITORIA DA IA ({html.escape(auditoria.modelo_utilizado)})</b>\n"
        f"{racional_limpo}\n\n"
        f"⚠️ <b>Ponto de Atenção:</b> {alerta_limpo}\n"
        f"<i>🕒 {agora_str} · Estudo quantitativo automatizado (não configura recomendação de investimento).</i>"
    )


def carregar_historico(caminho: Path = HISTORICO_PATH) -> Dict[str, Any]:
    if caminho.exists():
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                dados.setdefault("sinais", [])
                dados.setdefault("ultimo_update_id", 0)
                return dados
        except Exception as exc:
            logger.warning("Erro ao ler %s: %s", caminho, exc)
    return {"sinais": [], "ultimo_update_id": 0}


def salvar_historico(dados: Dict[str, Any], caminho: Path = HISTORICO_PATH) -> None:
    agora_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    sinais_limpos = [
        s for s in dados.get("sinais", [])
        if isinstance(s, dict) and (agora_ts - float(s.get("ts", 0))) <= 72 * 3600
    ][-400:]
    payload = {
        "atualizado_em": dt.datetime.now(TZ_BRT).isoformat(),
        "ultimo_update_id": int(dados.get("ultimo_update_id") or 0),
        "sinais": sinais_limpos,
    }
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


def sinal_em_cooldown(
    simbolo: str,
    direcao: str,
    intervalo: str,
    historico: Dict[str, Any],
    agora_ts: Optional[float] = None,
) -> bool:
    ts_atual = agora_ts if agora_ts is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    chave = f"{simbolo.upper()}::{direcao.upper()}::{intervalo.lower()}"
    for reg in reversed(historico.get("sinais", [])):
        if reg.get("chave") == chave:
            decorrido_min = (ts_atual - float(reg.get("ts", 0))) / 60.0
            if 0 <= decorrido_min < COOLDOWN_SINAL_MINUTOS:
                return True
    return False


def registrar_sinal_historico(
    historico: Dict[str, Any],
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    auditoria: AuditoriaIA,
) -> None:
    chave = f"{snap.simbolo.upper()}::{plano.direcao.upper()}::{snap.intervalo.lower()}"
    historico.setdefault("sinais", []).append(
        {
            "chave": chave,
            "simbolo": snap.simbolo,
            "direcao": plano.direcao,
            "intervalo": snap.intervalo,
            "modalidade": plano.modalidade,
            "entrada": plano.entrada,
            "stop_loss": plano.stop_loss,
            "alvo_1": plano.alvo_1,
            "alvo_2": plano.alvo_2,
            "alvo_3": plano.alvo_3,
            "gatilho_breakeven": plano.gatilho_breakeven,
            "score_quant": snap.score_quant_local,
            "nota_ia": auditoria.nota_confluencia,
            "ts": dt.datetime.now(dt.timezone.utc).timestamp(),
        }
    )


def enviar_telegram_html(
    texto_html: str,
    token: str = TELEGRAM_BOT_TOKEN,
    chat_id: str = TELEGRAM_CHAT_ID,
    reply_to_message_id: Optional[int] = None,
) -> bool:
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausente.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": texto_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True

    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            return True
        logger.error("Erro Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.error("Falha de rede ao enviar para Telegram: %s", exc)
    return False


def obter_username_bot(token: str = TELEGRAM_BOT_TOKEN) -> str:
    if not token:
        return ""
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if resp.status_code == 200:
            return str(resp.json().get("result", {}).get("username") or "").strip()
    except Exception:
        pass
    return ""


def responder_perguntas_chat(historico: Dict[str, Any], bot_username: str = "") -> int:
    """Lê novas mensagens enviadas no grupo/chat (getUpdates), identifica:
    1) Pedidos de recomendação/ranking de Swing Trade ou Day Trade ('quero fazer swing trade, qual ação recomenda?', '/swing')
    2) Pedidos de análise de um ativo específico ('o que acha de PETR4?', 'vale a pena swing trade em VALE3?')
    """
    if not TELEGRAM_BOT_TOKEN:
        return 0

    ultimo_id = int(historico.get("ultimo_update_id") or 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params: Dict[str, Any] = {"timeout": 2, "allowed_updates": json.dumps(["message"])}
    if ultimo_id > 0:
        params["offset"] = ultimo_id + 1

    try:
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code != 200:
            return 0
        updates = resp.json().get("result") or []
    except Exception as exc:
        logger.warning("Aviso ao buscar mensagens do chat (getUpdates): %s", str(exc)[:100])
        return 0

    if not updates:
        return 0

    respondidas = 0
    mudou_offset = False
    agora_ts = dt.datetime.now(dt.timezone.utc).timestamp()

    for upd in updates:
        if not isinstance(upd, dict):
            continue
        upd_id = int(upd.get("update_id") or 0)
        if upd_id > ultimo_id:
            ultimo_id = upd_id
            historico["ultimo_update_id"] = ultimo_id
            mudou_offset = True

        msg = upd.get("message")
        if not isinstance(msg, dict):
            continue
        remetente = msg.get("from") or {}
        if remetente.get("is_bot"):
            continue

        msg_ts = float(msg.get("date") or 0)
        if msg_ts > 0 and (agora_ts - msg_ts) > 3600:
            continue

        texto = str(msg.get("text") or msg.get("caption") or "").strip()
        if not texto:
            continue

        chat_id_msg = str((msg.get("chat") or {}).get("id") or "")
        msg_id = int(msg.get("message_id") or 0)
        if not chat_id_msg:
            continue

        # 1. Comando de ajuda
        if re.match(r"^/(?:start|ajuda|help)\b", texto, flags=re.IGNORECASE):
            ajuda_html = (
                "🤖 <b>Analista Quant Day Trade &amp; Swing Trade (6 Camadas) Ativo!</b>\n\n"
                "🏆 <b>Pedir Recomendações / Top Oportunidades:</b>\n"
                "• <code>/swing</code> ou pergunte: <i>\"Quero fazer swing trade, qual ação recomenda?\"</i>\n"
                "• <code>/oportunidades</code> ou pergunte: <i>\"Qual a melhor cripto pra entrar agora?\"</i>\n\n"
                "🔎 <b>Analisar um Ativo Específico (Day Trade ou Swing Trade):</b>\n"
                "• <code>/analisar PETR4 15m</code> ou <code>/analisar VALE3 1d</code>\n"
                "• Ou pergunte naturalmente: <i>\"O que acha de ITUB4 para swing trade?\"</i>, <i>\"Como está o Bitcoin?\"</i>"
            )
            enviar_telegram_html(ajuda_html, chat_id=chat_id_msg, reply_to_message_id=msg_id)
            continue

        # 2. Verifica primeiro se o usuário mencionou um ativo específico
        ativo_pedido = identificar_ativo_na_pergunta(texto, bot_username=bot_username)

        # 3. Se NÃO mencionou um ativo específico, verifica se pediu recomendação/calls de Swing Trade ou Day Trade
        if not ativo_pedido:
            pedido_rec = identificar_pedido_recomendacao_ranking(texto, bot_username=bot_username)
            if pedido_rec:
                modalidade_rec, segmento_rec = pedido_rec
                logger.info(
                    "🏆 Pedido de recomendação detectado no chat (%s): modalidade=%s | segmento=%s | texto='%s'",
                    chat_id_msg,
                    modalidade_rec,
                    segmento_rec,
                    texto[:80],
                )
                top_oportunidades = escanear_melhores_oportunidades(
                    modalidade=modalidade_rec,
                    segmento=segmento_rec,
                    top_n=3,
                )
                painel_html = formatar_painel_recomendacoes_html(
                    modalidade=modalidade_rec,
                    segmento=segmento_rec,
                    oportunidades=top_oportunidades,
                )
                if enviar_telegram_html(painel_html, chat_id=chat_id_msg, reply_to_message_id=msg_id):
                    respondidas += 1
                    logger.info("✅ Painel Top 3 (%s - %s) enviado no chat.", modalidade_rec, segmento_rec)
            continue

        # 4. Análise de ativo específico (em modo Day Trade ou Swing Trade conforme a pergunta)
        modalidade_pedido = detectar_modalidade_na_mensagem(texto)
        intervalo_pedido = extrair_intervalo_da_mensagem(texto)
        eh_swing_ativo = modalidade_pedido == "SWING TRADE" or intervalo_pedido in ("4h", "1d", "1w")
        mult_atr_pedido = ATR_MULTIPLIER_SWING if eh_swing_ativo else ATR_MULTIPLIER
        mod_final = "SWING TRADE" if eh_swing_ativo else "DAY TRADE"

        logger.info(
            "💬 Pergunta detectada no chat (%s): ativo=%s (%s - %s) | texto='%s'",
            chat_id_msg,
            ativo_pedido.simbolo,
            intervalo_pedido,
            mod_final,
            texto[:80],
        )

        snap = extrair_dados_tradingview(ativo_pedido, intervalo_pedido, enriquecer_mtf=True)
        if not snap:
            aviso_erro = (
                f"⚠️ Não consegui localizar dados em tempo real no TradingView para "
                f"<code>{html.escape(ativo_pedido.simbolo)}</code>. Verifique o código do ticker "
                f"(ex.: <code>PETR4</code>, <code>VALE3</code>, <code>BTCUSDT</code>, <code>SOLUSDT</code>)."
            )
            enviar_telegram_html(aviso_erro, chat_id=chat_id_msg, reply_to_message_id=msg_id)
            continue

        direcao_vies, status_vies = diagnosticar_direcao_sob_demanda(snap)
        plano = calcular_gestao_risco(
            preco_entrada=snap.preco_atual,
            atr=snap.atr,
            direcao=direcao_vies,
            multiplicador_atr=mult_atr_pedido,
            casas_decimais=snap.casas_decimais,
            modalidade=mod_final,
            moeda=snap.moeda,
        )

        auditoria = auditar_sinal_com_gemini(snap, plano, pergunta_usuario=texto)
        if not auditoria:
            auditoria = gerar_auditoria_quant_fallback(snap, plano)

        resposta_html = formatar_resposta_analise_chat_html(snap, plano, auditoria, status_vies)
        if enviar_telegram_html(resposta_html, chat_id=chat_id_msg, reply_to_message_id=msg_id):
            respondidas += 1
            logger.info("✅ Resposta enviada no chat para %s (%s - %s).", ativo_pedido.simbolo, intervalo_pedido, mod_final)

    if mudou_offset:
        salvar_historico(historico)

    return respondidas


def executar_varredura_sinais(historico: Dict[str, Any]) -> int:
    b3_aberta = pregao_b3_aberto()
    enviados = 0

    logger.info(
        "Iniciando varredura Quant Day Trade 6 Camadas (Timeframe=%s | Pregão B3=%s | ATR=%.1fx | Nota Mín=%d)...",
        TIMEFRAME_PADRAO,
        "ABERTO" if b3_aberta else "FECHADO (somente Cripto 24/7)",
        ATR_MULTIPLIER,
        NOTA_MINIMA_IA,
    )

    for ativo in WATCHLIST_PADRAO:
        if enviados >= MAX_SINAIS_POR_CICLO:
            break
        if ativo.apenas_pregao_b3 and not b3_aberta:
            continue

        snap = extrair_dados_tradingview(ativo, TIMEFRAME_PADRAO, enriquecer_mtf=True)
        if not snap:
            continue

        aprovado_pre, motivo_pre = pre_filtrar_confluencia_quant(snap)
        if not aprovado_pre:
            logger.info("[%s] Pré-filtro 6 camadas vetou: %s", ativo.simbolo, motivo_pre)
            continue

        if sinal_em_cooldown(snap.simbolo, snap.direcao_preliminar, snap.intervalo, historico):
            logger.info("[%s] Sinal %s em cooldown no histórico; pulando.", snap.simbolo, snap.direcao_preliminar)
            continue

        plano = calcular_gestao_risco(
            preco_entrada=snap.preco_atual,
            atr=snap.atr,
            direcao=snap.direcao_preliminar,
            multiplicador_atr=ATR_MULTIPLIER,
            casas_decimais=snap.casas_decimais,
            modalidade="DAY TRADE",
            moeda=snap.moeda,
        )

        auditoria = auditar_sinal_com_gemini(snap, plano)
        if not auditoria:
            if snap.score_quant_local >= 85:
                auditoria = gerar_auditoria_quant_fallback(snap, plano)
                logger.info(
                    "[%s] Gemini em 503; sinal de alta confluência (Score %d/100) auditado pelo Motor Quant Institucional.",
                    snap.simbolo,
                    snap.score_quant_local,
                )
            else:
                logger.warning("[%s] Auditoria IA indisponível; sinal descartado por segurança.", snap.simbolo)
                continue

        if not auditoria.aprovado:
            logger.info(
                "[%s] Vetado na auditoria (%s | Nota %d/100): %s",
                snap.simbolo,
                auditoria.modelo_utilizado,
                auditoria.nota_confluencia,
                auditoria.motivo_veto,
            )
            continue

        msg_html = formatar_mensagem_sinal_html(snap, plano, auditoria)
        if enviar_telegram_html(msg_html):
            registrar_sinal_historico(historico, snap, plano, auditoria)
            salvar_historico(historico)
            enviados += 1
            logger.info(
                "✅ Sinal disparado [%s | %s | Score Quant %d/100 | IA %d/100 | Entrada %s | Stop %s | Break-Even %s]",
                snap.simbolo,
                plano.direcao,
                snap.score_quant_local,
                auditoria.nota_confluencia,
                plano.entrada,
                plano.stop_loss,
                plano.gatilho_breakeven,
            )

    logger.info("Varredura finalizada: %d sinal(is) aprovado(s) e enviado(s).", enviados)
    return enviados


def executar_ciclo_daytrade() -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
        logger.critical("Configure TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID e GEMINI_API_KEY nos Secrets.")
        sys.exit(1)

    historico = carregar_historico()
    bot_username = obter_username_bot()

    # 1. Responde imediatamente a quaisquer perguntas ou pedidos de Swing Trade pendentes no chat
    responder_perguntas_chat(historico, bot_username=bot_username)

    # 2. Executa a varredura automática da Watchlist com as 6 camadas institucionais
    enviados = executar_varredura_sinais(historico)

    # 3. Se configurado para escuta contínua no chat durante a janela do GitHub Actions
    segundos_escuta = int(os.environ.get("CHAT_LISTEN_SECONDS", "0"))
    if segundos_escuta > 0:
        logger.info("💬 Escuta ativa do chat iniciada por %d segundos (polling a cada 8s)...", segundos_escuta)
        fim_ts = time.time() + segundos_escuta
        while time.time() < fim_ts:
            responder_perguntas_chat(historico, bot_username=bot_username)
            time.sleep(8)

    return enviados


if __name__ == "__main__":
    executar_ciclo_daytrade()
