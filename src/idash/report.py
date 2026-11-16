"""Relatório em Markdown: tabela determinística + resumo por IA (quando houver chave)."""
from __future__ import annotations

from pathlib import Path

from .analysis import br


def _fmt(v, casas=2, sufixo="", sinal=False):
    return "—" if v is None else f"{br(v, casas, sinal)}{sufixo}"


def render(analysis: dict, health: dict, summary: dict | None) -> str:
    ind = analysis["indicadores"]
    lines = [f"# Indicadores econômicos · referência {analysis['data_referencia']}", ""]
    if summary and summary.get("texto"):
        lines += ["## Resumo (gerado por IA)", "", summary["texto"], ""]
        if summary.get("numeros_sem_respaldo"):
            lines += [f"> ⚠️ Números sem respaldo nos dados: {', '.join(summary['numeros_sem_respaldo'])}", ""]
    else:
        lines += ["## Resumo (gerado por IA)", "", "_Não gerado nesta execução (sem GEMINI_API_KEY)._", ""]

    lines += ["## Câmbio", "", "| Indicador | Último | Data | 1 dia | 7 dias | 30 dias | 12 meses |",
              "|---|---|---|---|---|---|---|"]
    for slug in ("dolar", "euro"):
        if slug in ind:
            i = ind[slug]
            lines.append(f"| {i['nome']} | R$ {_fmt(i['ultimo_valor'], 4)} | {i['ultima_data']} | "
                         f"{_fmt(i['var_1d_pct'], sufixo='%', sinal=True)} | {_fmt(i['var_7d_pct'], sufixo='%', sinal=True)} | "
                         f"{_fmt(i['var_30d_pct'], sufixo='%', sinal=True)} | {_fmt(i['var_12m_pct'], sufixo='%', sinal=True)} |")
    lines += ["", "## Juros e inflação", "", "| Indicador | Último | Referência | Acumulado 12 meses | No ano |",
              "|---|---|---|---|---|"]
    if "selic" in ind:
        lines.append(f"| {ind['selic']['nome']} | {_fmt(ind['selic']['ultimo_valor'])}% a.a. | "
                     f"{ind['selic']['ultima_data']} | — | — |")
    for slug in ("ipca", "igpm"):
        if slug in ind:
            i = ind[slug]
            lines.append(f"| {i['nome']} | {_fmt(i['ultimo_valor'])}% | {i['ultima_data'][:7]} | "
                         f"{_fmt(i['acumulado_12m_pct'], sufixo='%')} | {_fmt(i['acumulado_ano_pct'], sufixo='%')} |")

    lines += ["", "## Alertas", ""]
    lines += [f"- **{a['severidade']}** · {a['mensagem']}" for a in analysis["alertas"]] or ["- Nenhum alerta."]

    if health.get("execucoes"):
        lines += ["", "## Saúde da coleta", "",
                  f"- Última execução: {health['ultima_execucao']} ({_fmt(health['horas_desde_ultima'], 1)} h atrás)",
                  f"- Duração mediana: {_fmt(health['duracao_mediana_s'])} s · taxa de sucesso: "
                  f"{_fmt(health['taxa_sucesso_pct'], 1)}% · {health['execucoes']} execuções registradas"]
        if health.get("intervalo_mediano_h") is not None:
            lines.append(f"- Intervalo mediano entre coletas: {_fmt(health['intervalo_mediano_h'], 1)} h")
    lines += ["", "_Fonte: Banco Central do Brasil, SGS (api.bcb.gov.br)._", ""]
    return "\n".join(lines)


def write(analysis: dict, health: dict, summary: dict | None, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(analysis, health, summary), encoding="utf-8")
    return path
