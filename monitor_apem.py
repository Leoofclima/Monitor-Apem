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

import csv
import io
import json
import math
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from bs4 import BeautifulSoup


def agora_br():
    """Retorna o horário atual de São Luís/MA (UTC-3), sem fuso anexado.

    Necessário porque o GitHub Actions roda os scripts em UTC por padrão —
    sem isso, todos os horários salvos apareceriam 3h adiantados.
    """
    return datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)

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

# Planilha (CSV) com o histórico de navios que realmente atracaram
# (não é enviada pro WhatsApp, é só um "banco de dados" pra consulta futura)
# Arquivo JSON com o snapshot dos Navios Fundeados + rota sugerida (index.html lê esse arquivo)
PAINEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "painel_dados.json")

HISTORICO_ATRACACOES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "historico_atracacoes.csv"
)

# Ponto de partida da embarcação (base/marina) — usado para calcular a rota sugerida
BASE_LAT = float(os.environ.get("BASE_LAT", "-2.66098123540369"))
BASE_LON = float(os.environ.get("BASE_LON", "-44.356990008636686"))
BASE_NOME = os.environ.get("BASE_NOME", "Porto Grande - São Luís")

# Tempo mínimo (em horas) que um navio precisa ainda ter fundeado pra valer a
# pena incluir ele na rota — o processo de visita (deslocamento + operação)
# leva algumas horas, então um navio que já vai atracar em breve não deve
# entrar na rota, mesmo que esteja pertinho.
TEMPO_MINIMO_VISITA_HORAS = float(os.environ.get("TEMPO_MINIMO_VISITA_HORAS", "3"))

# =============================================================


URL_ATRACADOS = "http://www.apem-ma.com.br/?module=berthedships"
URL_FUNDEADOS = "http://www.apem-ma.com.br/?module=berthageships"


