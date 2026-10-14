"""Turn company policy documents into machine-checkable rules.

A language model reads the document and proposes rules of eight known kinds (allowed regions,
customer-managed keys, no public endpoints, backup retention, approved services, a spending cap,
required tags, log retention), each with the section and the sentence it came from. A proposed rule
whose quote is not found in the document is dropped, so nothing is enforced on a clause the model
invented. Without a model, a rule-based extractor finds the same kinds of rules from common
phrasings. Either way the rules come back as *proposed*: the person reviews them and only accepted
rules are enforced.
"""

from __future__ import annotations

import json
import re

from .catalogue import SERVICES
from .config import Settings
from .documents import Sentence, grounded, read_document, sentences
from .models import PolicyCheck, PolicyRule
from .regions import ALL, RESIDENCY_REGIONS, _squash

CHECKS: dict[str, str] = {
    "allowed_regions": "data may only be stored or processed in these places",
    "customer_managed_keys": "data must be encrypted with customer-managed keys",
    "no_public_endpoints": "data stores must not be reachable from the public internet",
    "min_backup_retention_days": "backups must be kept at least N days",
    "approved_services": "only these cloud services may be used",
    "max_monthly_spend": "monthly cloud spend must not exceed an amount",
    "required_tags": "every resource must carry these tags",
    "min_log_retention_days": "logs must be kept at least N days",
}

_OBLIGATION = re.compile(
    r"\b(must|shall|required|requires|is to be|are to be|may only|may not|only|never|mandatory|need to|needs to)\b|^no\b.*\bmay\b", re.I
)
# Abbreviations are matched case-sensitively, so the word "us" in a sentence is not the United States.
_PLACES = [
    (re.compile(r"(?i:european union|european economic area)|\bEU\b|\bEEA\b"), "eu"),
    (re.compile(r"(?i:united kingdom|great britain)|\bUK\b"), "uk"),
    (re.compile(r"(?i:united states)|\bU\.?S\.?A?\b"), "us"),
    (re.compile(r"(?i:\bcanada\b)"), "canada"),
    (re.compile(r"(?i:\baustralia\b)"), "australia"),
    (re.compile(r"(?i:\bnew zealand\b|\baotearoa\b)"), "nz"),
]
_UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_DURATION = re.compile(r"\b(\d+|" + "|".join(_NUMBERS) + r")\s*(days?|weeks?|months?|years?)\b", re.I)
_MONEY = re.compile(r"(US\$|A\$|AU\$|NZ\$|C\$|CA\$|£|€|\$|\b(?:USD|EUR|GBP|AUD|NZD|CAD|CHF)\s?)\s*([\d,]+(?:\.\d+)?)\s*(k|thousand)?", re.I)
_SYMBOL_CUR = {"us$": "USD", "$": "USD", "a$": "AUD", "au$": "AUD", "nz$": "NZD", "c$": "CAD", "ca$": "CAD", "£": "GBP", "€": "EUR"}

# Names people use for catalogue services, as they appear in policy documents.
SERVICE_NAMES = {
    "app_service": ["app service", "web apps", "web app"],
    "container_apps": ["container apps"],
    "functions": ["azure functions", "functions"],
    "postgres": ["postgresql", "postgres"],
    "azure_sql": ["azure sql", "sql database", "sql server"],
    "cosmos_db": ["cosmos db", "cosmos"],
    "blob_storage": ["blob storage", "storage account", "azure storage", "storage"],
    "redis": ["redis", "cache for redis"],
    "key_vault": ["key vault"],
    "log_analytics": ["log analytics", "azure monitor"],
    "ai_search": ["ai search", "cognitive search", "azure search"],
    "azure_openai": ["azure openai", "openai service", "foundry models"],
    "front_door": ["front door"],
    "app_gateway": ["application gateway", "app gateway"],
    "aks": ["kubernetes service", "aks", "kubernetes"],
    "vm": ["virtual machines", "virtual machine", "vms"],
    "entra_external_id": ["entra external id", "external id"],
    "backup_vault": ["azure backup", "backup vault"],
}


