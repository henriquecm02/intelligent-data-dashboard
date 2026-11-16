"""Camada de IA: resumo executivo da semana e perguntas em linguagem natural.

- O resumo recebe só a análise determinística (JSON) e precisa citar números que existem nela;
  `ungrounded_numbers` confere isso e, se houver valor sem respaldo, pede uma reescrita.
- As perguntas usam function calling sobre ferramentas fixas (sem SQL livre gerado pelo modelo),
  o que torna as respostas auditáveis e impede consultas arbitrárias ao banco.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from .analysis import load, numbers_in
from .collector import BY_SLUG
from .llm import LLM, Message, ToolSpec, Usage

PROMPTS = Path(__file__).parent / "prompts"
NUM_RE = re.compile(r"(?<![\w/])[-+]?\d{1,3}(?:\.\d{3})*(?:,\d+)?\s?(?:%|p\.p\.)|R\$\s?[-+]?\d+(?:\.\d{3})*(?:,\d+)?")


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


def _to_float(token: str) -> float:
    t = token.replace("R$", "").replace("p.p.", "").replace("%", "").strip().replace(".", "").replace(",", ".")
    return round(float(t), 2)


def ungrounded_numbers(text: str, source: dict) -> list[str]:
    """Percentuais e valores em R$ do texto que não aparecem na análise.

    Os dois lados estão arredondados em duas casas, então a comparação é praticamente exata:
    uma tolerância de 0,01 deixaria "99,99%" passar como "100,00%"."""
    allowed = numbers_in(source)
    allowed |= {abs(v) for v in allowed}
    bad = []
    for m in NUM_RE.finditer(text):
        v = _to_float(m.group(0))
        if not any(abs(abs(v) - a) < 0.005 for a in allowed):
            bad.append(m.group(0))
    return sorted(set(bad))


def weekly_summary(llm: LLM, analysis: dict, health: dict, max_rewrites: int = 1) -> dict:
    """Gera o resumo; se citar número inexistente, devolve o aviso ao modelo e pede correção."""
    payload = json.dumps({"analise": analysis, "saude_da_coleta": health}, ensure_ascii=False, default=str)
    usage = Usage()
    messages = [Message(role="user", text=f"Dados da semana (fonte única de números):\n{payload}")]
    text, bad = "", []
    for attempt in range(max_rewrites + 1):
        resp = llm.chat(prompt("summary_v1"), messages, [])
        usage.add(resp.usage)
        text = (resp.text or "").strip()
        bad = ungrounded_numbers(text, {"a": analysis, "h": health})
        if not bad:
            break
        messages += [Message(role="assistant", text=text, raw=resp.raw),
                     Message(role="user", text="Estes números não existem nos dados: " + ", ".join(bad) +
                             ". Reescreva usando apenas valores da análise, com duas casas decimais.")]
    return {"texto": text, "numeros_sem_respaldo": bad, "tentativas": attempt + 1,
            "chamadas_llm": usage.calls, "tokens": usage.input_tokens + usage.output_tokens}


# --------------------------------------------------------------------------- perguntas

def _series(db: Path, slug: str) -> pd.Series:
    df = load(db)
    return df[df["serie"] == slug].set_index("data")["valor"].sort_index()


def tool_get_indicator(db: Path, analysis: dict, args: dict) -> dict:
    slug = args["indicador"]
    if slug not in analysis["indicadores"]:
        return {"erro": f"Indicador desconhecido. Use um de: {list(analysis['indicadores'])}"}
    return analysis["indicadores"][slug]


def tool_period_stats(db: Path, analysis: dict, args: dict) -> dict:
    slug = args["indicador"]
    if slug not in BY_SLUG:
        return {"erro": f"Indicador desconhecido: {slug}"}
    s = _series(db, slug)
    ini, fim = pd.Timestamp(args["inicio"]), pd.Timestamp(args["fim"])
    w = s[(s.index >= ini) & (s.index <= fim)]
    if w.empty:
        return {"erro": "Sem dados no período.", "disponivel_de": s.index.min().date().isoformat(),
                "disponivel_ate": s.index.max().date().isoformat()}
    out = {"indicador": slug, "inicio": w.index[0].date().isoformat(), "fim": w.index[-1].date().isoformat(),
           "observacoes": int(len(w)), "primeiro": round(float(w.iloc[0]), 4), "ultimo": round(float(w.iloc[-1]), 4),
           "minimo": round(float(w.min()), 4), "data_minimo": w.idxmin().date().isoformat(),
           "maximo": round(float(w.max()), 4), "data_maximo": w.idxmax().date().isoformat(),
           "media": round(float(w.mean()), 4)}
    if BY_SLUG[slug].freq == "mensal":
        out["acumulado_pct"] = round(((1 + w / 100).prod() - 1) * 100, 2)
    else:
        out["variacao_pct"] = round((w.iloc[-1] / w.iloc[0] - 1) * 100, 2)
    return out


def tool_list_alerts(db: Path, analysis: dict, args: dict) -> dict:
    return {"alertas": analysis["alertas"]}


QA_TOOLS = {
    "get_indicator": (ToolSpec("get_indicator", "Situação atual de um indicador: último valor, variações, "
                               "acumulados, mínimo/máximo de 30 dias e anomalias.",
                               {"type": "object", "properties": {"indicador": {"type": "string",
                                "enum": list(BY_SLUG)}}, "required": ["indicador"]}), tool_get_indicator),
    "period_stats": (ToolSpec("period_stats", "Estatísticas de um indicador entre duas datas (AAAA-MM-DD): "
                              "primeiro, último, mínimo, máximo, média e variação ou acumulado.",
                              {"type": "object", "properties": {
                                  "indicador": {"type": "string", "enum": list(BY_SLUG)},
                                  "inicio": {"type": "string"}, "fim": {"type": "string"}},
                               "required": ["indicador", "inicio", "fim"]}), tool_period_stats),
    "list_alerts": (ToolSpec("list_alerts", "Alertas ativos (variações acima do limite, anomalias, mudanças de "
                             "patamar, inflação acima do teto, dados desatualizados).",
                             {"type": "object", "properties": {}}), tool_list_alerts),
}


def ask(llm: LLM, db: Path, analysis: dict, question: str, max_steps: int = 6) -> dict:
    """Responde a pergunta usando só as ferramentas; registra cada chamada para auditoria."""
    specs = [spec for spec, _ in QA_TOOLS.values()]
    msgs = [Message(role="user", text=f"Hoje é {analysis['data_referencia']}. Pergunta: {question}")]
    calls, usage, evidence = [], Usage(), {}
    for _ in range(max_steps):
        resp = llm.chat(prompt("qa_v1"), msgs, specs)
        usage.add(resp.usage)
        msgs.append(Message(role="assistant", text=resp.text, tool_calls=resp.tool_calls, raw=resp.raw))
        if not resp.tool_calls:
            answer = (resp.text or "").strip()
            return {"resposta": answer, "ferramentas": calls, "chamadas_llm": usage.calls,
                    "numeros_sem_respaldo": ungrounded_numbers(answer, evidence) if evidence else []}
        results = []
        for c in resp.tool_calls:
            fn = QA_TOOLS.get(c.name, (None, None))[1]
            try:
                res = fn(db, analysis, c.args) if fn else {"erro": f"Ferramenta desconhecida: {c.name}"}
            except Exception as e:
                res = {"erro": f"{type(e).__name__}: {e}"}
            calls.append({"ferramenta": c.name, "args": c.args})
            evidence[f"{c.name}_{len(calls)}"] = res
            results.append((c.name, res))
        msgs.append(Message(role="tool", tool_results=results))
    return {"resposta": "Não consegui concluir dentro do limite de passos.", "ferramentas": calls,
            "chamadas_llm": usage.calls, "numeros_sem_respaldo": []}