def buscar_navios_atracados_detalhado():
    """Busca a página de Navios Atracados e retorna a lista completa de navios,
    com todos os campos (terminal, berço, nome, bandeira, agência, data/hora
    de atracação), organizados por terminal.

    Importante: a página organiza os navios em VÁRIAS tabelas, uma por
    terminal (VALE, ITAQUI, ALUMAR, etc). Além disso, os cabeçalhos "Berço" e
    "Agência" usam rowspan (ocupam 2 linhas), o que torna a contagem de
    colunas do cabeçalho pouco confiável para casar com os dados. Por isso,
    em vez de tentar casar coluna por coluna, usamos a posição fixa conhecida
    das colunas nas linhas de dados: [Berço, Status, Nome, Bandeira, Calado,
    DWT, Imo, Loa, Boca, Agência, Data, Hora].

    Se a página falhar por qualquer motivo, retorna uma lista vazia em vez
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

        navios = []
        terminal_atual = "?"

        for tabela_html in tabelas_html:
            for tr in tabela_html.find_all("tr"):
                ths = tr.find_all("th")
                tds = tr.find_all("td")

                # Linha só com 1 cabeçalho = nome do terminal (ex: "ALUMAR")
                if ths and len(ths) == 1:
                    texto_th = ths[0].get_text(strip=True)
                    if texto_th and "Nome" not in texto_th and "Berço" not in texto_th:
                        terminal_atual = texto_th
                    continue

                if not tds or len(tds) < 10:
                    continue  # linha de cabeçalho ou divisor, não é dado de navio

                valores = [td.get_text(strip=True) for td in tds]
                nome_candidato = valores[2].strip().upper() if len(valores) > 2 else ""
                eh_numero = nome_candidato.replace(",", "").replace(".", "").isdigit()
                if not nome_candidato or eh_numero or len(nome_candidato) <= 2:
                    continue  # não parece um nome de navio válido

                navios.append({
                    "terminal": terminal_atual,
                    "berco": valores[0] if len(valores) > 0 else "?",
                    "nome": nome_candidato,
                    "bandeira": valores[3] if len(valores) > 3 else "?",
                    "agencia": valores[9] if len(valores) > 9 else "?",
                    "data_atracacao": valores[10] if len(valores) > 10 else "?",
                    "hora_atracacao": valores[11] if len(valores) > 11 else "?",
                })

        return navios
    except Exception as e:
        print(f"[AVISO] Não consegui checar Navios Atracados: {e}")
        return []


def buscar_navios_atracados():
    """Retorna um conjunto (set) só com os nomes dos navios atracados —
    usado para diferenciar um cancelamento real de uma manobra que já
    aconteceu de verdade (o navio atracou e por isso saiu da lista de
    previstas). Reaproveita buscar_navios_atracados_detalhado().
    """
    return {navio["nome"] for navio in buscar_navios_atracados_detalhado()}


def parse_coordenada(texto):
    """Converte uma coordenada no formato do site ('02 05,52 S' ou '044 05,09 W')
    para graus decimais (float). Retorna None se não conseguir interpretar.
    """
    if not texto:
        return None
    texto = texto.strip()
    if not texto:
        return None
    partes = texto.split()
    if len(partes) < 3:
        return None
    try:
        graus = float(partes[0])
        minutos = float(partes[1].replace(",", "."))
        hemisferio = partes[2].strip().upper()
    except (ValueError, IndexError):
        return None
    decimal = graus + minutos / 60
    if hemisferio in ("S", "W", "O"):
        decimal = -decimal
    return round(decimal, 6)


def parse_data_hora_apem(data_str, hora_str):
    """Converte 'DD/MM/AA' + 'HH:MM' do site num datetime. Retorna None se falhar."""
    try:
        return datetime.strptime(f"{data_str.strip()} {hora_str.strip()}", "%d/%m/%y %H:%M")
    except Exception:
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    """Distância em linha reta (km) entre duas coordenadas geográficas."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def buscar_navios_fundeados():
    """Busca a página de Navios Fundeados e retorna uma lista de dicts com
    nome, bandeira, agência, área de fundeio, posição (lat/lon em graus
    decimais) e data/hora em que fundearam.

    Assim como as outras páginas do site, os navios são organizados em
    várias tabelas (uma por área: AREA 2, AREA 3, etc), e os cabeçalhos
    "Navio", "Agência" e "Fundeio" usam rowspan/colspan — por isso a
    extração usa a posição fixa das colunas nas linhas de dados:
    [Nome, Bandeira, Indicativo, Calado, DWT, Imo, Loa, Boca, Agência,
    Lat, Long, Data, Hora].

    Se a página falhar por qualquer motivo, retorna lista vazia em vez de
    quebrar o script inteiro.
    """
    try:
        headers_req = {"User-Agent": "Mozilla/5.0 (compatible; MonitorAPEM/1.0)"}
        resp = requests.get(URL_FUNDEADOS, headers=headers_req, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        tabelas_html = [
            t for t in soup.find_all("table")
            if t.find("th", string=lambda s: s and "Nome" in s)
        ]

        navios = []
        area_atual = "?"

        for tabela_html in tabelas_html:
            for tr in tabela_html.find_all("tr"):
                ths = tr.find_all("th")
                tds = tr.find_all("td")

                # Linha só com 1 cabeçalho = nome da área (ex: "AREA 2")
                if ths and len(ths) == 1:
                    texto_th = ths[0].get_text(strip=True)
                    if texto_th and "Nome" not in texto_th:
                        area_atual = texto_th
                    continue

                if not tds or len(tds) < 12:
                    continue  # linha de cabeçalho ou divisor, não é dado de navio

                valores = [td.get_text(strip=True) for td in tds]
                nome = valores[0].strip().upper() if len(valores) > 0 else ""
                if not nome:
                    continue

                lat = parse_coordenada(valores[9] if len(valores) > 9 else "")
                lon = parse_coordenada(valores[10] if len(valores) > 10 else "")

                navios.append({
                    "area": area_atual,
                    "nome": nome,
                    "bandeira": valores[1] if len(valores) > 1 else "?",
                    "imo": valores[5] if len(valores) > 5 else "?",
                    "agencia": valores[8] if len(valores) > 8 else "?",
                    "lat": lat,
                    "lon": lon,
                    "data_fundeio": valores[11] if len(valores) > 11 else "?",
                    "hora_fundeio": valores[12] if len(valores) > 12 else "?",
                })

        return navios
    except Exception as e:
        print(f"[AVISO] Não consegui checar Navios Fundeados: {e}")
        return []


def enriquecer_fundeados_com_previsao(navios_fundeados, atual_dados):
    """Cruza a lista de fundeados com as Manobras Previstas pra descobrir,
    quando possível, quando cada navio tem atracação agendada — e assim
    estimar quanto tempo ele ainda vai ficar fundeado.
    """
    agora = agora_br()

    previsoes_por_navio = {}
    for dados in atual_dados.values():
        if dados["tipo"].strip().upper() != "EA":
            continue  # só nos interessa previsão de ATRACAÇÃO
        dt_prevista = parse_data_hora_apem(dados["data"], dados["hora"])
        if dt_prevista is None:
            continue
        nome = dados["nome"].strip().upper()
        anterior_prevista = previsoes_por_navio.get(nome)
        if anterior_prevista is None or dt_prevista < anterior_prevista:
            previsoes_por_navio[nome] = dt_prevista

    for navio in navios_fundeados:
        dt_fundeio = parse_data_hora_apem(navio["data_fundeio"], navio["hora_fundeio"])
        navio["tempo_fundeado_horas"] = (
            round((agora - dt_fundeio).total_seconds() / 3600, 1) if dt_fundeio else None
        )

        dt_prevista = previsoes_por_navio.get(navio["nome"])
        if dt_prevista:
            navio["previsao_atracacao"] = dt_prevista.strftime("%d/%m/%Y %H:%M")
            navio["tempo_restante_horas"] = round((dt_prevista - agora).total_seconds() / 3600, 1)
        else:
            navio["previsao_atracacao"] = None
            navio["tempo_restante_horas"] = None

    return navios_fundeados


# Pontos do canal navegável de saída da base até o mar aberto (fornecidos
# pelo usuário, que conhece a região). Usado só na PRIMEIRA perna da rota
# (saindo da base), pra não desenhar/calcular uma linha reta que atravesse
# terra — depois do mar aberto, assume-se água livre entre os fundeios.
CANAL_SAIDA = [
    (-2.656008651124698, -44.359276648104576),
    (-2.6483581103731697, -44.35935696764403),
    (-2.641797442629266, -44.3593589382668),
    (-2.630807895299515, -44.36608242069828),
    (-2.62770110303325, -44.37521720983472),
]


def distancia_via_canal_km(base_lat, base_lon, destino_lat, destino_lon):
    """Distância da base até um ponto, passando pelos pontos do canal de
    saída em vez de linha reta (que cortaria terra)."""
    pontos = [(base_lat, base_lon)] + CANAL_SAIDA + [(destino_lat, destino_lon)]
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(pontos, pontos[1:]):
        total += haversine_km(lat1, lon1, lat2, lon2)
    return total


def montar_rota_otimizada(base_lat, base_lon, navios_fundeados):
    """Monta uma ordem sugerida de visita aos navios fundeados, equilibrando
    distância (visitar quem está mais perto) e urgência (visitar antes quem
    tem menos tempo restante até a atracação prevista).

    É uma heurística gulosa simples (escolhe sempre o próximo "melhor"
    ponto), pensada como apoio de planejamento — não é navegação certificada.
    """
    candidatos = [
        n for n in navios_fundeados
        if n.get("lat") is not None and n.get("lon") is not None
        and (n.get("tempo_restante_horas") is None or n["tempo_restante_horas"] >= TEMPO_MINIMO_VISITA_HORAS)
    ]
    restantes = candidatos.copy()
    rota = []
    pos_lat, pos_lon = base_lat, base_lon
    ordem = 1

    while restantes:
        eh_primeira_perna = (pos_lat == base_lat and pos_lon == base_lon)
        if eh_primeira_perna:
            distancias = [distancia_via_canal_km(pos_lat, pos_lon, n["lat"], n["lon"]) for n in restantes]
        else:
            distancias = [haversine_km(pos_lat, pos_lon, n["lat"], n["lon"]) for n in restantes]
        urgencias = [
            n["tempo_restante_horas"] if n.get("tempo_restante_horas") is not None else 999
            for n in restantes
        ]

        d_min, d_max = min(distancias), max(distancias)
        u_min, u_max = min(urgencias), max(urgencias)

        def normalizar(v, vmin, vmax):
            return 0.0 if vmax == vmin else (v - vmin) / (vmax - vmin)

        scores = [
            0.5 * normalizar(distancias[i], d_min, d_max) + 0.5 * normalizar(urgencias[i], u_min, u_max)
            for i in range(len(restantes))
        ]

        idx_escolhido = scores.index(min(scores))
        navio_escolhido = restantes.pop(idx_escolhido)
        distancia_trecho = distancias[idx_escolhido]

        rota.append({**navio_escolhido, "ordem": ordem, "distancia_trecho_km": round(distancia_trecho, 1)})
        pos_lat, pos_lon = navio_escolhido["lat"], navio_escolhido["lon"]
        ordem += 1

    return rota


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
    carimbo = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{carimbo}] {mensagem}\n")
        f.write("-" * 50 + "\n")


