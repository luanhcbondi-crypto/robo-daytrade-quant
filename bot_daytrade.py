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
NOTA_MINIMA_IA = int(os.environ.get("NOTA_MINIMA_IA", "75"))
COOLDOWN_SINAL_MINUTOS = int(os.environ.get("COOLDOWN_SINAL_MINUTOS", "180"))
MAX_SINAIS_POR_CICLO = int(os.environ.get("MAX_SINAIS_POR_CICLO", "3"))

TZ_BRT = dt.timezone(dt.timedelta(hours=-3))

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODELS_FALLBACK: Tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
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

MAPA_INTERVALOS_TV: Dict[str, str] = {
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
}


@dataclass(frozen=True)
class AtivoMonitorado:
    simbolo: str
    exchange: str
    screener: str
    moeda: str
    casas_decimais: int = 2
    apenas_pregao_b3: bool = False


# Watchlist padrão para varredura automática: Blue Chips B3 + Criptoativos Majors 24/7
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
REGEX_INTENCAO_PERGUNTA = re.compile(
    r"(?:o\s*que\s+(?:vc\s+|voc[êe]\s+)?acha|oque\s+acha|analis[ae]|an[áa]lise|avalia|avaliar|"
    r"como\s+(?:est[áa]|t[áa]|ta)\b|gr[áa]fico|setup|vale\s+a\s+pena|compra\s+ou\s+venda|"
    r"entrar\s+em|opini[ãa]o\s+sobre|tend[êe]ncia\s+d[eo]|alvo\s+para|stop\s+para)",
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


@dataclass
class PlanoRisco:
    direcao: str
    entrada: float
    stop_loss: float
    risco_unitario: float
    risco_percentual: float
    alvo_1: float  # 1:1.5
    alvo_2: float  # 1:2.0
    alvo_3: float  # 1:3.0


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
# 3. EXTRAÇÃO DE DADOS DO TRADINGVIEW (tradingview-ta) E PRÉ-FILTRO QUANT
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


def extrair_dados_tradingview(ativo: AtivoMonitorado, intervalo_str: str = TIMEFRAME_PADRAO) -> Optional[SnapshotIndicadores]:
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

        ema_9 = _num(ind.get("EMA9"), _num(ind.get("EMA10"), close))
        ema_21 = _num(ind.get("EMA21"), _num(ind.get("EMA20"), close))
        sma_200 = _num(ind.get("SMA200"), _num(ind.get("EMA200"), close))

        rsi = _num(ind.get("RSI"), 50.0)
        macd_line = _num(ind.get("MACD.macd"))
        macd_signal = _num(ind.get("MACD.signal"))
        macd_hist = macd_line - macd_signal
        volume = _num(ind.get("volume"))
        adx = _num(ind.get("ADX"), 20.0)

        atr_raw = _num(ind.get("ATR"))
        if atr_raw <= 0:
            bb_up = _num(ind.get("BB.upper"))
            bb_low = _num(ind.get("BB.lower"))
            amplitude_vela = abs(high - low)
            amplitude_bb = abs(bb_up - bb_low) / 4.0 if (bb_up > bb_low > 0) else 0.0
            atr_raw = max(amplitude_vela, amplitude_bb, close * 0.005)

        rec_tv = str(summary.get("RECOMMENDATION") or "NEUTRAL").upper()
        casas = 4 if (close < 1.0 and ativo.casas_decimais == 2) else ativo.casas_decimais

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
        )
    except Exception as exc:
        logger.warning("Falha ao extrair TradingView para %s:%s (%s)", ativo.exchange, ativo.simbolo, str(exc)[:120])
        return None


