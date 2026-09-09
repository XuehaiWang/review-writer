"""Separate verified study facts from organization choices and lexical hints."""

from review_writer_core.scientific_facts import fact_usage


def routing_facts(row):
    return [fact for fact in row.get("scientific_facts") or []
            if isinstance(fact, dict) and fact.get("field_id") != "abstract_summary"
            and fact_usage(fact) in {"direct", "classification"}]


def confirmed_tags(row):
    tags = row.get("human_confirmed_tags")
    if isinstance(tags, dict) and tags:
        return tags
    if row.get("project_tag_review_status") == "confirmed" and isinstance(row.get("project_tags"), dict):
        return row["project_tags"]
    return {}


def current_classification_tags(row):
    facts = {str(fact.get("fact_id")): fact for fact in routing_facts(row)
             if fact_usage(fact) == "classification"}
    output = {}
    for axis, entries in (row.get("evidence_backed_tags") or {}).items():
        valid = [tag for tag in entries or [] if isinstance(tag, dict) and tag.get("fact_ids")
                 and all(str(fid) in facts for fid in tag["fact_ids"])]
        if valid:
            output[str(axis)] = valid
    return output


def verified_route(row):
    route = row.get("routing_recommendation") or {}
    registered = {str(fact.get("fact_id")): fact for fact in routing_facts(row)}
    bound = registered.get(str(route.get("verification_fact_id") or ""), {})
    verification = bound.get("verification") or {}
    return route if (route.get("status") == "classified" and route.get("evidence_refs")
                     and fact_usage(bound) == "classification" and verification.get("status") == "supported") else {}
