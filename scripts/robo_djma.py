"""
robo_djma.py — Robô de leitura automática do DJMA
PGM Buriticupu — Distribuidor de Prazos

Executa todo dia útil às 8h via GitHub Actions.
Baixa o PDF do Diário de Justiça do Maranhão, extrai intimações
onde a PGM de Buriticupu é parte, calcula vencimentos e salva no Supabase.
"""

import os
import re
import json
import time
import tempfile
import datetime
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# ── Configuração ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SUPABASE_URL = "https://khhejpxiyzbyrctaxdhm.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtoaGVqcHhpeXpieXJjdGF4ZGhtIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NTY4NzMyNCwiZXhwIjoyMTAxMjYzMzI0fQ.kCRUfxAVj0jo7sYTBu0u0Axy4QInNc8odr-1-Kkx_ik"

# Termos que identificam a PGM de Buriticupu no diário
TERMOS_PGM = [
    "PGM BURITICUPU",
    "PGM DE BURITICUPU",
    "PROCURADORIA.*BURITICUPU",
    "MUNICÍPIO DE BURITICUPU",
    "MUNICIPIO DE BURITICUPU",
    "PREFEITURA.*BURITICUPU",
    "BURITICUPU.*PROCURADORIA",
]

# Mapeamento: tipo de intimação → área da PGM
AREA_MAP = {
    "TRABALHO":       ["trabalhista", "TRT", "CLT", "RECLAMAÇÃO TRABALHISTA", "DISSÍDIO"],
    "SAÚDE":          ["saúde", "SUS", "medicamento", "internação", "tratamento"],
    "TRIBUTÁRIO":     ["tributário", "imposto", "ISS", "IPTU", "execução fiscal", "CDA"],
    "ADMINISTRATIVO": ["administrativo", "licitação", "contrato", "servidor", "cargo"],
    "JUDICIAL":       [],  # fallback padrão
}

# Prazos legais por tipo de ato (em dias corridos)
PRAZOS_LEGAIS = {
    "contestação":         15,
    "contrarrazões":       15,
    "recurso ordinário":   15,
    "apelação":            15,
    "agravo":              15,
    "embargos":            15,
    "impugnação":          15,
    "memoriais":           10,
    "manifestação":        15,
    "informações":         30,
    "resposta":            15,
    "defesa":              15,
    "recurso":             15,
    "default":             15,
}

# Feriados nacionais 2026 (adicione feriados estaduais/municipais conforme necessário)
FERIADOS_2026 = {
    datetime.date(2026, 1, 1),   # Ano Novo
    datetime.date(2026, 4, 3),   # Sexta-feira Santa
    datetime.date(2026, 4, 21),  # Tiradentes
    datetime.date(2026, 5, 1),   # Dia do Trabalho
    datetime.date(2026, 6, 4),   # Corpus Christi
    datetime.date(2026, 9, 7),   # Independência
    datetime.date(2026, 10, 12), # N. Sra. Aparecida
    datetime.date(2026, 11, 2),  # Finados
    datetime.date(2026, 11, 15), # Proclamação da República
    datetime.date(2026, 11, 20), # Consciência Negra
    datetime.date(2026, 12, 25), # Natal
    # Feriados do Maranhão
    datetime.date(2026, 7, 28),  # Adesão do MA à Independência
}


# ── Funções utilitárias ───────────────────────────────────────────────────────

def dia_util(data: datetime.date) -> bool:
    """Retorna True se a data é dia útil (não é FDS nem feriado)."""
    return data.weekday() < 5 and data not in FERIADOS_2026