def pre_filtrar_confluencia_quant(snap: SnapshotIndicadores) -> Tuple[bool, str]:
    """Filtra ruídos matematicamente antes de acionar a IA no scanner automático."""
    if snap.volume <= 0 and snap.screener != "forex" and snap.screener != "cfd":
        return False, "Sem volume negociado no período."
    if snap.atr <= 0 or snap.preco_atual <= 0:
        return False, "ATR ou preço inválido."

    spread_emas_pct = abs(snap.ema_9 - snap.ema_21) / snap.preco_atual
    if spread_emas_pct < 0.0010:
        return False, f"Consolidação: EMA9 e EMA21 emboladas (spread {spread_emas_pct * 100:.3f}%)."

    if 0 < snap.adx < 17.0:
        return False, f"Consolidação: tendência sem força direcional (ADX {snap.adx:.1f} < 17)."

    dist_ema9_atr = abs(snap.preco_atual - snap.ema_9) / snap.atr
    if dist_ema9_atr > 2.4:
        return False, f"Preço esticado demais em relação à EMA9 ({dist_ema9_atr:.1f}x ATR — risco de violinada)."

    tendencia_alta = snap.preco_atual > snap.ema_9 and snap.ema_9 > snap.ema_21 and snap.preco_atual > snap.sma_200
    momentum_alta = snap.macd_line > snap.macd_signal and 52.0 <= snap.rsi <= 73.0

    tendencia_baixa = snap.preco_atual < snap.ema_9 and snap.ema_9 < snap.ema_21 and snap.preco_atual < snap.sma_200
    momentum_baixa = snap.macd_line < snap.macd_signal and 27.0 <= snap.rsi <= 48.0

    if tendencia_alta and momentum_alta:
        snap.direcao_preliminar = "COMPRA"
        return True, "Confluência preliminar de COMPRA detectada."
    if tendencia_baixa and momentum_baixa:
        snap.direcao_preliminar = "VENDA"
        return True, "Confluência preliminar de VENDA detectada."

    return False, "Sem alinhamento simultâneo de tendência (EMA9/21 + SMA200) e momentum (MACD + RSI)."


def diagnosticar_direcao_sob_demanda(snap: SnapshotIndicadores) -> Tuple[str, str]:
    """Para consultas no chat, classifica o viés técnico atual mesmo que o ativo esteja lateral."""
    ok_confluencia, _ = pre_filtrar_confluencia_quant(snap)
    if ok_confluencia and snap.direcao_preliminar in ("COMPRA", "VENDA"):
        return snap.direcao_preliminar, "CONFLUÊNCIA ATIVA"

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
    if snap.macd_line > snap.macd_signal:
        pontos_alta += 1
    else:
        pontos_baixa += 1
    if snap.rsi >= 53.0:
        pontos_alta += 1
    elif snap.rsi <= 47.0:
        pontos_baixa += 1

    spread_emas_pct = abs(snap.ema_9 - snap.ema_21) / max(snap.preco_atual, 1e-9)
    if spread_emas_pct < 0.0010 or (0 < snap.adx < 17.0):
        vies = "COMPRA" if pontos_alta >= pontos_baixa else "VENDA"
        return vies, "CONSOLIDAÇÃO / AGUARDAR ROMPIMENTO"

    if pontos_alta > pontos_baixa:
        return "COMPRA", "VIÉS COMPRADOR (PARCIAL)"
    return "VENDA", "VIÉS VENDEDOR (PARCIAL)"


