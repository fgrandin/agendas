# 📅 e-Agendas Scraper

Extrai e explora agendas públicas de autoridades do Governo Federal. Combina dados do [e-Agendas (CGU)](https://eagendas.cgu.gov.br) com a agenda do Presidente da República disponível no [Planalto](https://www.gov.br/planalto).

Funciona de três formas: **interface web no browser**, **linha de comando** ou **Docker** (sem instalar Python).

---

## Interface web (recomendado)

### Rodar localmente

```bash
# 1. Clone ou baixe este repositório
git clone https://github.com/SEU_USUARIO/eagendas-scraper.git
cd eagendas-scraper

# 2. Instale as dependências
pip install -r requirements.txt

# 3. Inicie a interface
streamlit run app.py
```

Acesse `http://localhost:8501` no browser.

### Hospedar de graça no Streamlit Cloud

1. Faça fork deste repositório no GitHub
2. Acesse [share.streamlit.io](https://share.streamlit.io)
3. Conecte sua conta GitHub, selecione o repositório e o arquivo `app.py`
4. Clique em **Deploy** — em ~1 minuto sua ferramenta estará online com URL pública

---

## Linha de comando (CLI)

```bash
pip install requests

# Presidente + todos os ministros + Vice-Presidente (amanhã)
python scraper.py --ministros --data amanha --output-dir ./dados

# Presidente + todos os ministros + Vice-Presidente (data específica)
python scraper.py --ministros --data 2026-05-14 --output-dir ./dados

# Extrair todas as autoridades de um ministério
python scraper.py \
  --govbr-url "https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades" \
  --output-dir ./dados_mme

# Extrair uma autoridade específica
python scraper.py \
  --eagendas-url "https://eagendas.cgu.gov.br?filtro_codigo_orgao=2852&filtro_tipo_cargo=cargo_comissao&filtro_descricao_cargo=MINISTRO%20DE%20MINAS%20E%20ENERGIA&filtro_nome_servidor=Alexandre%20Silveira%20de%20Oliveira&origem_request=govbr" \
  --output-dir ./dados

# Extrair pelo ID interno do e-Agendas
python scraper.py \
  --servidor-id 14248 \
  --orgao-id 661 \
  --cargo "MINISTRO DE MINAS E ENERGIA" \
  --output-dir ./dados
```

### Opções do CLI

| Argumento | Descrição | Padrão |
|---|---|---|
| `--ministros` | Busca Presidente + todos os ministros + Vice-Presidente de uma vez | — |
| `--govbr-url` | URL da página de agendas no gov.br de um ministério | — |
| `--eagendas-url` | URL direta de um oficial no e-Agendas | — |
| `--servidor-id` | ID interno do servidor (requer `--orgao-id` e `--cargo`) | — |
| `--data` | Filtra por data: `hoje`, `amanha` ou `YYYY-MM-DD` | — |
| `--output-dir` | Diretório de saída | `./dados` |
| `--formato` | Formatos: `csv`, `json` ou `csv,json` | `csv,json` |
| `--delay` | Segundos entre requisições | `1.5` |
| `--sem-viagens` | Exclui viagens SCDP do resultado | `false` |
| `--limite` | Limita o número de autoridades (0 = todas) | `0` |

---

## Docker (sem instalar Python)

```bash
# Interface web
docker build -t eagendas .
docker run -p 8501:8501 eagendas

# CLI com saída local
docker run --rm \
  -v $(pwd)/dados:/app/dados \
  eagendas \
  python scraper.py \
    --govbr-url "https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades" \
    --output-dir /app/dados
```

---

## Dados extraídos

Cada evento exportado contém:

| Campo | Descrição |
|---|---|
| `tipo` | Tipo do evento (Reunião, Audiência, Evento, Viagem SCDP…) |
| `titulo` | Título/descrição do compromisso |
| `data_inicio` | Data e hora de início (ISO 8601) |
| `data_fim` | Data e hora de fim |
| `local` | Local do compromisso |
| `agenda_de` | Nome da autoridade |
| `cargo_oficial` | Cargo da autoridade |
| `orgao` | Nome do órgão |
| `orgao_sigla` | Sigla do órgão |
| `compromisso_id` | ID único no e-Agendas |
| `publicado_em` | Data de publicação |
| `modificado_em` | Data da última modificação |
| `agentes_publicos` | Participantes públicos (separados por ` \| `) |
| `agentes_privados` | Participantes privados |
| `url_compromisso` | Link direto para o compromisso |
| `pertenencia_id` | ID interno da autoridade no e-Agendas |
| `nome_oficial` | Nome da autoridade |

---

## URLs de ministérios

| Ministério | URL |
|---|---|
| Minas e Energia (MME) | `https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades` |
| Fazenda (MF) | `https://www.gov.br/fazenda/pt-br/acesso-a-informacao/agendas-de-autoridades` |
| Educação (MEC) | `https://www.gov.br/mec/pt-br/acesso-a-informacao/agendas-de-autoridades` |
| Saúde (MS) | `https://www.gov.br/saude/pt-br/acesso-a-informacao/agendas-de-autoridades` |
| Justiça (MJ) | `https://www.gov.br/justica/pt-br/acesso-a-informacao/agendas-de-autoridades` |
| Casa Civil | `https://www.gov.br/casacivil/pt-br/acesso-a-informacao/agendas-de-autoridades` |
| Relações Exteriores (MRE) | `https://www.gov.br/mre/pt-br/acesso-a-informacao/agendas-de-autoridades` |
| Defesa (MD) | `https://www.gov.br/defesa/pt-br/acesso-a-informacao/agendas-de-autoridades` |

---

## Notas

- A agenda do **Presidente da República** é extraída de [planalto.gov.br](https://www.gov.br/planalto) (não está no e-Agendas) e sempre requer uma data específica.
- Nem todas as autoridades listadas no gov.br têm agenda cadastrada no e-Agendas (apenas "Agentes Públicos Obrigados"). O scraper identifica e pula esses casos automaticamente.
- Todos os dados extraídos são **públicos**: [eagendas.cgu.gov.br](https://eagendas.cgu.gov.br) e [planalto.gov.br](https://www.gov.br/planalto).
- Use um delay razoável (`--delay 1.5` ou maior) para não sobrecarregar os servidores.