def registrar_atracacao_historico(dados):
    """Adiciona uma linha no CSV de histórico de navios que atracaram de verdade.

    Cria o arquivo com cabeçalho na primeira vez que for chamado.
    """
    arquivo_novo = not os.path.exists(HISTORICO_ATRACACOES_FILE)
    with open(HISTORICO_ATRACACOES_FILE, "a", newline="", encoding="utf-8") as f:
        campos = ["Data", "Hora", "Navio", "De", "Berco", "Agencia", "DetectadoEm"]
        writer = csv.DictWriter(f, fieldnames=campos)
        if arquivo_novo:
            writer.writeheader()
        writer.writerow({
            "Data": dados.get("data", "?"),
            "Hora": dados.get("hora", "?"),
            "Navio": dados.get("nome", "?"),
            "De": dados.get("de", "?"),
            "Berco": dados.get("berco", "?"),
            "Agencia": dados.get("agencia", "?"),
            "DetectadoEm": agora_br().strftime("%d/%m/%Y %H:%M:%S"),
        })


def enviar_whatsapp(mensagem):
    """Envia a mensagem para todos os destinatários. Retorna True somente se
    TODOS os envios tiverem sucesso (status 200-299). Se qualquer um falhar,
    retorna False — o chamador usa isso pra decidir se pode marcar aquela
    mudança como "já notificada" ou se precisa tentar de novo depois.
    """
    if ZAPI_INSTANCE_ID == "SEU_INSTANCE_ID_AQUI" or ZAPI_TOKEN == "SEU_TOKEN_AQUI":
        print("[AVISO] Z-API não configurado ainda. Mensagem que seria enviada:\n")
        print(mensagem)
        print("-" * 40)
        return True  # não é falha de envio, é só ainda não configurado

    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json"}
    if ZAPI_CLIENT_TOKEN:
        headers["Client-Token"] = ZAPI_CLIENT_TOKEN

    sucesso_total = True
    for phone in DESTINATARIOS:
        try:
            r = requests.post(url, json={"phone": phone, "message": mensagem}, headers=headers, timeout=20)
            print(f"[WhatsApp Z-API -> {phone}] status {r.status_code}: {r.text[:200]}")
            if not (200 <= r.status_code < 300):
                sucesso_total = False
        except Exception as e:
            print(f"[ERRO] Falha ao enviar WhatsApp para {phone}: {e}")
            sucesso_total = False

    return sucesso_total


