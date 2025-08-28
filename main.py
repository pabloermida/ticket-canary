#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Router de Polling (Agidesk -> OpenAI Responses API -> Teams -> Agidesk)
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import requests
import logging
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agidesk import AgideskAPI, Ticket

# ============ VARS / CONFIG ============

AGIDESK_ACCOUNT_ID = os.getenv("AGIDESK_ACCOUNT_ID", "")
AGIDESK_APP_KEY = os.getenv("AGIDESK_APP_KEY", "")

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

POLL_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL", "300"))
FETCH_TIME_SECONDS = int(os.getenv("FETCH_TIME_SECONDS"))

MODE = os.getenv("MODE", "development")  # Default to 'development' for safety

ID_BOARD_SERVICOS = "9"

PROCESSED_IDS_FILE = "processed_ids.json"


def load_processed_ids() -> set[str]:
    """
    Carrega os IDs dos tickets processados do arquivo JSON.
    """
    if not os.path.exists(PROCESSED_IDS_FILE):
        return set()
    
    try:
        with open(PROCESSED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"Não foi possível ler o arquivo de IDs processados: {e}")
        return set()


def save_processed_ids(processed_ids: set[str]):
    """
    Salva o conjunto de IDs de tickets processados no arquivo JSON.
    """
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(processed_ids), f)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ============ TEMPO/FORMATOS ============

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ds_time(dt: datetime) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC, sem TZ)"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_dt_loose(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if not dt.tzinfo:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def within_last_seconds(created_at: str, seconds: int = 300) -> bool:
    dt = parse_dt_loose(created_at)
    return bool(dt) and (now_utc() - dt) <= timedelta(seconds=seconds)


# ============ Funções de Extração de Imagem ============

def extract_image_urls(html_content: str) -> list[str]:
    """Extrai URLs de imagem de um conteúdo HTML."""
    if not html_content:
        return []
    # Encontra todas as URLs de imagem dentro de tags <img>
    urls = re.findall(r'<img [^>]*src="([^"]+)"', html_content)
    return urls

# ============ OpenAI (Responses API com Structured Outputs) ============

def call_openai_simplified(ticket: Ticket) -> Dict[str, Any]:
    """
    Chama a API da OpenAI para obter um resumo e uma solução para o ticket usando o modo JSON.
    Inclui o processamento de imagens se houver.
    """
    system_text = (
        "Você é um engenheiro de suporte técnico especialista. Com base nas informações do ticket, "
        "forneça um objeto JSON com duas chaves: 'resumo_problema' (um resumo curto e claro "
        "do problema do usuário) e 'sugestao_solucao' (uma possível solução ou passos para resolvê-lo)."
    )
    
    user_text = f"Título: {ticket.title}\nConteúdo: {ticket.content}"
    image_urls = extract_image_urls(ticket.htmlcontent or "")

    user_message_content: list[Dict[str, Any]] = [{"type": "text", "text": user_text}]
    for url in image_urls:
        user_message_content.append({"type": "image_url", "image_url": {"url": url}})
        logging.info(f"Encontrada imagem para análise no ticket {ticket.id}: {url}")

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    
    payload = {
        "model": OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_message_content},
        ],
        "max_tokens": 1000,
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        
        data = r.json()
        response_content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        
        ai_summary = json.loads(response_content)
        logging.info(f"Resposta da IA para o ticket {ticket.id}: {ai_summary}")
        return ai_summary

    except requests.RequestException as e:
        logging.error(f"Erro ao chamar a API da OpenAI: {e}")
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logging.error(f"Falha ao analisar a resposta da OpenAI: {e}")
    
    return {"resumo_problema": "Erro: Não foi possível processar a resposta da IA.", "sugestao_solucao": "N/A"}


# ============ Teams Webhook ============

def notify_teams(message: str) -> bool:
    """
    Envia notificação para o Microsoft Teams.
    Retorna True se sucesso, False se falha.
    """
    if not TEAMS_WEBHOOK_URL:
        logging.error("TEAMS_WEBHOOK_URL não configurada")
        return False

    payload = {"text": message}

    try:
        response = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=30)
        if response.status_code == 200:
            logging.info("Mensagem enviada ao Teams com sucesso")
            return True
        else:
            logging.error(f"Erro ao enviar para Teams: {response.status_code} - {response.text}")
            return False
    except requests.RequestException as e:
        logging.error(f"Erro de conexão com Teams: {e}")
        return False


def build_ai_comment_html(ai_summary: Dict[str, str]) -> str:
    """
    Constrói o conteúdo HTML para o comentário do Agidesk.
    """
    resumo = ai_summary.get('resumo_problema', 'N/A')
    solucao = ai_summary.get('sugestao_solucao', 'N/A')
    
    return (
        f"<b>Resumo do Problema (IA):</b><br>{resumo}<br><br>"
        f"<b>Sugestão de Solução (IA):</b><br>{solucao}"
    )


