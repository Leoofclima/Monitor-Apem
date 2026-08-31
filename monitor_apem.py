#!/usr/bin/env python3
"""
Monitor de Manobras - APEM (Praticagem MA)
--------------------------------------------
Monitora a página "Manobras Previstas" do site da APEM
(http://www.apem-ma.com.br/?module=shipmaneuvering) e envia um
aviso no WhatsApp (via Z-API) para uma lista de destinatários quando:

  - Uma manobra NOVA aparece
  - Uma manobra que estava na lista DESAPARECE (atracou ou foi cancelada)

Como usar:
  1. pip install requests beautifulsoup4 lxml pandas
  2. Preencha ZAPI_INSTANCE_ID, ZAPI_TOKEN e DESTINATARIOS abaixo
  3. Rode manualmente para testar:  python monitor_apem.py
  4. Depois de validar, agende para rodar a cada 5-10 min (cron / task scheduler)
"""

import io
import json
import os
import sys
from datetime import datetime

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ======================= CONFIGURAÇÃO =======================

URL = "http://www.apem-ma.com.br/?module=shipmaneuvering"

# Palavras-chave que precisam aparecer no campo "De" ou "Berço"
# para a manobra ser considerada relevante (case-insensitive)
KEYWORDS = ["ALUMAR", "ITAQUI", "VALE"]

# Configuração do Z-API (https://www.z-api.io/)
# ID e Token ficam em: Instâncias > Meu número > Credenciais
ZAPI_INSTANCE_ID = os.environ.get("ZAPI_INSTANCE_ID", "SEU_INSTANCE_ID_AQUI")
ZAPI_TOKEN = os.environ.get("ZAPI_TOKEN", "SEU_TOKEN_AQUI")
# Opcional: alguns planos exigem o "Account Token" (Painel > Segurança)
ZAPI_CLIENT_TOKEN = os.environ.get("ZAPI_CLIENT_TOKEN", "")

# Lista de números que vão receber os avisos (com DDI, sem espaço/traço),
# separados por vírgula. Ex: "5598999999999,5598888888888"
DESTINATARIOS = [
    p.strip() for p in os.environ.get("ZAPI_DESTINATARIOS", "559899657365").split(",")
    if p.strip()
]

# Arquivo onde guardamos o "estado anterior" para comparar
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apem_state.json")

# Arquivo de histórico/log de tudo que já foi notificado
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_apem.txt")

# =============================================================


URL_ATRACADOS = "http://www.apem-ma.com.br/?module=berthedships"