def calcular_vencimento(data_publicacao: datetime.date, tipo_prazo: str) -> datetime.date:
    """
    Calcula a data de vencimento considerando:
    - O prazo começa a contar no primeiro dia útil após a publicação
    - Conta apenas dias úteis
    - Retorna o último dia útil do prazo
    """
    dias = PRAZOS_LEGAIS.get(tipo_prazo.lower(), PRAZOS_LEGAIS["default"])

    # Início da contagem: primeiro dia útil após a publicação
    inicio = data_publicacao + datetime.timedelta(days=1)
    while not dia_util(inicio):
        inicio += datetime.timedelta(days=1)

    # Contar dias úteis
    dias_contados = 0
    data_atual = inicio
    while dias_contados < dias:
        if dia_util(data_atual):
            dias_contados += 1
        if dias_contados < dias:
            data_atual += datetime.timedelta(days=1)

    # Garantir que o vencimento caia em dia útil
    while not dia_util(data_atual):
        data_atual += datetime.timedelta(days=1)

    return data_atual


def detectar_area(texto: str) -> str:
    """Detecta a área da PGM baseado nas palavras-chave do texto da intimação."""
    texto_upper = texto.upper()
    for area, keywords in AREA_MAP.items():
        if area == "JUDICIAL":
            continue
        for kw in keywords:
            if kw.upper() in texto_upper:
                return area
    return "JUDICIAL"


def detectar_tipo_prazo(texto: str) -> str:
    """Detecta o tipo de prazo baseado no texto da intimação."""
    texto_lower = texto.lower()
    for tipo in PRAZOS_LEGAIS:
        if tipo in texto_lower:
            return tipo.capitalize()
    return "Manifestação"


def extrair_numero_processo(texto: str) -> str | None:
    """Extrai o número CNJ do processo (formato: NNNNNNN-DD.AAAA.J.TT.OOOO)."""
    padrao = r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}'
    match = re.search(padrao, texto)
    return match.group(0) if match else None


# ── Download do texto via Jusbrasil (sem login, sem PDF) ─────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