# ============ Pipeline de 1 ticket ============

def process_issue(agi_client, issue: Ticket) -> Optional[Dict[str, Any]]:
    """
    Processa um ticket se passar na validação. As ações executadas dependem do MODE.
    """
    # Validação do ticket
    # if not within_last_seconds(issue.created_at or "", FETCH_TIME_SECONDS):
    #     logging.debug(f"Ticket {issue.id} fora da janela de tempo")
    #     return None
    
    # Checa se o ticket pertence ao board 'Time Suporte' (ID_BOARD_SERVICOS)
    board_found = False
    if issue.lists:
        board_found = any(
            ticket_list.boards and ID_BOARD_SERVICOS in ticket_list.boards
            for ticket_list in issue.lists.values()
        )

    if not board_found:
        logging.debug(f"Ticket {issue.id} não pertence ao board {ID_BOARD_SERVICOS}.")
        return None

    # 1. Notifica o Teams (executado em ambos os modos)
    notification_text = f"Novo ticket recebido: ID {issue.id} - {issue.title}"
    if notify_teams(notification_text):
        logging.info(f"✅ Notificação para o ticket {issue.id} enviada ao Teams")
    else:
        logging.error(f"❌ Falha ao enviar notificação para o ticket {issue.id} ao Teams")

    # 2. Obtém o resumo da IA (executado em ambos os modos)
    ai_summary = call_openai_simplified(issue)
    
    update_resp: Dict[str, Any] = {"status": "skipped in development mode"}
    # 3. Atualiza o ticket no Agidesk (somente em modo de produção)
    if MODE == "production" or issue.id == "3315":
        comment_html = build_ai_comment_html(ai_summary)
        try:
            update_resp = agi_client.add_comment(issue.id, comment_html)
            logging.info(f"Comentário da IA adicionado ao ticket {issue.id} no Agidesk.")
        except Exception as e:
            update_resp = {"error": str(e)}
            logging.error(f"Falha ao adicionar comentário ao ticket {issue.id} no Agidesk: {e}")
    else:
        logging.info(f"Skipping update for ticket {issue.id} (not the test ticket 3315).")
        update_resp = {"status": "skipped, not test ticket"}

    return {
        "issue_id": issue.id,
        "ai_summary": ai_summary,
        "agidesk_update_response": update_resp
    }


# ============ MAIN ============

def main() -> None:
    required_vars = ["AGIDESK_ACCOUNT_ID", "AGIDESK_APP_KEY", "TEAMS_WEBHOOK_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logging.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return

    logging.info("--- Starting Ticket Canary ---")

    agi = AgideskAPI(account_id=AGIDESK_ACCOUNT_ID, app_key=AGIDESK_APP_KEY)
    while True:
        try:
            start_time = now_utc()
            time_ago = start_time - timedelta(seconds=FETCH_TIME_SECONDS)
            initial_date_str = time_ago.strftime('%Y-%m-%d %H:%M:%S')

            # Carrega os IDs da última execução para evitar duplicados
            processed_ids = load_processed_ids()
            
            # Lógica principal de processamento
            issues = agi.search_tickets(
                forecast='inbox', # all, inbox, myaction
                # periodfield='created_at', # comentado para testes
                # initialdate=initial_date_str, # comentado para testes
                period='today', 
                per_page=100,
                # team=[ID_TIME_SERVICOS], # nao podemos filtrar por team
                fields='id,title,content,htmlcontent,created_at,lists'
            )
            logging.info(f"Encontrados {len(issues)} tickets.")

            # Lógica para identificar e processar novos tickets
            new_tickets = []
            for issue in issues:
                if str(issue.id) not in processed_ids:
                    new_tickets.append(issue)
            
            logging.info(f"Encontrados {len(new_tickets)} novos tickets para processar.")

            if MODE == "development":
                logging.info("--- MODO DE DESENVOLVIMENTO ATIVADO (Agidesk update desativado) ---")
            elif MODE == "production":
                logging.info("--- MODO DE PRODUÇÃO ATIVADO (LEITURA E ESCRITA) ---")
            else:
                logging.error(f"Modo '{MODE}' inválido. Use 'development' ou 'production'.")
                return # Sai se o modo for inválido

            for issue in new_tickets:
                result = process_issue(agi, issue)
                if result:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
            
            # Sobrescreve o arquivo de estado com TODOS os IDs da busca ATUAL
            all_ids_from_current_fetch = {str(issue.id) for issue in issues}
            if all_ids_from_current_fetch:
                save_processed_ids(all_ids_from_current_fetch)
                logging.info(f"Arquivo de estado atualizado com {len(all_ids_from_current_fetch)} IDs da busca atual.")
            
        except Exception as e:
            logging.error(f"Ocorreu um erro no ciclo de polling: {e}")
        
        logging.info(f"Ciclo concluído. Aguardando {POLL_INTERVAL_SEC} segundos...")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