# ==============================================================================
# 4. GESTÃO MATEMÁTICA DE RISCO ESTRITA (ENTRADA, STOP ATR, ALVOS 1:1.5, 1:2, 1:3)
# ==============================================================================
def calcular_gestao_risco(
    preco_entrada: float,
    atr: float,
    direcao: str,
    multiplicador_atr: float = ATR_MULTIPLIER,
    casas_decimais: int = 2,
) -> PlanoRisco:
    if preco_entrada <= 0 or atr <= 0:
        raise ValueError("Preço de entrada e ATR devem ser maiores que zero.")

    dir_norm = direcao.strip().upper()
    if dir_norm not in ("COMPRA", "VENDA"):
        raise ValueError(f"Direção inválida para cálculo de risco: {direcao}")

    risco_r = max(atr * multiplicador_atr, preco_entrada * 0.0025)
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

    return PlanoRisco(
        direcao=dir_norm,
        entrada=round(preco_entrada, casas_decimais),
        stop_loss=round(stop_loss, casas_decimais),
        risco_unitario=round(risco_r, casas_decimais),
        risco_percentual=round(risco_pct, 2),
        alvo_1=round(alvo_1, casas_decimais),
        alvo_2=round(alvo_2, casas_decimais),
        alvo_3=round(alvo_3, casas_decimais),
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
        "Você é um Quant Trader Sênior e Gestor de Risco Institucional. "
        "Sua função é auditar setups algorítmicos de Day Trade extraídos do TradingView e VETAR "
        "qualquer operação com ruído, consolidação lateral, baixo volume, divergência de momentum, "
        "exaustão de RSI ou preço excessivamente esticado em relação às médias móveis. "
        "Nunca invente valores fora do JSON fornecido e responda estritamente em JSON válido."
    )

    payload_indicadores = {
        "ativo": f"{snap.exchange}:{snap.simbolo}",
        "timeframe": snap.intervalo,
        "direcao_avaliada": plano.direcao,
        "cotacao": {
            "abertura": round(snap.abertura, snap.casas_decimais),
            "maxima": round(snap.maxima, snap.casas_decimais),
            "minima": round(snap.minima, snap.casas_decimais),
            "fechamento_atual": round(snap.preco_atual, snap.casas_decimais),
            "volume": round(snap.volume, 2),
        },
        "indicadores": {
            "ema_9": round(snap.ema_9, snap.casas_decimais),
            "ema_21": round(snap.ema_21, snap.casas_decimais),
            "sma_200": round(snap.sma_200, snap.casas_decimais),
            "rsi_14": round(snap.rsi, 2),
            "macd_line": round(snap.macd_line, 4),
            "macd_signal": round(snap.macd_signal, 4),
            "macd_histograma": round(snap.macd_hist, 4),
            "atr_14": round(snap.atr, snap.casas_decimais),
            "adx_14": round(snap.adx, 2),
            "consenso_tradingview": snap.recomendacao_tv,
        },
        "plano_risco_algoritmico": asdict(plano),
    }

    bloco_pergunta = (
        f"\nPERGUNTA DO INVESTIDOR NO CHAT: \"{pergunta_usuario.strip()}\"\n"
        "Responda diretamente à dúvida do investidor dentro do campo 'racional_tecnico', "
        "explicando com clareza se o gráfico está favorável para entrada agora ou se é melhor aguardar.\n"
        if pergunta_usuario.strip()
        else ""
    )

    user_prompt = (
        "Audite o seguinte JSON de indicadores em tempo real do TradingView:\n\n"
        f"{json.dumps(payload_indicadores, ensure_ascii=False, indent=2)}\n"
        f"{bloco_pergunta}\n"
        "REGRAS DE AUDITORIA QUANTITATIVA:\n"
        "1. VETE (aprovado=false) se notar consolidação/lateralização (EMA9 muito próxima da EMA21, ADX fraco ou preço preso sem direção clara).\n"
        "2. VETE (aprovado=false) se o RSI indicar exaustão perigosa (>72 na COMPRA ou <28 na VENDA) ou falta de força direcional.\n"
        "3. VETE (aprovado=false) se houver divergência entre a direção avaliada, a tendência primária (SMA 200) e o histograma MACD.\n"
        "4. APROVE (aprovado=true) somente se houver alinhamento limpo de tendência (EMA 9/21 + SMA 200), momentum (RSI + MACD), volume e volatilidade (ATR) compatível com o Stop Loss.\n\n"
        "Retorne APENAS um objeto JSON com as chaves exatas:\n"
        "{\n"
        '  "aprovado": boolean,\n'
        '  "nota_confluencia": integer (0 a 100),\n'
        '  "regime_mercado": "TENDENCIA_FORTE" | "TENDENCIA_MODERADA" | "CONSOLIDACAO" | "EXAUSTAO",\n'
        '  "racional_tecnico": "string em português (2 a 3 frases diretas citando os números exatos de EMA9/21, SMA200, RSI, MACD e ATR)",\n'
        '  "alerta_risco": "string curta (1 frase objetiva sobre o risco principal ou gatilho a aguardar)",\n'
        '  "motivo_veto": "string curta caso aprovado=false, ou vazio caso aprovado=true"\n'
        "}"
    )
    return system_instruction, user_prompt