def obter_links_intimacoes_jusbrasil(data: datetime.date) -> list[str]:
    """
    Acessa a página do DJMA no Jusbrasil para a data dada e
    coleta os links de todas as intimações publicadas.
    URL padrão: https://www.jusbrasil.com.br/diarios/DJMA/AAAA/MM/DD
    """
    url = f"https://www.jusbrasil.com.br/diarios/DJMA/{data.year}/{data.month:02d}/{data.day:02d}"
    log.info(f"Acessando Jusbrasil: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers=HEADERS)
        try:
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            html = page.content()
            browser.close()
        except Exception as e:
            log.error(f"Erro ao acessar Jusbrasil: {e}")
            browser.close()
            return []

    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/diarios/documentos/" in href and "DJMA" in href.upper():
            full = href if href.startswith("http") else "https://www.jusbrasil.com.br" + href
            if full not in links:
                links.append(full)

    log.info(f"Links de intimações encontrados: {len(links)}")
    return links


def buscar_intimacoes_pgm(data: datetime.date) -> list[str]:
    """
    Estratégia alternativa: busca no Jusbrasil diretamente por
    'Buriticupu' no DJMA da data — retorna os blocos de texto encontrados.
    """
    # Tenta a busca textual do Jusbrasil pelo município
    url_busca = (
        "https://www.jusbrasil.com.br/diarios/busca/"
        f"?q=Buriticupu&diario=DJMA"
        f"&startDate={data.isoformat()}&endDate={data.isoformat()}"
    )
    log.info(f"Buscando no Jusbrasil: {url_busca}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers=HEADERS)
        try:
            page.goto(url_busca, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            html = page.content()
            browser.close()
        except Exception as e:
            log.error(f"Erro na busca Jusbrasil: {e}")
            browser.close()
            return []

    soup = BeautifulSoup(html, "html.parser")

    blocos = []
    # Coleta os trechos exibidos nos resultados
    for item in soup.select(".search-result, .result-item, article, .content"):
        texto = item.get_text(separator=" ", strip=True)
        if any(re.search(t, texto, re.IGNORECASE) for t in TERMOS_PGM):
            blocos.append(texto)

    log.info(f"Blocos com menção à PGM: {len(blocos)}")
    return blocos


# ── Extração de intimações ────────────────────────────────────────────────────

def extrair_intimacoes(blocos_texto: list[str], data_publicacao: datetime.date) -> list[dict]:
    """
    Recebe lista de blocos de texto (vindos do Jusbrasil) e extrai
    as intimações onde a PGM de Buriticupu é parte.
    """
    padrao_pgm = re.compile("|".join(TERMOS_PGM), re.IGNORECASE)
    intimacoes = []
    processos_vistos = set()

    for bloco in blocos_texto:
        if not padrao_pgm.search(bloco):
            continue

        numero = extrair_numero_processo(bloco)
        if not numero or numero in processos_vistos:
            continue
        processos_vistos.add(numero)

        tipo = detectar_tipo_prazo(bloco)
        area = detectar_area(bloco)
        vencimento = calcular_vencimento(data_publicacao, tipo)

        intimacao = {
            "numero_processo": numero,
            "area":            area,
            "tipo_prazo":      tipo,
            "data_publicacao": data_publicacao.isoformat(),
            "data_vencimento": vencimento.isoformat(),
            "dias_restantes":  (vencimento - datetime.date.today()).days,
            "status":          "pendente",
            "responsavel":     None,
            "origem":          "DJMA",
            "texto_intimacao": bloco[:600].strip(),
        }

        log.info(f"  ✓ {numero} | {area} | {tipo} | vence {vencimento}")
        intimacoes.append(intimacao)

    log.info(f"Total: {len(intimacoes)} intimação(ões) da PGM encontrada(s)")
    return intimacoes


# ── Supabase ──────────────────────────────────────────────────────────────────

def salvar_no_supabase(intimacoes: list[dict]) -> dict:
    """
    Salva as intimações no Supabase.
    Usa upsert para não duplicar caso o robô rode mais de uma vez no dia.
    Retorna estatísticas: novos, atualizados, ignorados.
    """
    if not intimacoes:
        return {"novos": 0, "atualizados": 0}

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    novos = 0
    atualizados = 0

    for intimacao in intimacoes:
        # Verifica se o processo já existe com prazo de hoje
        existente = (
            supabase.table("prazos_judiciais")
            .select("id, status")
            .eq("numero_processo", intimacao["numero_processo"])
            .eq("data_publicacao", intimacao["data_publicacao"])
            .execute()
        )

        if existente.data:
            # Já existe — atualiza apenas se ainda estiver pendente
            if existente.data[0]["status"] == "pendente":
                supabase.table("prazos_judiciais") \
                    .update(intimacao) \
                    .eq("id", existente.data[0]["id"]) \
                    .execute()
                atualizados += 1
        else:
            # Novo prazo
            supabase.table("prazos_judiciais").insert(intimacao).execute()
            novos += 1

    log.info(f"Supabase: {novos} novo(s), {atualizados} atualizado(s)")
    return {"novos": novos, "atualizados": atualizados}


# ── Ponto de entrada ──────────────────────────────────────────────────────────

def main():
    hoje = datetime.date.today()

    if not dia_util(hoje):
        log.info(f"{hoje} não é dia útil. Robô encerrado.")
        return

    log.info(f"=== Robô DJMA — {hoje.strftime('%d/%m/%Y')} ===")

    # 1. Buscar intimações com "Buriticupu" diretamente no Jusbrasil
    blocos = buscar_intimacoes_pgm(hoje)

    if not blocos:
        log.warning("Nenhuma publicação encontrada para hoje. "
                    "O diário pode não ter sido publicado ainda, ou não há intimações da PGM.")

    # 2. Extrair e estruturar as intimações
    intimacoes = extrair_intimacoes(blocos, hoje)

    # 3. Salvar no Supabase
    stats = salvar_no_supabase(intimacoes)

    log.info(f"=== Concluído: {stats['novos']} novo(s), {stats['atualizados']} atualizado(s) ===")

    # Saída para o GitHub Actions summary
    print(json.dumps({
        "data": hoje.isoformat(),
        "intimacoes_encontradas": len(intimacoes),
        **stats
    }))


if __name__ == "__main__":
    main()
