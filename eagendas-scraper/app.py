"""
Interface web para o Extrator de Agendas de Autoridades — e-Agendas CGU
Execute com: streamlit run app.py
"""

import io
import re
import time
import json
import zipfile
from datetime import date, timedelta
import streamlit as st
import pandas as pd

from scraper import (
    build_session,
    get_officials_from_govbr,
    scrape_official,
    resolve_official,
    extract_events,
    event_to_record,
    filter_by_date,
    parse_ng_init_json,
    BASE_URL,
)
from urllib.parse import urlencode

# ──────────────────────────────────────────────────────────────────────────────
# Configuração da página
# ──────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Agendas de Autoridades — e-Agendas CGU",
    page_icon="📅",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📅 e-Agendas Scraper")
    st.caption("Extrai agendas públicas de autoridades federais do [e-Agendas](https://eagendas.cgu.gov.br) (CGU)")

    st.divider()
    st.subheader("Fonte de dados")

    modo = st.radio(
        "Como deseja buscar?",
        ["Por ministério (gov.br)", "Por URL do e-Agendas", "Por ID interno"],
        index=0,
    )

    if modo == "Por ministério (gov.br)":
        govbr_url = st.text_input(
            "URL da página de agendas no gov.br",
            value="https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades",
            help="Exemplo: https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades",
        )
        eagendas_url = None
        servidor_id = orgao_id = cargo = None

    elif modo == "Por URL do e-Agendas":
        eagendas_url = st.text_input(
            "URL do e-Agendas",
            placeholder="https://eagendas.cgu.gov.br?filtro_codigo_orgao=...",
        )
        govbr_url = None
        servidor_id = orgao_id = cargo = None

    else:
        servidor_id = st.number_input("ID do servidor", min_value=1, step=1)
        orgao_id = st.number_input("ID do órgão", min_value=1, step=1)
        cargo = st.text_input("Cargo", placeholder="MINISTRO DE MINAS E ENERGIA")
        govbr_url = eagendas_url = None

    st.divider()
    st.subheader("Opções")

    limite = st.number_input(
        "Limite de autoridades (0 = todas)",
        min_value=0,
        max_value=500,
        value=0,
        help="Útil para testar antes de baixar tudo",
    )

    delay = st.slider(
        "Intervalo entre requisições (s)",
        min_value=0.5,
        max_value=5.0,
        value=1.5,
        step=0.5,
        help="Respeite o servidor público 🙂",
    )

    excluir_viagens = st.checkbox("Excluir viagens SCDP", value=False)

    st.divider()
    st.subheader("Filtro de data")

    amanha = date.today() + timedelta(days=1)
    opcao_data = st.radio(
        "Período",
        ["Todos os dias disponíveis", "Apenas amanhã", "Data específica"],
        index=0,
    )

    if opcao_data == "Apenas amanhã":
        data_alvo = amanha
        st.caption(f"📅 Amanhã: **{amanha.strftime('%d/%m/%Y')}**")
    elif opcao_data == "Data específica":
        data_alvo = st.date_input("Escolha a data", value=amanha, format="DD/MM/YYYY")
    else:
        data_alvo = None

    st.divider()
    executar = st.button("▶ Extrair agendas", type="primary", use_container_width=True)

# ──────────────────────────────────────────────────────────────────────────────
# Página principal
# ──────────────────────────────────────────────────────────────────────────────

st.title("Agendas de Autoridades Federais")
st.markdown(
    "Extraia e explore compromissos públicos de autoridades do Governo Federal "
    "registrados no [e-Agendas (CGU)](https://eagendas.cgu.gov.br). "
    "Configure a fonte na barra lateral e clique em **Extrair agendas**."
)

# Colunas visíveis na tabela (ordem amigável)
DISPLAY_COLS = [
    "data_inicio", "tipo", "titulo", "local",
    "agenda_de", "cargo_oficial", "orgao_sigla",
    "agentes_publicos", "agentes_privados",
    "publicado_em", "modificado_em",
    "url_compromisso",
]

LABELS = {
    "data_inicio": "Data/Hora início",
    "data_fim": "Data/Hora fim",
    "tipo": "Tipo",
    "titulo": "Título",
    "local": "Local",
    "agenda_de": "Autoridade",
    "cargo_oficial": "Cargo",
    "orgao": "Órgão",
    "orgao_sigla": "Sigla",
    "agentes_publicos": "Agentes Públicos",
    "agentes_privados": "Agentes Privados",
    "publicado_em": "Publicado em",
    "modificado_em": "Modificado em",
    "url_compromisso": "Link",
    "compromisso_id": "ID Compromisso",
    "pertenencia_id": "ID Servidor",
    "nome_oficial": "Nome Oficial",
}


