# router.py (pseudo)

INIT:
  load_env()                             # tokens, account, teams ids, model, etc.
  state.last_seen_updated_at = load_watermark() or T0

LOOP (cada 5 min):
  window = [last_seen_updated_at - 60s, now()]
  tickets = agidesk.fetch_tickets_updated(window, per_page=5, source="email")

  for t in sort_by_updated_at_asc(tickets):
    if agidesk.already_processed(t): 
        continue

    full = agidesk.get_ticket_details(t.id, extrafield="all")
    context = build_context(full)        # title, description, requester, formfields, history

    prompt, json_schema = build_openai_request(context)
    ai_json = openai.structured_analysis(prompt, json_schema)  # resposta obrigatoriamente em JSON válido

    blog_markdown = format_as_blog_post(ai_json)
    teams.send_channel_message(blog_markdown)                  # opcional: linkar ticket

    agidesk.update_ticket(
        id=t.id,
        problem_description=ai_json["problem"]["summary"],
        tasks=ai_json["remediation"]["tasks"],                 # com estimativa (min)
        extra_form=ai_json["debug"]["clarifying_questions"],   # checklist de confirmação/causas prováveis
        tags=["router:processed","ai:triaged"],
        formfields={"router_processed": True,
                    "router_processed_at": now_iso()}
    )

    state.last_seen_updated_at = t.updated_at
    persist_watermark(state.last_seen_updated_at)

  sleep(5 minutes)
