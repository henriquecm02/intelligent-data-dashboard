"""Dashboard de indicadores econômicos.

  streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import tema

from idash import analysis as an
from idash.analysis import br
from idash.cli import make_llm
from idash.collector import BY_SLUG

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "indicadores.db"
REPORT = ROOT / "reports" / "ultimo.json"
SEV = {"alta": ("critico", "Alta"), "media": ("atencao", "Média"), "baixa": ("atencao", "Baixa")}

st.set_page_config(page_title="Indicadores econômicos", page_icon="📈", layout="wide")


@st.cache_data(ttl=600)
def data(_mtime: float):
    return an.load(DB), an.analyze(DB), an.collection_health(DB)


if not DB.exists():
    st.error("Banco vazio. Rode `idash coletar` primeiro.")
    st.stop()

df, analysis, health = data(DB.stat().st_mtime)
ind = analysis["indicadores"]

fresh = f"{br(health['horas_desde_ultima'], 1)} h" if health.get("execucoes") else "—"
tema.aplicar_tema(
    "Indicadores econômicos para pequenas empresas",
    "Câmbio, juros e inflação coletados do Banco Central, com alertas quando algo sai do normal e um resumo semanal "
    "por IA em que todo número é conferido contra os dados.",
    kicker="Dados públicos · Banco Central (SGS)",
    chips=[f"Última coleta há {fresh}", f"Duração mediana {br(health.get('duracao_mediana_s') or 0)} s",
           f"Sucesso {br(health.get('taxa_sucesso_pct') or 0, 1)}% em {health.get('execucoes', 0)} execuções"])

cols = st.columns(5)
cards = [
    ("dolar", "Dólar (PTAX)", lambda i: f"R$ {br(i['ultimo_valor'], 4)}", lambda i: i["var_7d_pct"], "7 dias"),
    ("euro", "Euro (PTAX)", lambda i: f"R$ {br(i['ultimo_valor'], 4)}", lambda i: i["var_7d_pct"], "7 dias"),
    ("selic", "Meta Selic", lambda i: f"{br(i['ultimo_valor'])}% a.a.", lambda i: None, ""),
    ("ipca", "IPCA 12 meses", lambda i: f"{br(i['acumulado_12m_pct'])}%", lambda i: i["ultimo_valor"], "no mês"),
    ("igpm", "IGP-M 12 meses", lambda i: f"{br(i['acumulado_12m_pct'])}%", lambda i: i["ultimo_valor"], "no mês"),
]
for col, (slug, label, val, delta, suffix) in zip(cols, cards):
    if slug in ind:
        d = delta(ind[slug])
        col.metric(label, val(ind[slug]), f"{br(d, sinal=True)}% {suffix}" if d is not None else None,
                   delta_color="inverse", help=f"Último dado: {ind[slug]['ultima_data']}")

left, right = st.columns([2, 1])
with left:
    c1, c2 = st.columns([1, 1])
    slug = c1.selectbox("Indicador", list(BY_SLUG), format_func=lambda s: BY_SLUG[s].name)
    periodo = c2.segmented_control("Período", ["30 dias", "6 meses", "1 ano", "Tudo"], default="6 meses")
    s = df[df["serie"] == slug].copy()
    if periodo != "Tudo":
        days = {"30 dias": 30, "6 meses": 182, "1 ano": 365}[periodo]
        s = s[s["data"] >= s["data"].max() - timedelta(days=days)]
    base = alt.Chart(s).encode(x=alt.X("data:T", title=None, axis=alt.Axis(format="%d/%m/%y")))
    y = alt.Y("valor:Q", title=BY_SLUG[slug].unit, scale=alt.Scale(zero=False))
    area = base.mark_area(color=alt.Gradient(gradient="linear", x1=1, x2=1, y1=1, y2=0, stops=[
        alt.GradientStop(color=tema.rgba(tema.cor(0), 0.0), offset=0),
        alt.GradientStop(color=tema.rgba(tema.cor(0), 0.18), offset=1)])).encode(y=y, y2=alt.datum(s["valor"].min()))
    line = base.mark_line(color=tema.cor(0), strokeWidth=2).encode(
        y=y, tooltip=[alt.Tooltip("data:T", format="%d/%m/%Y", title="data"), alt.Tooltip("valor:Q", format=".4f")])
    layers = [area, line]
    anoms = pd.DataFrame(ind.get(slug, {}).get("anomalias", []))
    if not anoms.empty:
        anoms["data"] = pd.to_datetime(anoms["data"])
        anoms = anoms[anoms["data"] >= s["data"].min()]
        layers.append(alt.Chart(anoms).mark_point(color=tema.cor(1), size=110, filled=True, opacity=1,
                                                   stroke="white", strokeWidth=1.5).encode(
            x="data:T", y="valor:Q", tooltip=[alt.Tooltip("data:T", format="%d/%m/%Y"),
                                               alt.Tooltip("variacao_pct:Q", title="variação %"), "z:Q"]))
    st.altair_chart(alt.layer(*layers).properties(height=330), use_container_width=True)
    if not anoms.empty:
        st.caption("🔵 pontos azuis: variações diárias atípicas (|z| ≥ 3 em relação aos 90 dias anteriores).")

with right:
    st.subheader("Alertas")
    for a in analysis["alertas"]:
        nivel, rotulo = SEV.get(a["severidade"], ("atencao", a["severidade"]))
        with st.container(border=True):
            st.html(tema.selo(nivel, f"Severidade {rotulo.lower()}"))
            st.markdown(a["mensagem"])
    if not analysis["alertas"]:
        st.success("Nenhum alerta.")

st.divider()
tab_resumo, tab_pergunta, tab_dados = st.tabs(["🧠 Resumo da semana", "💬 Pergunte aos dados", "🗃️ Dados"])
with tab_resumo:
    saved = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    resumo = (saved.get("resumo") or {}).get("texto")
    if resumo:
        st.markdown(resumo)
    else:
        st.info("Resumo por IA ainda não gerado. Rode `idash relatorio --ia` com GEMINI_API_KEY definida.")
with tab_pergunta:
    llm = make_llm()
    if llm is None:
        st.info("Defina GEMINI_API_KEY para perguntar em linguagem natural (ex.: \"Quanto o dólar subiu no último mês?\").")
    else:
        q = st.text_input("Pergunta", "Quanto o dólar variou no último mês e qual foi o pico?")
        if st.button("Perguntar", type="primary"):
            from idash.insights import ask
            with st.spinner("Consultando os dados…"):
                res = ask(llm, DB, analysis, q)
            st.markdown(res["resposta"])
            with st.expander("Ferramentas consultadas"):
                st.json(res["ferramentas"])
with tab_dados:
    st.dataframe(df[df["serie"] == slug].sort_values("data", ascending=False), hide_index=True,
                 use_container_width=True)
