import datetime as dt
import html
import json
import logging
import os
import re
import socket
import sys
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
    "gemini-3.6-flash",
    "gemini-3-flash",
    "gemini-2.5-flash",
)
_MODELOS_GEMINI_EXCLUSAO = (
    "embedding", "imagen", "veo", "tts", "audio", "live", "robotics",
    "computer-use", "deep-research", "aqa", "gemma", "learnlm", "vision",
    "image", "customtools", "thinking-exp", "omni", "transcribe", "translate",
    "nano-banana",
)
_CACHE_MODELOS_ATIVOS: List[str] = []
_MODELOS_BLOQUEADOS_SESSAO: Set[str] = set()

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


# Watchlist padrão: Blue Chips B3 de alta liquidez + Criptoativos Majors 24/7
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

        # EMA 9 e EMA 21 com fallback limpo para EMA10/EMA20 caso a exchange omita a chave customizada
        ema_9 = _num(ind.get("EMA9"), _num(ind.get("EMA10"), close))
        ema_21 = _num(ind.get("EMA21"), _num(ind.get("EMA20"), close))
        sma_200 = _num(ind.get("SMA200"), _num(ind.get("EMA200"), close))

        rsi = _num(ind.get("RSI"), 50.0)
        macd_line = _num(ind.get("MACD.macd"))
        macd_signal = _num(ind.get("MACD.signal"))
        macd_hist = macd_line - macd_signal
        volume = _num(ind.get("volume"))
        adx = _num(ind.get("ADX"), 20.0)

        # ATR real do TradingView com fallback seguro baseado na amplitude da vela / Bollinger
        atr_raw = _num(ind.get("ATR"))
        if atr_raw <= 0:
            bb_up = _num(ind.get("BB.upper"))
            bb_low = _num(ind.get("BB.lower"))
            amplitude_vela = abs(high - low)
            amplitude_bb = abs(bb_up - bb_low) / 4.0 if (bb_up > bb_low > 0) else 0.0
            atr_raw = max(amplitude_vela, amplitude_bb, close * 0.005)

        rec_tv = str(summary.get("RECOMMENDATION") or "NEUTRAL").upper()

        return SnapshotIndicadores(
            simbolo=ativo.simbolo,
            exchange=ativo.exchange,
            screener=ativo.screener,
            intervalo=intervalo_str,
            moeda=ativo.moeda,
            casas_decimais=ativo.casas_decimais,
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
    """Filtra ruídos matematicamente antes de acionar a IA:
    - Veta ativos sem volume ou ATR inválido;
    - Veta mercado lateral/consolidação (médias coladas, ADX fraco ou RSI morto);
    - Exige alinhamento mínimo entre tendência (EMA9/EMA21/SMA200) e momentum (MACD/RSI).
    """
    if snap.volume <= 0:
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

    # Confluência de COMPRA (LONG)
    tendencia_alta = snap.preco_atual > snap.ema_9 and snap.ema_9 > snap.ema_21 and snap.preco_atual > snap.sma_200
    momentum_alta = snap.macd_line > snap.macd_signal and 52.0 <= snap.rsi <= 73.0

    # Confluência de VENDA (SHORT)
    tendencia_baixa = snap.preco_atual < snap.ema_9 and snap.ema_9 < snap.ema_21 and snap.preco_atual < snap.sma_200
    momentum_baixa = snap.macd_line < snap.macd_signal and 27.0 <= snap.rsi <= 48.0

    if tendencia_alta and momentum_alta:
        snap.direcao_preliminar = "COMPRA"
        return True, "Confluência preliminar de COMPRA detectada."
    if tendencia_baixa and momentum_baixa:
        snap.direcao_preliminar = "VENDA"
        return True, "Confluência preliminar de VENDA detectada."

    return False, "Sem alinhamento simultâneo de tendência (EMA9/21 + SMA200) e momentum (MACD + RSI)."


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

    # Distância técnica do Stop Loss (1R) baseada no ATR com piso mínimo de 0.25% do preço
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
# 5. AUDITORIA QUANTITATIVA VIA GEMINI API (FREE TIER)
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
    if _CACHE_MODELOS_ATIVOS:
        return [m for m in _CACHE_MODELOS_ATIVOS if m not in _MODELOS_BLOQUEADOS_SESSAO]

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
            descobertos = [nome for _, nome in ranqueados[:8]]
    except Exception as exc:
        logger.warning("Aviso ao listar modelos Gemini (usando lista padrão): %s", str(exc)[:100])

    _CACHE_MODELOS_ATIVOS.extend(descobertos if descobertos else GEMINI_MODELS_FALLBACK)
    return [m for m in _CACHE_MODELOS_ATIVOS if m not in _MODELOS_BLOQUEADOS_SESSAO]


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


def construir_prompt_auditoria(snap: SnapshotIndicadores, plano: PlanoRisco) -> Tuple[str, str]:
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
        "direcao_proposta": plano.direcao,
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

    user_prompt = (
        "Audite o seguinte JSON de indicadores em tempo real e decida se o sinal de Day Trade "
        "possui confluência institucional suficiente para disparo:\n\n"
        f"{json.dumps(payload_indicadores, ensure_ascii=False, indent=2)}\n\n"
        "REGRAS DE AUDITORIA QUANTITATIVA:\n"
        "1. VETE (aprovado=false) se notar consolidação/lateralização (EMA9 muito próxima da EMA21, ADX fraco ou preço preso sem direção clara).\n"
        "2. VETE (aprovado=false) se o RSI indicar exaustão perigosa (>72 na COMPRA ou <28 na VENDA) ou falta de força direcional.\n"
        "3. VETE (aprovado=false) se houver divergência entre a direção proposta, a tendência primária (SMA 200) e o histograma MACD.\n"
        "4. APROVE (aprovado=true) somente se houver alinhamento limpo de tendência (EMA 9/21 + SMA 200), momentum (RSI + MACD), volume e volatilidade (ATR) compatível com o Stop Loss.\n\n"
        "Retorne APENAS um objeto JSON com as chaves exatas:\n"
        "{\n"
        '  "aprovado": boolean,\n'
        '  "nota_confluencia": integer (0 a 100),\n'
        '  "regime_mercado": "TENDENCIA_FORTE" | "TENDENCIA_MODERADA" | "CONSOLIDACAO" | "EXAUSTAO",\n'
        '  "racional_tecnico": "string em português (2 a 3 frases diretas citando os números exatos de EMA9/21, SMA200, RSI, MACD e ATR)",\n'
        '  "alerta_risco": "string curta (1 frase objetiva sobre o risco principal do setup)",\n'
        '  "motivo_veto": "string curta caso aprovado=false, ou vazio caso aprovado=true"\n'
        "}"
    )
    return system_instruction, user_prompt


def auditar_sinal_com_gemini(snap: SnapshotIndicadores, plano: PlanoRisco, api_key: str = GEMINI_API_KEY) -> Optional[AuditoriaIA]:
    if not api_key:
        logger.error("GEMINI_API_KEY não configurada.")
        return None

    modelos = descobrir_modelos_gemini_gratuitos(api_key)
    if not modelos:
        logger.error("Nenhum modelo Gemini gratuito disponível nesta sessão.")
        return None

    sys_inst, usr_prompt = construir_prompt_auditoria(snap, plano)
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
                timeout=20,
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
                logger.warning("Gemini (%s) retornou JSON incompleto; tentando próximo modelo.", modelo)
            elif resp.status_code in (404, 429):
                _MODELOS_BLOQUEADOS_SESSAO.add(modelo)
                logger.warning("Gemini (%s) sem cota/indisponível (HTTP %s); alternando modelo.", modelo, resp.status_code)
            elif resp.status_code in (401, 403):
                logger.error("GEMINI_API_KEY inválida ou sem permissão (HTTP %s).", resp.status_code)
                return None
            else:
                logger.warning("Gemini (%s) erro HTTP %s; tentando próximo modelo.", modelo, resp.status_code)
        except Exception as exc:
            logger.warning("Timeout/erro ao consultar Gemini (%s): %s", modelo, str(exc)[:100])

    return None


# ==============================================================================
# 6. FORMATAÇÃO HTML TELEGRAM, CONTROLE DE DUPLICIDADE (historico.json) E ENVIO
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
                return dados
        except Exception as exc:
            logger.warning("Erro ao ler %s: %s", caminho, exc)
    return {"sinais": []}


def salvar_historico(dados: Dict[str, Any], caminho: Path = HISTORICO_PATH) -> None:
    agora_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    sinais_limpos = [
        s for s in dados.get("sinais", [])
        if isinstance(s, dict) and (agora_ts - float(s.get("ts", 0))) <= 72 * 3600
    ][-400:]
    payload = {"atualizado_em": dt.datetime.now(TZ_BRT).isoformat(), "sinais": sinais_limpos}
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


def enviar_telegram_html(texto_html: str, token: str = TELEGRAM_BOT_TOKEN, chat_id: str = TELEGRAM_CHAT_ID) -> bool:
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausente.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": texto_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            return True
        logger.error("Erro Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.error("Falha de rede ao enviar para Telegram: %s", exc)
    return False


def executar_ciclo_daytrade() -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
        logger.critical("Configure TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID e GEMINI_API_KEY nos Secrets.")
        sys.exit(1)

    historico = carregar_historico()
    b3_aberta = pregao_b3_aberto()
    enviados = 0

    logger.info(
        "Iniciando varredura Quant Day Trade (Timeframe=%s | Pregão B3=%s | ATR=%.1fx)...",
        TIMEFRAME_PADRAO,
        "ABERTO" if b3_aberta else "FECHADO (somente Cripto 24/7)",
        ATR_MULTIPLIER,
    )

    for ativo in WATCHLIST_PADRAO:
        if enviado_limite := (enviados >= MAX_SINAIS_POR_CICLO):
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

    logger.info("Ciclo finalizado: %d sinal(is) aprovado(s) e enviado(s).", enviados)
    return enviados


if __name__ == "__main__":
    executar_ciclo_daytrade()
