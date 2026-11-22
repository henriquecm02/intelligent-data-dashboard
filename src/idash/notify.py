"""Envio de alertas novos para o Telegram (opcional).

Só manda alertas que ainda não foram enviados: a chave de cada alerta (indicador + tipo + mensagem)
fica gravada em data/alertas_enviados.json, que o robô de coleta versiona junto com os dados.
Sem TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID, nada é enviado.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

SEVERIDADES = {"alta": 2, "media": 1, "baixa": 0}


def chave(alerta: dict) -> str:
    texto = f"{alerta['indicador']}|{alerta['tipo']}|{alerta['mensagem']}"
    return hashlib.sha1(texto.encode()).hexdigest()[:16]


def novos(alertas: list[dict], historico: Path, minimo: str = "alta") -> list[dict]:
    enviados = set(json.loads(historico.read_text(encoding="utf-8"))) if historico.exists() else set()
    return [a for a in alertas if SEVERIDADES.get(a["severidade"], 0) >= SEVERIDADES[minimo] and chave(a) not in enviados]


def registrar(alertas: list[dict], historico: Path) -> None:
    enviados = set(json.loads(historico.read_text(encoding="utf-8"))) if historico.exists() else set()
    enviados |= {chave(a) for a in alertas}
    historico.write_text(json.dumps(sorted(enviados), indent=1) + "\n", encoding="utf-8")


def texto(alertas: list[dict], referencia: str) -> str:
    linhas = [f"Indicadores · {referencia}"] + [f"[{a['severidade']}] {a['mensagem']}" for a in alertas]
    return "\n".join(linhas)


def enviar_telegram(mensagem: str, token: str, chat_id: str) -> None:
    dados = urllib.parse.urlencode({"chat_id": chat_id, "text": mensagem}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=dados)
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - URL fixa da API do Telegram
        if r.status != 200:
            raise RuntimeError(f"Telegram respondeu {r.status}")


def notificar(alertas: list[dict], referencia: str, historico: Path, minimo: str = "alta") -> dict:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    pendentes = novos(alertas, historico, minimo)
    if not pendentes:
        return {"enviados": 0, "motivo": "nenhum alerta novo"}
    if not (token and chat):
        return {"enviados": 0, "motivo": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não definidos", "pendentes": len(pendentes)}
    enviar_telegram(texto(pendentes, referencia), token, chat)
    registrar(pendentes, historico)
    return {"enviados": len(pendentes)}
