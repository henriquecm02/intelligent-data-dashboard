"""Coleta incremental de séries do Banco Central (SGS) com validação e registro de execução.

Fonte: API pública do Sistema Gerenciador de Séries Temporais (api.bcb.gov.br), sem chave.
Cada execução grava em `coletas` quando começou, quanto durou, quantas linhas novas trouxe e
se houve erro, o que permite medir frequência e latência reais da atualização.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
USER_AGENT = "intelligent-data-dashboard/0.1 (projeto de portfólio)"


@dataclass(frozen=True)
class Series:
    code: int
    slug: str
    name: str
    unit: str
    freq: str  # "diaria" | "mensal"


SERIES = [
    Series(1, "dolar", "Dólar comercial (venda, PTAX)", "R$", "diaria"),
    Series(21619, "euro", "Euro (venda, PTAX)", "R$", "diaria"),
    Series(432, "selic", "Meta Selic (Copom)", "% a.a.", "diaria"),
    Series(433, "ipca", "IPCA (variação mensal)", "%", "mensal"),
    Series(189, "igpm", "IGP-M (variação mensal)", "%", "mensal"),
]
BY_SLUG = {s.slug: s for s in SERIES}

SCHEMA = """
CREATE TABLE IF NOT EXISTS observacoes (
    serie TEXT NOT NULL, data TEXT NOT NULL, valor REAL NOT NULL,
    coletado_em TEXT NOT NULL, PRIMARY KEY (serie, data));
CREATE TABLE IF NOT EXISTS coletas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, execucao TEXT NOT NULL, inicio TEXT NOT NULL, segundos REAL NOT NULL,
    serie TEXT NOT NULL, linhas_recebidas INTEGER, linhas_novas INTEGER, linhas_alteradas INTEGER,
    rejeitadas INTEGER, status TEXT NOT NULL, erro TEXT);
"""


class ValidationError(ValueError):
    pass


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    return con


def parse(payload: list[dict], today: date | None = None) -> tuple[list[tuple[str, float]], list[dict]]:
    """Converte a resposta da API em (data ISO, valor). Rejeita linhas inválidas e datas no futuro.

    A série da meta Selic é publicada com datas futuras (a meta vale até a próxima reunião);
    mantemos apenas até hoje para não exibir "dados" que ainda não aconteceram.
    """
    if not isinstance(payload, list):
        raise ValidationError(f"Resposta inesperada da API: {type(payload).__name__}")
    today = today or date.today()
    rows, rejected = {}, []
    for item in payload:
        try:
            d = datetime.strptime(item["data"], "%d/%m/%Y").date()
            v = float(str(item["valor"]).replace(",", "."))
        except (KeyError, ValueError, TypeError) as e:
            rejected.append({"item": item, "motivo": f"formato inválido: {e}"})
            continue
        if d > today:
            rejected.append({"item": item, "motivo": "data futura"})
            continue
        rows[d.isoformat()] = v  # duplicatas na mesma resposta: fica a última
    return sorted(rows.items()), rejected


def fetch(series: Series, start: date, end: date, session: requests.Session | None = None,
          retries: int = 3, timeout: float = 30) -> list[dict]:
    s = session or requests.Session()
    params = {"formato": "json", "dataInicial": start.strftime("%d/%m/%Y"), "dataFinal": end.strftime("%d/%m/%Y")}
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            r = s.get(API.format(code=series.code), params=params, timeout=timeout,
                      headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            if r.status_code == 404:  # a API responde 404 quando não há dados no intervalo
                return []
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, json.JSONDecodeError):
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def last_date(con: sqlite3.Connection, slug: str) -> date | None:
    row = con.execute("SELECT MAX(data) FROM observacoes WHERE serie = ?", (slug,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def upsert(con: sqlite3.Connection, slug: str, rows: list[tuple[str, float]], now: str) -> tuple[int, int]:
    """Insere novas linhas e atualiza revisões (o BCB pode revisar valores). Retorna (novas, alteradas)."""
    new = changed = 0
    for d, v in rows:
        old = con.execute("SELECT valor FROM observacoes WHERE serie=? AND data=?", (slug, d)).fetchone()
        if old is None:
            con.execute("INSERT INTO observacoes VALUES (?,?,?,?)", (slug, d, v, now))
            new += 1
        elif abs(old[0] - v) > 1e-9:
            con.execute("UPDATE observacoes SET valor=?, coletado_em=? WHERE serie=? AND data=?", (v, now, slug, d))
            changed += 1
    return new, changed


def collect(db: Path, start: date = date(2023, 1, 1), overlap_days: int = 45, today: date | None = None,
            fetcher=fetch, series: list[Series] = SERIES) -> list[dict]:
    """Coleta incremental: busca a partir da última data salva menos uma margem (para capturar revisões)."""
    today = today or date.today()
    con = connect(db)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    results = []
    for s in series:
        t0 = time.perf_counter()
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        last = last_date(con, s.slug)
        since = max(start, last - timedelta(days=overlap_days)) if last else start
        log = {"execucao": run_id, "inicio": started, "serie": s.slug}
        try:
            payload = fetcher(s, since, today)
            rows, rejected = parse(payload, today)
            new, changed = upsert(con, s.slug, rows, started)
            log |= {"linhas_recebidas": len(payload), "linhas_novas": new, "linhas_alteradas": changed,
                    "rejeitadas": len(rejected), "status": "ok", "erro": None}
        except Exception as e:  # uma série com problema não impede as outras
            log |= {"linhas_recebidas": None, "linhas_novas": 0, "linhas_alteradas": 0, "rejeitadas": None,
                    "status": "erro", "erro": f"{type(e).__name__}: {e}"[:500]}
        log["segundos"] = round(time.perf_counter() - t0, 3)
        con.execute("INSERT INTO coletas (execucao, inicio, segundos, serie, linhas_recebidas, linhas_novas, linhas_alteradas,"
                    " rejeitadas, status, erro) VALUES (:execucao, :inicio, :segundos, :serie, :linhas_recebidas, :linhas_novas,"
                    " :linhas_alteradas, :rejeitadas, :status, :erro)", log)
        con.commit()
        results.append(log)
    con.close()
    return results


def export_csv(db: Path, out_dir: Path) -> list[Path]:
    """CSV por série: legível no GitHub e fácil de versionar."""
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    paths = []
    for s in SERIES:
        df = pd.read_sql("SELECT data, valor FROM observacoes WHERE serie=? ORDER BY data", con, params=(s.slug,))
        p = out_dir / f"{s.slug}.csv"
        df.to_csv(p, index=False)
        paths.append(p)
    con.close()
    return paths