def gerar_auditoria_quant_fallback(snap: SnapshotIndicadores, plano: PlanoRisco) -> AuditoriaIA:
    """Fallback determinístico caso a API do Gemini esteja temporariamente em HTTP 503,
    garantindo que o usuário no chat sempre receba a leitura técnica real do gráfico.
    """
    ok_pre, motivo = pre_filtrar_confluencia_quant(snap)
    m, c = snap.moeda, snap.casas_decimais
    pos_sma = "acima" if snap.preco_atual >= snap.sma_200 else "abaixo"
    pos_macd = "positivo" if snap.macd_hist >= 0 else "negativo"

    if ok_pre:
        nota = 80 if snap.adx >= 25 else 76
        regime = "TENDENCIA_FORTE" if snap.adx >= 25 else "TENDENCIA_MODERADA"
        racional = (
            f"O ativo opera a {_fmt_preco(snap.preco_atual, m, c)} com alinhamento entre EMA 9 "
            f"({_fmt_preco(snap.ema_9, m, c)}), EMA 21 ({_fmt_preco(snap.ema_21, m, c)}) e {pos_sma} da SMA 200 "
            f"({_fmt_preco(snap.sma_200, m, c)}). O RSI em {snap.rsi:.1f} e o histograma MACD {pos_macd} "
            f"({snap.macd_hist:+.4f}) confirmam momentum a favor do movimento."
        )
        alerta = f"Respeitar rigorosamente o Stop Loss técnico calculado por {ATR_MULTIPLIER:.1f}x ATR em {_fmt_preco(plano.stop_loss, m, c)}."
        return AuditoriaIA(True, nota, regime, racional, alerta, "", "Motor Quant Local")

    regime = "CONSOLIDACAO" if (0 < snap.adx < 17.0 or abs(snap.ema_9 - snap.ema_21) / snap.preco_atual < 0.0012) else "INDEFINIDO"
    racional = (
        f"No gráfico de {snap.intervalo}, {snap.simbolo} negocia a {_fmt_preco(snap.preco_atual, m, c)} "
        f"(EMA 9 em {_fmt_preco(snap.ema_9, m, c)}, EMA 21 em {_fmt_preco(snap.ema_21, m, c)} e SMA 200 em "
        f"{_fmt_preco(snap.sma_200, m, c)}), com RSI em {snap.rsi:.1f} e ADX em {snap.adx:.1f}. "
        f"No momento não há confluência limpa para entrada imediata ({motivo})."
    )
    alerta = "Aguardar rompimento com expansão de volume e alinhamento das médias antes de abrir posição."
    return AuditoriaIA(False, 48, regime, racional, alerta, motivo, "Motor Quant Local")


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
# 6. ATENDIMENTO INTERATIVO NO CHAT DO TELEGRAM ("O QUE ACHA DE TAL ATIVO?")
# ==============================================================================
def extrair_intervalo_da_mensagem(texto: str) -> str:
    t = (texto or "").lower()
    for padrao, tf in (
        (r"\b(?:5\s*m|5\s*min|m5)\b", "5m"),
        (r"\b(?:15\s*m|15\s*min|m15)\b", "15m"),
        (r"\b(?:30\s*m|30\s*min|m30)\b", "30m"),
        (r"\b(?:1\s*h|60\s*m|60\s*min|h1)\b", "1h"),
        (r"\b(?:4\s*h|h4)\b", "4h"),
        (r"\b(?:1\s*d|di[áa]rio|d1)\b", "1d"),
    ):
        if re.search(padrao, t):
            return tf
    return TIMEFRAME_PADRAO


