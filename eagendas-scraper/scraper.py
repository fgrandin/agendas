#!/usr/bin/env python3
"""
Extrator de Agendas de Autoridades do Governo Federal
Extrai dados do e-Agendas (eagendas.cgu.gov.br)

Uso:
    python scraper.py --govbr-url "https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades"
    python scraper.py --eagendas-url "https://eagendas.cgu.gov.br?filtro_codigo_orgao=2852&..."
    python scraper.py --servidor-id 14248 --orgao-id 661 --cargo "MINISTRO DE MINAS E ENERGIA"
"""

import re
import json
import csv
import time
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta, date
from urllib.parse import urlencode, urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    print("Instale as dependências: pip install requests", file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://eagendas.cgu.gov.br"
DEFAULT_DELAY = 1.5  # segundos entre requisições

PLANALTO_BASE = (
    "https://www.gov.br/planalto/pt-br/acompanhe-o-planalto"
    "/agenda-do-presidente-da-republica-lula"
    "/agenda-do-presidente-da-republica"
)


def build_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; AgendaScraper/1.0; +https://github.com/eagendas-scraper)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
    })
    return session


def extract_ng_init(html, key):
    """Extrai o valor de um atributo ng-init='key=VALUE'."""
    pattern = rf'ng-init="{re.escape(key)}=([^"]+)"'
    match = re.search(pattern, html)
    if not match:
        return None
    value = match.group(1)
    # Remove aspas simples de strings simples
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def parse_ng_init_json(html, key):
    """Extrai e faz parse de JSON embutido em ng-init."""
    raw = extract_ng_init(html, key)
    if not raw:
        return None
    # HTML entities
    raw = raw.replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def parse_detalhe(detalhe_html):
    """Converte o campo HTML 'detalhe' em listas de participantes."""
    if not detalhe_html:
        return [], []

    html = (
        detalhe_html
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("<br>", "\n")
        .replace("<strong>", "")
        .replace("</strong>", "")
    )
    # Remove remaining tags
    html = re.sub(r"<[^>]+>", "", html)

    public_agents, private_agents = [], []
    current = None

    for line in html.splitlines():
        line = line.strip()
        if not line:
            continue
        if "Agentes públicos participantes:" in line:
            current = "public"
        elif "Agentes privados participantes:" in line:
            current = "private"
        elif line.startswith("-"):
            agent = line[1:].strip()
            if current == "public":
                public_agents.append(agent)
            elif current == "private":
                # "NOME representando EMPRESA" — mantém o texto inteiro
                agent = agent.replace(" representando ", " (representando) ")
                private_agents.append(agent)

    return public_agents, private_agents


