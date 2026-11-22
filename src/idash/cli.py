"""Linha de comando.

  idash coletar                 coleta incremental e exporta CSVs
  idash relatorio [--ia]        análise + relatório Markdown (resumo por IA opcional)
  idash pipeline [--ia]         coletar + relatorio (usado pelo agendamento)
  idash perguntar "Quanto o dólar subiu no último mês?"
  idash notificar               envia alertas novos de severidade alta ao Telegram (se configurado)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from . import analysis as an
from . import report
from .collector import collect, export_csv

DB = Path("data/indicadores.db")


def make_llm():
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path.home() / ".config" / "portfolio" / ".env")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    from .llm import GeminiLLM
    return GeminiLLM(api_key=key, model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))


def cmd_coletar(args) -> int:
    results = collect(args.db)
    export_csv(args.db, args.db.parent / "csv")
    for r in results:
        print(f"{r['serie']:6} {r['status']:5} novas={r['linhas_novas']} alteradas={r['linhas_alteradas']} "
              f"{r['segundos']}s {r['erro'] or ''}")
    return 0 if all(r["status"] == "ok" for r in results) else 1


def cmd_relatorio(args) -> int:
    analysis = an.analyze(args.db, reference=args.data)
    health = an.collection_health(args.db)
    summary = None
    if args.ia:
        llm = make_llm()
        if llm is None:
            print("Sem GEMINI_API_KEY: relatório gerado sem o resumo por IA.", file=sys.stderr)
        else:
            from .insights import weekly_summary
            summary = weekly_summary(llm, analysis, health)
    an.save({"analise": analysis, "saude": health, "resumo": summary}, Path("reports/ultimo.json"))
    p = report.write(analysis, health, summary, Path("reports/ultimo.md"))
    print(f"Relatório: {p} · {len(analysis['alertas'])} alertas")
    return 0


def cmd_perguntar(args) -> int:
    llm = make_llm()
    if llm is None:
        print("Defina GEMINI_API_KEY para fazer perguntas.", file=sys.stderr)
        return 2
    from .insights import ask
    res = ask(llm, args.db, an.analyze(args.db, reference=args.data), args.pergunta)
    print(res["resposta"])
    print("\nFerramentas usadas:", json.dumps(res["ferramentas"], ensure_ascii=False))
    return 0


def cmd_notificar(args) -> int:
    from .notify import notificar

    analysis = an.analyze(args.db, reference=args.data)
    res = notificar(analysis["alertas"], analysis["data_referencia"], args.db.parent / "alertas_enviados.json")
    print(json.dumps(res, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="idash", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--data", type=date.fromisoformat, default=None, help="data de referência (AAAA-MM-DD)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("coletar")
    r = sub.add_parser("relatorio")
    r.add_argument("--ia", action="store_true")
    p = sub.add_parser("pipeline")
    p.add_argument("--ia", action="store_true")
    q = sub.add_parser("perguntar")
    q.add_argument("pergunta")
    sub.add_parser("notificar")
    args = ap.parse_args(argv)
    if args.cmd == "coletar":
        return cmd_coletar(args)
    if args.cmd == "relatorio":
        return cmd_relatorio(args)
    if args.cmd == "pipeline":
        code = cmd_coletar(args)
        return cmd_relatorio(args) or code
    if args.cmd == "notificar":
        return cmd_notificar(args)
    return cmd_perguntar(args)


if __name__ == "__main__":
    raise SystemExit(main())