def identificar_ativo_na_pergunta(texto: str, bot_username: str = "") -> Optional[AtivoMonitorado]:
    if not texto:
        return None

    t_up = texto.upper()
    t_low = texto.lower()

    # 1. Verifica comando explícito (/analisar PETR4, /grafico BTC, etc.) ou menção/pergunta
    eh_comando = bool(REGEX_COMANDO_CHAT.search(t_low))
    mencionou_bot = bool(bot_username and f"@{bot_username.lower()}" in t_low)
    tem_intencao = eh_comando or mencionou_bot or bool(REGEX_INTENCAO_PERGUNTA.search(t_low))

    # 2. Procura par cripto explícito (ex.: BTCUSDT, SOLUSDT, PEPEUSDT)
    m_cripto = REGEX_PAR_CRIPTO.search(t_up)
    if m_cripto and (tem_intencao or len(t_up.split()) <= 4):
        simb = m_cripto.group(1)
        for a in WATCHLIST_PADRAO:
            if a.simbolo == simb:
                return a
        return AtivoMonitorado(simb, "BINANCE", "crypto", "US$", 2, False)

    # 3. Procura ticker padrão B3 (ex.: PETR4, VALE3, KLBN11, HGLG11, NVDC34, SMFT3)
    m_b3 = REGEX_TICKER_B3.search(t_up)
    if m_b3 and (tem_intencao or len(t_up.split()) <= 4):
        simb = m_b3.group(1)
        for a in WATCHLIST_PADRAO:
            if a.simbolo == simb:
                return a
        return AtivoMonitorado(simb, "BMFBOVESPA", "brazil", "R$", 2, True)

    # 4. Procura nomes/apelidos conhecidos (ex.: "o que acha do bitcoin?", "analisa petrobras", "como tá o dólar?")
    if tem_intencao or len(t_low.split()) <= 3:
        for apelido in sorted(ALIASES_ATIVOS_CHAT.keys(), key=len, reverse=True):
            if re.search(r"(?<!\w)" + re.escape(apelido) + r"(?!\w)", t_low):
                return ALIASES_ATIVOS_CHAT[apelido]

    # 5. Se usou comando explícito (/analisar SUI), tenta resolver cripto
    if eh_comando:
        limpo = REGEX_COMANDO_CHAT.sub("", t_up).strip()
        if bot_username:
            limpo = re.sub(rf"@{re.escape(bot_username.upper())}\b", "", limpo).strip()
        tokens = [tok for tok in re.findall(r"[A-Z0-9]{2,10}", limpo) if tok not in {"5M", "15M", "30M", "1H", "4H", "1D"}]
        if tokens:
            tk = tokens[0]
            if tk.lower() in ALIASES_ATIVOS_CHAT:
                return ALIASES_ATIVOS_CHAT[tk.lower()]
            if not tk.endswith("USDT") and len(tk) <= 5:
                return AtivoMonitorado(f"{tk}USDT", "BINANCE", "crypto", "US$", 2, False)

    return None