def service_ids(text: str) -> list[str]:
    """Catalogue ids named in a piece of text ('Azure Database for PostgreSQL' -> postgres)."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    found = []
    for sid, names in SERVICE_NAMES.items():
        if any(f" {re.sub(r'[^a-z0-9]+', ' ', n)} " in t for n in names):
            found.append(sid)
    return found


def places_to_regions(text: str) -> list[str]:
    """Azure regions named directly ('UK South'), or every region of the jurisdictions named ('the EU')."""
    squashed = _squash(text)
    named = [r.code for r in ALL.values() if _squash(r.name) in squashed]
    if named:
        return sorted(set(named))
    regions: set[str] = set()
    for pattern, jurisdiction in _PLACES:
        if pattern.search(text):
            regions |= RESIDENCY_REGIONS[jurisdiction]
    return sorted(regions)


def _days(text: str) -> int | None:
    m = _DURATION.search(text)
    if not m:
        return None
    n = m.group(1).lower()
    value = int(n) if n.isdigit() else _NUMBERS[n]
    unit = m.group(2).lower()
    if unit.startswith("month") and value % 12 == 0:
        return value // 12 * 365  # "12 months" is a year
    return value * _UNITS[unit]


def _money(text: str, default_currency: str | None) -> tuple[float, str] | None:
    m = _MONEY.search(text)
    if not m:
        return None
    sym = m.group(1).strip().lower()
    currency = _SYMBOL_CUR.get(sym, sym.upper() if sym.isalpha() else default_currency or "USD")
    amount = float(m.group(2).replace(",", ""))
    if m.group(3):
        amount *= 1000
    return amount, currency


def _tags(text: str) -> list[str]:
    quoted = re.findall(r"[`'\"‘“]([A-Za-z][\w:.-]*)[`'\"’”]", text)
    if quoted:
        return [t.lower() for t in quoted]
    m = re.search(r"tag(?:ged|s)?\s*(?:with\s+|:\s*|named\s+|called\s+)(.+)", text, re.I)
    if not m:
        return []
    names = re.split(r",\s*|\s+and\s+", m.group(1).rstrip("."))
    return [n.strip().lower() for n in names if re.fullmatch(r"[a-z][\w-]*", n.strip().lower())]


def _rule(check: PolicyCheck, s: Sentence, source: str, **params) -> PolicyRule:
    return PolicyRule(check=check, source=source, section=s.section, quote=s.text, extracted_by="rules", **params)


def extract_with_rules(text: str, source: str) -> list[PolicyRule]:
    """Find rules from common phrasings. Each sentence must state an obligation (must, shall, only, ...)."""
    out: list[PolicyRule] = []
    for s in sentences(text):
        t = s.text
        low = t.lower()
        if not _OBLIGATION.search(t) and not re.search(r"\b(capped at|not exceed|stay under|no more than|limited to)\b", low):
            continue
        if re.search(r"\b(stored|store|hosted|host|processed|process|reside|resides|located|kept|remain|remains|deployed|held)\b", low) and re.search(
            r"\b(data|records|information|phi|pii|files|workloads?)\b", low
        ):
            regions = places_to_regions(t)
            if regions and not re.search(r"\b(back ?ups?|logs?)\b.*\b(days?|weeks?|months?|years?)\b", low):
                out.append(_rule("allowed_regions", s, source, regions=regions))
                continue
        if re.search(r"customer[- ]managed keys?|bring your own key|\bbyok\b|keys? (?:that )?(?:we|the company) (?:hold|control|own)", low):
            out.append(_rule("customer_managed_keys", s, source))
            continue
        if re.search(
            r"(not|never) (?:be )?(?:directly )?(?:reachable|accessible|exposed|available)\b.*\b(public )?internet"
            r"|^no\b.*\b(?:may|can|shall|must) be (?:directly )?(?:reachable|accessible|exposed)\b.*\binternet"
            r"|no public (?:network )?(?:access|endpoints?|ip)|not expose public endpoints?|public network access (?:must|shall) be disabled"
            r"|(?:only|must) (?:be )?(?:reached|accessed) (?:through|via|over) private",
            low,
        ):
            out.append(_rule("no_public_endpoints", s, source))
            continue
        days = _days(t)
        if days and re.search(r"\bback ?ups?\b", low) and re.search(r"retain|kept|keep|retention|stored|held", low):
            out.append(_rule("min_backup_retention_days", s, source, days=days))
            continue
        if days and re.search(r"\b(logs?|audit trails?|log data)\b", low) and re.search(r"retain|kept|keep|retention|stored|held", low):
            out.append(_rule("min_log_retention_days", s, source, days=days))
            continue
        if re.search(r"\b(spend|spending|cost|costs|bill|budget)\b", low) and re.search(r"month|monthly|per month|a month", low):
            money = _money(t, None)
            if money:
                out.append(_rule("max_monthly_spend", s, source, amount=money[0], currency=money[1]))
                continue
        if re.search(r"\btag(?:s|ged)?\b", low):
            tags = _tags(t)
            if tags:
                out.append(_rule("required_tags", s, source, tags=tags))
                continue
        if re.search(r"\b(approved|permitted|allowed|may only use|only use|only the following)\b", low) and re.search(r"services?|cloud", low):
            ids = service_ids(" ".join(s.follows) if s.follows else t)
            if ids:
                out.append(_rule("approved_services", s, source, services=ids))
                continue
    return _number(out)


def _number(rules: list[PolicyRule]) -> list[PolicyRule]:
    for n, r in enumerate(rules, 1):
        r.id = f"P{n}"
    return rules


PROMPT = (
    """You read company IT and security policies and extract rules a cloud architecture can be checked against.