def buscar_navios_atracados():
    """Retorna um conjunto (set) com os nomes dos navios atualmente atracados.

    Usado para diferenciar um cancelamento real de uma manobra que simplesmente
    já aconteceu (o navio atracou e por isso saiu da lista de previstas).
    Se a página falhar por qualquer motivo, retorna um conjunto vazio em vez
    de quebrar o script inteiro.
    """
    try:
        headers_req = {"User-Agent": "Mozilla/5.0 (compatible; MonitorAPEM/1.0)"}
        resp = requests.get(URL_ATRACADOS, headers=headers_req, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        tabela_html = None
        for t in soup.find_all("table"):
            if t.find("th", string=lambda s: s and "Nome" in s):
                tabela_html = t
                break

        if tabela_html is None:
            return set()

        colunas = None
        nomes = set()
        for tr in tabela_html.find_all("tr"):
            ths = tr.find_all("th")
            tds = tr.find_all("td")

            if ths:
                textos = [th.get_text(strip=True) for th in ths]
                if colunas is None and "Nome" in textos:
                    colunas = textos
                continue

            if tds and colunas:
                valores = [td.get_text(strip=True) for td in tds]
                if len(valores) == len(colunas) and any(valores):
                    linha = dict(zip(colunas, valores))
                    nome = linha.get("Nome", "").strip().upper()
                    if nome:
                        nomes.add(nome)

        return nomes
    except Exception as e:
        print(f"[AVISO] Não consegui checar Navios Atracados: {e}")
        return set()


def buscar_tabela():
    """Baixa a página e retorna a tabela de manobras como DataFrame.

    Faz o parsing manualmente com BeautifulSoup porque a tabela do site tem
    uma linha de cabeçalho "agrupador" (Navio / Manobra) antes da linha real
    de nomes de coluna, o que confunde leitores automáticos como pd.read_html.
    """
    headers_req = {"User-Agent": "Mozilla/5.0 (compatible; MonitorAPEM/1.0)"}
    resp = requests.get(URL, headers=headers_req, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    tabela_html = None
    for t in soup.find_all("table"):
        if t.find("th", string=lambda s: s and "Nome" in s):
            tabela_html = t
            break

    if tabela_html is None:
        raise RuntimeError("Não encontrei a tabela de manobras na página. O site pode ter mudado o layout.")

    colunas = None
    linhas = []

    for tr in tabela_html.find_all("tr"):
        ths = tr.find_all("th")
        tds = tr.find_all("td")

        if ths:
            textos = [th.get_text(strip=True) for th in ths]
            if colunas is None and "Nome" in textos and any("Berç" in c for c in textos):
                colunas = textos
            continue  # linhas de cabeçalho nunca são dados

        if tds and colunas:
            valores = [td.get_text(strip=True) for td in tds]
            if len(valores) == len(colunas) and any(valores):
                linhas.append(dict(zip(colunas, valores)))

    if colunas is None:
        raise RuntimeError("Não consegui identificar as colunas da tabela. O site pode ter mudado o layout.")

    return pd.DataFrame(linhas)


def filtrar_relevantes(df):
    """Retorna todas as manobras da lista, sem filtro de terminal/berço."""
    df = df.dropna(how="all")
    return df


def linha_para_chave(row):
    """Cria um identificador único e estável para cada manobra."""
    campos = ["Nome", "Data", "Hora", "Tipo", "Berço"]
    valores = [str(row.get(c, "")).strip() for c in campos]
    return " | ".join(valores)


def linha_para_texto(row):
    nome = row.get("Nome", "?")
    data = row.get("Data", "?")
    hora = row.get("Hora", "?")
    tipo = row.get("Tipo", "?")
    de = row.get("De", "?")
    berco = row.get("Berço", row.get("Berco", "?"))
    agencia = row.get("Agência", row.get("Agencia", "?"))
    tipo_desc = {"DS": "Desatracação", "EA": "Atracação"}.get(str(tipo).strip().upper(), tipo)
    return (
        f"Navio: {nome}\n"
        f"Manobra: {tipo_desc}\n"
        f"Data/Hora: {data} {hora}\n"
        f"De: {de}\n"
        f"Para/Berço: {berco}\n"
        f"Agência: {agencia}"
    )


def carregar_estado_anterior():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def registrar_log(mensagem):
    """Adiciona uma entrada com data/hora no arquivo de histórico."""
    carimbo = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{carimbo}] {mensagem}\n")
        f.write("-" * 50 + "\n")


def enviar_whatsapp(mensagem):
    if ZAPI_INSTANCE_ID == "SEU_INSTANCE_ID_AQUI" or ZAPI_TOKEN == "SEU_TOKEN_AQUI":
        print("[AVISO] Z-API não configurado ainda. Mensagem que seria enviada:\n")
        print(mensagem)
        print("-" * 40)
        return

    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json"}
    if ZAPI_CLIENT_TOKEN:
        headers["Client-Token"] = ZAPI_CLIENT_TOKEN

    for phone in DESTINATARIOS:
        try:
            r = requests.post(url, json={"phone": phone, "message": mensagem}, headers=headers, timeout=20)
            print(f"[WhatsApp Z-API -> {phone}] status {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[ERRO] Falha ao enviar WhatsApp para {phone}: {e}")


def main():
    print(f"[{datetime.now()}] Verificando manobras...")

    try:
        df = buscar_tabela()
    except Exception as e:
        print(f"[ERRO] Não consegui ler a página: {e}")
        sys.exit(1)

    relevantes = filtrar_relevantes(df)

    atual = {}
    for _, row in relevantes.iterrows():
        chave = linha_para_chave(row)
        if chave.strip(" |"):
            atual[chave] = linha_para_texto(row)

    anterior = carregar_estado_anterior()

    novas = {k: v for k, v in atual.items() if k not in anterior}
    sumidas = {k: v for k, v in anterior.items() if k not in atual}

    navios_atracados = buscar_navios_atracados() if sumidas else set()

    for chave, texto in novas.items():
        msg = f"🚢 NOVA MANOBRA AGENDADA (APEM)\n\n{texto}"
        print(msg)
        enviar_whatsapp(msg)
        registrar_log(f"NOVA MANOBRA:\n{texto}")

    for chave, texto in sumidas.items():
        nome_navio = chave.split(" | ")[0].strip().upper()
        if nome_navio in navios_atracados:
            msg = f"✅ NAVIO ATRACOU (manobra concluída)\n\n{texto}"
            registrar_log(f"MANOBRA CONCLUÍDA (navio atracou):\n{texto}")
        else:
            msg = f"⚠️ MANOBRA SAIU DA LISTA (possível cancelamento/desmarcação)\n\n{texto}"
            registrar_log(f"MANOBRA CANCELADA/SUMIU (não encontrado em Navios Atracados):\n{texto}")
        print(msg)
        enviar_whatsapp(msg)

    if not novas and not sumidas:
        print("Nenhuma mudança detectada.")

    salvar_estado(atual)


if __name__ == "__main__":
    main()