def formatar_resposta_analise_chat_html(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    auditoria: AuditoriaIA,
    status_vies: str,
) -> str:
    m, c = snap.moeda, snap.casas_decimais
    agora_str = dt.datetime.now(TZ_BRT).strftime("%d/%m/%Y às %H:%M BRT")

    if auditoria.aprovado:
        badge_status = (
            "🟢 <b>SINAL APROVADO — COMPRA (LONG)</b>"
            if plano.direcao == "COMPRA"
            else "🔴 <b>SINAL APROVADO — VENDA (SHORT)</b>"
        )
        titulo_niveis = f"🎯 <b>PLANO DE TRADE ({plano.direcao} · ATR {ATR_MULTIPLIER:.1f}x)</b>"
    else:
        icone_vies = "🟡" if "CONSOLIDAÇÃO" in status_vies else ("🟢" if plano.direcao == "COMPRA" else "🔴")
        badge_status = f"{icone_vies} <b>{html.escape(status_vies)} — SEM ENTRADA AGORA</b>"
        titulo_niveis = f"📐 <b>NÍVEIS DE REFERÊNCIA ({plano.direcao} SE CONFIRMAR ROMPIMENTO)</b>"

    racional_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.racional_tecnico))
    alerta_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.alerta_risco))
    regime_limpo = html.escape(auditoria.regime_mercado.replace("_", " "))

    return (
        f"<blockquote>🔎 <b>ANÁLISE GRÁFICA SOB DEMANDA</b>  ·  <code>#{html.escape(snap.simbolo)}</code>  ·  <b>{html.escape(snap.intervalo.upper())}</b></blockquote>\n\n"
        f"{badge_status}\n"
        f"🏛️ <b>Bolsa:</b> {html.escape(snap.exchange)}  |  📊 <b>Força Técnica:</b> <code>{auditoria.nota_confluencia}/100</code> ({regime_limpo})\n\n"
        f"🧠 <b>PARECER DO ANALISTA QUANT ({html.escape(auditoria.modelo_utilizado)})</b>\n"
        f"{racional_limpo}\n\n"
        f"{titulo_niveis}\n"
        f"• <b>Preço Atual:</b> <code>{_fmt_preco(plano.entrada, m, c)}</code>\n"
        f"• <b>Stop Técnico (ATR):</b> <code>{_fmt_preco(plano.stop_loss, m, c)}</code> <i>({plano.risco_percentual:.2f}%)</i>\n"
        f"• <b>Alvo 1 (1:1.5):</b> <code>{_fmt_preco(plano.alvo_1, m, c)}</code>\n"
        f"• <b>Alvo 2 (1:2.0):</b> <code>{_fmt_preco(plano.alvo_2, m, c)}</code>\n"
        f"• <b>Alvo 3 (1:3.0):</b> <code>{_fmt_preco(plano.alvo_3, m, c)}</code>\n\n"
        f"📊 <b>INDICADORES TRADINGVIEW ({html.escape(snap.intervalo.upper())})</b>\n"
        f"• <b>EMA 9 / EMA 21:</b> <code>{_fmt_preco(snap.ema_9, m, c)}</code> / <code>{_fmt_preco(snap.ema_21, m, c)}</code>\n"
        f"• <b>SMA 200:</b> <code>{_fmt_preco(snap.sma_200, m, c)}</code>\n"
        f"• <b>RSI (14):</b> <code>{snap.rsi:.1f}</code>  |  <b>ADX:</b> <code>{snap.adx:.1f}</code>\n"
        f"• <b>MACD / Sinal:</b> <code>{snap.macd_line:.4f}</code> / <code>{snap.macd_signal:.4f}</code> (Hist: <code>{snap.macd_hist:+.4f}</code>)\n"
        f"• <b>ATR (14):</b> <code>{_fmt_preco(snap.atr, m, c)}</code>  |  <b>Consenso TV:</b> <code>{html.escape(snap.recomendacao_tv)}</code>\n\n"
        f"⚠️ <b>Atenção:</b> {alerta_limpo}\n"
        f"<i>🕒 {agora_str} · Análise quantitativa educacional (não é recomendação de investimento).</i>"
    )


# ==============================================================================
# 7. FORMATAÇÃO DE SINAIS AUTOMÁTICOS, HISTÓRICO E INTEGRAÇÃO TELEGRAM
# ==============================================================================
def _fmt_preco(valor: float, moeda: str, casas: int) -> str:
    s = f"{valor:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{moeda} {s}"


