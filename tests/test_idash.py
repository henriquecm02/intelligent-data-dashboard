"""Testes offline: coleta com API simulada, análise com séries sintéticas e IA com LLM roteirizado."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from idash import analysis as an
from idash.collector import BY_SLUG, collect, parse
from idash.insights import ask, ungrounded_numbers, weekly_summary
from idash.llm import LLMResponse, ScriptedLLM, ToolCall, Usage

TODAY = date(2026, 6, 30)


def fake_api(values: dict[str, list[tuple[date, float]]], fail: set[str] = frozenset()):
    calls = []

    def fetcher(series, start, end):
        calls.append((series.slug, start, end))
        if series.slug in fail:
            raise ConnectionError("API fora do ar")
        return [{"data": d.strftime("%d/%m/%Y"), "valor": f"{v}"} for d, v in values.get(series.slug, [])
                if start <= d <= end + timedelta(days=60)]  # inclui datas futuras, como a API da Selic
    fetcher.calls = calls
    return fetcher


def daily(start: date, n: int, base: float, step: float = 0.01):
    return [(start + timedelta(days=i), round(base + step * (i % 7), 4)) for i in range(n)]


def monthly(start_year: int, values: list[float]):
    return [(date(start_year + (i // 12), i % 12 + 1, 1), v) for i, v in enumerate(values)]


@pytest.fixture
def db(tmp_path):
    vals = {
        "dolar": daily(date(2025, 6, 1), 395, 5.0),
        "euro": daily(date(2025, 6, 1), 395, 6.0),
        "selic": [(date(2025, 6, 1) + timedelta(days=i), 14.0 if i < 380 else 13.75) for i in range(420)],
        "ipca": monthly(2025, [0.5] * 18),
        "igpm": monthly(2025, [0.3] * 18),
    }
    vals["dolar"][-1] = (vals["dolar"][-1][0], 5.40)  # salto no último dia
    path = tmp_path / "t.db"
    collect(path, start=date(2025, 1, 1), today=TODAY, fetcher=fake_api(vals))
    return path


def test_parse_rejects_bad_rows_and_future_dates():
    rows, rejected = parse([{"data": "01/06/2026", "valor": "5.1"}, {"data": "xx", "valor": "1"},
                            {"data": "02/06/2026", "valor": "abc"}, {"data": "01/07/2026", "valor": "5.2"},
                            {"data": "01/06/2026", "valor": "5.3"}], today=TODAY)
    assert rows == [("2026-06-01", 5.3)]
    assert {r["motivo"].split(":")[0] for r in rejected} == {"formato inválido", "data futura"}


def test_collect_is_incremental_and_logs_runs(db):
    con = sqlite3.connect(db)
    before = con.execute("SELECT COUNT(*) FROM observacoes").fetchone()[0]
    assert con.execute("SELECT MAX(data) FROM observacoes WHERE serie='selic'").fetchone()[0] <= TODAY.isoformat()
    fetcher = fake_api({"dolar": daily(date(2025, 6, 1), 395, 5.0)})
    collect(db, today=TODAY, fetcher=fetcher, series=[BY_SLUG["dolar"]])
    slug, start, _ = fetcher.calls[0]
    assert start > date(2026, 5, 1)  # busca só a janela recente (última data − margem)
    assert con.execute("SELECT COUNT(DISTINCT execucao) FROM coletas").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM observacoes").fetchone()[0] == before
    con.close()


def test_revisions_are_updated(db):
    fetcher = fake_api({"dolar": [(date(2026, 6, 25), 9.99)]})
    res = collect(db, today=TODAY, fetcher=fetcher, series=[BY_SLUG["dolar"]])
    assert res[0]["linhas_alteradas"] == 1


def test_one_failing_series_does_not_stop_others(tmp_path):
    res = collect(tmp_path / "x.db", today=TODAY, fetcher=fake_api({"euro": daily(date(2026, 6, 1), 10, 6.0)},
                                                                     fail={"dolar"}))
    status = {r["serie"]: r["status"] for r in res}
    assert status["dolar"] == "erro" and status["euro"] == "ok"
    assert an.collection_health(tmp_path / "x.db")["taxa_sucesso_pct"] == 0.0


def test_analysis_metrics_alerts_and_anomaly(db):
    a = an.analyze(db, reference=TODAY)
    d = a["indicadores"]["dolar"]
    assert d["ultimo_valor"] == 5.40 and d["var_1d_pct"] > 5
    assert any(x["data"] == d["ultima_data"] for x in d["anomalias"])
    assert a["indicadores"]["ipca"]["acumulado_12m_pct"] == pytest.approx(6.17, abs=0.01)  # 1,005^12 − 1
    kinds = {(x["indicador"], x["tipo"]) for x in a["alertas"]}
    assert {("dolar", "variacao"), ("dolar", "anomalia"), ("selic", "mudanca"), ("ipca", "limite")} <= kinds
    assert ("igpm", "limite") not in kinds  # 3,66% < 6%


def test_grounding_check_flags_invented_numbers(db):
    a = an.analyze(db, reference=TODAY)
    assert ungrounded_numbers("O IPCA acumulou 6,17% e a Selic está em 13,75%.", a) == []
    assert ungrounded_numbers("O dólar subiu 3,33% na semana.", a) == ["3,33%"]


def test_summary_rewrites_when_number_is_invented(db):
    a, h = an.analyze(db, reference=TODAY), an.collection_health(db)
    llm = ScriptedLLM([LLMResponse("O dólar subiu 99,99%.", [], Usage(calls=1)),
                       LLMResponse("O IPCA em 12 meses está em 6,17%.", [], Usage(calls=1))])
    out = weekly_summary(llm, a, h)
    assert out["tentativas"] == 2 and out["numeros_sem_respaldo"] == []
    assert "99,99%" in llm.chat_inputs[-1][-1].text


def test_qa_uses_tools_and_answers_from_evidence(db):
    a = an.analyze(db, reference=TODAY)
    llm = ScriptedLLM([
        LLMResponse(None, [ToolCall("period_stats", {"indicador": "selic", "inicio": "2026-01-01",
                                                     "fim": "2026-06-30"})], Usage(calls=1)),
        LLMResponse("A Selic foi de 14,00% para 13,75% no período.", [], Usage(calls=1)),
    ])
    res = ask(llm, db, a, "O que aconteceu com a Selic em 2026?")
    assert res["ferramentas"][0]["ferramenta"] == "period_stats"
    assert res["numeros_sem_respaldo"] == []
    tool_msg = llm.chat_inputs[-1][-1]
    stats = tool_msg.tool_results[0][1]
    assert stats["primeiro"] == 14.0 and stats["ultimo"] == 13.75


def test_qa_rejects_unknown_indicator(db):
    a = an.analyze(db, reference=TODAY)
    llm = ScriptedLLM([LLMResponse(None, [ToolCall("get_indicator", {"indicador": "bitcoin"})], Usage(calls=1)),
                       LLMResponse("Não tenho dados de bitcoin.", [], Usage(calls=1))])
    res = ask(llm, db, a, "E o bitcoin?")
    assert "erro" in llm.chat_inputs[-1][-1].tool_results[0][1]
    assert res["resposta"].startswith("Não tenho")


def test_near_miss_is_not_accepted(db):
    a = {"taxa": 100.0, "x": 5.6518}
    assert ungrounded_numbers("Sucesso de 99,99% e dólar a R$ 5,65.", a) == ["99,99%"]


# ------------------------------------------------------------------ dias úteis e notificações
def test_atraso_em_dias_uteis_ignora_fim_de_semana():
    import pandas as pd

    from idash.analysis import business_days_between

    T = pd.Timestamp
    assert business_days_between(T("2026-11-20"), T("2026-11-22")) == 0  # sexta -> domingo
    assert business_days_between(T("2026-11-20"), T("2026-11-23")) == 1  # sexta -> segunda
    assert business_days_between(T("2026-11-13"), T("2026-11-20")) == 5


def test_notifica_so_alertas_novos_de_severidade_alta(tmp_path, monkeypatch):
    from idash import notify

    enviados = []
    monkeypatch.setattr(notify, "enviar_telegram", lambda msg, token, chat: enviados.append(msg))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    alertas = [{"indicador": "dolar", "tipo": "variacao", "severidade": "alta", "mensagem": "Dólar +5%"},
               {"indicador": "ipca", "tipo": "limite", "severidade": "media", "mensagem": "IPCA acima"}]
    hist = tmp_path / "enviados.json"
    assert notify.notificar(alertas, "2026-11-21", hist)["enviados"] == 1
    assert "Dólar +5%" in enviados[0] and "IPCA" not in enviados[0]
    assert notify.notificar(alertas, "2026-11-22", hist)["enviados"] == 0  # já enviado


def test_sem_token_nao_envia(tmp_path, monkeypatch):
    from idash import notify

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    alertas = [{"indicador": "dolar", "tipo": "variacao", "severidade": "alta", "mensagem": "Dólar +5%"}]
    r = notify.notificar(alertas, "2026-11-21", tmp_path / "e.json")
    assert r["enviados"] == 0 and r["pendentes"] == 1
