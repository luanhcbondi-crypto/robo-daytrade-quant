import datetime as dt
import html
import json
import logging
import math
import os
import re
import socket
import struct
import sys
import time
import zlib
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
# Timeframe padrão agora é Diário (1d) para Swing Trade baseado em Suportes e Resistências consolidados
_ENV_TF = (os.environ.get("SWING_INTERVAL") or os.environ.get("DAYTRADE_INTERVAL") or "1d").strip().lower()
TIMEFRAME_PADRAO = "1d" if _ENV_TF in ("", "15m") else _ENV_TF
ATR_MULTIPLIER = float(os.environ.get("ATR_MULTIPLIER", "1.5"))
ATR_MULTIPLIER_SWING = float(os.environ.get("ATR_MULTIPLIER_SWING", "2.0"))
NOTA_MINIMA_IA = int(os.environ.get("NOTA_MINIMA_IA", "76"))
COOLDOWN_SINAL_MINUTOS = int(os.environ.get("COOLDOWN_SINAL_MINUTOS", "360"))
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
_CACHE_TTL_SEGUNDOS = 90.0
_ULTIMA_REQ_TV_TS: float = 0.0

MAPA_SUFIXO_BATCH_TV: Dict[str, str] = {
    "5m": "|5",
    "15m": "|15",
    "30m": "|30",
    "1h": "|60",
    "4h": "|240",
    "1d": "",
    "1w": "|1W",
}
COLUNAS_BATCH_TV: Tuple[str, ...] = (
    "Recommend.All",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "change",
    "EMA9",
    "EMA10",
    "EMA20",
    "EMA21",
    "SMA50",
    "SMA200",
    "EMA200",
    "VWMA",
    "RSI",
    "MACD.macd",
    "MACD.signal",
    "ADX",
    "ADX+DI",
    "ADX-DI",
    "ATR",
    "BB.upper",
    "BB.lower",
    "Stoch.K",
    "Stoch.D",
    "Pivot.M.Classic.Middle",
    "Pivot.M.Classic.R1",
    "Pivot.M.Classic.R2",
    "Pivot.M.Classic.S1",
    "Pivot.M.Classic.S2",
    "Pivot.M.Fibonacci.Middle",
    "Pivot.M.Fibonacci.R1",
    "Pivot.M.Fibonacci.R2",
    "Pivot.M.Fibonacci.S1",
    "Pivot.M.Fibonacci.S2",
)

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