def formatar_mensagem_sinal_html(
    snap: SnapshotIndicadores,
    plano: PlanoRisco,
    auditoria: AuditoriaIA,
) -> str:
    icone_dir = "🟢 <b>COMPRA (LONG)</b>" if plano.direcao == "COMPRA" else "🔴 <b>VENDA (SHORT)</b>"
    agora_str = dt.datetime.now(TZ_BRT).strftime("%d/%m/%Y às %H:%M BRT")
    m, c = snap.moeda, snap.casas_decimais

    racional_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.racional_tecnico))
    alerta_limpo = html.escape(re.sub(r"[*_`#]+", "", auditoria.alerta_risco))
    regime_limpo = html.escape(auditoria.regime_mercado.replace("_", " "))

    return (
        f"<blockquote>⚡ <b>RADAR QUANT DAY TRADE</b>  ·  <code>#{html.escape(snap.simbolo)}</code>  ·  <b>{html.escape(snap.intervalo.upper())}</b></blockquote>\n\n"
        f"{icone_dir}\n"
        f"🏛️ <b>Bolsa:</b> {html.escape(snap.exchange)}  |  📊 <b>Confluência IA:</b> <code>{auditoria.nota_confluencia}/100</code> ({regime_limpo})\n\n"
        f"🎯 <b>GESTÃO MATEMÁTICA DE RISCO (ATR {ATR_MULTIPLIER:.1f}x)</b>\n"
        f"• <b>Entrada:</b> <code>{_fmt_preco(plano.entrada, m, c)}</code>\n"
        f"• <b>Stop Loss:</b> <code>{_fmt_preco(plano.stop_loss, m, c)}</code> <i>(-{plano.risco_percentual:.2f}%)</i>\n"
        f"• <b>Alvo 1 (1:1.5):</b> <code>{_fmt_preco(plano.alvo_1, m, c)}</code>\n"
        f"• <b>Alvo 2 (1:2.0):</b> <code>{_fmt_preco(plano.alvo_2, m, c)}</code>\n"
        f"• <b>Alvo 3 (1:3.0):</b> <code>{_fmt_preco(plano.alvo_3, m, c)}</code>\n\n"
        f"📐 <b>INDICADORES EM TEMPO REAL (TRADINGVIEW)</b>\n"
        f"• <b>EMA 9 / EMA 21:</b> <code>{_fmt_preco(snap.ema_9, m, c)}</code> / <code>{_fmt_preco(snap.ema_21, m, c)}</code>\n"
        f"• <b>SMA 200:</b> <code>{_fmt_preco(snap.sma_200, m, c)}</code>\n"
        f"• <b>RSI (14):</b> <code>{snap.rsi:.1f}</code>  |  <b>ADX:</b> <code>{snap.adx:.1f}</code>\n"
        f"• <b>MACD / Sinal:</b> <code>{snap.macd_line:.4f}</code> / <code>{snap.macd_signal:.4f}</code> (Hist: <code>{snap.macd_hist:+.4f}</code>)\n"
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
            "entrada": plano.entrada,
            "stop_loss": plano.stop_loss,
            "alvo_1": plano.alvo_1,
            "alvo_2": plano.alvo_2,
            "alvo_3": plano.alvo_3,
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
    """Lê novas mensagens enviadas no grupo/chat (getUpdates), identifica pedidos de análise
    de ativos (ex.: 'o que acha de PETR4?', '/analisar BTC 1h') e responde com o diagnóstico completo.
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

        if re.match(r"^/(?:start|ajuda|help)\b", texto, flags=re.IGNORECASE):
            ajuda_html = (
                "🤖 <b>Analista Quant Day Trade Ativo!</b>\n\n"
                "Você pode me pedir a análise técnica em tempo real de qualquer ação da B3 ou Criptomoeda:\n"
                "• <code>/analisar PETR4</code> ou <code>/analisar BTC 15m</code>\n"
                "• Ou pergunte naturalmente: <i>\"O que acha de VALE3?\"</i>, <i>\"Analisa ITUB4 1h\"</i>, <i>\"Como está o Bitcoin?\"</i>"
            )
            enviar_telegram_html(ajuda_html, chat_id=chat_id_msg, reply_to_message_id=msg_id)
            continue

        ativo_pedido = identificar_ativo_na_pergunta(texto, bot_username=bot_username)
        if not ativo_pedido:
            continue

        intervalo_pedido = extrair_intervalo_da_mensagem(texto)
        logger.info(
            "💬 Pergunta detectada no chat (%s): ativo=%s (%s) | texto='%s'",
            chat_id_msg,
            ativo_pedido.simbolo,
            intervalo_pedido,
            texto[:80],
        )

        snap = extrair_dados_tradingview(ativo_pedido, intervalo_pedido)
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
            multiplicador_atr=ATR_MULTIPLIER,
            casas_decimais=snap.casas_decimais,
        )

        auditoria = auditar_sinal_com_gemini(snap, plano, pergunta_usuario=texto)
        if not auditoria:
            auditoria = gerar_auditoria_quant_fallback(snap, plano)

        resposta_html = formatar_resposta_analise_chat_html(snap, plano, auditoria, status_vies)
        if enviar_telegram_html(resposta_html, chat_id=chat_id_msg, reply_to_message_id=msg_id):
            respondidas += 1
            logger.info("✅ Resposta enviada no chat para %s (%s).", ativo_pedido.simbolo, intervalo_pedido)

    if mudou_offset:
        salvar_historico(historico)

    return respondidas


def executar_varredura_sinais(historico: Dict[str, Any]) -> int:
    b3_aberta = pregao_b3_aberto()
    enviados = 0

    logger.info(
        "Iniciando varredura Quant Day Trade (Timeframe=%s | Pregão B3=%s | ATR=%.1fx)...",
        TIMEFRAME_PADRAO,
        "ABERTO" if b3_aberta else "FECHADO (somente Cripto 24/7)",
        ATR_MULTIPLIER,
    )

    for ativo in WATCHLIST_PADRAO:
        if enviados >= MAX_SINAIS_POR_CICLO:
            break
        if ativo.apenas_pregao_b3 and not b3_aberta:
            continue

        snap = extrair_dados_tradingview(ativo, TIMEFRAME_PADRAO)
        if not snap:
            continue

        aprovado_pre, motivo_pre = pre_filtrar_confluencia_quant(snap)
        if not aprovado_pre:
            logger.info("[%s] Pré-filtro vetou: %s", ativo.simbolo, motivo_pre)
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
        )

        auditoria = auditar_sinal_com_gemini(snap, plano)
        if not auditoria:
            logger.warning("[%s] Auditoria IA indisponível; sinal descartado por segurança.", snap.simbolo)
            continue

        if not auditoria.aprovado:
            logger.info(
                "[%s] Vetado pela IA (%s | Nota %d/100): %s",
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
                "✅ Sinal disparado [%s | %s | Nota %d/100 | Entrada %s | Stop %s]",
                snap.simbolo,
                plano.direcao,
                auditoria.nota_confluencia,
                plano.entrada,
                plano.stop_loss,
            )

    logger.info("Varredura finalizada: %d sinal(is) aprovado(s) e enviado(s).", enviados)
    return enviados


def executar_ciclo_daytrade() -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
        logger.critical("Configure TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID e GEMINI_API_KEY nos Secrets.")
        sys.exit(1)

    historico = carregar_historico()
    bot_username = obter_username_bot()

    # 1. Responde imediatamente a quaisquer perguntas pendentes no chat
    responder_perguntas_chat(historico, bot_username=bot_username)

    # 2. Executa a varredura automática da Watchlist
    enviados = executar_varredura_sinais(historico)

    # 3. Escuta contínua do chat durante a janela do GitHub Actions (responde em ~8s)
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
