"""Análise determinística: variações, acumulados, anomalias, alertas e saúde da coleta.

É a única fonte de números para o resumo gerado por IA.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .collector import BY_SLUG, SERIES

DEFAULT_RULES = {
    # variação percentual em N dias corridos acima do limite (valor absoluto)
    "dolar": {"variacao_dias": 7, "limite_pct": 2.0},
    "euro": {"variacao_dias": 7, "limite_pct": 2.0},
    # mudança de patamar da meta Selic
    "selic": {"mudanca_pp": 0.0},
    # inflação acumulada em 12 meses acima do teto (meta 3,0% + tolerância 1,5 p.p.)
    "ipca": {"acumulado_12m_max": 4.5},
    "igpm": {"acumulado_12m_max": 6.0},
}


def load(db: Path) -> pd.DataFrame:
    con = sqlite3.connect(db)
    df = pd.read_sql("SELECT serie, data, valor FROM observacoes ORDER BY serie, data", con, parse_dates=["data"])
    con.close()
    return df


def br(v: float, casas: int = 2, sinal: bool = False) -> str:
    s = f"{v:+,.{casas}f}" if sinal else f"{v:,.{casas}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if b else None


def _value_on_or_before(s: pd.Series, when: pd.Timestamp) -> float | None:
    s = s[s.index <= when]
    return float(s.iloc[-1]) if len(s) else None


def daily_metrics(s: pd.Series, ref: pd.Timestamp) -> dict:
    last_d, last_v = s.index[-1], float(s.iloc[-1])
    out = {"ultimo_valor": round(last_v, 4), "ultima_data": last_d.date().isoformat(),
           "dias_desde_ultimo_dado": int((ref - last_d).days)}
    for label, days in [("var_1d_pct", 1), ("var_7d_pct", 7), ("var_30d_pct", 30), ("var_12m_pct", 365)]:
        base = _value_on_or_before(s, last_d - pd.Timedelta(days=days)) if days > 1 else \
            (float(s.iloc[-2]) if len(s) > 1 else None)
        out[label] = _pct(last_v, base) if base else None
    win = s[s.index > last_d - pd.Timedelta(days=30)]
    out["min_30d"], out["max_30d"] = round(float(win.min()), 4), round(float(win.max()), 4)
    return out


def monthly_metrics(s: pd.Series) -> dict:
    last_d, last_v = s.index[-1], float(s.iloc[-1])
    last12 = s.iloc[-12:]
    ytd = s[s.index.year == last_d.year]
    compound = lambda x: round(((1 + x / 100).prod() - 1) * 100, 2)
    return {"ultimo_valor": round(last_v, 2), "ultima_data": last_d.date().isoformat(),
            "acumulado_12m_pct": compound(last12) if len(last12) == 12 else None,
            "acumulado_ano_pct": compound(ytd), "media_12m": round(float(last12.mean()), 2)}


def anomalies(s: pd.Series, window: int = 90, z: float = 3.0, last_n_days: int = 60) -> list[dict]:
    """Variações diárias fora do padrão: |z-score| da variação vs. janela móvel anterior."""
    ret = s.pct_change() * 100
    mu = ret.rolling(window, min_periods=30).mean().shift(1)
    sd = ret.rolling(window, min_periods=30).std().shift(1)
    zs = (ret - mu) / sd
    hits = zs[(zs.abs() >= z) & (zs.index > s.index[-1] - pd.Timedelta(days=last_n_days))]
    return [{"data": d.date().isoformat(), "variacao_pct": round(float(ret[d]), 2), "z": round(float(v), 1),
             "valor": round(float(s[d]), 4)} for d, v in hits.items()]


def selic_changes(s: pd.Series, last_n: int = 3) -> list[dict]:
    ch = s[s.diff().fillna(0) != 0]
    return [{"data": d.date().isoformat(), "de": round(float(s[s.index < d].iloc[-1]), 2),
             "para": round(float(v), 2)} for d, v in ch.iloc[-last_n:].items()]


def analyze(db: Path, reference: date | None = None, rules: dict | None = None) -> dict:
    rules = rules or DEFAULT_RULES
    ref = pd.Timestamp(reference or date.today())
    df = load(db)
    out: dict = {"data_referencia": ref.date().isoformat(), "indicadores": {}, "alertas": []}
    for meta in SERIES:
        s = df[df["serie"] == meta.slug].set_index("data")["valor"].sort_index()
        if s.empty:
            continue
        info = {"nome": meta.name, "unidade": meta.unit, "frequencia": meta.freq}
        if meta.slug == "selic":
            info |= {"ultimo_valor": float(s.iloc[-1]), "ultima_data": s.index[-1].date().isoformat(),
                     "mudancas_recentes": selic_changes(s)}
        elif meta.freq == "diaria":
            info |= daily_metrics(s, ref)
            info["anomalias"] = anomalies(s)
        else:
            info |= monthly_metrics(s)
        out["indicadores"][meta.slug] = info
        out["alertas"] += evaluate_rules(meta.slug, info, s, rules.get(meta.slug, {}), ref)
    return out


def evaluate_rules(slug: str, info: dict, s: pd.Series, rule: dict, ref: pd.Timestamp) -> list[dict]:
    alerts = []
    name = BY_SLUG[slug].name
    if "limite_pct" in rule:
        key = f"var_{rule['variacao_dias']}d_pct"
        v = info.get(key)
        if v is not None and abs(v) >= rule["limite_pct"]:
            alerts.append({"indicador": slug, "tipo": "variacao", "severidade": "alta" if abs(v) >= 2 * rule["limite_pct"]
                           else "media", "mensagem": f"{name} variou {br(v, sinal=True)}% em {rule['variacao_dias']} dias "
                           f"(limite {br(rule['limite_pct'], 1)}%).", "valor": v})
        for a in info.get("anomalias", [])[-3:]:
            alerts.append({"indicador": slug, "tipo": "anomalia", "severidade": "media",
                           "mensagem": f"{name}: variação diária atípica de {br(a['variacao_pct'], sinal=True)}% em {a['data']} "
                           f"(z = {br(a['z'], 1)}).", "valor": a["variacao_pct"]})
    if "mudanca_pp" in rule:
        recent = [c for c in info.get("mudancas_recentes", [])
                  if pd.Timestamp(c["data"]) > ref - pd.Timedelta(days=60)]
        for c in recent:
            alerts.append({"indicador": slug, "tipo": "mudanca", "severidade": "alta",
                           "mensagem": f"Meta Selic mudou de {br(c['de'])}% para {br(c['para'])}% em {c['data']}.",
                           "valor": c["para"]})
    if "acumulado_12m_max" in rule:
        v = info.get("acumulado_12m_pct")
        if v is not None and v > rule["acumulado_12m_max"]:
            alerts.append({"indicador": slug, "tipo": "limite", "severidade": "media",
                           "mensagem": f"{name}: acumulado em 12 meses de {br(v)}% acima de "
                           f"{br(rule['acumulado_12m_max'], 1)}%.", "valor": v})
    stale = info.get("dias_desde_ultimo_dado")
    if stale is not None and stale > 5:
        alerts.append({"indicador": slug, "tipo": "dado_desatualizado", "severidade": "media",
                       "mensagem": f"{name}: último dado tem {stale} dias.", "valor": stale})
    return alerts


def collection_health(db: Path, last_n: int = 200) -> dict:
    """Frequência, duração e taxa de sucesso medidas a partir do log real de coletas."""
    con = sqlite3.connect(db)
    df = pd.read_sql("SELECT * FROM coletas ORDER BY id DESC LIMIT ?", con, params=(last_n,))
    con.close()
    if df.empty:
        return {"execucoes": 0}
    df["inicio"] = pd.to_datetime(df["inicio"], utc=True)
    runs = df.groupby("execucao").agg(inicio=("inicio", "min"), segundos=("segundos", "sum"),
                                      ok=("status", lambda x: (x == "ok").all()),
                                      novas=("linhas_novas", "sum")).set_index("inicio").sort_index()
    gaps = runs.index.to_series().diff().dropna().dt.total_seconds() / 3600
    now = datetime.now(timezone.utc)
    return {
        "execucoes": int(len(runs)),
        "ultima_execucao": runs.index[-1].isoformat(),
        "horas_desde_ultima": round((now - runs.index[-1].to_pydatetime()).total_seconds() / 3600, 1),
        "duracao_mediana_s": round(float(runs["segundos"].median()), 2),
        "duracao_max_s": round(float(runs["segundos"].max()), 2),
        "intervalo_mediano_h": round(float(gaps.median()), 1) if len(gaps) else None,
        "taxa_sucesso_pct": round(float(runs["ok"].mean() * 100), 1),
        "linhas_novas_ultima": int(runs["novas"].iloc[-1]),
    }


def numbers_in(obj, skip=frozenset({"z", "dias_desde_ultimo_dado", "execucoes"})) -> set[float]:
    found: set[float] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip:
                found |= numbers_in(v, skip)
    elif isinstance(obj, list):
        for v in obj:
            found |= numbers_in(v, skip)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        found.add(round(float(obj), 2))
    return found


def save(obj: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