Return JSON only: {"rules": [{"check": ..., "section": ..., "quote": ..., <parameters>}]}.
Check types and their parameters:
"""
    + "\n".join(f"- {k}: {v}" for k, v in CHECKS.items())
    + """
Parameters: allowed_regions -> "places": countries, jurisdictions (EU, UK, US, Canada, Australia, New Zealand) or Azure
regions named in the sentence; *_retention_days -> "days" (convert weeks, months and years to days: 1 month = 30 days,
1 year = 365 days); approved_services -> "services": the service names exactly as listed; max_monthly_spend -> "amount"
and "currency" (ISO code); required_tags -> "tags": tag names.
Rules:
- Only extract obligations of these eight kinds. Ignore training, passwords, devices, people processes and anything else.
- "quote" must be the sentence that states the rule, copied word for word from the document. "section" is its heading.
- One rule per obligation. If nothing qualifies, return {"rules": []}.
"""
)


def extract_with_model(text: str, source: str, cfg: Settings, client=None) -> tuple[list[PolicyRule], int]:
    """Rules proposed by the model, and how many it proposed that were dropped because the quote is not in the document."""
    from . import llm

    io = {} if client is not None else {"cache": llm.Cache(cfg.llm_cache) if cfg.llm_cache else None, "min_interval": cfg.min_interval}
    client = client or llm.client(cfg)
    resp = llm.complete(
        client,
        **io,
        model=cfg.llm_model,
        temperature=0,
        max_tokens=4000,
        messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": f"Document: {source}\n\n{text[:30000]}"}],
    )
    raw = resp.choices[0].message.content or ""
    try:
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except ValueError:
        return [], 0
    out, dropped = [], 0
    for item in data.get("rules", []) if isinstance(data, dict) else []:
        rule = _from_model(item, source)
        if rule is None:
            continue
        if not grounded(rule.quote, text):
            dropped += 1
            continue
        out.append(rule)
    return _number(out), dropped


def _from_model(item: dict, source: str) -> PolicyRule | None:
    check = item.get("check")
    if check not in CHECKS:
        return None
    params: dict = {}
    if check == "allowed_regions":
        params["regions"] = places_to_regions(" ".join(item.get("places") or item.get("regions") or []))
        if not params["regions"]:
            return None
    elif check in ("min_backup_retention_days", "min_log_retention_days"):
        try:
            params["days"] = int(float(item.get("days")))
        except (TypeError, ValueError):
            return None
    elif check == "approved_services":
        params["services"] = sorted({s for name in item.get("services") or [] for s in service_ids(str(name))})
        if not params["services"]:
            return None
    elif check == "max_monthly_spend":
        try:
            params["amount"] = float(str(item.get("amount")).replace(",", ""))
        except ValueError:
            return None
        params["currency"] = str(item.get("currency") or "USD").upper()[:3]
    elif check == "required_tags":
        params["tags"] = [str(t).strip("`'\" ").lower() for t in item.get("tags") or [] if str(t).strip()]
        if not params["tags"]:
            return None
    return PolicyRule(
        check=check,
        source=source,
        section=str(item.get("section") or "")[:60],
        quote=str(item.get("quote") or "")[:400],
        extracted_by="model",
        **params,
    )


def extract(name: str, data: bytes, cfg: Settings | None = None, client=None) -> dict:
    """Read a policy document and propose rules: with the model when one is configured, otherwise by rules."""
    text = read_document(name, data)
    note = ""
    if cfg is not None and (client is not None or cfg.llm_configured):
        try:
            rules, dropped = extract_with_model(text, name, cfg, client)
            note = f"Extracted by {cfg.llm_model}." + (f" {dropped} proposed rule(s) dropped: their quote is not in the document." if dropped else "")
            return {"rules": [r.model_dump() for r in rules], "method": "model", "note": note, "characters": len(text)}
        except Exception as exc:  # noqa: BLE001  the model is unavailable: fall back, and say so
            note = f"The model was unavailable ({str(exc)[:120]}). "
    rules = extract_with_rules(text, name)
    note += "No language model was used: these rules come from a rule-based extractor that recognises common phrasings. Check them carefully."
    return {"rules": [r.model_dump() for r in rules], "method": "rules", "note": note, "characters": len(text)}


def service_label(sid: str) -> str:
    return SERVICES[sid].name if sid in SERVICES else sid
