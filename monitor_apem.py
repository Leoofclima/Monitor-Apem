#!/usr/bin/env python3
"""
Monitor de Manobras - APEM (Praticagem MA)
--------------------------------------------
Monitora a página "Manobras Previstas" do site da APEM
(http://www.apem-ma.com.br/?module=shipmaneuvering) e envia um
aviso no WhatsApp (via CallMeBot) quando:

  - Uma manobra NOVA aparece envolvendo Alumar, Itaqui ou Vale
  - Uma manobra que estava na lista DESAPARECE (provável cancelamento)

Como usar:
  1. pip install requests beautifulsoup4 lxml pandas
  2. Preencha CALLMEBOT_PHONE e CALLMEBOT_APIKEY abaixo
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

# Preencha depois de ativar o CallMeBot no seu WhatsApp
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE", "559899657365")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "3871953")

# Arquivo onde guardamos o "estado anterior" para comparar
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apem_state.json")

# =============================================================


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
    tipo_desc = {"DS": "Desatracação", "EA": "Atracação"}.get(str(tipo).strip().upper(), tipo)
    return f"Navio: {nome}\nManobra: {tipo_desc}\nData/Hora: {data} {hora}\nDe: {de}\nPara/Berço: {berco}"


def carregar_estado_anterior():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def enviar_whatsapp(mensagem):
    if CALLMEBOT_PHONE == "SEU_NUMERO_COM_DDI" or CALLMEBOT_APIKEY == "SUA_APIKEY_AQUI":
        print("[AVISO] CallMeBot não configurado ainda. Mensagem que seria enviada:\n")
        print(mensagem)
        print("-" * 40)
        return

    api_url = "https://api.callmebot.com/whatsapp.php"
    params = {
        "phone": CALLMEBOT_PHONE,
        "text": mensagem,
        "apikey": CALLMEBOT_APIKEY,
    }
    try:
        r = requests.get(api_url, params=params, timeout=20)
        print(f"[WhatsApp] status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[ERRO] Falha ao enviar WhatsApp: {e}")


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

    for chave, texto in novas.items():
        msg = f"🚢 NOVA MANOBRA AGENDADA (APEM)\n\n{texto}"
        print(msg)
        enviar_whatsapp(msg)

    for chave, texto in sumidas.items():
        msg = f"⚠️ MANOBRA SAIU DA LISTA (possível cancelamento/desmarcação)\n\n{texto}"
        print(msg)
        enviar_whatsapp(msg)

    if not novas and not sumidas:
        print("Nenhuma mudança detectada.")

    salvar_estado(atual)


if __name__ == "__main__":
    main()
