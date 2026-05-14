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
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs, unquote

try:
    import requests
except ImportError:
    print("Instale as dependências: pip install requests", file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://eagendas.cgu.gov.br"
DEFAULT_DELAY = 1.5  # segundos entre requisições


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


def fetch_page(session, url, delay=0):
    """Faz GET com retry simples e delay opcional."""
    if delay:
        time.sleep(delay)
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  [ERRO] {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
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

    # Modo 1: Gov.br → extrai lista de autoridades
    if args.govbr_url:
        officials = get_officials_from_govbr(session, args.govbr_url, delay=args.delay)
        if not officials:
            print("Nenhuma autoridade encontrada.", file=sys.stderr)
            sys.exit(1)

        if args.limite:
            officials = officials[: args.limite]
            print(f"Limitado a {args.limite} autoridade(s)")

        for official in officials:
            records = scrape_official(session, official, delay=args.delay)
            if records:
                all_records.extend(records)
                # Salva arquivo individual por oficial
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

    # Filtra viagens se pedido
    if args.sem_viagens:
        before = len(all_records)
        all_records = [r for r in all_records if r.get("tipo") != "Viagem SCDP"]
        print(f"Viagens excluídas: {before - len(all_records)} registros removidos")

    # Salva arquivo consolidado
    if all_records:
        prefix = "agenda_consolidada"
        if "csv" in formatos:
            save_csv(all_records, output_dir / f"{prefix}_{timestamp}.csv")
        if "json" in formatos:
            save_json(all_records, output_dir / f"{prefix}_{timestamp}.json")

        print(f"\nTotal: {len(all_records)} eventos extraídos")
    else:
        print("Nenhum evento extraído.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
