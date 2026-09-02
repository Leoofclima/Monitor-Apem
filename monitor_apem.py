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

    Importante: a página organiza os navios em VÁRIAS tabelas, uma por
    terminal (VALE, ITAQUI, ALUMAR, etc), então é preciso ler TODAS as
    tabelas da página, não só a primeira.

    Se a página falhar por qualquer motivo, retorna um conjunto vazio em vez
    de quebrar o script inteiro.
    """
    try:
        headers_req = {"User-Agent": "Mozilla/5.0 (compatible; MonitorAPEM/1.0)"}
        resp = requests.get(URL_ATRACADOS, headers=headers_req, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        tabelas_html = [
            t for t in soup.find_all("table")
            if t.find("th", string=lambda s: s and "Nome" in s)
        ]

        nomes = set()

        for tabela_html in tabelas_html:
            colunas = None
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


def linha_para_dados(row):
    """Extrai os campos relevantes de uma linha em um dicionário estruturado."""
    return {
        "nome": str(row.get("Nome", "?")).strip(),
        "data": str(row.get("Data", "?")).strip(),
        "hora": str(row.get("Hora", "?")).strip(),
        "tipo": str(row.get("Tipo", "?")).strip(),
        "de": str(row.get("De", "?")).strip(),
        "berco": str(row.get("Berço", row.get("Berco", "?"))).strip(),
        "agencia": str(row.get("Agência", row.get("Agencia", "?"))).strip(),
    }


def chave_de_dados(dados):
    """Identificador único e estável (inclui data/hora) para cada manobra."""
    return f"{dados['nome']} | {dados['data']} | {dados['hora']} | {dados['tipo']} | {dados['berco']}"


def identidade_de_chave(chave):
    """Identidade do navio ignorando data/hora — usada para detectar reagendamentos.

    Duas manobras com a mesma identidade (nome + tipo + berço) mas com
    data/hora diferentes são tratadas como a MESMA manobra que só mudou de
    horário, em vez de uma "cancelada" + uma "nova".
    """
    partes = chave.split(" | ")
    if len(partes) < 5:
        return chave
    nome, _data, _hora, tipo, berco = partes[:5]
    return f"{nome} | {tipo} | {berco}"


def dados_para_texto(dados):
    tipo_desc = {"DS": "Desatracação", "EA": "Atracação"}.get(dados["tipo"].upper(), dados["tipo"])
    return (
        f"Navio: {dados['nome']}\n"
        f"Manobra: {tipo_desc}\n"
        f"Data/Hora: {dados['data']} {dados['hora']}\n"
        f"De: {dados['de']}\n"
        f"Para/Berço: {dados['berco']}\n"
        f"Agência: {dados['agencia']}"
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

    atual_dados = {}
    for _, row in relevantes.iterrows():
        dados = linha_para_dados(row)
        chave = chave_de_dados(dados)
        if chave.strip(" |"):
            atual_dados[chave] = dados

    atual = {k: dados_para_texto(v) for k, v in atual_dados.items()}

    anterior = carregar_estado_anterior()

    novas_brutas = {k: v for k, v in atual.items() if k not in anterior}
    sumidas_brutas = {k: v for k, v in anterior.items() if k not in atual}

    # Agrupa as "sumidas" por identidade (ignorando data/hora) pra poder
    # casar com uma "nova" que seja, na verdade, a mesma manobra reagendada.
    sumidas_por_id = {}
    for k in sumidas_brutas:
        sumidas_por_id.setdefault(identidade_de_chave(k), []).append(k)

    reagendadas = []  # lista de (chave_antiga, chave_nova)
    novas = {}
    for k, v in novas_brutas.items():
        id_ = identidade_de_chave(k)
        candidatos = sumidas_por_id.get(id_)
        if candidatos:
            chave_antiga = candidatos.pop(0)
            if not candidatos:
                del sumidas_por_id[id_]
            reagendadas.append((chave_antiga, k))
        else:
            novas[k] = v

    # O que sobrou depois de tirar os reagendamentos são cancelamentos/atracações de verdade
    sumidas = {}
    for lista in sumidas_por_id.values():
        for k in lista:
            sumidas[k] = sumidas_brutas[k]

    navios_atracados = buscar_navios_atracados() if sumidas else set()

    for chave, texto in novas.items():
        msg = f"🚢 NOVA MANOBRA AGENDADA (APEM)\n\n{texto}"
        print(msg)
        enviar_whatsapp(msg)
        registrar_log(f"NOVA MANOBRA:\n{texto}")

    for chave_antiga, chave_nova in reagendadas:
        partes_antiga = chave_antiga.split(" | ")
        partes_nova = chave_nova.split(" | ")
        dados_novos = atual_dados[chave_nova]
        tipo_desc = {"DS": "Desatracação", "EA": "Atracação"}.get(dados_novos["tipo"].upper(), dados_novos["tipo"])
        msg = (
            f"⏰ HORÁRIO ALTERADO (APEM)\n\n"
            f"Navio: {dados_novos['nome']}\n"
            f"Manobra: {tipo_desc}\n"
            f"Berço: {dados_novos['berco']}\n"
            f"Antes: {partes_antiga[1]} {partes_antiga[2]}\n"
            f"Agora: {partes_nova[1]} {partes_nova[2]}\n"
            f"Agência: {dados_novos['agencia']}"
        )
        print(msg)
        enviar_whatsapp(msg)
        registrar_log(f"REAGENDAMENTO:\n{msg}")

    for chave, texto in sumidas.items():
        nome_navio = chave.split(" | ")[0].strip().upper()
        if nome_navio in navios_atracados:
            msg = f"✅ Navio {nome_navio} atracou com sucesso!\n\n{texto}"
            registrar_log(f"MANOBRA CONCLUÍDA (navio atracou):\n{texto}")
        else:
            msg = f"⚠️ MANOBRA SAIU DA LISTA (possível cancelamento/desmarcação)\n\n{texto}"
            registrar_log(f"MANOBRA CANCELADA/SUMIU (não encontrado em Navios Atracados):\n{texto}")
        print(msg)
        enviar_whatsapp(msg)

    if not novas and not sumidas and not reagendadas:
        print("Nenhuma mudança detectada.")

    salvar_estado(atual)


if __name__ == "__main__":
    main()