def to_zip(records: list[dict]) -> bytes:
    """Gera um ZIP com CSV + JSON dos registros."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # CSV
        csv_buf = io.StringIO()
        if records:
            import csv as csv_mod
            writer = csv_mod.DictWriter(csv_buf, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
        zf.writestr("agendas.csv", csv_buf.getvalue().encode("utf-8"))
        # JSON
        zf.writestr("agendas.json", json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"))
    return buf.getvalue()


def to_csv_bytes(records: list[dict]) -> bytes:
    buf = io.StringIO()
    if records:
        import csv as csv_mod
        writer = csv_mod.DictWriter(buf, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    return buf.getvalue().encode("utf-8")


if executar:
    session = build_session()
    all_records: list[dict] = []

    # ── Modo gov.br ──────────────────────────────────────────────────────────
    if govbr_url:
        with st.spinner("Buscando lista de autoridades no gov.br..."):
            officials = get_officials_from_govbr(session, govbr_url, delay=0)

        if not officials:
            st.error("Nenhuma autoridade encontrada. Verifique a URL.")
            st.stop()

        if limite:
            officials = officials[:limite]

        st.info(f"**{len(officials)} autoridade(s)** encontrada(s). Extraindo eventos...")

        progress_bar = st.progress(0)
        status_text = st.empty()
        per_official_expander = st.expander("Ver progresso por autoridade", expanded=False)
        log_lines = []

        for i, official in enumerate(officials):
            nome = official.get("nome") or official.get("cargo", "—")
            status_text.markdown(f"⏳ Processando **{nome}**... ({i+1}/{len(officials)})")

            records = scrape_official(session, official, delay=delay)
            all_records.extend(records)

            if records:
                log_lines.append(f"✅ **{nome}** — {len(records)} eventos")
            else:
                log_lines.append(f"ℹ️ **{nome}** — sem agenda obrigatória")

            with per_official_expander:
                st.markdown("\n".join(log_lines))

            progress_bar.progress((i + 1) / len(officials))

        status_text.markdown(f"✅ Extração concluída!")

    # ── Modo URL direta ──────────────────────────────────────────────────────
    elif eagendas_url:
        with st.spinner("Extraindo eventos..."):
            official = {"url": eagendas_url, "nome": "", "cargo": ""}
            all_records = scrape_official(session, official, delay=delay)

    # ── Modo ID direto ───────────────────────────────────────────────────────
    elif servidor_id and orgao_id and cargo:
        params = {
            "filtro_orgaos_ativos": "on",
            "filtro_orgao": str(int(orgao_id)),
            "filtro_cargos_ativos": "on",
            "filtro_cargo": cargo,
            "filtro_apos_ativos": "on",
            "filtro_servidor": str(int(servidor_id)),
            "cargo_confianca_id": "",
            "is_cargo_vago": "false",
        }
        url = f"{BASE_URL}/?{urlencode(params)}"
        with st.spinner("Extraindo eventos..."):
            official = {"url": url, "nome": "", "cargo": cargo}
            all_records = scrape_official(session, official, delay=delay)
    else:
        st.warning("Configure uma fonte de dados na barra lateral.")
        st.stop()

    # ── Filtros e exibição ───────────────────────────────────────────────────
    if excluir_viagens:
        before = len(all_records)
        all_records = [r for r in all_records if r.get("tipo") != "Viagem SCDP"]
        st.caption(f"{before - len(all_records)} viagens SCDP excluídas")

    if data_alvo:
        total_antes = len(all_records)
        all_records = filter_by_date(all_records, data_alvo)
        label = "amanhã" if data_alvo == amanha else data_alvo.strftime("%d/%m/%Y")
        if all_records:
            st.success(f"📅 {len(all_records)} compromisso(s) encontrado(s) para **{label}** (de {total_antes} eventos no total)")
        else:
            st.warning(f"Nenhum compromisso encontrado para **{label}**. A agenda pode ainda não ter sido publicada.")
            st.stop()

    if not all_records:
        st.warning("Nenhum evento encontrado com os parâmetros informados.")
        st.stop()

    df = pd.DataFrame(all_records)

    # ── Métricas resumo ──────────────────────────────────────────────────────
    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total de eventos", len(df))
    c2.metric("Autoridades", df["agenda_de"].nunique() if "agenda_de" in df else "—")
    c3.metric("Reuniões", int((df["tipo"] == "Reunião").sum()) if "tipo" in df else 0)
    c4.metric("Eventos públicos", int((df["tipo"] == "Evento").sum()) if "tipo" in df else 0)

    # ── Filtros interativos ──────────────────────────────────────────────────
    st.divider()
    with st.expander("🔍 Filtros", expanded=True):
        col1, col2, col3 = st.columns(3)

        tipos_disponiveis = sorted(df["tipo"].unique().tolist()) if "tipo" in df else []
        tipos_sel = col1.multiselect("Tipo de evento", tipos_disponiveis, default=tipos_disponiveis)

        autoridades = sorted(df["agenda_de"].unique().tolist()) if "agenda_de" in df else []
        aut_sel = col2.multiselect("Autoridade", autoridades, default=autoridades)

        busca = col3.text_input("Buscar no título", placeholder="ex: Petrobras, energia solar...")

    df_filtrado = df.copy()
    if tipos_sel:
        df_filtrado = df_filtrado[df_filtrado["tipo"].isin(tipos_sel)]
    if aut_sel:
        df_filtrado = df_filtrado[df_filtrado["agenda_de"].isin(aut_sel)]
    if busca:
        df_filtrado = df_filtrado[
            df_filtrado["titulo"].str.contains(busca, case=False, na=False)
        ]

    df_filtrado = df_filtrado.sort_values("data_inicio", ascending=False)

    st.markdown(f"**{len(df_filtrado)}** eventos exibidos")

    # Colunas exibidas (apenas as que existem)
    cols_show = [c for c in DISPLAY_COLS if c in df_filtrado.columns]
    df_show = df_filtrado[cols_show].rename(columns=LABELS)

    # Transforma URLs em links clicáveis
    if "Link" in df_show.columns:
        df_show["Link"] = df_show["Link"].apply(
            lambda u: f"[🔗]({u})" if u else ""
        )

    st.dataframe(
        df_show,
        use_container_width=True,
        height=500,
        column_config={
            "Link": st.column_config.LinkColumn("Link"),
            "Data/Hora início": st.column_config.TextColumn(width="small"),
        },
    )

    # ── Downloads ─────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Baixar dados")

    col_dl1, col_dl2, col_dl3 = st.columns(3)

    records_export = df_filtrado.to_dict(orient="records")

    col_dl1.download_button(
        label="⬇ CSV",
        data=to_csv_bytes(records_export),
        file_name="agendas.csv",
        mime="text/csv",
        use_container_width=True,
    )

    col_dl2.download_button(
        label="⬇ JSON",
        data=json.dumps(records_export, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="agendas.json",
        mime="application/json",
        use_container_width=True,
    )

    col_dl3.download_button(
        label="⬇ ZIP (CSV + JSON)",
        data=to_zip(records_export),
        file_name="agendas.zip",
        mime="application/zip",
        use_container_width=True,
    )

    # ── Gráficos ─────────────────────────────────────────────────────────────
    if len(df_filtrado) > 5:
        st.divider()
        st.subheader("Análise")

        tab1, tab2 = st.tabs(["Por tipo de evento", "Linha do tempo"])

        with tab1:
            tipo_counts = df_filtrado["tipo"].value_counts().reset_index()
            tipo_counts.columns = ["Tipo", "Quantidade"]
            st.bar_chart(tipo_counts.set_index("Tipo"))

        with tab2:
            df_time = df_filtrado.copy()
            df_time["mes"] = pd.to_datetime(df_time["data_inicio"], errors="coerce").dt.to_period("M").astype(str)
            timeline = df_time.groupby("mes").size().reset_index(name="Eventos")
            timeline = timeline.sort_values("mes")
            st.line_chart(timeline.set_index("mes"))

else:
    # ── Estado inicial ────────────────────────────────────────────────────────
    st.markdown("""
    ### Como usar

    1. Na barra lateral, escolha a **fonte de dados**:
       - **Por ministério**: cole a URL da página de agendas do gov.br do ministério
       - **Por URL do e-Agendas**: cole o link direto de uma autoridade
       - **Por ID interno**: informe o ID do servidor, órgão e cargo

    2. Configure as **opções** (limite para testes, intervalo entre requisições)

    3. Clique em **▶ Extrair agendas**

    4. Explore os dados na tabela, aplique filtros e **baixe em CSV ou JSON**

    ---
    ### Exemplos de URLs de ministérios

    | Ministério | URL |
    |---|---|
    | Minas e Energia (MME) | `https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades` |
    | Fazenda (MF) | `https://www.gov.br/fazenda/pt-br/acesso-a-informacao/agendas-de-autoridades` |
    | Educação (MEC) | `https://www.gov.br/mec/pt-br/acesso-a-informacao/agendas-de-autoridades` |
    | Saúde (MS) | `https://www.gov.br/saude/pt-br/acesso-a-informacao/agendas-de-autoridades` |
    | Justiça (MJ) | `https://www.gov.br/justica/pt-br/acesso-a-informacao/agendas-de-autoridades` |
    | Casa Civil | `https://www.gov.br/casacivil/pt-br/acesso-a-informacao/agendas-de-autoridades` |
    """)