def main():
    print(f"[{agora_br()}] Verificando manobras...")

    try:
        df = buscar_tabela()
    except Exception as e:
        print(f"[ERRO] Não consegui ler a página: {e}")
        registrar_log(f"ERRO: falha ao acessar o site - {e}")
        sys.exit(0)  # não derruba o workflow, só encerra essa execução mais cedo

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

    navios_atracados_detalhado = buscar_navios_atracados_detalhado()
    navios_atracados = {navio["nome"] for navio in navios_atracados_detalhado}

    eventos = []  # cada item guarda a mensagem + como "desfazer" se o envio falhar

    for chave, texto in novas.items():
        msg = f"🚢 NOVA MANOBRA AGENDADA (APEM)\n\n{texto}"
        print(msg)
        registrar_log(f"NOVA MANOBRA:\n{texto}")
        eventos.append({"tipo": "nova", "chave": chave, "msg": msg})

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
        registrar_log(f"REAGENDAMENTO:\n{msg}")
        eventos.append({
            "tipo": "reagendada", "chave_antiga": chave_antiga, "chave_nova": chave_nova, "msg": msg
        })

    for chave, texto in sumidas.items():
        partes_chave = chave.split(" | ")
        nome_navio = partes_chave[0].strip().upper()
        tipo_manobra = partes_chave[3].strip().upper() if len(partes_chave) > 3 else ""
        esta_atracado = nome_navio in navios_atracados

        if tipo_manobra == "EA":
            # Manobra era uma ATRACAÇÃO: se o navio está na lista de atracados, deu certo.
            if esta_atracado:
                msg = f"✅ Navio {nome_navio} atracou com sucesso!\n\n{texto}"
                registrar_log(f"MANOBRA CONCLUÍDA (navio atracou):\n{texto}")
                registrar_atracacao_historico({
                    "data": partes_chave[1] if len(partes_chave) > 1 else "?",
                    "hora": partes_chave[2] if len(partes_chave) > 2 else "?",
                    "nome": nome_navio,
                    "de": texto.split("De: ")[1].split("\n")[0] if "De: " in texto else "?",
                    "berco": partes_chave[4] if len(partes_chave) > 4 else "?",
                    "agencia": texto.split("Agência: ")[1].split("\n")[0] if "Agência: " in texto else "?",
                })
            else:
                msg = f"⚠️ MANOBRA SAIU DA LISTA (possível cancelamento/desmarcação)\n\n{texto}"
                registrar_log(f"MANOBRA CANCELADA/SUMIU (não encontrado em Navios Atracados):\n{texto}")

        elif tipo_manobra == "DS":
            # Manobra era uma DESATRACAÇÃO: a lógica é invertida — se o navio
            # AINDA está atracado, a desatracação não aconteceu (foi adiada).
            # Se ele NÃO está mais lá, desatracou de verdade.
            if esta_atracado:
                msg = f"⏸️ DESATRACAÇÃO ADIADA — {nome_navio} ainda está atracado\n\n{texto}"
                registrar_log(f"DESATRACAÇÃO ADIADA (navio ainda encontrado em Navios Atracados):\n{texto}")
            else:
                msg = f"✅ Navio {nome_navio} desatracou com sucesso!\n\n{texto}"
                registrar_log(f"MANOBRA CONCLUÍDA (navio desatracou):\n{texto}")

        else:
            # Tipo de manobra desconhecido — mantém o comportamento antigo como fallback
            if esta_atracado:
                msg = f"✅ Navio {nome_navio} atracou com sucesso!\n\n{texto}"
                registrar_log(f"MANOBRA CONCLUÍDA (navio atracou):\n{texto}")
            else:
                msg = f"⚠️ MANOBRA SAIU DA LISTA (possível cancelamento/desmarcação)\n\n{texto}"
                registrar_log(f"MANOBRA CANCELADA/SUMIU (não encontrado em Navios Atracados):\n{texto}")

        print(msg)
        eventos.append({"tipo": "sumida", "chave": chave, "texto": texto, "msg": msg})

    mensagens_pendentes = [e["msg"] for e in eventos]

    if len(mensagens_pendentes) == 1:
        # Só uma mudança: manda a mensagem normal, sem cabeçalho de "resumo"
        sucesso_envio = enviar_whatsapp(mensagens_pendentes[0])
    elif len(mensagens_pendentes) > 1:
        # Mais de uma mudança na mesma checagem: agrupa tudo numa única mensagem
        separador = "\n\n" + ("─" * 24) + "\n\n"
        cabecalho = f"📋 {len(mensagens_pendentes)} atualizações de manobras (APEM)\n\n"
        mensagem_agrupada = cabecalho + separador.join(mensagens_pendentes)
        sucesso_envio = enviar_whatsapp(mensagem_agrupada)
    else:
        sucesso_envio = True  # nada pra enviar

    # Estado final a salvar: começa a partir da leitura atual do site...
    estado_final = dict(atual)

    if not sucesso_envio and eventos:
        # O envio falhou: "desfaz" no estado só as mudanças dessa rodada,
        # pra elas serem detectadas de novo (e reenviadas) na próxima execução.
        print("[AVISO] Falha ao enviar WhatsApp — mudanças serão tentadas novamente na próxima execução.")
        registrar_log("FALHA DE ENVIO — mudanças desta rodada NÃO confirmadas, serão re-tentadas.")
        for e in eventos:
            if e["tipo"] == "nova":
                estado_final.pop(e["chave"], None)
            elif e["tipo"] == "reagendada":
                estado_final.pop(e["chave_nova"], None)
                estado_final[e["chave_antiga"]] = anterior.get(e["chave_antiga"], "")
            elif e["tipo"] == "sumida":
                estado_final[e["chave"]] = e["texto"]

    if not novas and not sumidas and not reagendadas:
        print("Nenhuma mudança detectada.")

    # Monta o painel web: Navios Fundeados + rota sugerida pra embarcação
    navios_fundeados = buscar_navios_fundeados()
    navios_fundeados = enriquecer_fundeados_com_previsao(navios_fundeados, atual_dados)
    rota_sugerida = montar_rota_otimizada(BASE_LAT, BASE_LON, navios_fundeados)

    # Navios que aparecem fundeados MAS já têm atracação prevista em breve
    # demais (menos que TEMPO_MINIMO_VISITA_HORAS) — não entram na rota
    # porque não dá tempo de visitar, mas ficam listados por transparência.
    nomes_na_rota = {n["nome"] for n in rota_sugerida}
    navios_atracando_em_breve = sorted(
        [
            n for n in navios_fundeados
            if n["nome"] not in nomes_na_rota
            and n.get("tempo_restante_horas") is not None
            and n["tempo_restante_horas"] < TEMPO_MINIMO_VISITA_HORAS
        ],
        key=lambda n: n["tempo_restante_horas"],
    )

    painel = {
        "atualizado_em": agora_br().strftime("%d/%m/%Y %H:%M:%S"),
        "base": {"lat": BASE_LAT, "lon": BASE_LON, "nome": BASE_NOME},
        "navios_fundeados": navios_fundeados,
        "rota_sugerida": rota_sugerida,
        "navios_atracando_em_breve": navios_atracando_em_breve,
    }
    with open(PAINEL_FILE, "w", encoding="utf-8") as f:
        json.dump(painel, f, ensure_ascii=False, indent=2)

    salvar_estado(estado_final)


if __name__ == "__main__":
    main()
