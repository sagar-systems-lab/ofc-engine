"""Read-only, session-scoped activity assembled from persisted attempt states."""


def _states(db, query, run_id):
    return [row[0] for row in db.execute(query, (run_id,)).fetchall()]


def activity_projection(db, run_id: str, record: dict) -> dict:
    searches = _states(
        db, "select status from ofc_private.copilot_search where run_id=%s", run_id,
    )
    ranks = _states(
        db, "select state from ofc_private.dynamic_qloo_attempts where plan_id=%s", run_id,
    )
    catalog = _states(
        db, "select state from ofc_private.copilot_domain_observations where run_id=%s",
        run_id,
    )
    discoveries = _states(
        db, "select state from ofc_private.copilot_discoveries where run_id=%s", run_id,
    )
    rank_states = ranks + catalog + discoveries
    plan = record.get("plan")
    receipt = record.get("value_receipt")
    status = record.get("status", "PROCESSING")
    has_plan = isinstance(plan, dict) and isinstance(plan.get("choices"), list)
    has_review = status == "REVIEW_REQUIRED" and isinstance(receipt, dict)
    complete_ranks = rank_states.count("COMPLETE")
    reserved_ranks = rank_states.count("RESERVED")
    if complete_ranks:
        evidence = "RANK_OBSERVED" if not reserved_ranks else "PARTIAL"
    elif reserved_ranks:
        evidence = "RANK_PENDING_OR_INTERRUPTED"
    elif searches.count("COMPLETE"):
        evidence = "SEARCH_ONLY"
    elif searches.count("RESERVED"):
        evidence = "SEARCH_PENDING_OR_INTERRUPTED"
    else:
        evidence = "NOT_OBSERVED"
    return {
        "source": "PERSISTED_ATTEMPTS",
        "agent_plan": "OBSERVED" if has_plan else (
            "ADMITTED" if status == "PROCESSING" else "NOT_OBSERVED"
        ),
        "qloo_evidence": evidence,
        "ofc_review": "OBSERVED" if has_review else "NOT_OBSERVED",
        "results": status,
        "requests": {
            "search_complete": searches.count("COMPLETE"),
            "search_reserved": searches.count("RESERVED"),
            "rank_complete": complete_ranks,
            "rank_reserved": reserved_ranks,
        },
    }
