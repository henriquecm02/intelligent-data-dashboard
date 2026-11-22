"""Avaliação das perguntas sobre os indicadores, com gabarito recalculado direto do SQLite.

O banco é atualizado todo dia útil pelo robô de coleta, então as respostas esperadas não são fixas:
para cada pergunta, este script calcula o valor certo com SQL e pandas, sem usar as funções do
pacote, e compara com o que a ferramenta do agente devolve.

  python eval/run_eval.py            # confere as ferramentas (sem chave)
  python eval/run_eval.py --gemini   # também pergunta ao Gemini e confere o número na resposta

Grava eval/resultados.json e eval/resultados.md.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from idash.analysis import analyze  # noqa: E402
from idash.insights import tool_get_indicator, tool_list_alerts, tool_period_stats  # noqa: E402

DB = ROOT / "data" / "indicadores.db"


def serie(slug: str) -> pd.Series:
    con = sqlite3.connect(DB)
    df = pd.read_sql("SELECT data, valor FROM observacoes WHERE serie = ? ORDER BY data", con, params=(slug,))
    con.close()
    return df.set_index(pd.to_datetime(df["data"]))["valor"]


def janela(slug: str, ini: str, fim: str) -> pd.Series:
    s = serie(slug)
    return s[(s.index >= ini) & (s.index <= fim)]


def casos() -> list[dict]:
    """(pergunta, ferramenta, argumentos, campo devolvido, valor esperado calculado aqui)."""
    out = []
    for slug, nome in (("dolar", "do dólar"), ("euro", "do euro"), ("selic", "da Selic"), ("ipca", "do IPCA"),
                       ("igpm", "do IGP-M")):
        s = serie(slug)
        out.append({"pergunta": f"Qual o último valor {nome}?", "ferramenta": "get_indicator",
                    "args": {"indicador": slug}, "campo": "ultimo_valor", "esperado": float(s.iloc[-1]),
                    "casas": 4 if slug in ("dolar", "euro") else 2})
    for slug, nome, ini, fim, rot in (("dolar", "o dólar", "2026-10-01", "2026-10-31", "outubro de 2026"),
                                      ("euro", "o euro", "2026-09-01", "2026-09-30", "setembro de 2026"),
                                      ("dolar", "o dólar", "2025-01-01", "2025-12-31", "2025"),
                                      ("euro", "o euro", "2024-01-01", "2024-12-31", "2024")):
        w = janela(slug, ini, fim)
        out.append({"pergunta": f"Quanto {nome} variou em {rot}?", "ferramenta": "period_stats",
                    "args": {"indicador": slug, "inicio": ini, "fim": fim}, "campo": "variacao_pct",
                    "esperado": (w.iloc[-1] / w.iloc[0] - 1) * 100, "casas": 2})
        out.append({"pergunta": f"Qual foi a máxima d{nome[0]} {nome[2:]} em {rot}?", "ferramenta": "period_stats",
                    "args": {"indicador": slug, "inicio": ini, "fim": fim}, "campo": "maximo",
                    "esperado": float(w.max()), "casas": 4})
        out.append({"pergunta": f"Em que dia {nome} teve a mínima em {rot}?", "ferramenta": "period_stats",
                    "args": {"indicador": slug, "inicio": ini, "fim": fim}, "campo": "data_minimo",
                    "esperado": w.idxmin().date().isoformat(), "casas": None})
    for slug, nome, ano in (("ipca", "IPCA", 2025), ("igpm", "IGP-M", 2025), ("ipca", "IPCA", 2024)):
        w = janela(slug, f"{ano}-01-01", f"{ano}-12-31")
        out.append({"pergunta": f"Qual o {nome} acumulado em {ano}?", "ferramenta": "period_stats",
                    "args": {"indicador": slug, "inicio": f"{ano}-01-01", "fim": f"{ano}-12-31"},
                    "campo": "acumulado_pct", "esperado": ((1 + w / 100).prod() - 1) * 100, "casas": 2})
    for slug, nome in (("ipca", "IPCA"), ("igpm", "IGP-M")):
        s = serie(slug).iloc[-12:]
        out.append({"pergunta": f"Quanto o {nome} acumula nos últimos 12 meses?", "ferramenta": "get_indicator",
                    "args": {"indicador": slug}, "campo": "acumulado_12m_pct",
                    "esperado": ((1 + s / 100).prod() - 1) * 100, "casas": 2})
    w = janela("selic", "2026-01-01", "2026-12-31")
    out.append({"pergunta": "Qual a Selic média em 2026 até agora?", "ferramenta": "period_stats",
                "args": {"indicador": "selic", "inicio": "2026-01-01", "fim": "2026-12-31"}, "campo": "media",
                "esperado": float(w.mean()), "casas": 2})
    return out


FERRAMENTAS = {"get_indicator": tool_get_indicator, "period_stats": tool_period_stats, "list_alerts": tool_list_alerts}


def confere(caso: dict, obtido) -> bool:
    if caso["casas"] is None:
        return obtido == caso["esperado"]
    return obtido is not None and abs(float(obtido) - round(caso["esperado"], caso["casas"])) <= 10 ** -caso["casas"]


def main() -> int:
    analise = analyze(DB)
    linhas = []
    for c in casos():
        res = FERRAMENTAS[c["ferramenta"]](DB, analise, c["args"])
        obtido = res.get(c["campo"])
        linhas.append({**c, "obtido": obtido, "ok": confere(c, obtido),
                       "esperado": round(c["esperado"], c["casas"]) if c["casas"] is not None else c["esperado"]})

    if "--gemini" in sys.argv:
        from idash.cli import make_llm
        from idash.insights import ask

        llm = make_llm()
        if llm is None:
            print("--gemini pedido, mas GEMINI_API_KEY não está definida")
            return 2
        for linha in linhas:
            r = ask(llm, DB, analise, linha["pergunta"])
            esperado = linha["esperado"]
            alvo = f"{esperado:.{linha['casas']}f}".replace(".", ",") if linha["casas"] is not None else esperado
            linha["resposta_gemini"] = r["resposta"]
            linha["gemini_ok"] = str(alvo) in r["resposta"] and not r["numeros_sem_respaldo"]

    ok = sum(x["ok"] for x in linhas)
    res = {"referencia": analise["data_referencia"], "acertos_ferramentas": ok, "total": len(linhas), "casos": linhas}
    if "--gemini" in sys.argv:
        res["acertos_gemini"] = sum(x.get("gemini_ok", False) for x in linhas)
    (ROOT / "eval" / "resultados.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = [f"# Avaliação das perguntas · referência {analise['data_referencia']}\n",
          f"**Ferramentas: {ok} de {len(linhas)} respostas iguais ao gabarito recalculado do SQLite.**\n",
          "| Pergunta | Ferramenta | Esperado | Obtido | Resultado |", "|---|---|---|---|---|"]
    md += [f"| {x['pergunta']} | `{x['ferramenta']}` | {x['esperado']} | {x['obtido']} | {'ok' if x['ok'] else 'FALHOU'} |"
           for x in linhas]
    (ROOT / "eval" / "resultados.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"{ok}/{len(linhas)} respostas das ferramentas conferem com o gabarito")
    for x in linhas:
        if not x["ok"]:
            print("FALHOU:", x["pergunta"], x["esperado"], x["obtido"])
    return 0 if ok == len(linhas) else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