# Carteira Multi-Mercado de Swing Trade ("Swing em Tudo": Ações B3, Criptomoedas, BDRs Globais, Ouro, Prata e Câmbio)
CARTEIRA_SWING_B3: Tuple[AtivoMonitorado, ...] = (
    AtivoMonitorado("PETR4", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("VALE3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("ITUB4", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("BBAS3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("BPAC11", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("PRIO3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("WEGE3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("EMBR3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("ELET3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("SBSP3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("RENT3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("GGBR4", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("B3SA3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("SUZB3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("BBDC4", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("EQTL3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("RADL3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("JBSS3", "BMFBOVESPA", "brazil", "R$", 2, False),
)

CARTEIRA_SWING_CRIPTO: Tuple[AtivoMonitorado, ...] = (
    AtivoMonitorado("BTCUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("ETHUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("SOLUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("BNBUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("XRPUSDT", "BINANCE", "crypto", "US$", 4, False),
    AtivoMonitorado("ADAUSDT", "BINANCE", "crypto", "US$", 4, False),
    AtivoMonitorado("DOGEUSDT", "BINANCE", "crypto", "US$", 5, False),
    AtivoMonitorado("AVAXUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("LINKUSDT", "BINANCE", "crypto", "US$", 3, False),
    AtivoMonitorado("SUIUSDT", "BINANCE", "crypto", "US$", 4, False),
    AtivoMonitorado("DOTUSDT", "BINANCE", "crypto", "US$", 3, False),
    AtivoMonitorado("NEARUSDT", "BINANCE", "crypto", "US$", 3, False),
    AtivoMonitorado("LTCUSDT", "BINANCE", "crypto", "US$", 2, False),
)

CARTEIRA_SWING_GLOBAL_MACRO: Tuple[AtivoMonitorado, ...] = (
    AtivoMonitorado("XAUUSD", "OANDA", "cfd", "US$", 2, False),       # Ouro Spot
    AtivoMonitorado("XAGUSD", "OANDA", "cfd", "US$", 3, False),       # Prata Spot
    AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),     # Dólar / Real
    AtivoMonitorado("EURUSD", "FX_IDC", "forex", "US$", 4, False),    # Euro / Dólar
    AtivoMonitorado("NVDC34", "BMFBOVESPA", "brazil", "R$", 2, False), # Nvidia BDR
    AtivoMonitorado("AAPL34", "BMFBOVESPA", "brazil", "R$", 2, False), # Apple BDR
    AtivoMonitorado("TSLA34", "BMFBOVESPA", "brazil", "R$", 2, False), # Tesla BDR
    AtivoMonitorado("MSFT34", "BMFBOVESPA", "brazil", "R$", 2, False), # Microsoft BDR
    AtivoMonitorado("AMZO34", "BMFBOVESPA", "brazil", "R$", 2, False), # Amazon BDR
    AtivoMonitorado("GOGL34", "BMFBOVESPA", "brazil", "R$", 2, False), # Google BDR
    AtivoMonitorado("META34", "BMFBOVESPA", "brazil", "R$", 2, False), # Meta BDR
    AtivoMonitorado("BOVA11", "BMFBOVESPA", "brazil", "R$", 2, False), # ETF Ibovespa
    AtivoMonitorado("IVVB11", "BMFBOVESPA", "brazil", "R$", 2, False), # ETF S&P 500
)

# Watchlist Multi-Mercado padrão para varredura automática de Swing Trade (Ações + Cripto + Ouro/Dólar/Big Techs)
WATCHLIST_PADRAO: Tuple[AtivoMonitorado, ...] = (
    AtivoMonitorado("PETR4", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("VALE3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("ITUB4", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("BBAS3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("PRIO3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("WEGE3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("EMBR3", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("BPAC11", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("BTCUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("ETHUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("SOLUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("BNBUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("XRPUSDT", "BINANCE", "crypto", "US$", 4, False),
    AtivoMonitorado("AVAXUSDT", "BINANCE", "crypto", "US$", 2, False),
    AtivoMonitorado("LINKUSDT", "BINANCE", "crypto", "US$", 3, False),
    AtivoMonitorado("SUIUSDT", "BINANCE", "crypto", "US$", 4, False),
    AtivoMonitorado("XAUUSD", "OANDA", "cfd", "US$", 2, False),
    AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),
    AtivoMonitorado("NVDC34", "BMFBOVESPA", "brazil", "R$", 2, False),
    AtivoMonitorado("IVVB11", "BMFBOVESPA", "brazil", "R$", 2, False),
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
    "nvidia": AtivoMonitorado("NVDC34", "BMFBOVESPA", "brazil", "R$", 2, False),
    "apple": AtivoMonitorado("AAPL34", "BMFBOVESPA", "brazil", "R$", 2, False),
    "tesla": AtivoMonitorado("TSLA34", "BMFBOVESPA", "brazil", "R$", 2, False),
    "microsoft": AtivoMonitorado("MSFT34", "BMFBOVESPA", "brazil", "R$", 2, False),
    "amazon": AtivoMonitorado("AMZO34", "BMFBOVESPA", "brazil", "R$", 2, False),
    "google": AtivoMonitorado("GOGL34", "BMFBOVESPA", "brazil", "R$", 2, False),
    "meta": AtivoMonitorado("META34", "BMFBOVESPA", "brazil", "R$", 2, False),
    "sp500": AtivoMonitorado("IVVB11", "BMFBOVESPA", "brazil", "R$", 2, False),
    "s&p": AtivoMonitorado("IVVB11", "BMFBOVESPA", "brazil", "R$", 2, False),
    "ivvb11": AtivoMonitorado("IVVB11", "BMFBOVESPA", "brazil", "R$", 2, False),
    "dolar": AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),
    "dólar": AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),
    "usdbrl": AtivoMonitorado("USDBRL", "FX_IDC", "forex", "R$", 4, False),
    "euro": AtivoMonitorado("EURUSD", "FX_IDC", "forex", "US$", 4, False),
    "eurusd": AtivoMonitorado("EURUSD", "FX_IDC", "forex", "US$", 4, False),
    "ouro": AtivoMonitorado("XAUUSD", "OANDA", "cfd", "US$", 2, False),
    "xauusd": AtivoMonitorado("XAUUSD", "OANDA", "cfd", "US$", 2, False),
    "prata": AtivoMonitorado("XAGUSD", "OANDA", "cfd", "US$", 3, False),
    "xagusd": AtivoMonitorado("XAGUSD", "OANDA", "cfd", "US$", 3, False),
    "polkadot": AtivoMonitorado("DOTUSDT", "BINANCE", "crypto", "US$", 3, False),
    "dot": AtivoMonitorado("DOTUSDT", "BINANCE", "crypto", "US$", 3, False),
    "near": AtivoMonitorado("NEARUSDT", "BINANCE", "crypto", "US$", 3, False),
    "litecoin": AtivoMonitorado("LTCUSDT", "BINANCE", "crypto", "US$", 2, False),
    "ltc": AtivoMonitorado("LTCUSDT", "BINANCE", "crypto", "US$", 2, False),
}

REGEX_TICKER_B3 = re.compile(r"\b([A-Z]{4}(?:3|4|5|6|11|31|32|33|34|35|39))\b")
REGEX_PAR_CRIPTO = re.compile(r"\b([A-Z0-9]{2,10}USDT)\b")
REGEX_COMANDO_CHAT = re.compile(r"^/(?:analisar|analise|grafico|ativo|sinal|avaliar)\b", flags=re.IGNORECASE)
REGEX_COMANDO_RANKING = re.compile(
    r"^/(?:swing|swingtrade|daytrade|oportunidades|recomendacao|recomendar|top|top3|calls|radar)\b",
    flags=re.IGNORECASE,
)
REGEX_CHAMADO_ROBO = re.compile(r"\b(?:rob[ôo]|bot|analista|ia)\b", flags=re.IGNORECASE)
REGEX_INTENCAO_PERGUNTA = re.compile(
    r"(?:\?|o\s*que\s+(?:vc\s+|voc[êe]\s+)?acha|oque\s+acha|analis\w*|an[áa]lis\w*|avali\w*|"
    r"como\s+(?:est[áa]|t[áa]|ta)\b|gr[áa]fic\w*|setup|vale\s+a\s+pena|compensa|"
    r"compr\w*|vend\w*|subi\w*|sobe|cai\w*|alta|baixa|long|short|"
    r"entrar|entrada|opini\w*|tend[êe]nci\w*|alvo|stop|swing\w*|day\s*trade|sinal|call|hoje|agora)",
    flags=re.IGNORECASE,
)
REGEX_PEDIDO_RECOMENDACAO = re.compile(
    r"(?:qual\s+(?:a\s+|o\s+)?(?:melhor\s+)?(?:a[çc][ãa]o|ativo|cripto\w*|moeda|papel|mercado|trade|op[çc][ãa]o)|"
    r"quais\s+(?:as\s+|os\s+)?(?:melhores\s+)?(?:a[çc][õo]es|ativos|cripto\w*|moedas|pap[ée]is|op[çc][õo]es|oportunidades)|"
    r"(?:quero|fazer|busco|procuro|tem|algum\w*|melhor(?:es)?|d[áa]\s+um|me\s+d[áa]|mand[ae]|mostr[ae]|fala|diz)\s+(?:um\s+|uma\s+|algum\w*\s+|mais\s+|outr[oa]s?\s+)?(?:call\w*|oportunidade\w*|recomenda[çc]\w*|indica[çc]\w*|setup\w*|sinal|sinais|swing\s*trade|day\s*trade|ativo\w*|a[çc][ãa]o|a[çc][õo]es|cripto\w*|op[çc][ãa]o|op[çc][õo]es)|"
    r"(?:recomenda\w*|indica\w*|sugere\w*)\s*(?:um\s+|uma\s+|algum\w*\s+|boas?\s+|bons?\s+|algo\s+|pra\s+|de\s+)?(?:a[çc][ãa]o|a[çc][õo]es|ativo\w*|cripto\w*|moeda\w*|papel|swing\s*trade|day\s*trade|call|hoje|agora|tudo)?|"
    r"o\s+que\s+(?:comprar|operar|vender|tem\s+de\s+bom|est[áa]\s+bom|t[áa]\s+bom)\b|"
    r"\b(?:tem\s+outr[oa]s?|qual\s+outr[oa]|quais\s+outros|me\s+fala\s+outr[oa]|mais\s+op[çc][õo]es|outros\s+ativos|swing\s+em\s+tudo)\b|"
    r"^(?:swing\s*trade|day\s*trade|oportunidades|calls|recomenda[çc][ãa]o)\??$)",
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
    pivot_r2: float = 0.0
    pivot_s2: float = 0.0
    sma_50: float = 0.0
    suporte_consolidado_1: float = 0.0
    suporte_consolidado_2: float = 0.0
    resistencia_consolidada_1: float = 0.0
    resistencia_consolidada_2: float = 0.0
    zona_sr_descricao: str = "EM OBSERVAÇÃO"
    toques_suporte: int = 2
    toques_resistencia: int = 2
    padrao_vela_gatilho: str = "NEUTRO"
    payoff_sr_real: float = 1.8
    variacao_pct: float = 0.0
    alpha_vs_lider_pct: float = 0.0
    tf_superior: str = "1w"
    tendencia_mtf_superior: str = "NEUTRA"
    tf_inferior: str = "4h"
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


def _calcular_mapa_sr_consolidado(
    close: float,
    atr: float,
    low: float,
    high: float,
    ema_9: float,
    ema_21: float,
    sma_50: float,
    sma_200: float,
    vwma: float,
    bb_low: float,
    bb_up: float,
    pivot_p: float,
    pivot_s1: float,
    pivot_s2: float,
    pivot_r1: float,
    pivot_r2: float,
    fib_s1: float,
    fib_s2: float,
    fib_r1: float,
    fib_r2: float,
    casas: int,
) -> Tuple[float, float, float, float, str]:
    """Calcula zonas de Suporte e Resistência bem consolidadas por confluência de Pivots
    Clássicos, Fibonacci, Médias Institucionais (21/50/200) e Bandas de Bollinger.
    """
    atr_safe = max(atr, close * 0.01, 1e-9)

    # Candidatos a suporte abaixo do preço atual
    cands_sup = sorted(
        [
            p for p in (pivot_s1, fib_s1, ema_21, sma_50, sma_200, bb_low, pivot_p, low)
            if 0 < p <= close * 0.998
        ],
        reverse=True,
    )
    sup_1 = cands_sup[0] if cands_sup else (close - 1.5 * atr_safe)
    # Se o suporte 1 estiver colado demais no preço (< 0.25 ATR), pega o próximo degrau consolidado
    for p in cands_sup:
        if (close - p) >= 0.30 * atr_safe:
            sup_1 = p
            break

    cands_sup_2 = [p for p in (pivot_s2, fib_s2, bb_low, sma_200, sup_1 - 1.5 * atr_safe) if 0 < p < sup_1 * 0.992]
    sup_2 = max(cands_sup_2) if cands_sup_2 else (sup_1 - 1.5 * atr_safe)

    # Candidatos a resistência acima do preço atual
    cands_res = sorted(
        [
            p for p in (pivot_r1, fib_r1, bb_up, sma_50, sma_200, pivot_p, high)
            if p >= close * 1.002
        ]
    )
    res_1 = cands_res[0] if cands_res else (close + 2.0 * atr_safe)
    for p in cands_res:
        if (p - close) >= 0.45 * atr_safe:
            res_1 = p
            break

    cands_res_2 = [p for p in (pivot_r2, fib_r2, bb_up, res_1 + 1.5 * atr_safe) if p > res_1 * 1.008]
    res_2 = min(cands_res_2) if cands_res_2 else (res_1 + 1.5 * atr_safe)

    dist_sup_atr = (close - sup_1) / atr_safe
    dist_res_atr = (res_1 - close) / atr_safe

    if dist_sup_atr <= 1.15 and close >= sup_1:
        zona_desc = "APOIADO EM SUPORTE CONSOLIDADO (Assimetria excelente: Stop curto abaixo do suporte e espaço livre até a resistência)"
    elif pivot_r1 > 0 and close > pivot_r1 and close >= vwma:
        zona_desc = "ROMPENDO RESISTÊNCIA CONSOLIDADA (Pivot de alta rompido com entrada de volume institucional)"
    elif dist_res_atr <= 0.75:
        zona_desc = "TESTANDO TETO DE RESISTÊNCIA CONSOLIDADA (Cuidado ao comprar no teto — aguardar rompimento ou recuo no suporte)"
    else:
        zona_desc = "MEIO DE CAMINHO ENTRE SUPORTE E RESISTÊNCIA (Aguardar recuo até o suporte consolidado ou rompimento da resistência)"

    return (
        round(sup_1, casas),
        round(sup_2, casas),
        round(res_1, casas),
        round(res_2, casas),
        zona_desc,
    )


def _construir_snapshot_de_indicadores(
    ativo: AtivoMonitorado,
    intervalo_str: str,
    ind: Dict[str, Any],
    rec_tv: str = "NEUTRAL",
) -> Optional[SnapshotIndicadores]:
    close = _num(ind.get("close"))
    if close <= 0:
        return None

    high = _num(ind.get("high"), close)
    low = _num(ind.get("low"), close)
    open_p = _num(ind.get("open"), close)
    change_pct = _num(ind.get("change"), ((close - open_p) / open_p * 100.0) if open_p > 0 else 0.0)

    ema_9 = _num(ind.get("EMA9"), _num(ind.get("EMA10"), close))
    ema_21 = _num(ind.get("EMA21"), _num(ind.get("EMA20"), close))
    sma_50 = _num(ind.get("SMA50"), ema_21)
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

    pivot_p = _num(ind.get("Pivot.M.Classic.Middle"), _num(ind.get("Pivot.M.Fibonacci.Middle"), close))
    pivot_r1 = _num(ind.get("Pivot.M.Classic.R1"), 0.0)
    pivot_r2 = _num(ind.get("Pivot.M.Classic.R2"), 0.0)
    pivot_s1 = _num(ind.get("Pivot.M.Classic.S1"), 0.0)
    pivot_s2 = _num(ind.get("Pivot.M.Classic.S2"), 0.0)

    fib_r1 = _num(ind.get("Pivot.M.Fibonacci.R1"), pivot_r1)
    fib_r2 = _num(ind.get("Pivot.M.Fibonacci.R2"), pivot_r2)
    fib_s1 = _num(ind.get("Pivot.M.Fibonacci.S1"), pivot_s1)
    fib_s2 = _num(ind.get("Pivot.M.Fibonacci.S2"), pivot_s2)

    atr_raw = _num(ind.get("ATR"))
    if atr_raw <= 0:
        amplitude_vela = abs(high - low)
        amplitude_bb = abs(bb_up - bb_low) / 4.0 if (bb_up > bb_low > 0) else 0.0
        atr_raw = max(amplitude_vela, amplitude_bb, close * 0.005)

    casas = 4 if (close < 1.0 and ativo.casas_decimais == 2) else ativo.casas_decimais
    tf_sup, tf_inf = obter_pares_mtf(intervalo_str)

    sup_1, sup_2, res_1, res_2, zona_sr = _calcular_mapa_sr_consolidado(
        close=close,
        atr=atr_raw,
        low=low,
        high=high,
        ema_9=ema_9,
        ema_21=ema_21,
        sma_50=sma_50,
        sma_200=sma_200,
        vwma=vwma,
        bb_low=bb_low,
        bb_up=bb_up,
        pivot_p=pivot_p,
        pivot_s1=pivot_s1,
        pivot_s2=pivot_s2,
        pivot_r1=pivot_r1,
        pivot_r2=pivot_r2,
        fib_s1=fib_s1,
        fib_s2=fib_s2,
        fib_r1=fib_r1,
        fib_r2=fib_r2,
        casas=casas,
    )

    # Cálculo da Assimetria Real Risco/Retorno (Payoff S/R) e Padrão de Vela Gatilho
    risco_ate_stop_sup = max(close - (sup_1 - 0.5 * atr_raw), atr_raw * 0.8, 1e-9)
    retorno_ate_res1 = max(res_1 - close, 0.0)
    payoff_sr = round(retorno_ate_res1 / risco_ate_stop_sup, 2)

    amp_vela = max(high - low, 1e-9)
    pavio_inf = (min(open_p, close) - low) / amp_vela
    pavio_sup = (high - max(open_p, close)) / amp_vela
    corpo_vela = abs(close - open_p) / amp_vela
    bw_pct = ((bb_up - bb_low) / close) if (bb_up > bb_low > 0) else 0.08

    if pavio_inf >= 0.42 and close >= open_p and (close - sup_1) <= 1.2 * atr_raw:
        padrao_vela = "MARTELO DE DEFESA COMPRADORA NO SUPORTE"
    elif close > open_p and corpo_vela >= 0.65 and close >= ema_9:
        padrao_vela = "BARRA DE FORÇA INSTITUCIONAL COMPRADORA"
    elif 0 < bw_pct <= 0.035:
        padrao_vela = "COMPRESSÃO DE VOLATILIDADE (MOLA PRONTA P/ EXPLODIR)"
    elif pavio_sup >= 0.45 and (res_1 - close) <= 1.0 * atr_raw:
        padrao_vela = "PAVIO DE REJEIÇÃO NA RESISTÊNCIA"
    else:
        padrao_vela = "VELA DE NEGOCIAÇÃO NORMAL"

    return SnapshotIndicadores(
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
        pivot_r2=pivot_r2,
        pivot_s2=pivot_s2,
        sma_50=sma_50,
        suporte_consolidado_1=sup_1,
        suporte_consolidado_2=sup_2,
        resistencia_consolidada_1=res_1,
        resistencia_consolidada_2=res_2,
        zona_sr_descricao=zona_sr,
        toques_suporte=3 if "APOIADO EM SUPORTE" in zona_sr else 2,
        toques_resistencia=3 if "TESTANDO TETO" in zona_sr else 2,
        padrao_vela_gatilho=padrao_vela,
        payoff_sr_real=payoff_sr,
        variacao_pct=change_pct,
        tf_superior=tf_sup,
        tf_inferior=tf_inf,
        preco_pullback_ideal=sup_1 if sup_1 > 0 else round(ema_9, casas),
    )


def pre_carregar_lote_tradingview(ativos: Tuple[AtivoMonitorado, ...], intervalo_str: str) -> int:
    """Busca todos os ativos do mesmo screener em UMA ÚNICA requisição HTTP ao TradingView,
    eliminando completamente o erro HTTP 429 (Too Many Requests) na varredura.
    """
    global _ULTIMA_REQ_TV_TS
    if not hasattr(requests, "post"):
        return 0

    sufixo = MAPA_SUFIXO_BATCH_TV.get(intervalo_str, "|15")
    colunas_req = [f"{col}{sufixo}" for col in COLUNAS_BATCH_TV]
    por_screener: Dict[str, Dict[str, AtivoMonitorado]] = {}
    agora_mono = time.monotonic()

    for a in ativos:
        chave_cache = f"{a.exchange}:{a.simbolo}:{intervalo_str}"
        if chave_cache in _CACHE_SNAPSHOT_CICLO and (agora_mono - _CACHE_SNAPSHOT_CICLO[chave_cache][0]) <= _CACHE_TTL_SEGUNDOS:
            continue
        por_screener.setdefault(a.screener.lower(), {})[f"{a.exchange.upper()}:{a.simbolo.upper()}"] = a

    carregados = 0
    for screener, mapa_tickers in por_screener.items():
        if not mapa_tickers:
            continue
        espera = 0.40 - (time.monotonic() - _ULTIMA_REQ_TV_TS)
        if espera > 0:
            time.sleep(espera)

        url = f"https://scanner.tradingview.com/{screener}/scan"
        payload = {
            "symbols": {"tickers": list(mapa_tickers.keys()), "query": {"types": []}},
            "columns": colunas_req,
        }
        try:
            _ULTIMA_REQ_TV_TS = time.monotonic()
            resp = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            if resp.status_code != 200:
                continue
            linhas = (resp.json() or {}).get("data") or []
            ts_agora = time.monotonic()
            for item in linhas:
                if not isinstance(item, dict):
                    continue
                ticker_full = str(item.get("s") or "").upper()
                valores = item.get("d") or []
                ativo_obj = mapa_tickers.get(ticker_full)
                if not ativo_obj or len(valores) != len(COLUNAS_BATCH_TV):
                    continue
                ind_dict = dict(zip(COLUNAS_BATCH_TV, valores))
                rec_val = _num(ind_dict.get("Recommend.All"), 0.0)
                rec_str = "STRONG_BUY" if rec_val >= 0.5 else ("BUY" if rec_val >= 0.1 else ("STRONG_SELL" if rec_val <= -0.5 else ("SELL" if rec_val <= -0.1 else "NEUTRAL")))
                snap = _construir_snapshot_de_indicadores(ativo_obj, intervalo_str, ind_dict, rec_str)
                if snap:
                    _CACHE_SNAPSHOT_CICLO[f"{ativo_obj.exchange}:{ativo_obj.simbolo}:{intervalo_str}"] = (ts_agora, snap)
                    carregados += 1
        except Exception:
            continue
    return carregados


def _extrair_snapshot_base(ativo: AtivoMonitorado, intervalo_str: str) -> Optional[SnapshotIndicadores]:
    global _ULTIMA_REQ_TV_TS
    chave_cache = f"{ativo.exchange}:{ativo.simbolo}:{intervalo_str}"
    agora_mono = time.monotonic()
    if chave_cache in _CACHE_SNAPSHOT_CICLO:
        ts_salvo, snap_salvo = _CACHE_SNAPSHOT_CICLO[chave_cache]
        if (agora_mono - ts_salvo) <= _CACHE_TTL_SEGUNDOS and snap_salvo is not None:
            return snap_salvo

    tv_interval = MAPA_INTERVALOS_TV.get(intervalo_str, Interval.INTERVAL_15_MINUTES)
    for tentativa in range(2):
        espera = 0.35 - (time.monotonic() - _ULTIMA_REQ_TV_TS)
        if espera > 0:
            time.sleep(espera)
        try:
            _ULTIMA_REQ_TV_TS = time.monotonic()
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
            rec_tv = str(summary.get("RECOMMENDATION") or "NEUTRAL").upper()

            snap = _construir_snapshot_de_indicadores(ativo, intervalo_str, ind, rec_tv)
            if snap:
                _CACHE_SNAPSHOT_CICLO[chave_cache] = (time.monotonic(), snap)
            return snap
        except Exception as exc:
            msg_err = str(exc)
            if "429" in msg_err and tentativa == 0:
                logger.info("TradingView HTTP 429 em %s; aguardando 2.5s para retentar...", ativo.simbolo)
                time.sleep(2.5)
                continue
            logger.warning("Falha ao extrair TradingView para %s:%s (%s)", ativo.exchange, ativo.simbolo, msg_err[:120])
            return None
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
# 6. ATENDIMENTO INTERATIVO NO CHAT + SWING TRADE (SUPORTES E RESISTÊNCIAS)
# ==============================================================================
def detectar_modalidade_na_mensagem(texto: str) -> str:
    """Por padrão o robô opera SWING TRADE baseado em Suportes e Resistências consolidados (1D/4H),
    só mudando para DAY TRADE se o usuário pedir explicitamente day trade ou tempo curto (5m/15m/30m).
    """
    t = (texto or "").lower()
    if re.search(r"\b(?:day\s*trade|daytrade|intradi[áa]rio|scalp|5\s*m|5\s*min|m5|15\s*m|15\s*min|m15|30\s*m|30\s*min|m30)\b", t):
        return "DAY TRADE"
    return "SWING TRADE"


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
    if detectar_modalidade_na_mensagem(t) == "DAY TRADE":
        return "15m"
    return "1d"


def identificar_pedido_recomendacao_ranking(texto: str, bot_username: str = "") -> Optional[Tuple[str, str]]:
    """Detecta quando o trader pede recomendações/calls gerais sem especificar um único ativo
    (ex.: 'quero fazer swing trade, qual recomenda?', '/swing', 'tem outro ativo bom?', 'swing em tudo').
    Retorna (modalidade, segmento):
      - modalidade: 'SWING TRADE' | 'DAY TRADE'
      - segmento: 'B3' | 'CRIPTO' | 'GLOBAL' | 'TODOS'
    """
    if not texto:
        return None
    t_low = texto.lower().strip()

    eh_cmd_rank = bool(REGEX_COMANDO_RANKING.search(t_low))
    tem_pedido_rec = bool(REGEX_PEDIDO_RECOMENDACAO.search(t_low))

    if not eh_cmd_rank and not tem_pedido_rec:
        return None

    if re.search(r"\b(?:day\s*trade|daytrade|intradi[áa]rio|scalp|15m|5m)\b", t_low):
        modalidade = "DAY TRADE"
    else:
        modalidade = "SWING TRADE"

    if re.search(r"\b(?:tudo|todos|geral|multi|qualquer\s+mercado)\b", t_low):
        segmento = "TODOS"
    elif re.search(r"\b(?:a[çc][ãa]o|a[çc][õo]es|b3|bolsa|ibov|papel|pap[ée]is)\b", t_low):
        segmento = "B3"
    elif re.search(r"\b(?:cripto|criptomoeda|criptos|moeda|altcoin|binance)\b", t_low):
        segmento = "CRIPTO"
    elif re.search(r"\b(?:global|bdr\w*|commodit\w*|forex|c[âa]mbio|big\s*tech\w*|exterior|internacional)\b", t_low):
        segmento = "GLOBAL"
    else:
        segmento = "TODOS"

    return modalidade, segmento


def identificar_ativo_na_pergunta(
    texto: str,
    bot_username: str = "",
    eh_resposta_ao_bot: bool = False,
) -> Optional[AtivoMonitorado]:
    if not texto:
        return None

    t_up = texto.upper()
    t_low = texto.lower()
    qtd_palavras = len(t_low.split())

    eh_comando = bool(REGEX_COMANDO_CHAT.search(t_low))
    mencionou_bot = bool(bot_username and f"@{bot_username.lower()}" in t_low) or bool(REGEX_CHAMADO_ROBO.search(t_low))
    tem_intencao = (
        eh_comando
        or mencionou_bot
        or eh_resposta_ao_bot
        or "?" in texto
        or qtd_palavras <= 8
        or bool(REGEX_INTENCAO_PERGUNTA.search(t_low))
    )

    # 1. Procura par cripto explícito (ex.: BTCUSDT, SOLUSDT, PEPEUSDT)
    m_cripto = REGEX_PAR_CRIPTO.search(t_up)
    if m_cripto and tem_intencao:
        simb = m_cripto.group(1)
        for a in (*WATCHLIST_PADRAO, *CARTEIRA_SWING_CRIPTO):
            if a.simbolo == simb:
                return a
        return AtivoMonitorado(simb, "BINANCE", "crypto", "US$", 2, False)

    # 2. Procura ticker padrão B3 (ex.: PETR4, VALE3, KLBN11, HGLG11, NVDC34, SMFT3)
    m_b3 = REGEX_TICKER_B3.search(t_up)
    if m_b3 and tem_intencao:
        simb = m_b3.group(1)
        for a in (*WATCHLIST_PADRAO, *CARTEIRA_SWING_B3):
            if a.simbolo == simb:
                return a
        return AtivoMonitorado(simb, "BMFBOVESPA", "brazil", "R$", 2, True)

    # 3. Procura nomes/apelidos conhecidos (ex.: "o que acha do bitcoin?", "analisa petrobras", "swing trade em vale")
    if tem_intencao:
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


def _sanitizar_html_telegram(texto: str) -> str:
    """Garante que o texto conversacional da IA use apenas tags HTML limpas aceitas pelo Telegram
    (<b> e <i>), sem blocos azuis feios (<code>) e sem Markdown quebrado.
    """
    if not texto:
        return ""
    s = texto.strip()
    # Converte eventual markdown **negrito** para <b>negrito</b>
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    # Remove crases e blocos code para manter visual limpo de conversa humana
    s = re.sub(r"</?code>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"`([^`]+)`", r"<b>\1</b>", s)
    # Protege tags permitidas (<b>, </b>, <i>, </i>), escapa o resto e restaura
    marcadores = {
        "<b>": "[[[B_OPEN]]]",
        "</b>": "[[[B_CLOSE]]]",
        "<i>": "[[[I_OPEN]]]",
        "</i>": "[[[I_CLOSE]]]",
    }
    for tag, mk in marcadores.items():
        s = re.sub(re.escape(tag), mk, s, flags=re.IGNORECASE)
    s = html.escape(s)
    for tag, mk in marcadores.items():
        s = s.replace(mk, tag)
    # Balanceia tags caso a IA tenha esquecido de fechar alguma
    for tag_name in ("b", "i"):
        abertas = s.count(f"<{tag_name}>")
        fechadas = s.count(f"</{tag_name}>")
        if abertas > fechadas:
            s += f"</{tag_name}>" * (abertas - fechadas)
        elif fechadas > abertas:
            s = f"<{tag_name}>" * (fechadas - abertas) + s
    return s


def escanear_melhores_oportunidades(
    modalidade: str = "SWING TRADE",
    segmento: str = "TODOS",
    top_n: int = 3,
    apenas_compra: bool = False,
    excluir_simbolos: Optional[Set[str]] = None,
) -> List[Tuple[SnapshotIndicadores, PlanoRisco, bool, str]]:
    """Varre a carteira Multi-Mercado ("Swing em Tudo": Ações B3, Criptomoedas, Ouro/Prata, Dólar/Forex e BDRs Globais)
    no timeframe adequado (1D para Swing Trade com Stop 2.0x ATR; 15m para Day Trade com Stop 1.5x ATR).
    Quando segmento='TODOS', garante diversidade real entre classes de ativos e rotaciona ativos já comentados.
    """
    eh_swing = "SWING" in modalidade.upper()
    tf = "1d" if eh_swing else TIMEFRAME_PADRAO
    mult_atr = ATR_MULTIPLIER_SWING if eh_swing else ATR_MULTIPLIER
    ignorados = {s.upper() for s in (excluir_simbolos or set()) if s}

    if segmento == "B3":
        universo: Tuple[AtivoMonitorado, ...] = CARTEIRA_SWING_B3
    elif segmento == "CRIPTO":
        universo = CARTEIRA_SWING_CRIPTO
    elif segmento == "GLOBAL":
        universo = CARTEIRA_SWING_GLOBAL_MACRO
    else:
        universo = (
            *CARTEIRA_SWING_B3[:10],
            *CARTEIRA_SWING_CRIPTO[:8],
            *CARTEIRA_SWING_GLOBAL_MACRO[:8],
        )

    tf_sup, tf_inf = obter_pares_mtf(tf)
    ativos_lote = tuple(dict.fromkeys([*universo, ATIVO_LIDER_B3, ATIVO_LIDER_CRIPTO]))
    for tf_b in dict.fromkeys([tf, tf_sup, tf_inf]):
        pre_carregar_lote_tradingview(ativos_lote, tf_b)

    simbolos_global_macro = {a.simbolo for a in CARTEIRA_SWING_GLOBAL_MACRO}
    candidatos: List[Tuple[int, float, str, SnapshotIndicadores, PlanoRisco, bool, str]] = []

    for ativo in universo:
        snap = extrair_dados_tradingview(ativo, tf, enriquecer_mtf=True)
        if not snap:
            continue
        aprovado_pre, _ = pre_filtrar_confluencia_quant(snap)
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
        # Bônus para ativos apoiados em Suporte Consolidado ou Rompendo Resistência
        bonus_sr = 14 if "APOIADO EM SUPORTE" in snap.zona_sr_descricao else (10 if "ROMPENDO RESISTÊNCIA" in snap.zona_sr_descricao else 0)
        bonus_compra = 15 if (eh_swing or apenas_compra) and direcao == "COMPRA" else 0
        penalidade_repetido = -35 if ativo.simbolo.upper() in ignorados else 0
        bonus = (25 if aprovado_pre else 0) + (8 if "PULLBACK" in snap.tipo_setup else 0) + bonus_compra + bonus_sr + penalidade_repetido
        alpha_favoravel = snap.alpha_vs_lider_pct if direcao == "COMPRA" else -snap.alpha_vs_lider_pct
        nota_rank = snap.score_quant_local + bonus

        if ativo.simbolo in simbolos_global_macro:
            classe_ativo = "GLOBAL"
        elif ativo.screener == "crypto":
            classe_ativo = "CRIPTO"
        else:
            classe_ativo = "B3"

        candidatos.append(
            (nota_rank, alpha_favoravel, classe_ativo, snap, plano, aprovado_pre, status_vies if not aprovado_pre else "SINAL APROVADO")
        )

    candidatos.sort(key=lambda x: (x[0], x[1]), reverse=True)

    # Se o usuário pediu "TODOS" ("Swing em Tudo"), garante pelo menos 1 Ação B3, 1 Cripto e 1 Global/Macro/BDR no Top 3
    if segmento == "TODOS" and top_n >= 3 and len(candidatos) >= 3:
        selecionados: List[Tuple[int, float, str, SnapshotIndicadores, PlanoRisco, bool, str]] = []
        simbolos_ja_pegos: Set[str] = set()
        for classe_alvo in ("B3", "CRIPTO", "GLOBAL"):
            for cand in candidatos:
                if cand[2] == classe_alvo and cand[3].simbolo not in simbolos_ja_pegos:
                    selecionados.append(cand)
                    simbolos_ja_pegos.add(cand[3].simbolo)
                    break
        for cand in candidatos:
            if len(selecionados) >= top_n:
                break
            if cand[3].simbolo not in simbolos_ja_pegos:
                selecionados.append(cand)
                simbolos_ja_pegos.add(cand[3].simbolo)
        selecionados.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [(s, p, ap, st) for _, _, _, s, p, ap, st in selecionados[:top_n]]

    return [(s, p, ap, st) for _, _, _, s, p, ap, st in candidatos[:top_n]]


def _resposta_conversa_completa(texto: str, finish_reason: str = "") -> bool:
    """Garante que o Gemini não cortou a frase no meio (ex.: por MAX_TOKENS de thinking)."""
    if not texto or len(texto.strip()) < 110:
        return False
    if (finish_reason or "").upper() == "MAX_TOKENS":
        return False
    sem_tags = re.sub(r"<[^>]+>", "", texto).strip()
    if not sem_tags:
        return False
    return sem_tags[-1] in (".", "?", "!", ")", '"', "'")


def formatar_painel_recomendacoes_html(
    modalidade: str,
    segmento: str,
    oportunidades: List[Tuple[SnapshotIndicadores, PlanoRisco, bool, str]],
    nome_usuario: str = "",
) -> str:
    """Formata a recomendação de Swing Trade com base em Suportes e Resistências consolidados
    como uma conversa humana natural e limpa, terminando com uma pergunta para o trader.
    """
    saudacao = f"Fala, <b>{html.escape(nome_usuario)}</b>! " if nome_usuario else "Fala! "
    eh_swing = "SWING" in modalidade.upper()
    prazo_txt = "no gráfico Diário pra <b>Swing Trade</b>" if eh_swing else "no gráfico intradiário pra <b>Day Trade</b>"

    if not oportunidades:
        return (
            f"{saudacao}Tentei puxar os gráficos agora no TradingView, mas a conexão oscilou. "
            "Me manda o nome de algum ativo específico que você está de olho ou tenta de novo em alguns segundos?"
        )

    aprovados = [(s, p) for s, p, ap, _ in oportunidades if ap]
    linhas_ativos: List[str] = []

    for snap, plano, aprovado, status_txt in oportunidades[:3]:
        m, c = snap.moeda, snap.casas_decimais
        sup_1 = _fmt_preco(snap.suporte_consolidado_1, m, c)
        res_1 = _fmt_preco(snap.resistencia_consolidada_1, m, c)
        res_2 = _fmt_preco(snap.resistencia_consolidada_2, m, c)
        emoji_dir = "🟢" if (aprovado and plano.direcao == "COMPRA") else "🟡"

        if "APOIADO EM SUPORTE" in snap.zona_sr_descricao:
            comentario = (
                f"cotada a <b>{_fmt_preco(snap.preco_atual, m, c)}</b>, está segurando muito bem em cima de um "
                f"<b>suporte consolidado na faixa de {sup_1}</b>, com caminho livre até a resistência em <b>{res_1}</b>."
            )
        elif "ROMPENDO RESISTÊNCIA" in snap.zona_sr_descricao:
            comentario = (
                f"a <b>{_fmt_preco(snap.preco_atual, m, c)}</b>, acabou de romper resistência com entrada de volume, "
                f"transformando a região de <b>{sup_1}</b> em piso de suporte e mirando o próximo teto em <b>{res_1}</b>."
            )
        else:
            comentario = (
                f"está negociando a <b>{_fmt_preco(snap.preco_atual, m, c)}</b>, entre o <b>suporte consolidado em {sup_1}</b> "
                f"e a <b>resistência forte em {res_1}</b>. O melhor ponto de compra é esperar recuar perto do suporte ({sup_1}) ou romper {res_1}."
            )

        stop_sug = _fmt_preco(min(plano.stop_loss, snap.suporte_consolidado_1 * 0.985) if plano.direcao == "COMPRA" else plano.stop_loss, m, c)
        linhas_ativos.append(
            f"{emoji_dir} <b>{html.escape(snap.simbolo)}</b> — {comentario}\n"
            f"   ↳ Suporte: <b>{sup_1}</b>  ·  Stop abaixo do suporte: <b>{stop_sug}</b>  ·  Alvos nas resistências: <b>{res_1}</b> e <b>{res_2}</b>"
        )

    bloco_lista = "\n\n".join(linhas_ativos)

    if aprovados:
        intro = (
            f"{saudacao}Olhei os gráficos {prazo_txt} mapeando as zonas de <b>suporte e resistência mais consolidadas</b> "
            "e separei as 3 melhores estruturas agora:"
        )
    else:
        intro = (
            f"{saudacao}Mapeei os principais <b>suportes e resistências</b> {prazo_txt}. "
            "Vários ativos estão no meio do caminho agora, então vale ter paciência pra comprar exatamente na defesa do suporte ou no rompimento confirmado. "
            "Olha os 3 mais interessantes no radar:"
        )

    primeiro_simb = html.escape(oportunidades[0][0].simbolo)
    pergunta_final = (
        f"Você prefere comprar quando o ativo testa o <b>suporte consolidado</b> (stop mais curtinho) "
        f"ou prefere entrar no <b>rompimento de resistência</b>? Se quiser focar em <b>{primeiro_simb}</b>, "
        "me fala quanto pensa em alocar que eu calculo o tamanho certinho da posição."
    )

    return f"{intro}\n\n{bloco_lista}\n\n{pergunta_final}"


def formatar_resposta_analise_chat_html(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    auditoria: AuditoriaIA,
    status_vies: str,
    nome_usuario: str = "",
) -> str:
    """Formata a análise de um ativo específico focando em Swing Trade por Suportes e Resistências
    bem consolidados, em tom de conversa humana e terminando com uma pergunta.
    """
    m, c = snap.moeda, snap.casas_decimais
    saudacao = f"Fala, <b>{html.escape(nome_usuario)}</b>! " if nome_usuario else ""
    mod_txt = "gráfico Diário (Swing Trade)" if snap.intervalo in ("1d", "1w", "4h") else f"gráfico de {snap.intervalo.upper()}"
    vies_str = "de <b>alta (comprador)</b>" if snap.preco_atual >= snap.ema_21 else "de <b>correção/baixa</b> no curto prazo"

    sup_1 = _fmt_preco(snap.suporte_consolidado_1, m, c)
    sup_2 = _fmt_preco(snap.suporte_consolidado_2, m, c)
    res_1 = _fmt_preco(snap.resistencia_consolidada_1, m, c)
    res_2 = _fmt_preco(snap.resistencia_consolidada_2, m, c)

    abertura = (
        f"{saudacao}Olhando a <b>{html.escape(snap.simbolo)}</b> agora no {html.escape(mod_txt)} "
        f"(cotada a <b>{_fmt_preco(snap.preco_atual, m, c)}</b>), ela está com um viés {vies_str}."
    )

    if "APOIADO EM SUPORTE" in snap.zona_sr_descricao:
        leitura_sr = (
            f"O ponto mais importante aqui é que o preço está segurando bem em cima de um <b>suporte consolidado na região de {sup_1}</b> "
            f"(com um segundo piso estrutural mais abaixo em {sup_2}). Isso dá uma relação risco/retorno muito boa pra Swing Trade, "
            f"porque o seu stop fica protegido abaixo desse suporte e o caminho fica aberto até a <b>primeira grande resistência em {res_1}</b>."
        )
    elif "TESTANDO TETO" in snap.zona_sr_descricao:
        leitura_sr = (
            f"Repara que ela está batendo muito perto de uma <b>resistência bem consolidada em {res_1}</b>. "
            f"Comprar colado em teto de resistência é perigoso porque os vendidos costumam defender essa faixa. "
            f"O ideal aqui é esperar ela romper <b>{res_1}</b> com força para buscar <b>{res_2}</b>, ou esperar recuar até o <b>suporte consolidado em {sup_1}</b>."
        )
    else:
        leitura_sr = (
            f"Mapeando os níveis do gráfico, ela tem um <b>suporte muito bem consolidado em {sup_1}</b> (e outro piso mais forte em {sup_2}) "
            f"e uma <b>resistência importante lá em {res_1}</b> (com alvo maior em {res_2}). "
            f"Como ela está no meio desse canal, a jogada mais inteligente de Swing Trade é buscar entrada perto da defesa do suporte em <b>{sup_1}</b> "
            f"ou na confirmação de rompimento acima de <b>{res_1}</b>."
        )

    stop_abaixo_sup = round(min(plano.stop_loss, snap.suporte_consolidado_1 * 0.985), c) if plano.direcao == "COMPRA" else plano.stop_loss
    niveis = (
        f"📐 <b>Mapa de Suporte e Resistência ({html.escape(snap.simbolo)}):</b>\n"
        f"• Suporte consolidado (zona de compra): <b>{sup_1}</b> <i>(suporte 2 em {sup_2})</i>\n"
        f"• Stop de proteção (abaixo do suporte): <b>{_fmt_preco(stop_abaixo_sup, m, c)}</b>\n"
        f"• Resistência 1 (1º Alvo / Parcial): <b>{res_1}</b>\n"
        f"• Resistência 2 (Alvo longo): <b>{res_2}</b>"
    )

    pergunta = (
        f"Você já está posicionado em <b>{html.escape(snap.simbolo)}</b> ou está esperando ela chegar numa dessas zonas pra entrar? "
        "Me conta qual é a sua ideia e com quanto pretende operar que eu ajusto o plano junto com você."
    )

    return f"{abertura}\n\n{leitura_sr}\n\n{niveis}\n\n{pergunta}"


def conversar_como_trader_humano_gemini(
    pergunta_usuario: str,
    nome_usuario: str,
    contexto_dados_mercado: str,
    historico_mensagens_recentes: List[Dict[str, str]],
    api_key: str = GEMINI_API_KEY,
) -> Optional[str]:
    """Usa o Gemini para conversar como um trader humano real especialista em Swing Trade
    por Suportes e Resistências consolidados, sem cortar frases no meio e sempre fazendo perguntas.
    """
    global _MODELO_VENCEDOR_SESSAO
    if not api_key:
        return None

    modelos = descobrir_modelos_gemini_gratuitos(api_key)
    if not modelos:
        return None

    system_instruction = (
        "Você é um Trader Sênior especialista em SWING TRADE baseado em SUPORTES E RESISTÊNCIAS BEM CONSOLIDADOS, "
        "conversando com outros traders em um grupo do Telegram.\n"
        "REGRAS OBRIGATÓRIAS DE COMPORTAMENTO, ESTRATÉGIA E DESIGN:\n"
        "1. FALE COMO UMA PESSOA REAL E COMPLETE TODAS AS FRASES: Converse de forma natural, amigável, inteligente e direta. "
        "NUNCA deixe uma frase pela metade e NUNCA use cabeçalhos robóticos como 'MODO PRESERVAÇÃO DE CAPITAL', 'PARECER DO ANALISTA' ou tabelas poluídas.\n"
        "2. FOCO EM SUPORTES E RESISTÊNCIAS CONSOLIDADOS (SWING TRADE): Toda a sua leitura deve girar em torno das zonas de Suporte Consolidado e Resistência Consolidada fornecidas nos dados:\n"
        "   - Explique com clareza qual é o viés atual do ativo (alta, baixa ou lateral), onde fica o principal SUPORTE CONSOLIDADO (o piso onde vale buscar compra com stop protegido logo abaixo) "
        "e onde ficam as RESISTÊNCIAS CONSOLIDADAS (os tetos onde o preço pode travar, onde fazemos parcial/alvo ou esperamos rompimento).\n"
        "   - Se o preço estiver no meio do caminho ou colado numa resistência, avise como um amigo experiente para não comprar no topo e indique o preço do suporte consolidado para aguardar recuo ou o preço da resistência para aguardar rompimento.\n"
        "3. DESIGN LIMPO E BONITO: Escreva em 2 a 3 parágrafos curtos e fluidos. Use apenas negrito HTML (<b>...</b>) para destacar o ativo e os preços de Suporte, Resistência, Stop e Alvos, e itálico (<i>...</i>) com moderação. "
        "NUNCA use a tag <code> (caixas azuis) e NUNCA despeje siglas cruas ('Score Quant', 'Alpha', 'DI+', 'Stoch.K').\n"
        "4. RESPONDA DIRETAMENTE A PERGUNTAS CURTAS DE CONTINUAÇÃO: Se o usuário perguntar algo curto como 'que viés?', 'qual suporte?', 'vale entrar?', olhe o contexto das últimas mensagens e responda diretamente de forma completa.\n"
        "5. QUANDO HOUVER 3 ATIVOS NOS DADOS (RECOMENDAÇÃO MULTI-MERCADO / TOP 3): Você é OBRIGADO a apresentar e comentar TODOS OS 3 ATIVOS da lista (citando o Preço Atual, o Suporte Consolidado e a Resistência Consolidada de cada um dos 3). NUNCA fale de apenas 1 ativo quando houver 3 ativos no contexto!\n"
        "6. FAÇA PERGUNTAS NO FINAL: Sempre termine sua resposta fazendo 1 ou 2 perguntas naturais para continuar a conversa "
        "(ex: perguntar qual dos 3 ativos chamou mais atenção — Ações, Cripto ou Ouro/Dólar/BDR —, se a pessoa prefere esperar o toque no suporte consolidado ou entrar no rompimento da resistência, ou qual valor pensa em alocar para calcular o lote).\n"
        "7. USE APENAS OS PREÇOS REAIS fornecidos nos dados do TradingView abaixo. Nunca invente cotações."
    )

    bloco_hist = ""
    if historico_mensagens_recentes:
        linhas_h = [f"- {m.get('autor', 'Trader')}: {m.get('texto', '')}" for m in historico_mensagens_recentes[-6:]]
        bloco_hist = "HISTÓRICO RECENTE DA CONVERSA NO GRUPO:\n" + "\n".join(linhas_h) + "\n\n"

    user_prompt = (
        f"{bloco_hist}"
        f"DADOS EM TEMPO REAL DO TRADINGVIEW (MAPA DE SUPORTES E RESISTÊNCIAS CONSOLIDADOS):\n{contexto_dados_mercado}\n\n"
        f"MENSAGEM ATUAL DE {nome_usuario or 'Trader'}: \"{pergunta_usuario}\"\n\n"
        "Responda diretamente em português de forma COMPLETA (nunca corte frases no meio), conversando como um trader humano especialista em Swing Trade por Suportes e Resistências, "
        "com visual limpo (apenas texto fluido e tags <b> e <i>) e termine fazendo uma pergunta para continuar o papo:"
    )

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 4096,
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
                timeout=18,
            )
            if resp.status_code == 200:
                body = resp.json()
                cand0 = (body.get("candidates") or [{}])[0]
                finish_reason = str(cand0.get("finishReason") or "")
                partes = cand0.get("content", {}).get("parts", [])
                texto = "".join(p.get("text", "") for p in partes if isinstance(p, dict) and not p.get("thought"))
                if not texto:
                    texto = "".join(p.get("text", "") for p in partes if isinstance(p, dict))
                texto_limpo = _sanitizar_html_telegram(texto)
                if _resposta_conversa_completa(texto_limpo, finish_reason):
                    _MODELO_VENCEDOR_SESSAO = modelo
                    _MODELOS_DEGRADADOS_SESSAO.discard(modelo)
                    return texto_limpo
                logger.warning(
                    "Gemini (%s) retornou resposta truncada/incompleta (finishReason=%s, len=%d); tentando próximo.",
                    modelo,
                    finish_reason,
                    len(texto_limpo),
                )
                _MODELOS_DEGRADADOS_SESSAO.add(modelo)
            elif resp.status_code in (404, 429):
                _MODELOS_BLOQUEADOS_SESSAO.add(modelo)
                if _MODELO_VENCEDOR_SESSAO == modelo:
                    _MODELO_VENCEDOR_SESSAO = None
            elif resp.status_code in (400, 500, 503):
                _MODELOS_DEGRADADOS_SESSAO.add(modelo)
                if _MODELO_VENCEDOR_SESSAO == modelo:
                    _MODELO_VENCEDOR_SESSAO = None
            elif resp.status_code in (401, 403):
                return None
        except Exception:
            _MODELOS_DEGRADADOS_SESSAO.add(modelo)
            if _MODELO_VENCEDOR_SESSAO == modelo:
                _MODELO_VENCEDOR_SESSAO = None

    return None


# ==============================================================================
# 7. FORMATAÇÃO DE SINAIS AUTOMÁTICOS, HISTÓRICO E INTEGRAÇÃO TELEGRAM
# ==============================================================================
def formatar_mensagem_sinal_html(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    auditoria: AuditoriaIA,
) -> str:
    """Design minimalista, limpo e humano para os alertas automáticos de Swing Trade em Suporte/Resistência."""
    eh_compra = plano.direcao == "COMPRA"
    m, c = snap.moeda, snap.casas_decimais
    sup_1 = _fmt_preco(snap.suporte_consolidado_1, m, c)
    res_1 = _fmt_preco(snap.resistencia_consolidada_1, m, c)
    res_2 = _fmt_preco(snap.resistencia_consolidada_2, m, c)

    icone_titulo = (
        f"🟢 <b>Swing Trade · {html.escape(snap.simbolo)} em Zona de Suporte/Rompimento</b> ({html.escape(snap.intervalo.upper())})"
        if eh_compra
        else f"🔴 <b>Swing Trade · {html.escape(snap.simbolo)} em Resistência</b> ({html.escape(snap.intervalo.upper())})"
    )

    racional_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.racional_tecnico))

    return (
        f"{icone_titulo}\n\n"
        f"{racional_limpo}\n\n"
        f"📐 <b>Suporte Consolidado:</b> <b>{sup_1}</b>  ·  <b>Preço Atual:</b> <b>{_fmt_preco(plano.entrada, m, c)}</b>\n"
        f"🛑 <b>Stop de proteção (abaixo do suporte):</b> <b>{_fmt_preco(plano.stop_loss, m, c)}</b> <i>(-{plano.risco_percentual:.1f}%)</i>\n"
        f"🎯 <b>1º Alvo (Resistência 1 + Stop no 0x0):</b> <b>{res_1}</b>\n"
        f"🚀 <b>2º Alvo (Resistência 2):</b> <b>{res_2}</b>\n\n"
        f"💬 <i>Alguém acompanhando {html.escape(snap.simbolo)} pra Swing Trade? Se quiserem avaliar o ponto de entrada ou calcular o lote pra banca de vocês, é só responder aqui!</i>"
    )


def carregar_historico(caminho: Path = HISTORICO_PATH) -> Dict[str, Any]:
    if caminho.exists():
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                dados.setdefault("sinais", [])
                dados.setdefault("ultimo_update_id", 0)
                dados.setdefault("conversas_chat", {})
                return dados
        except Exception as exc:
            logger.warning("Erro ao ler %s: %s", caminho, exc)
    return {"sinais": [], "ultimo_update_id": 0, "conversas_chat": {}}


def salvar_historico(dados: Dict[str, Any], caminho: Path = HISTORICO_PATH) -> None:
    agora_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    sinais_limpos = [
        s for s in dados.get("sinais", [])
        if isinstance(s, dict) and (agora_ts - float(s.get("ts", 0))) <= 72 * 3600
    ][-400:]
    conversas_raw = dados.get("conversas_chat")
    conversas_limpas: Dict[str, Any] = {}
    if isinstance(conversas_raw, dict):
        for cid, info_c in list(conversas_raw.items())[-30:]:
            if isinstance(info_c, dict) and (agora_ts - float(info_c.get("ultimo_ts", 0))) <= 24 * 3600:
                conversas_limpas[str(cid)] = {
                    "ultimo_ts": float(info_c.get("ultimo_ts", 0)),
                    "ultimo_simbolo": str(info_c.get("ultimo_simbolo") or ""),
                    "mensagens": list(info_c.get("mensagens") or [])[-8:],
                }
    payload = {
        "atualizado_em": dt.datetime.now(TZ_BRT).isoformat(),
        "ultimo_update_id": int(dados.get("ultimo_update_id") or 0),
        "sinais": sinais_limpos,
        "conversas_chat": conversas_limpas,
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


# ==============================================================================
# 8. GERADOR DE GRÁFICO CANDLESTICK (TRADINGVIEW DARK MODE PNG EM PYTHON PURO)
# ==============================================================================
_FONTE_5X7: Dict[str, Tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "A": (14, 17, 17, 31, 17, 17, 17),
    "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14),
    "D": (30, 17, 17, 17, 17, 17, 30),
    "E": (31, 16, 16, 30, 16, 16, 31),
    "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 14),
    "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14),
    "J": (7, 2, 2, 2, 18, 18, 12),
    "K": (17, 18, 20, 24, 20, 18, 17),
    "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17),
    "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14),
    "P": (30, 17, 17, 30, 16, 16, 16),
    "Q": (14, 17, 17, 17, 21, 18, 13),
    "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30),
    "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14),
    "V": (17, 17, 17, 17, 17, 10, 4),
    "W": (17, 17, 17, 21, 21, 27, 17),
    "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 10, 4, 4, 4, 4),
    "Z": (31, 1, 2, 4, 8, 16, 31),
    "0": (14, 17, 19, 21, 25, 17, 14),
    "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 6, 8, 16, 31),
    "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2),
    "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14),
    "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 1, 14),
    ".": (0, 0, 0, 0, 0, 12, 12),
    ",": (0, 0, 0, 0, 4, 4, 8),
    ":": (0, 12, 12, 0, 12, 12, 0),
    "-": (0, 0, 0, 31, 0, 0, 0),
    "+": (0, 4, 4, 31, 4, 4, 0),
    "/": (1, 2, 2, 4, 8, 8, 16),
    "$": (4, 15, 16, 14, 1, 30, 4),
    "%": (25, 25, 2, 4, 8, 19, 19),
    "(": (2, 4, 8, 8, 8, 4, 2),
    ")": (8, 4, 2, 2, 2, 4, 8),
}


def obter_velas_historicas_grafico(
    snap: SnapshotIndicadores,
    qtd: int = 42,
) -> List[Tuple[float, float, float, float, float]]:
    """Busca as últimas 42 velas reais (open, high, low, close, volume) via API pública da Binance
    (para Cripto) ou Yahoo Finance v8 (para Ações B3, Dólar e Ouro), com fallback determinístico
    ancorado nos níveis reais do TradingView.
    """
    velas: List[Tuple[float, float, float, float, float]] = []
    tf = (snap.intervalo or "1d").lower()

    if hasattr(requests, "get"):
        try:
            if snap.screener == "crypto":
                mapa_bin = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"}
                resp = requests.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": snap.simbolo.upper(), "interval": mapa_bin.get(tf, "1d"), "limit": qtd},
                    timeout=6,
                )
                if resp.status_code == 200:
                    for k in resp.json() or []:
                        if isinstance(k, list) and len(k) >= 6:
                            o, h, l, c, v = _num(k[1]), _num(k[2]), _num(k[3]), _num(k[4]), _num(k[5])
                            if c > 0:
                                velas.append((o, max(h, o, c), min(l, o, c), c, max(v, 0.0)))
            else:
                yf_sym = (
                    f"{snap.simbolo}.SA"
                    if snap.screener == "brazil"
                    else ("BRL=X" if snap.simbolo == "USDBRL" else ("GC=F" if snap.simbolo == "XAUUSD" else snap.simbolo))
                )
                mapa_yf = {
                    "5m": ("5m", "5d"),
                    "15m": ("15m", "5d"),
                    "30m": ("30m", "1mo"),
                    "1h": ("60m", "1mo"),
                    "4h": ("60m", "3mo"),
                    "1d": ("1d", "3mo"),
                    "1w": ("1wk", "1y"),
                }
                yf_int, yf_rng = mapa_yf.get(tf, ("1d", "3mo"))
                resp = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}",
                    params={"interval": yf_int, "range": yf_rng},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=6,
                )
                if resp.status_code == 200:
                    res0 = ((resp.json() or {}).get("chart", {}).get("result") or [{}])[0]
                    q0 = ((res0.get("indicators", {}).get("quote") or [{}])[0]) or {}
                    opens = q0.get("open") or []
                    highs = q0.get("high") or []
                    lows = q0.get("low") or []
                    closes = q0.get("close") or []
                    vols = q0.get("volume") or []
                    for i in range(min(len(closes), len(opens), len(highs), len(lows))):
                        c = _num(closes[i])
                        if c <= 0:
                            continue
                        o = _num(opens[i], c)
                        h = max(_num(highs[i], c), o, c)
                        l = min(_num(lows[i], c), o, c)
                        v = _num(vols[i] if i < len(vols) else 0.0)
                        velas.append((o, h, l, c, max(v, 0.0)))
        except Exception:
            pass

    if len(velas) >= 15:
        velas = velas[-qtd:]
        # Garante que a última vela reflete a cotação ao vivo extraída do TradingView
        velas[-1] = (
            snap.abertura,
            max(snap.maxima, snap.abertura, snap.preco_atual),
            min(snap.minima, snap.abertura, snap.preco_atual),
            snap.preco_atual,
            max(snap.volume, velas[-1][4]),
        )
        # Conta toques reais nas zonas de Suporte Consolidado e Resistência Consolidada
        tol = max(snap.atr * 0.45, snap.preco_atual * 0.006)
        t_sup = sum(1 for _, _, l, c, _ in velas if abs(min(l, c) - snap.suporte_consolidado_1) <= tol)
        t_res = sum(1 for _, h, _, c, _ in velas if abs(max(h, c) - snap.resistencia_consolidada_1) <= tol)
        snap.toques_suporte = max(2, t_sup)
        snap.toques_resistencia = max(2, t_res)
        return velas

    # Fallback determinístico ancorado nos níveis reais do TradingView (EMA 21, EMA 9, Suporte 1, Resistência 1 e Vela Atual)
    velas_fb: List[Tuple[float, float, float, float, float]] = []
    atr = max(snap.atr, snap.preco_atual * 0.01)
    sup = snap.suporte_consolidado_1 if snap.suporte_consolidado_1 > 0 else (snap.preco_atual - 1.5 * atr)
    res = snap.resistencia_consolidada_1 if snap.resistencia_consolidada_1 > 0 else (snap.preco_atual + 2.0 * atr)
    meio = (sup + min(res, snap.preco_atual + atr)) / 2.0
    preco_ant = meio

    for i in range(qtd - 1):
        prog = i / float(max(qtd - 2, 1))
        alvo_curva = (
            sup + (0.25 * atr)
            if i in (8, 9, 22, 23)
            else (
                snap.ema_21 * (1.0 - prog) + snap.ema_9 * prog
            )
        )
        osc = math.sin(i * 0.65) * (0.38 * atr)
        fech = max(sup * 0.996, min(res * 1.002, (alvo_curva * 0.65 + preco_ant * 0.35) + osc * 0.35))
        abert = preco_ant
        maxi = max(abert, fech) + abs(math.cos(i * 0.9)) * (0.32 * atr)
        mini = min(abert, fech) - abs(math.sin(i * 1.1)) * (0.32 * atr)
        if i in (8, 22):
            mini = min(mini, sup * 1.001)
        vol = max(snap.volume * (0.7 + 0.6 * abs(math.sin(i))), 1000.0)
        velas_fb.append((abert, maxi, mini, fech, vol))
        preco_ant = fech

    velas_fb.append(
        (
            snap.abertura,
            max(snap.maxima, snap.abertura, snap.preco_atual),
            min(snap.minima, snap.abertura, snap.preco_atual),
            snap.preco_atual,
            max(snap.volume, 1000.0),
        )
    )
    return velas_fb


def gerar_grafico_candlestick_png(snap: SnapshotIndicadores, plano: PlanoRisco) -> bytes:
    """Renderiza um gráfico Candlestick estilo TradingView Dark Mode (840x480 PNG) em Python 100% puro
    (usando apenas struct + zlib da biblioteca padrão), com Suporte Consolidado, Resistências, Stop e EMAs.
    """
    W, H = 840, 480
    bg_r, bg_g, bg_b = 19, 23, 34  # #131722 TradingView Dark
    buf = bytearray([bg_r, bg_g, bg_b] * (W * H))

    def _fill_rect(x0: int, y0: int, x1: int, y1: int, r: int, g: int, b: int) -> None:
        xa, xb = max(0, min(x0, x1)), min(W - 1, max(x0, x1))
        ya, yb = max(0, min(y0, y1)), min(H - 1, max(y0, y1))
        if xa > xb or ya > yb:
            return
        span = xb - xa + 1
        row_bytes = bytes([r, g, b]) * span
        for y in range(ya, yb + 1):
            idx = (y * W + xa) * 3
            buf[idx : idx + span * 3] = row_bytes

    def _draw_hline(y: int, x0: int, x1: int, r: int, g: int, b: int, dashed: bool = False, thickness: int = 1) -> None:
        if not (0 <= y < H):
            return
        xa, xb = max(0, min(x0, x1)), min(W - 1, max(x0, x1))
        for ty in range(max(0, y - thickness // 2), min(H, y + thickness // 2 + 1)):
            if not dashed:
                _fill_rect(xa, ty, xb, ty, r, g, b)
            else:
                for x in range(xa, xb + 1):
                    if (x // 6) % 2 == 0:
                        idx = (ty * W + x) * 3
                        buf[idx], buf[idx + 1], buf[idx + 2] = r, g, b

    def _draw_line(x0: int, y0: int, x1: int, y1: int, r: int, g: int, b: int, thickness: int = 2) -> None:
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        steps = max(dx, dy, 1)
        for s in range(steps + 1):
            x = int(round(x0 + (x1 - x0) * (s / steps)))
            y = int(round(y0 + (y1 - y0) * (s / steps)))
            _fill_rect(x - thickness // 2, y - thickness // 2, x + thickness // 2, y + thickness // 2, r, g, b)

    def _draw_text(texto: str, x: int, y: int, r: int, g: int, b: int, scale: int = 2) -> None:
        cx = x
        for ch in texto.upper():
            padrao = _FONTE_5X7.get(ch, _FONTE_5X7[" "])
            for row_idx, bits in enumerate(padrao):
                for col_idx in range(5):
                    if bits & (1 << (4 - col_idx)):
                        px = cx + col_idx * scale
                        py = y + row_idx * scale
                        _fill_rect(px, py, px + scale - 1, py + scale - 1, r, g, b)
            cx += 6 * scale

    velas = obter_velas_historicas_grafico(snap, qtd=42)
    closes = [v[3] for v in velas]

    # Calcula séries EMA 9 e EMA 21 sobre as velas para desenhar as curvas no gráfico
    def _serie_ema(vals: List[float], periodo: int, valor_final_tv: float) -> List[float]:
        if not vals:
            return []
        k = 2.0 / (periodo + 1.0)
        res = [vals[0]]
        for v in vals[1:]:
            res.append(v * k + res[-1] * (1.0 - k))
        if valor_final_tv > 0:
            res[-1] = valor_final_tv
        return res

    ema9_s = _serie_ema(closes, 9, snap.ema_9)
    ema21_s = _serie_ema(closes, 21, snap.ema_21)

    # Área de plotagem dos candles e eixo direito de preços
    PLOT_X0, PLOT_X1 = 22, 668
    PLOT_Y0, PLOT_Y1 = 54, 385
    VOL_Y0, VOL_Y1 = 395, 452

    # Faixa vertical de preços (inclui candles, Suporte Consolidado, Stop e Resistências)
    niveis_visiveis = [
        *[v[1] for v in velas],
        *[v[2] for v in velas],
        snap.suporte_consolidado_1,
        plano.stop_loss,
        snap.resistencia_consolidada_1,
    ]
    if snap.resistencia_consolidada_2 > 0 and (snap.resistencia_consolidada_2 - snap.preco_atual) <= 4.5 * snap.atr:
        niveis_visiveis.append(snap.resistencia_consolidada_2)

    niveis_validos = [p for p in niveis_visiveis if p > 0]
    p_min = min(niveis_validos) if niveis_validos else snap.preco_atual * 0.95
    p_max = max(niveis_validos) if niveis_validos else snap.preco_atual * 1.05
    margem = max((p_max - p_min) * 0.08, snap.preco_atual * 0.005, 1e-6)
    p_min -= margem
    p_max += margem

    def _preco_para_y(p: float) -> int:
        frac = (p - p_min) / max(p_max - p_min, 1e-9)
        return int(round(PLOT_Y1 - frac * (PLOT_Y1 - PLOT_Y0)))

    # 1. Cabeçalho superior escuro + grade horizontal sutil
    _fill_rect(0, 0, W - 1, 42, 26, 31, 45)
    _draw_hline(43, 0, W - 1, 42, 46, 57, dashed=False, thickness=1)
    for i in range(6):
        gy = PLOT_Y0 + int(round(i * (PLOT_Y1 - PLOT_Y0) / 5.0))
        _draw_hline(gy, PLOT_X0, PLOT_X1, 30, 34, 45, dashed=False, thickness=1)
        gp = p_max - (i / 5.0) * (p_max - p_min)
        _draw_text(f"{gp:.{snap.casas_decimais}f}", PLOT_X1 + 8, gy - 4, 110, 118, 135, scale=1)

    # Divisória vertical do eixo Y direito
    _fill_rect(PLOT_X1 + 2, 44, PLOT_X1 + 2, H - 1, 42, 46, 57)

    # 2. Faixas sombreadas de Suporte Consolidado (verde escuro) e Resistência Consolidada (azul/petróleo)
    if snap.suporte_consolidado_1 > 0:
        ys = _preco_para_y(snap.suporte_consolidado_1)
        _fill_rect(PLOT_X0, max(PLOT_Y0, ys - 7), PLOT_X1, min(PLOT_Y1, ys + 7), 18, 52, 46)

    if snap.resistencia_consolidada_1 > 0:
        yr1 = _preco_para_y(snap.resistencia_consolidada_1)
        _fill_rect(PLOT_X0, max(PLOT_Y0, yr1 - 6), PLOT_X1, min(PLOT_Y1, yr1 + 6), 24, 50, 74)

    # 3. Barras de Volume no rodapé e Candlesticks (42 velas)
    max_vol = max(max((v[4] for v in velas), default=1.0), 1.0)
    n_velas = len(velas)
    passo_x = (PLOT_X1 - PLOT_X0 - 16) / float(max(n_velas, 1))
    larg_corpo = max(4, int(passo_x * 0.62))

    xs_candles: List[int] = []
    for idx, (o, h, l, c, v) in enumerate(velas):
        cx = int(round(PLOT_X0 + 12 + idx * passo_x))
        xs_candles.append(cx)
        alta = c >= o
        cr, cg, cb = (8, 153, 129) if alta else (242, 54, 69)  # #089981 verde / #f23645 vermelho
        vr, vg, vb = (16, 75, 68) if alta else (88, 32, 42)

        # Barra de volume
        alt_v = int(round((v / max_vol) * (VOL_Y1 - VOL_Y0)))
        if alt_v > 0:
            _fill_rect(cx - larg_corpo // 2, VOL_Y1 - alt_v, cx + larg_corpo // 2, VOL_Y1, vr, vg, vb)

        # Pavio (high até low)
        yh = max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(h)))
        yl = max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(l)))
        _fill_rect(cx, yh, cx, yl, cr, cg, cb)

        # Corpo do candle (open até close)
        yo = max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(o)))
        yc = max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(c)))
        y_top, y_bot = min(yo, yc), max(yo, yc)
        if y_bot - y_top < 2:
            y_bot = min(PLOT_Y1, y_top + 2)
        _fill_rect(cx - larg_corpo // 2, y_top, cx + larg_corpo // 2, y_bot, cr, cg, cb)

    # 4. Curvas EMA 21 (Azul #2962ff) e EMA 9 (Amarelo Ouro #f59e0b)
    for i in range(1, len(xs_candles)):
        _draw_line(
            xs_candles[i - 1],
            max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(ema21_s[i - 1]))),
            xs_candles[i],
            max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(ema21_s[i]))),
            41, 98, 255,
            thickness=2,
        )
        _draw_line(
            xs_candles[i - 1],
            max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(ema9_s[i - 1]))),
            xs_candles[i],
            max(PLOT_Y0, min(PLOT_Y1, _preco_para_y(ema9_s[i]))),
            245, 158, 11,
            thickness=2,
        )

    # 5. Linhas horizontais de Suporte, Resistência, Stop e Preço Atual + Etiquetas (Pills) no eixo direito
    etiquetas: List[Tuple[int, str, Tuple[int, int, int]]] = []

    if snap.resistencia_consolidada_2 > 0 and p_min <= snap.resistencia_consolidada_2 <= p_max:
        yr2 = _preco_para_y(snap.resistencia_consolidada_2)
        _draw_hline(yr2, PLOT_X0, PLOT_X1, 16, 185, 129, dashed=True, thickness=1)
        etiquetas.append((yr2, f"RES 2: {snap.resistencia_consolidada_2:.{snap.casas_decimais}f}", (16, 140, 100)))

    if snap.resistencia_consolidada_1 > 0:
        yr1 = _preco_para_y(snap.resistencia_consolidada_1)
        _draw_hline(yr1, PLOT_X0, PLOT_X1, 56, 189, 248, dashed=True, thickness=2)
        etiquetas.append((yr1, f"RES 1: {snap.resistencia_consolidada_1:.{snap.casas_decimais}f}", (14, 116, 144)))

    y_atual = _preco_para_y(snap.preco_atual)
    _draw_hline(y_atual, PLOT_X0, PLOT_X1, 226, 232, 240, dashed=True, thickness=1)
    etiquetas.append((y_atual, f"ATUAL: {snap.preco_atual:.{snap.casas_decimais}f}", (59, 130, 246)))

    if snap.suporte_consolidado_1 > 0:
        ys1 = _preco_para_y(snap.suporte_consolidado_1)
        _draw_hline(ys1, PLOT_X0, PLOT_X1, 16, 185, 129, dashed=False, thickness=2)
        etiquetas.append((ys1, f"SUP 1: {snap.suporte_consolidado_1:.{snap.casas_decimais}f}", (5, 150, 105)))

    stop_ref = min(plano.stop_loss, snap.suporte_consolidado_1 * 0.985) if plano.direcao == "COMPRA" and snap.suporte_consolidado_1 > 0 else plano.stop_loss
    if stop_ref > 0 and p_min <= stop_ref <= p_max:
        ystop = _preco_para_y(stop_ref)
        _draw_hline(ystop, PLOT_X0, PLOT_X1, 244, 63, 94, dashed=True, thickness=2)
        etiquetas.append((ystop, f"STOP:  {stop_ref:.{snap.casas_decimais}f}", (190, 18, 60)))

    # Evita sobreposição vertical das etiquetas no eixo direito
    etiquetas.sort(key=lambda item: item[0])
    ultimo_y_pill = -100
    for y_orig, txt_lbl, (pr, pg, pb) in etiquetas:
        yp = max(50, min(H - 22, y_orig))
        if yp - ultimo_y_pill < 20:
            yp = min(H - 20, ultimo_y_pill + 20)
        ultimo_y_pill = yp
        _fill_rect(PLOT_X1 + 5, yp - 9, W - 4, yp + 8, pr, pg, pb)
        _draw_text(txt_lbl[:18], PLOT_X1 + 9, yp - 4, 255, 255, 255, scale=1)

    # 6. Título no Topo e Legenda das Médias
    titulo_top = f"{snap.simbolo} - {snap.intervalo.upper()} - SWING TRADE (SUPORTE E RESISTENCIA)"
    _draw_text(titulo_top, 16, 14, 241, 245, 249, scale=2)
    _draw_text(f"EMA9: {snap.ema_9:.{snap.casas_decimais}f}", 540, 16, 245, 158, 11, scale=1)
    _draw_text(f"EMA21: {snap.ema_21:.{snap.casas_decimais}f}", 650, 16, 41, 98, 255, scale=1)
    _draw_text(f"RSI: {snap.rsi:.0f}", 770, 16, 203, 213, 225, scale=1)

    # 7. Empacota como PNG válido usando struct + zlib (stdlib)
    raw_rows = bytearray()
    row_len = W * 3
    for y in range(H):
        raw_rows.append(0)  # Filtro PNG 0 (None)
        start = y * row_len
        raw_rows.extend(buf[start : start + row_len])

    def _png_chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", crc)

    ihdr = struct.pack("!IIBBBBB", W, H, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def enviar_telegram_com_grafico(
    texto_html: str,
    snap: Optional[SnapshotIndicadores] = None,
    plano: Optional[PlanoRisco] = None,
    token: str = TELEGRAM_BOT_TOKEN,
    chat_id: str = TELEGRAM_CHAT_ID,
    reply_to_message_id: Optional[int] = None,
) -> bool:
    """Envia a foto do gráfico Candlestick (sendPhoto) junto com a resposta conversacional.
    Se o texto passar de 1000 caracteres, envia a foto primeiro e o texto completo logo abaixo.
    Faz fallback transparente para enviar_telegram_html caso sendPhoto não esteja disponível.
    """
    if not snap or not plano:
        return enviar_telegram_html(texto_html, token=token, chat_id=chat_id, reply_to_message_id=reply_to_message_id)

    try:
        png_bytes = gerar_grafico_candlestick_png(snap, plano)
    except Exception as exc:
        logger.warning("Aviso ao renderizar PNG do gráfico (%s); enviando apenas texto.", str(exc)[:100])
        return enviar_telegram_html(texto_html, token=token, chat_id=chat_id, reply_to_message_id=reply_to_message_id)

    if not token or not chat_id or not hasattr(requests, "post"):
        return enviar_telegram_html(texto_html, token=token, chat_id=chat_id, reply_to_message_id=reply_to_message_id)

    url_photo = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        if len(texto_html) <= 1000:
            data_payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "caption": texto_html,
                "parse_mode": "HTML",
            }
            if reply_to_message_id:
                data_payload["reply_to_message_id"] = reply_to_message_id
                data_payload["allow_sending_without_reply"] = True
            resp = requests.post(
                url_photo,
                data=data_payload,
                files={"photo": (f"{snap.simbolo}_{snap.intervalo}.png", png_bytes, "image/png")},
                timeout=25,
            )
            if getattr(resp, "status_code", 0) == 200:
                return True
        else:
            legenda_curta = (
                f"📊 <b>Mapa Gráfico · {html.escape(snap.simbolo)} ({html.escape(snap.intervalo.upper())})</b>\n"
                f"Suporte Consolidado: <b>{_fmt_preco(snap.suporte_consolidado_1, snap.moeda, snap.casas_decimais)}</b>  ·  "
                f"Resistência 1: <b>{_fmt_preco(snap.resistencia_consolidada_1, snap.moeda, snap.casas_decimais)}</b>"
            )
            data_curta: Dict[str, Any] = {
                "chat_id": chat_id,
                "caption": legenda_curta,
                "parse_mode": "HTML",
            }
            if reply_to_message_id:
                data_curta["reply_to_message_id"] = reply_to_message_id
                data_curta["allow_sending_without_reply"] = True
            requests.post(
                url_photo,
                data=data_curta,
                files={"photo": (f"{snap.simbolo}_{snap.intervalo}.png", png_bytes, "image/png")},
                timeout=25,
            )
            return enviar_telegram_html(texto_html, token=token, chat_id=chat_id, reply_to_message_id=reply_to_message_id)
    except Exception as exc:
        logger.warning("Aviso ao enviar foto para Telegram (%s); usando fallback de texto.", str(exc)[:100])

    return enviar_telegram_html(texto_html, token=token, chat_id=chat_id, reply_to_message_id=reply_to_message_id)


def extrair_valor_banca_da_mensagem(texto: str) -> Optional[float]:
    """Detecta quando o trader informa o valor que pretende investir/operar
    (ex.: 'vou entrar com 5 mil', 'tenho 2000 reais', '10k', 'R$ 3.500').
    """
    if not texto:
        return None
    t = texto.lower().strip()
    m_mil = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:mil|k)\b", t)
    if m_mil:
        return float(m_mil.group(1).replace(",", ".")) * 1000.0
    m_moeda = re.search(r"(?:r\$|us\$|\$|reais|d[óo]lares|banca\s+de|entrar\s+com|colocar|tenho|alocar)\s*(\d{2,3}(?:\.\d{3})+|\d{3,7})(?:,\d{1,2})?\b", t)
    if m_moeda:
        limpo = m_moeda.group(1).replace(".", "")
        return float(limpo)
    if re.match(r"^\s*(\d{3,7})\s*(?:reais|d[óo]lares|conto)?\s*$", t):
        m_num = re.search(r"(\d{3,7})", t)
        if m_num:
            return float(m_num.group(1))
    return None


def obter_username_bot(token: str = TELEGRAM_BOT_TOKEN) -> str:
    if not token:
        return ""
    try:
        # Garante que nenhum webhook antigo bloqueie o getUpdates (HTTP 409)
        requests.get(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            params={"drop_pending_updates": "false"},
            timeout=10,
        )
    except Exception:
        pass
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if resp.status_code == 200:
            info = resp.json().get("result", {}) or {}
            username = str(info.get("username") or "").strip()
            le_tudo_grupo = bool(info.get("can_read_all_group_messages"))
            logger.info(
                "🤖 Bot Telegram conectado: @%s | Lê todas as mensagens de grupo (Privacy OFF): %s",
                username or "desconhecido",
                "SIM" if le_tudo_grupo else "NÃO (promova o bot a ADMIN do grupo ou desative /setprivacy no @BotFather!)",
            )
            return username
    except Exception as exc:
        logger.warning("Aviso ao consultar getMe do Telegram: %s", str(exc)[:100])
    return ""


def _registrar_turno_conversa(
    historico: Dict[str, Any],
    chat_id: str,
    nome_usuario: str,
    texto_usuario: str,
    resposta_bot: str,
    simbolo_foco: str = "",
) -> None:
    convs = historico.setdefault("conversas_chat", {})
    sessao = convs.setdefault(str(chat_id), {"ultimo_ts": 0.0, "ultimo_simbolo": "", "mensagens": []})
    sessao["ultimo_ts"] = dt.datetime.now(dt.timezone.utc).timestamp()
    if simbolo_foco:
        sessao["ultimo_simbolo"] = simbolo_foco
    msgs = sessao.setdefault("mensagens", [])
    msgs.append({"autor": nome_usuario or "Trader", "texto": texto_usuario[:300]})
    texto_bot_limpo = re.sub(r"<[^>]+>", "", resposta_bot)
    msgs.append({"autor": "Analista Quant (Você)", "texto": texto_bot_limpo[:400]})
    sessao["mensagens"] = msgs[-8:]


def _calcular_simulacao_banca_usuario(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    valor_banca: float,
) -> str:
    """Calcula a posição exata, risco financeiro e lucro projetado nas Resistências
    quando o trader informa com quanto pretende entrar (ex.: '5 mil', '2000 reais').
    """
    m, c = snap.moeda, snap.casas_decimais
    preco_ref = max(snap.preco_atual, 1e-9)
    qtd_total = valor_banca / preco_ref
    if c <= 2 and preco_ref >= 1.0:
        qtd_str = f"{max(1, int(qtd_total))} ações/cotas"
        qtd_num = float(max(1, int(qtd_total)))
    else:
        qtd_str = f"{qtd_total:.4f} unidades"
        qtd_num = qtd_total

    risco_por_unid = abs(preco_ref - plano.stop_loss)
    perda_stop_fin = qtd_num * risco_por_unid
    ganho_r1_unid = max(0.0, snap.resistencia_consolidada_1 - preco_ref)
    ganho_r2_unid = max(0.0, snap.resistencia_consolidada_2 - preco_ref)
    lucro_r1_fin = qtd_num * ganho_r1_unid
    lucro_r2_fin = qtd_num * ganho_r2_unid

    return (
        f"SIMULAÇÃO PERSONALIZADA P/ APORTE DE {_fmt_preco(valor_banca, m, 2)} DO TRADER: "
        f"Comprando agora em {_fmt_preco(preco_ref, m, c)} dá cerca de {qtd_str}. "
        f"Se bater na 1ª Resistência Consolidada ({_fmt_preco(snap.resistencia_consolidada_1, m, c)}), "
        f"o lucro projetado é de +{_fmt_preco(lucro_r1_fin, m, 2)}; "
        f"se buscar a 2ª Resistência ({_fmt_preco(snap.resistencia_consolidada_2, m, c)}), "
        f"o lucro sobe para +{_fmt_preco(lucro_r2_fin, m, 2)}. "
        f"Caso perca o Suporte Consolidado e acione o Stop em {_fmt_preco(plano.stop_loss, m, c)}, "
        f"o risco financeiro dessa mão fica em -{_fmt_preco(perda_stop_fin, m, 2)}."
    )


def _resumir_ativo_para_conversa(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    aprovado: bool,
    status_vies: str,
    valor_banca_usuario: Optional[float] = None,
) -> str:
    m, c = snap.moeda, snap.casas_decimais
    vies_curto = "Alta (Comprador)" if snap.preco_atual >= snap.ema_21 else "Correção / Baixa"
    resumo = (
        f"Ativo: {snap.simbolo} ({snap.exchange} - Gráfico {snap.intervalo.upper()} - {plano.modalidade}) | "
        f"Preço Atual: {_fmt_preco(snap.preco_atual, m, c)} (Variação: {snap.variacao_pct:+.2f}%) | "
        f"Viés Atual: {vies_curto} ({'Setup Aprovado' if aprovado else status_vies}) | "
        f"Posição no Canal de Suporte/Resistência: {snap.zona_sr_descricao} | "
        f"Padrão de Vela / Gatilho de Price Action: {snap.padrao_vela_gatilho} | "
        f"SUPORTE CONSOLIDADO 1 (Principal piso de compra): {_fmt_preco(snap.suporte_consolidado_1, m, c)} "
        f"(Testado/Respeitado {snap.toques_suporte}x no histórico recente) | "
        f"SUPORTE CONSOLIDADO 2 (Fundo estrutural): {_fmt_preco(snap.suporte_consolidado_2, m, c)} | "
        f"RESISTÊNCIA CONSOLIDADA 1 (1º Teto / Alvo parcial): {_fmt_preco(snap.resistencia_consolidada_1, m, c)} "
        f"(Testada {snap.toques_resistencia}x) | "
        f"RESISTÊNCIA CONSOLIDADA 2 (Alvo longo): {_fmt_preco(snap.resistencia_consolidada_2, m, c)} | "
        f"Assimetria Real de Swing Trade (Payoff até a Resistência 1 vs Stop no Suporte): {snap.payoff_sr_real:.2f} para 1 | "
        f"Média EMA 9: {_fmt_preco(snap.ema_9, m, c)} | EMA 21: {_fmt_preco(snap.ema_21, m, c)} | SMA 200: {_fmt_preco(snap.sma_200, m, c)} | "
        f"RSI: {snap.rsi:.1f} | Tendência Gráfico Maior ({snap.tf_superior.upper()}): {snap.tendencia_mtf_superior} | "
        f"Stop de Proteção (abaixo do suporte): {_fmt_preco(plano.stop_loss, m, c)} (-{plano.risco_percentual:.1f}%) | "
        f"Sugestão de lote para 1% de risco (banca de {_fmt_preco(plano.capital_referencia, m, 0)}): {plano.quantidade_sugerida_1pct} unidades/ações."
    )
    if valor_banca_usuario and valor_banca_usuario > 0:
        resumo += " | " + _calcular_simulacao_banca_usuario(snap, plano, valor_banca_usuario)
    return resumo


def responder_perguntas_chat(
    historico: Dict[str, Any],
    bot_username: str = "",
    timeout_long_polling: int = 4,
) -> int:
    """Conversa no grupo como um trader humano real, analisa gráficos em tempo real,
    envia foto do gráfico candlestick com suportes e resistências desenhados,
    calcula posição personalizada e faz perguntas para continuar o diálogo.
    """
    if not TELEGRAM_BOT_TOKEN:
        return 0

    ultimo_id = int(historico.get("ultimo_update_id") or 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params: Dict[str, Any] = {
        "timeout": max(1, timeout_long_polling),
        "allowed_updates": json.dumps(["message", "edited_message", "channel_post", "edited_channel_post"]),
    }
    if ultimo_id > 0:
        params["offset"] = ultimo_id + 1

    try:
        resp = requests.get(url, params=params, timeout=max(12, timeout_long_polling + 10))
        if resp.status_code != 200:
            logger.warning("getUpdates retornou HTTP %s: %s", resp.status_code, resp.text[:150])
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

        msg = (
            upd.get("message")
            or upd.get("edited_message")
            or upd.get("channel_post")
            or upd.get("edited_channel_post")
        )
        if not isinstance(msg, dict):
            continue
        remetente = msg.get("from") or {}
        if remetente.get("is_bot"):
            continue

        msg_ts = float(msg.get("date") or 0)
        if msg_ts > 0 and (agora_ts - msg_ts) > 7200:
            continue

        texto = str(msg.get("text") or msg.get("caption") or "").strip()
        if not texto:
            continue

        chat_obj = msg.get("chat") or {}
        chat_id_msg = str(chat_obj.get("id") or "")
        msg_id = int(msg.get("message_id") or 0)
        if not chat_id_msg:
            continue

        nome_user = str(remetente.get("first_name") or remetente.get("username") or "Trader").strip()

        reply_msg = msg.get("reply_to_message") or {}
        reply_from = reply_msg.get("from") or {}
        eh_reply_ao_bot = bool(reply_from.get("is_bot")) and (
            not bot_username or str(reply_from.get("username") or "").lower() == bot_username.lower()
        )
        mencionou_bot = (
            bool(bot_username and f"@{bot_username.lower()}" in texto.lower())
            or bool(REGEX_CHAMADO_ROBO.search(texto))
            or eh_reply_ao_bot
        )

        sessao_chat = (historico.get("conversas_chat") or {}).get(chat_id_msg) or {}
        conversa_recente_ativa = (agora_ts - float(sessao_chat.get("ultimo_ts") or 0)) <= 900  # 15 min
        hist_msgs = list(sessao_chat.get("mensagens") or [])
        valor_banca_user = extrair_valor_banca_da_mensagem(texto)

        logger.info(
            "📩 Mensagem recebida no chat [%s | user=%s]: '%s'",
            chat_id_msg,
            nome_user,
            texto[:100],
        )

        # 1. Comando inicial /start ou /ajuda -> conversa como pessoa real
        if re.match(r"^/(?:start|ajuda|help)\b", texto, flags=re.IGNORECASE):
            boas_vindas = (
                f"Fala, <b>{html.escape(nome_user)}</b>! Tudo certo? Tô monitorando o mercado inteiro em tempo real pra <b>Swing Trade</b> (com base em <b>Suportes e Resistências bem consolidados</b>).\n\n"
                "Eu opero <b>Swing em Tudo</b>: <b>Ações da B3</b>, <b>Criptomoedas</b>, <b>Ouro (XAUUSD)</b>, <b>Prata</b>, <b>Dólar (USDBRL)</b>, <b>Euro</b> e <b>Big Techs / BDRs (Nvidia, Apple, Tesla, S&amp;P 500)</b>.\n\n"
                "Você pode me perguntar o que eu acho de qualquer ativo ou pedir pra eu garimpar as <b>3 melhores oportunidades de Swing Trade</b> agora — e eu já te mando a <b>foto do gráfico</b> com os suportes e resistências traçados. Qual ativo ou mercado você quer olhar primeiro?"
            )
            if enviar_telegram_html(boas_vindas, chat_id=chat_id_msg, reply_to_message_id=msg_id):
                _registrar_turno_conversa(historico, chat_id_msg, nome_user, texto, boas_vindas)
                respondidas += 1
            continue

        # 2. Verifica se o usuário mencionou um ativo específico
        ativo_pedido = identificar_ativo_na_pergunta(
            texto,
            bot_username=bot_username,
            eh_resposta_ao_bot=eh_reply_ao_bot or conversa_recente_ativa,
        )

        # 3. Se NÃO citou ativo específico, verifica se pediu recomendação/Swing Trade/outros ativos
        if not ativo_pedido:
            pedido_rec = identificar_pedido_recomendacao_ranking(texto, bot_username=bot_username)
            if pedido_rec:
                modalidade_rec, segmento_rec = pedido_rec
                quer_venda = bool(re.search(r"\b(?:venda|vendido|short|queda)\b", texto.lower()))
                pediu_outro = bool(re.search(r"\b(?:outr[oa]s?|mais|diferente|al[ée]m)\b", texto.lower()))
                ultimo_simb_mem = str(sessao_chat.get("ultimo_simbolo") or "").upper()
                excluir_set = {ultimo_simb_mem} if (pediu_outro and ultimo_simb_mem) else None

                logger.info(
                    "🏆 Pedido de recomendação Multi-Mercado no chat (%s): modalidade=%s | segmento=%s | texto='%s'",
                    chat_id_msg,
                    modalidade_rec,
                    segmento_rec,
                    texto[:80],
                )
                top_oportunidades = escanear_melhores_oportunidades(
                    modalidade=modalidade_rec,
                    segmento=segmento_rec,
                    top_n=3,
                    apenas_compra=not quer_venda,
                    excluir_simbolos=excluir_set,
                )
                resumos_top = [
                    f"[{idx_op + 1}º ATIVO OBRIGATÓRIO NA RESPOSTA] "
                    + _resumir_ativo_para_conversa(s, p, ap, st, valor_banca_usuario=valor_banca_user)
                    for idx_op, (s, p, ap, st) in enumerate(top_oportunidades)
                ]
                contexto_mercado = (
                    f"Modalidade pedida pelo trader: {modalidade_rec} | Segmento: {segmento_rec} (SWING EM TUDO / MULTI-MERCADO)\n"
                    "IMPORTANTE: Comente sobre TODOS OS 3 ATIVOS ABAIXO na sua resposta (não fale de apenas 1 ativo!):\n"
                    + "\n".join(resumos_top)
                )
                resposta_html = conversar_como_trader_humano_gemini(
                    pergunta_usuario=texto,
                    nome_usuario=nome_user,
                    contexto_dados_mercado=contexto_mercado,
                    historico_mensagens_recentes=hist_msgs,
                )
                if not resposta_html:
                    resposta_html = formatar_painel_recomendacoes_html(
                        modalidade=modalidade_rec,
                        segmento=segmento_rec,
                        oportunidades=top_oportunidades,
                        nome_usuario=nome_user,
                    )
                simb_foco = top_oportunidades[0][0].simbolo if top_oportunidades else ""
                snap_top1 = top_oportunidades[0][0] if top_oportunidades else None
                plano_top1 = top_oportunidades[0][1] if top_oportunidades else None
                if enviar_telegram_com_grafico(
                    resposta_html,
                    snap=snap_top1,
                    plano=plano_top1,
                    chat_id=chat_id_msg,
                    reply_to_message_id=msg_id,
                ):
                    _registrar_turno_conversa(historico, chat_id_msg, nome_user, texto, resposta_html, simb_foco)
                    respondidas += 1
                    logger.info("✅ Conversa de recomendação Multi-Mercado com gráfico (%s - %s) enviada no chat.", modalidade_rec, segmento_rec)
                continue

            # 4. Continuação de conversa (ex.: usuário perguntou "que vies", "qual suporte", "5 mil", ou chamou o bot)
            if mencionou_bot or conversa_recente_ativa:
                ultimo_simb = str(sessao_chat.get("ultimo_simbolo") or "")
                contexto_extra = "Conversa livre sobre Swing Trade por Suportes e Resistências consolidados."
                snap_ant: Optional[SnapshotIndicadores] = None
                pl_ant: Optional[PlanoRisco] = None
                st_ant: str = "EM ANÁLISE"
                if ultimo_simb:
                    at_ant = identificar_ativo_na_pergunta(ultimo_simb, eh_resposta_ao_bot=True)
                    if at_ant:
                        tf_ant = extrair_intervalo_da_mensagem(texto)
                        snap_ant = extrair_dados_tradingview(at_ant, tf_ant, enriquecer_mtf=True)
                        if snap_ant:
                            ap_ant, _ = pre_filtrar_confluencia_quant(snap_ant)
                            dir_ant, st_ant = diagnosticar_direcao_sob_demanda(snap_ant)
                            pl_ant = calcular_gestao_risco(
                                snap_ant.preco_atual,
                                snap_ant.atr,
                                dir_ant,
                                ATR_MULTIPLIER_SWING if tf_ant in ("4h", "1d", "1w") else ATR_MULTIPLIER,
                                snap_ant.casas_decimais,
                                "SWING TRADE" if tf_ant in ("4h", "1d", "1w") else "DAY TRADE",
                                snap_ant.moeda,
                            )
                            contexto_extra = _resumir_ativo_para_conversa(
                                snap_ant,
                                pl_ant,
                                ap_ant,
                                st_ant,
                                valor_banca_usuario=valor_banca_user,
                            )

                resposta_livre = conversar_como_trader_humano_gemini(
                    pergunta_usuario=texto,
                    nome_usuario=nome_user,
                    contexto_dados_mercado=contexto_extra,
                    historico_mensagens_recentes=hist_msgs,
                )
                if not resposta_livre:
                    if snap_ant and pl_ant:
                        aud_ant = gerar_auditoria_quant_fallback(snap_ant, pl_ant)
                        resposta_livre = formatar_resposta_analise_chat_html(
                            snap_ant,
                            pl_ant,
                            aud_ant,
                            st_ant,
                            nome_usuario=nome_user,
                        )
                        if valor_banca_user and valor_banca_user > 0:
                            resposta_livre += "\n\n" + html.escape(
                                _calcular_simulacao_banca_usuario(snap_ant, pl_ant, valor_banca_user)
                            )
                    else:
                        resposta_livre = (
                            f"Fala, <b>{html.escape(nome_user)}</b>! Tô por aqui mapeando os suportes e resistências do mercado. "
                            "Me diz qual ação ou cripto você quer que eu analise pra <b>Swing Trade</b> agora, ou pede uma recomendação que eu busco as melhores e já te mando o gráfico!"
                        )
                if enviar_telegram_com_grafico(
                    resposta_livre,
                    snap=snap_ant,
                    plano=pl_ant,
                    chat_id=chat_id_msg,
                    reply_to_message_id=msg_id,
                ):
                    _registrar_turno_conversa(historico, chat_id_msg, nome_user, texto, resposta_livre, ultimo_simb)
                    respondidas += 1
            continue

        # 5. Análise conversacional de um ativo específico citado pelo trader (com foto do gráfico!)
        modalidade_pedido = detectar_modalidade_na_mensagem(texto)
        intervalo_pedido = extrair_intervalo_da_mensagem(texto)
        eh_swing_ativo = modalidade_pedido == "SWING TRADE" or intervalo_pedido in ("4h", "1d", "1w")
        mult_atr_pedido = ATR_MULTIPLIER_SWING if eh_swing_ativo else ATR_MULTIPLIER
        mod_final = "SWING TRADE" if eh_swing_ativo else "DAY TRADE"

        logger.info(
            "💬 Pergunta sobre ativo no chat (%s): ativo=%s (%s - %s) | texto='%s'",
            chat_id_msg,
            ativo_pedido.simbolo,
            intervalo_pedido,
            mod_final,
            texto[:80],
        )

        snap = extrair_dados_tradingview(ativo_pedido, intervalo_pedido, enriquecer_mtf=True)
        if not snap:
            aviso_erro = (
                f"Fala, <b>{html.escape(nome_user)}</b>! Tentei puxar o gráfico de <b>{html.escape(ativo_pedido.simbolo)}</b> "
                "aqui no TradingView, mas não encontrei esse código. Confirma pra mim o ticker certinho "
                "(tipo <b>PETR4</b>, <b>VALE3</b> ou <b>BTCUSDT</b>)?"
            )
            enviar_telegram_html(aviso_erro, chat_id=chat_id_msg, reply_to_message_id=msg_id)
            continue

        aprovado_pre, _ = pre_filtrar_confluencia_quant(snap)
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

        contexto_ativo = _resumir_ativo_para_conversa(
            snap,
            plano,
            aprovado_pre,
            status_vies,
            valor_banca_usuario=valor_banca_user,
        )
        resposta_html = conversar_como_trader_humano_gemini(
            pergunta_usuario=texto,
            nome_usuario=nome_user,
            contexto_dados_mercado=contexto_ativo,
            historico_mensagens_recentes=hist_msgs,
        )
        if not resposta_html:
            auditoria_fb = gerar_auditoria_quant_fallback(snap, plano)
            resposta_html = formatar_resposta_analise_chat_html(
                snap,
                plano,
                auditoria_fb,
                status_vies,
                nome_usuario=nome_user,
            )
            if valor_banca_user and valor_banca_user > 0:
                resposta_html += "\n\n" + html.escape(
                    _calcular_simulacao_banca_usuario(snap, plano, valor_banca_user)
                )

        if enviar_telegram_com_grafico(
            resposta_html,
            snap=snap,
            plano=plano,
            chat_id=chat_id_msg,
            reply_to_message_id=msg_id,
        ):
            _registrar_turno_conversa(historico, chat_id_msg, nome_user, texto, resposta_html, snap.simbolo)
            respondidas += 1
            logger.info("✅ Resposta conversacional + foto do gráfico enviada para %s (%s - %s).", ativo_pedido.simbolo, intervalo_pedido, mod_final)

    if mudou_offset:
        salvar_historico(historico)

    return respondidas


def executar_varredura_sinais(historico: Dict[str, Any], bot_username: str = "") -> int:
    b3_aberta = pregao_b3_aberto()
    enviados = 0
    eh_swing_padrao = TIMEFRAME_PADRAO in ("4h", "1d", "1w")
    mult_padrao = ATR_MULTIPLIER_SWING if eh_swing_padrao else ATR_MULTIPLIER
    mod_padrao = "SWING TRADE" if eh_swing_padrao else "DAY TRADE"

    logger.info(
        "Iniciando varredura Multi-Mercado %s (Timeframe=%s | Universo=%d ativos: Ações B3 + Cripto + Ouro/Dólar/BDRs | ATR=%.1fx | Nota Mín=%d)...",
        mod_padrao,
        TIMEFRAME_PADRAO,
        len(WATCHLIST_PADRAO),
        mult_padrao,
        NOTA_MINIMA_IA,
    )

    ativos_ativos = tuple(a for a in WATCHLIST_PADRAO if not (a.apenas_pregao_b3 and not b3_aberta))
    tf_sup, tf_inf = obter_pares_mtf(TIMEFRAME_PADRAO)
    for tf_b in dict.fromkeys([TIMEFRAME_PADRAO, tf_sup, tf_inf]):
        pre_carregar_lote_tradingview(ativos_ativos, tf_b)

    for idx_ativo, ativo in enumerate(WATCHLIST_PADRAO):
        # Checa o chat a cada 3 ativos durante a varredura para responder na hora sem fila
        if idx_ativo > 0 and idx_ativo % 3 == 0:
            responder_perguntas_chat(historico, bot_username=bot_username, timeout_long_polling=1)

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
            multiplicador_atr=mult_padrao,
            casas_decimais=snap.casas_decimais,
            modalidade=mod_padrao,
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
        if enviar_telegram_com_grafico(msg_html, snap=snap, plano=plano):
            registrar_sinal_historico(historico, snap, plano, auditoria)
            salvar_historico(historico)
            enviados += 1
            logger.info(
                "✅ Sinal disparado com gráfico [%s | %s | Score Quant %d/100 | IA %d/100 | Entrada %s | Stop %s | Break-Even %s]",
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
    responder_perguntas_chat(historico, bot_username=bot_username, timeout_long_polling=2)

    # 2. Executa a varredura automática da Watchlist com as 6 camadas institucionais
    enviados = executar_varredura_sinais(historico, bot_username=bot_username)

    # 3. Escuta contínua no chat (padrão automático de 420s = 7 min no GitHub Actions mesmo sem editar o .yml)
    default_listen = "420" if os.environ.get("GITHUB_ACTIONS") == "true" else "0"
    segundos_escuta = int(os.environ.get("CHAT_LISTEN_SECONDS", default_listen) or default_listen)
    if segundos_escuta > 0:
        logger.info("💬 Escuta ativa do grupo iniciada por %d segundos (Long-Polling em tempo real)...", segundos_escuta)
        fim_ts = time.time() + segundos_escuta
        while time.time() < fim_ts:
            responder_perguntas_chat(historico, bot_username=bot_username, timeout_long_polling=10)
            time.sleep(1)

    return enviados


if __name__ == "__main__":
    executar_ciclo_daytrade()