def fetch_page(session, url, delay=0, timeout=30):
    """Faz GET com retry simples e delay opcional."""
    if delay:
        time.sleep(delay)
    for attempt in range(2):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.Timeout as e:
            # Timeout: não tem sentido ficar tentando — falha rápido
            print(f"  [ERRO] {url}: {e}", file=sys.stderr)
            return None
        except requests.RequestException as e:
            if attempt == 1:
                print(f"  [ERRO] {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2)
    return None


def resolve_official(session, eagendas_url, delay=DEFAULT_DELAY):
    """
    Dado um URL de eagendas (novo ou antigo formato), retorna:
    (pertenencia_id, orgao_id, cargo, nome_oficial, html_completo_com_events)

    - Novo formato: filtro_nome_servidor → precisa de segundo request para carregar events
    - Antigo formato: filtro_servidor → events já estão embutidos
    """
    html = fetch_page(session, eagendas_url, delay=0)
    if not html:
        return None

    idServidor = extract_ng_init(html, "idServidor")
    idOrgao = extract_ng_init(html, "idOrgao")
    idCargo = extract_ng_init(html, "idCargo")

    # Events já embutidos (formato antigo com filtro_servidor)
    events_match = re.search(r'ng-init="events=(\[.*?\])"', html, re.DOTALL)
    if events_match and idServidor:
        return {
            "pertenencia_id": idServidor,
            "orgao_id": idOrgao,
            "cargo": idCargo,
            "html": html,
        }

    # Precisa resolver pelo servidores ng-init
    servidores = parse_ng_init_json(html, "servidores")
    if not servidores:
        # Oficial não é Agente Público Obrigado (sem agenda no e-Agendas)
        return None

    # Pega o primeiro servidor com pertenencia_id
    servidor = next(
        (s for s in servidores if s.get("pertenencia_id") and s.get("pertenencia_id") != -1),
        None,
    )
    if not servidor:
        return None

    pertenencia_id = servidor.get("pertenencia_id")
    nome = servidor.get("nome", "")
    cargo = servidor.get("cargo", idCargo or "")
    orgao_id = servidor.get("orgao_id", idOrgao)

    if not pertenencia_id:
        return None

    # Segundo request: carrega página com events embutidos
    params = {
        "filtro_orgaos_ativos": "on",
        "filtro_orgao": str(orgao_id),
        "filtro_cargos_ativos": "on",
        "filtro_cargo": cargo,
        "filtro_apos_ativos": "on",
        "filtro_servidor": str(pertenencia_id),
        "cargo_confianca_id": "",
        "is_cargo_vago": "false",
    }
    full_url = f"{BASE_URL}/?{urlencode(params)}"
    html2 = fetch_page(session, full_url, delay=delay)
    if not html2:
        return None

    return {
        "pertenencia_id": str(pertenencia_id),
        "orgao_id": str(orgao_id),
        "cargo": cargo,
        "nome": nome,
        "html": html2,
    }


def extract_events(html):
    """Extrai os events do ng-init da página com events embutidos."""
    events = parse_ng_init_json(html, "events")
    return events or []


def event_to_record(event, orgao_nome="", orgao_sigla=""):
    """Converte um evento em dicionário plano para exportação."""
    tipo = event.get("tipo", "")
    titulo_raw = event.get("title", "")

    # Remove prefixo de tipo ("Reunião - ", "Evento - ", etc.)
    titulo = re.sub(r"^[^-]+ - ", "", titulo_raw, count=1).strip()

    base = {
        "tipo": tipo,
        "titulo": titulo,
        "data_inicio": event.get("start", ""),
        "data_fim": event.get("end", ""),
        "agenda_de": event.get("agenda_de", ""),
        "tipo_exercicio": event.get("tipo_exercicio", ""),
        "orgao": orgao_nome or event.get("agenda_de", ""),
        "orgao_sigla": orgao_sigla,
    }

    if tipo == "Viagem SCDP":
        base.update({
            "local": "",
            "compromisso_id": str(event.get("viagem_id", event.get("id_scdp", ""))),
            "publicado_em": "",
            "modificado_em": "",
            "agentes_publicos": "",
            "agentes_privados": "",
            "url_compromisso": "",
        })
    else:
        pertenencia_id = event.get("pertenencia_id", "")
        compromisso_id = event.get("compromisso_id", "")
        public_agents, private_agents = parse_detalhe(event.get("detalhe", ""))
        url = (
            f"{BASE_URL}/info-compromisso/agenda/{pertenencia_id}/compromisso/{compromisso_id}"
            if pertenencia_id and compromisso_id
            else ""
        )
        base.update({
            "local": event.get("local", ""),
            "compromisso_id": str(compromisso_id),
            "publicado_em": event.get("publicado_em", ""),
            "modificado_em": event.get("modificado_em", "").replace("T", " ").split(".")[0],
            "agentes_publicos": " | ".join(public_agents),
            "agentes_privados": " | ".join(private_agents),
            "url_compromisso": url,
        })

    return base


# Padrão para identificar cargo de nível ministerial / vice-presidencial
_MINISTER_RE = re.compile(
    r"^(MINISTRO|MINISTRA|VICE-PRESIDENTE)\b",
    re.IGNORECASE,
)

# IDs de órgãos que NÃO são ministérios mas são administração direta
# (presidência, comandos militares, polícias, etc.) — excluídos por default
_NON_MINISTRY_SIGLAS = {
    "PR", "COMAER", "MB", "CEX", "HFA", "PF", "PRF", "UFNT",
    "CC-PR", "GSI/PR", "CGU", "AGU", "SECOM", "SG", "SRI/PR",
    "SERS",
}


def get_all_orgs(session):
    """Retorna lista de todos os órgãos do eagendas (embedding na página principal)."""
    html = fetch_page(session, BASE_URL + "/")
    if not html:
        return []
    return parse_ng_init_json(html, "orgaos") or []


def get_ministerial_orgs(session):
    """
    Retorna lista de órgãos-alvo: ministérios + Vice-Presidência da República.
    O Presidente da República não publica agenda no e-Agendas.
    """
    orgs = get_all_orgs(session)
    targets = []
    for o in orgs:
        if not o.get("activa"):
            continue
        sigla = o.get("sigla", "")
        nome = o.get("nome", "")
        # Vice-Presidência sempre inclui
        if sigla == "VPR":
            targets.append(o)
        # Ministérios (têm "Ministério" no nome, administração direta)
        elif o.get("administracao_direta") and "Ministério" in nome:
            targets.append(o)
    return targets


def fetch_org_cargos(session, org_id, delay=0.3):
    """Busca a lista de cargos de um órgão específico."""
    url = (
        f"{BASE_URL}/?filtro_orgaos_ativos=on&filtro_orgao={org_id}"
        f"&filtro_cargos_ativos=on&filtro_apos_ativos=on"
    )
    html = fetch_page(session, url, delay=delay, timeout=10)
    if not html:
        return []
    return parse_ng_init_json(html, "cargos") or []


def find_minister_cargo(cargos):
    """Encontra o cargo de nível ministerial/vice-presidencial na lista."""
    matches = [c for c in cargos if _MINISTER_RE.match(c.get("nome", ""))]
    if not matches:
        return None
    # Prefere cargos sem data_termino (ainda vigente) e de nome mais curto
    active = [c for c in matches if not c.get("data_termino")]
    pool = active if active else matches
    return min(pool, key=lambda c: len(c["nome"]))


def fetch_org_servidores(session, org_id, cargo_nome, delay=0.3):
    """Busca os servidores de um órgão para um cargo específico."""
    url = (
        f"{BASE_URL}/?filtro_orgaos_ativos=on&filtro_orgao={org_id}"
        f"&filtro_cargos_ativos=on&filtro_cargo={requests.utils.quote(cargo_nome)}"
        f"&filtro_apos_ativos=on"
    )
    html = fetch_page(session, url, delay=delay, timeout=10)
    if not html:
        return [], html
    servidores = parse_ng_init_json(html, "servidores") or []
    return servidores, html


def _fetch_minister_for_org(org, target_date, delay):
    """
    Worker: encadeia as 3 requisições necessárias para um órgão,
    retorna (label, records).
    """
    session = build_session()
    org_id = org["id"]
    org_nome = org.get("nome", "")

    # 1. Cargos do órgão
    cargos = fetch_org_cargos(session, org_id, delay=delay)
    cargo_obj = find_minister_cargo(cargos)
    if not cargo_obj:
        return org_nome, []

    cargo_nome = cargo_obj["nome"]

    # 2. Servidores para o cargo
    servidores, _ = fetch_org_servidores(session, org_id, cargo_nome, delay=delay)
    servidor = next(
        (s for s in servidores if s.get("pertenencia_id") and s.get("pertenencia_id") != -1),
        None,
    )
    if not servidor:
        return org_nome, []

    pertenencia_id = servidor["pertenencia_id"]
    nome_oficial = servidor.get("nome", "")
    orgao_nome = servidor.get("orgao", org_nome)
    orgao_sigla = servidor.get("sigla", org.get("sigla", ""))

    # 3. Eventos do oficial
    params = {
        "filtro_orgaos_ativos": "on",
        "filtro_orgao": str(org_id),
        "filtro_cargos_ativos": "on",
        "filtro_cargo": cargo_nome,
        "filtro_apos_ativos": "on",
        "filtro_servidor": str(pertenencia_id),
        "cargo_confianca_id": "",
        "is_cargo_vago": "false",
    }
    html_events = fetch_page(session, f"{BASE_URL}/?{urlencode(params)}", delay=delay)
    if not html_events:
        return nome_oficial or org_nome, []

    events = parse_ng_init_json(html_events, "events") or []
    records = []
    for event in events:
        record = event_to_record(event, orgao_nome=orgao_nome, orgao_sigla=orgao_sigla)
        record["pertenencia_id"] = str(pertenencia_id)
        record["cargo_oficial"] = cargo_nome
        record["nome_oficial"] = nome_oficial
        records.append(record)

    if target_date:
        records = filter_by_date(records, target_date)

    return nome_oficial or org_nome, records


def scrape_president_agenda(session, target_date=None):
    """
    Busca a agenda do Presidente da República no planalto.gov.br.
    target_date: objeto date ou string YYYY-MM-DD. Se None, usa hoje.
    Retorna lista de records no mesmo formato do e-Agendas.
    """
    if target_date is None:
        target_date = date.today()
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    date_str = target_date.isoformat()
    url = f"{PLANALTO_BASE}/{date_str}"

    html = fetch_page(session, url, timeout=15)
    if not html:
        return []

    blocks = re.findall(
        r'<li class="item-compromisso-wrapper">(.*?)</li>',
        html,
        re.DOTALL,
    )
    if not blocks:
        return []

    records = []
    for block in blocks:
        inicio_m = re.search(r'<time class="compromisso-inicio">([^<]+)</time>', block)
        titulo_m = re.search(r'<h2 class="compromisso-titulo">([^<]+)</h2>', block)
        local_m = re.search(r'<div class="compromisso-local">([^<]+)</div>', block)
        vcal_m = re.search(r'href="([^"]+/vcal_view)"', block)

        hora_str = inicio_m.group(1).strip() if inicio_m else ""
        titulo = titulo_m.group(1).strip() if titulo_m else ""
        local = local_m.group(1).strip() if local_m else ""
        url_vcal = vcal_m.group(1).replace("/vcal_view", "") if vcal_m else url

        # Converte "08h00" → "08:00"
        hora_iso = hora_str.replace("h", ":") if hora_str else ""
        data_inicio = f"{date_str}T{hora_iso}:00" if hora_iso else date_str

        records.append({
            "tipo": "Compromisso",
            "titulo": titulo,
            "data_inicio": data_inicio,
            "data_fim": "",
            "local": local,
            "agenda_de": "LUIZ INÁCIO LULA DA SILVA",
            "cargo_oficial": "PRESIDENTE DA REPÚBLICA",
            "orgao": "Presidência da República",
            "orgao_sigla": "PR",
            "compromisso_id": "",
            "publicado_em": "",
            "modificado_em": "",
            "agentes_publicos": "",
            "agentes_privados": "",
            "url_compromisso": url_vcal,
            "pertenencia_id": "",
            "nome_oficial": "LUIZ INÁCIO LULA DA SILVA",
            "tipo_exercicio": "",
        })

    return records


def scrape_all_ministers(target_date=None, max_workers=3, delay=0.4, progress_cb=None):
    """
    Busca a agenda do Presidente + todos os ministros + Vice-Presidente em paralelo.
    Retorna lista consolidada de records.
    """
    session = build_session()
    orgs = get_ministerial_orgs(session)
    if not orgs:
        return []

    # Presidente da República (planalto.gov.br — não está no e-Agendas)
    pres_date = target_date if target_date else date.today()
    pres_records = scrape_president_agenda(session, pres_date)
    pres_label = "LUIZ INÁCIO LULA DA SILVA"
    all_records = list(pres_records)
    total = len(orgs)
    if progress_cb:
        progress_cb(pres_label, len(pres_records), 0, total)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_org = {
            executor.submit(_fetch_minister_for_org, org, target_date, delay): org
            for org in orgs
        }
        concluidos = 0
        for future in as_completed(future_to_org):
            nome, records = future.result()
            all_records.extend(records)
            concluidos += 1
            if progress_cb:
                progress_cb(nome, len(records), concluidos, total)

    return all_records


def filter_by_date(records, target_date):
    """
    Filtra registros pelo dia exato de data_inicio.
    target_date: objeto date ou string "YYYY-MM-DD".
    """
    prefix = target_date.isoformat() if isinstance(target_date, date) else target_date
    return [r for r in records if r.get("data_inicio", "").startswith(prefix)]


def get_officials_from_govbr(session, govbr_url, delay=DEFAULT_DELAY):
    """
    Extrai links de eagendas da página de agendas do gov.br de um ministério.
    Retorna lista de dicts com 'url', 'cargo', 'nome'.
    """
    print(f"Buscando autoridades em: {govbr_url}")
    html = fetch_page(session, govbr_url)
    if not html:
        return []

    raw_links = re.findall(
        r'href="(https://eagendas\.cgu\.gov\.br[^"]+)"',
        html,
    )

    officials = []
    seen = set()
    for link in raw_links:
        # Decode HTML entities before URL parsing
        clean_link = link.replace("&amp;", "&").replace("&#038;", "&")
        if clean_link in seen:
            continue
        seen.add(clean_link)

        parsed = urlparse(clean_link)
        params = parse_qs(parsed.query)
        cargo = unquote(params.get("filtro_descricao_cargo", params.get("filtro_cargo", [""]))[0])
        nome = unquote(params.get("filtro_nome_servidor", [""])[0])

        # Pula cargos vagos
        if "VAGO" in nome.upper():
            continue

        officials.append({"url": clean_link, "cargo": cargo, "nome": nome})

    print(f"  {len(officials)} autoridades encontradas")
    return officials


def scrape_official(session, official_info, delay=DEFAULT_DELAY):
    """
    Resolve um oficial e retorna lista de registros de eventos.
    """
    url = official_info.get("url", "")
    nome = official_info.get("nome", "")
    cargo = official_info.get("cargo", "")

    print(f"  Processando: {nome or cargo}")

    result = resolve_official(session, url, delay=delay)
    if not result:
        print(f"    [INFO] Sem agenda obrigatória no e-Agendas para: {nome or cargo}")
        return []

    events = extract_events(result["html"])
    if not events:
        print(f"    Nenhum evento encontrado")
        return []

    print(f"    {len(events)} eventos extraídos")

    # Tenta capturar nome do órgão do primeiro evento
    orgao_nome = ""
    orgao_sigla = ""
    servidores = parse_ng_init_json(result["html"], "servidores")
    if servidores and servidores[0]:
        orgao_nome = servidores[0].get("orgao", "")
        orgao_sigla = servidores[0].get("sigla", "")

    records = []
    for event in events:
        record = event_to_record(event, orgao_nome=orgao_nome, orgao_sigla=orgao_sigla)
        record["pertenencia_id"] = result["pertenencia_id"]
        record["cargo_oficial"] = result.get("cargo", cargo)
        record["nome_oficial"] = result.get("nome", nome)
        records.append(record)

    return records


def _fetch_one_official(official_info, target_date, delay):
    """Worker executado em thread separada: cria sua própria sessão e retorna
    apenas os registros que batem com target_date (ou todos se None)."""
    session = build_session()
    url = official_info.get("url", "")
    nome = official_info.get("nome", "")
    cargo = official_info.get("cargo", "")

    result = resolve_official(session, url, delay=delay)
    if not result:
        return nome or cargo, []

    events = extract_events(result["html"])

    servidores = parse_ng_init_json(result["html"], "servidores")
    orgao_nome = orgao_sigla = ""
    if servidores and servidores[0]:
        orgao_nome = servidores[0].get("orgao", "")
        orgao_sigla = servidores[0].get("sigla", "")

    records = []
    for event in events:
        record = event_to_record(event, orgao_nome=orgao_nome, orgao_sigla=orgao_sigla)
        record["pertenencia_id"] = result["pertenencia_id"]
        record["cargo_oficial"] = result.get("cargo", cargo)
        record["nome_oficial"] = result.get("nome", nome)
        records.append(record)

    if target_date:
        records = filter_by_date(records, target_date)

    return nome or cargo, records


def scrape_officials_parallel(officials, target_date=None, max_workers=6, delay=0.5, progress_cb=None):
    """
    Busca múltiplos oficiais em paralelo.
    progress_cb(nome, n_encontrados, concluidos, total) é chamado após cada oficial terminar.
    Retorna lista consolidada de records.
    """
    all_records = []
    total = len(officials)
    concluidos = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_official = {
            executor.submit(_fetch_one_official, off, target_date, delay): off
            for off in officials
        }
        for future in as_completed(future_to_official):
            nome, records = future.result()
            all_records.extend(records)
            concluidos += 1
            if progress_cb:
                progress_cb(nome, len(records), concluidos, total)

    return all_records


def save_csv(records, path):
    """Salva registros em CSV."""
    if not records:
        return
    fieldnames = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    print(f"  CSV salvo: {path} ({len(records)} registros)")


def save_json(records, path):
    """Salva registros em JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  JSON salvo: {path} ({len(records)} registros)")


def main():
    parser = argparse.ArgumentParser(
        description="Extrai agendas de autoridades do e-Agendas (CGU)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  # Scrape todo o Ministério de Minas e Energia via gov.br
  python scraper.py --govbr-url "https://www.gov.br/mme/pt-br/acesso-a-informacao/agendas-de-autoridades"

  # Scrape um oficial específico via URL do e-Agendas
  python scraper.py --eagendas-url "https://eagendas.cgu.gov.br?filtro_codigo_orgao=2852&filtro_tipo_cargo=cargo_comissao&filtro_descricao_cargo=MINISTRO+DE+MINAS+E+ENERGIA&filtro_nome_servidor=Alexandre+Silveira+de+Oliveira&origem_request=govbr"

  # Scrape direto pelo ID interno
  python scraper.py --servidor-id 14248 --orgao-id 661 --cargo "MINISTRO DE MINAS E ENERGIA"
        """,
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--govbr-url", help="URL da página de agendas no gov.br de um ministério")
    source.add_argument("--eagendas-url", help="URL direta de um oficial no e-Agendas")
    source.add_argument(
        "--servidor-id",
        type=int,
        help="ID interno do servidor no e-Agendas (requer --orgao-id e --cargo)",
    )
    source.add_argument(
        "--ministros",
        action="store_true",
        help="Busca todos os ministros + Vice-Presidente da República de uma vez",
    )

    parser.add_argument("--orgao-id", type=int, help="ID do órgão no e-Agendas (ex: 661 para MME)")
    parser.add_argument("--cargo", help="Cargo do servidor (ex: 'MINISTRO DE MINAS E ENERGIA')")
    parser.add_argument(
        "--output-dir",
        default="./dados",
        help="Diretório de saída (padrão: ./dados)",
    )
    parser.add_argument(
        "--formato",
        default="csv,json",
        help="Formatos de saída separados por vírgula: csv, json (padrão: csv,json)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Segundos entre requisições (padrão: {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--sem-viagens",
        action="store_true",
        help="Exclui eventos de Viagem SCDP da saída",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=0,
        help="Limita o número de autoridades processadas (0 = sem limite, útil para testes)",
    )
    parser.add_argument(
        "--amanha",
        action="store_true",
        help="Extrai apenas compromissos do dia seguinte à execução",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="Filtra por uma data específica no formato YYYY-MM-DD (ex: 2026-05-15)",
    )

    args = parser.parse_args()

    # Validações
    if args.servidor_id and (not args.orgao_id or not args.cargo):
        parser.error("--servidor-id requer --orgao-id e --cargo")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    formatos = [f.strip().lower() for f in args.formato.split(",")]

    session = build_session()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_records = []

    # Resolve filtro de data cedo para decidir estratégia de busca
    if args.amanha and args.data:
        parser.error("Use --amanha ou --data, não ambos ao mesmo tempo")

    target_date = None
    if args.amanha:
        target_date = date.today() + timedelta(days=1)
    elif args.data:
        try:
            target_date = date.fromisoformat(args.data)
        except ValueError:
            parser.error(f"Data inválida '{args.data}'. Use YYYY-MM-DD (ex: 2026-05-15)")

    # Modo 1: Gov.br → extrai lista de autoridades
    if args.govbr_url:
        officials = get_officials_from_govbr(session, args.govbr_url, delay=args.delay)
        if not officials:
            print("Nenhuma autoridade encontrada.", file=sys.stderr)
            sys.exit(1)

        if args.limite:
            officials = officials[: args.limite]
            print(f"Limitado a {args.limite} autoridade(s)")

        if target_date:
            # Modo paralelo: busca todos simultaneamente, filtrando por data em cada thread
            print(f"Buscando compromissos de {target_date.strftime('%d/%m/%Y')} em paralelo ({len(officials)} autoridades)...")

            def cli_progress(nome, encontrados, concluidos, total):
                status = f"✓ {encontrados} compromisso(s)" if encontrados else "  sem eventos"
                print(f"  [{concluidos}/{total}] {nome or '—'}: {status}")

            all_records = scrape_officials_parallel(
                officials,
                target_date=target_date,
                max_workers=6,
                delay=0.3,
                progress_cb=cli_progress,
            )
        else:
            # Modo sequencial completo com arquivos individuais por oficial
            for official in officials:
                records = scrape_official(session, official, delay=args.delay)
                if records:
                    all_records.extend(records)
                    safe_name = re.sub(r"[^\w\-]", "_", official.get("nome", official.get("cargo", "oficial")))
                    if "csv" in formatos:
                        save_csv(records, output_dir / f"{safe_name}.csv")
                    if "json" in formatos:
                        save_json(records, output_dir / f"{safe_name}.json")

    # Modo 2: URL direta do eagendas
    elif args.eagendas_url:
        official = {"url": args.eagendas_url, "nome": "", "cargo": ""}
        all_records = scrape_official(session, official, delay=args.delay)

    # Modo 3: IDs diretos
    elif args.servidor_id:
        params = {
            "filtro_orgaos_ativos": "on",
            "filtro_orgao": str(args.orgao_id),
            "filtro_cargos_ativos": "on",
            "filtro_cargo": args.cargo,
            "filtro_apos_ativos": "on",
            "filtro_servidor": str(args.servidor_id),
            "cargo_confianca_id": "",
            "is_cargo_vago": "false",
        }
        url = f"{BASE_URL}/?{urlencode(params)}"
        official = {"url": url, "nome": "", "cargo": args.cargo}
        all_records = scrape_official(session, official, delay=args.delay)

    # Modo 4: Presidente + todos os ministros + Vice-Presidente
    elif args.ministros:
        label = f"de {target_date.strftime('%d/%m/%Y')}" if target_date else "completa"
        print(f"Buscando agenda {label} de Presidente + ministros + Vice-Presidente...")

        def cli_progress(nome, encontrados, concluidos, total):
            icon = "✓" if encontrados else "·"
            suffix = f"{encontrados} compromisso(s)" if encontrados else "sem eventos"
            print(f"  [{concluidos:2d}/{total}] {icon} {nome}: {suffix}")

        all_records = scrape_all_ministers(
            target_date=target_date,
            delay=0.4,
            progress_cb=cli_progress,
        )

    # Filtra viagens se pedido
    if args.sem_viagens:
        before = len(all_records)
        all_records = [r for r in all_records if r.get("tipo") != "Viagem SCDP"]
        print(f"Viagens excluídas: {before - len(all_records)} registros removidos")

    # Para modos 2, 3 (URL direta / ID): aplica filtro de data pós-extração
    # Modos 1 e 4 com target_date já filtram dentro das threads
    uses_parallel_date_filter = (args.govbr_url and target_date) or getattr(args, "ministros", False)
    if target_date and not uses_parallel_date_filter:
        before = len(all_records)
        all_records = filter_by_date(all_records, target_date)
        print(f"Filtrando {target_date.strftime('%d/%m/%Y')}: {len(all_records)} compromisso(s) (de {before} no total)")

    # Salva arquivo consolidado
    if all_records:
        date_suffix = f"_{target_date.isoformat()}" if target_date else ""
        prefix = f"agenda{date_suffix}"
        if "csv" in formatos:
            save_csv(all_records, output_dir / f"{prefix}_{timestamp}.csv")
        if "json" in formatos:
            save_json(all_records, output_dir / f"{prefix}_{timestamp}.json")

        print(f"\nTotal: {len(all_records)} compromisso(s) extraído(s)")
    else:
        msg = f"Nenhum compromisso encontrado para {target_date.strftime('%d/%m/%Y')}." if target_date else "Nenhum evento extraído."
        print(msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
