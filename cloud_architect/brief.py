"""Read a requirements brief or RFP and propose values for the workload form.

Each proposal carries the sentence it came from, and the person confirms each one before it fills
the form. A language model reads the brief when one is configured (its quotes are checked against
the document, as for policies); otherwise a rule-based reader finds the common phrasings: user
numbers, data sizes and growth, recovery targets, frameworks, locations, integrations and budget.
"""

from __future__ import annotations

import json
import re

from .config import Settings
from .documents import grounded, read_document, sentences
from .models import Workload
from .regions import _COUNTRY_REGION, CURRENCIES

FIELDS = {
    "name": "short name of the system",
    "description": "one or two sentences on what it does",
    "needs": "list of: web_app, api, relational_db, nosql_db, object_storage, cache, llm, search, background_jobs, containers, virtual_machines",
    "monthly_users": "monthly active users (number)",
    "peak_concurrent_users": "users online at the busiest moment (number)",
    "user_locations": "countries where the users are (list)",
    "peak_rps": "peak requests per second (number)",
    "read_ratio": "share of requests that only read, 0 to 1",
    "data_gb": "database size today in GB",
    "data_growth_pct": "database growth per year in percent",
    "blob_gb": "files and documents in GB",
    "retention_years": "how long records must be kept, in years",
    "backup_retention_days": "how long backups must be kept, in days",
    "availability": "standard (99.9%), high (99.95%) or critical (99.99%)",
    "rpo_minutes": "most data they can afford to lose, in minutes",
    "rto_minutes": "longest outage they can accept after a disaster, in minutes",
    "residency": "where data must stay: eu, uk, us, canada, australia, nz or any",
    "frameworks": "list of: gdpr, hipaa, pci_dss, iso27001, soc2",
    "pii": "handles personal information (true/false)",
    "health_data": "handles health information (true/false)",
    "card_data": "handles payment card data (true/false)",
    "identity": "entra_id (staff sign-in), entra_external_id (customer sign-up and sign-in), external or none",
    "environments": "list of: dev, test, prod",
    "integrations": "outside systems it connects to (list of names)",
    "constraints": "stack constraints, e.g. 'must use PostgreSQL', '.NET team' (list)",
    "budget_month": "monthly budget (number)",
    "currency": "ISO currency code of the budget",
}

_NUM = r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|m|million)?"
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}
_MONEY = re.compile(r"(US\$|A\$|AU\$|NZ\$|C\$|CA\$|£|€|\$|\b(?:USD|EUR|GBP|AUD|NZD|CAD)\s?)\s*" + _NUM, re.I)
_SYMBOL = {"us$": "USD", "$": "USD", "a$": "AUD", "au$": "AUD", "nz$": "NZD", "c$": "CAD", "ca$": "CAD", "£": "GBP", "€": "EUR"}
_JURISDICTION = [
    (re.compile(r"(?i:european union|\beuropean economic area)|\bEU\b|\bEEA\b"), "eu"),
    (re.compile(r"(?i:united kingdom)|\bUK\b"), "uk"),
    (re.compile(r"(?i:united states)|\bUS\b|\bUSA\b"), "us"),
    (re.compile(r"(?i:\bcanada\b)"), "canada"),
    (re.compile(r"(?i:\baustralia\b)"), "australia"),
    (re.compile(r"(?i:\bnew zealand\b)"), "nz"),
]
_USERS = (
    r"monthly (?:active )?users|users a month|users per month|monthly visitors|visitors a month|visitors per month"
    r"|active users|patients a month|customers a month"
)
_KNOWN_SYSTEMS = ["Xero", "MYOB", "QuickBooks", "Salesforce", "HubSpot", "Dynamics 365", "SAP", "ServiceNow", "Workday", "Stripe", "Shopify"]


def _number(m: re.Match, group: int = 1) -> float:
    value = float(m.group(group).replace(",", ""))
    scale = (m.group(group + 1) or "").lower()
    return value * _SCALE.get(scale, 1)


def _gb(text: str, words: str) -> float | None:
    m = re.search(rf"(?:{words})[^.]*?{_NUM}\s*(TB|GB)\b|{_NUM}\s*(TB|GB)\b[^.]*?(?:{words})", text, re.I)
    if not m:
        return None
    if m.group(1):
        value, unit = _number(m, 1), m.group(3)
    else:
        value, unit = _number(m, 4), m.group(6)
    return value * (1024 if unit.upper() == "TB" else 1)


def _minutes(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?)\b", text, re.I)
    if not m:
        return None
    return float(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1)


def extract_with_rules(text: str) -> list[dict]:
    found: dict[str, dict] = {}

    def put(field: str, value, quote: str):
        if field not in found and value not in (None, [], ""):
            found[field] = {"field": field, "value": value, "quote": quote}

    def add_list(field: str, values: list, quote: str):
        if not values:
            return
        if field in found:
            found[field]["value"] = list(dict.fromkeys(found[field]["value"] + values))
        else:
            put(field, list(dict.fromkeys(values)), quote)

    for s in sentences(text):
        t, low = s.text, s.text.lower()
        if m := re.search(_NUM + r"\s+(?:" + _USERS + ")", t, re.I):
            put("monthly_users", int(_number(m)), t)
        if m := re.search(_NUM + r"\s+(?:concurrent|simultaneous)\s+(?:users|sessions|shoppers|visitors)", t, re.I) or re.search(
            r"(?:concurrent|simultaneous)\s+(?:users|sessions)[^.]*?" + _NUM, t, re.I
        ):
            put("peak_concurrent_users", int(_number(m)), t)
        if m := re.search(_NUM + r"\s*(?:requests per second|requests a second|requests/s|req/s|rps)\b", t, re.I):
            put("peak_rps", _number(m), t)
        if m := re.search(r"(\d+)\s*%\s*(?:of (?:all )?(?:requests|traffic) )?(?:are |is )?(?:reads|read-only)", t, re.I):
            put("read_ratio", round(int(m.group(1)) / 100, 2), t)
        if re.search(r"\bdatabase\b", low) and (gb := _gb(t, "database|data")) is not None:
            put("data_gb", gb, t)
        if m := re.search(r"(?:grow|grows|growing|growth)[^.]*?(\d+(?:\.\d+)?)\s*%", t, re.I):
            put("data_growth_pct", float(m.group(1)), t)
        if (
            re.search(r"\b(files|documents|drawings|images|photos|attachments|scans|uploads)\b", low)
            and (gb := _gb(t, "files|documents|drawings|images|photos|attachments|scans|uploads")) is not None
        ):
            put("blob_gb", gb, t)
        if (
            re.search(r"\b(records|files|documents|data)\b", low)
            and not re.search(r"backup", low)
            and (m := re.search(r"(?:kept|keep|retain|retained|stored)[^.]*?(\d+|five|six|seven|ten)\s+years", t, re.I))
        ):
            word = m.group(1).lower()
            put("retention_years", float({"five": 5, "six": 6, "seven": 7, "ten": 10}.get(word, word)), t)
        if re.search(r"\bbackups?\b", low) and (m := re.search(r"(\d+)\s*(days?|weeks?|months?|years?)", t, re.I)):
            unit = {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)[0].lower()]
            put("backup_retention_days", int(m.group(1)) * unit, t)
        if m := re.search(r"99\.(99|95|9)\s*%", t):
            put("availability", {"99": "critical", "95": "high", "9": "standard"}[m.group(1)], t)
        if re.search(r"\bRPO\b|recovery point|lose (?:no )?more than", t, re.I) and (mins := _minutes(t)) is not None:
            put("rpo_minutes", int(mins), t)
        if re.search(r"\bRTO\b|recovery time|back (?:online|up) within|restored within", t, re.I):
            m = re.search(r"(?:RTO|recovery time|within)[^.]*?(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?)", t, re.I)
            if m:
                put("rto_minutes", int(float(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1)), t)
        if re.search(r"\b(stored|hosted|stay|stays|remain|reside|kept|processed)\b", low) and re.search(r"\b(data|information|records)\b", low):
            for pattern, jurisdiction in _JURISDICTION:
                if pattern.search(t):
                    put("residency", jurisdiction, t)
                    break
        frameworks = []
        if re.search(r"\bGDPR\b", t):
            frameworks.append("gdpr")
        if re.search(r"\bHIPAA\b", t):
            frameworks.append("hipaa")
        if re.search(r"\bPCI(?:[- ]DSS)?\b", t):
            frameworks.append("pci_dss")
        if re.search(r"ISO(?:/IEC)?\s*27001", t):
            frameworks.append("iso27001")
        if re.search(r"\bSOC\s*2\b", t):
            frameworks.append("soc2")
        add_list("frameworks", frameworks, t)
        if re.search(r"personal (?:data|information|details)|\bPII\b|names and addresses|e-?mail addresses|phone numbers|dates of birth", t, re.I):
            put("pii", True, t)
        if re.search(r"health (?:data|information|records)|\bPHI\b|medical records|patient records", t, re.I):
            put("health_data", True, t)
        if re.search(r"card (?:payments?|data|details|numbers)|cardholder", t, re.I):
            put("card_data", True, t)
        if re.search(
            r"customer accounts|sign up|self-service registration|external id|\bB2C\b|(?:customers|patients|suppliers) (?:sign|log) in", t, re.I
        ):
            put("identity", "entra_external_id", t)
        elif re.search(r"single sign-on|\bEntra ID\b|Azure AD|Microsoft 365 accounts|staff (?:sign|log) in", t, re.I):
            put("identity", "entra_id", t)
        if re.search(r"\benvironments?\b", low):
            envs = [
                e
                for e, p in (("dev", r"\bdev(?:elopment)?\b"), ("test", r"\b(?:test|testing|staging|uat)\b"), ("prod", r"\bprod(?:uction)?\b"))
                if re.search(p, low)
            ]
            if envs:
                put("environments", envs if "prod" in envs else envs + ["prod"], t)
        systems = [name for name in _KNOWN_SYSTEMS if re.search(rf"\b{re.escape(name)}\b", t)]
        systems += [
            m.group(1) for m in re.finditer(r"\b(?:our|the|their) (ERP|CRM|HR system|accounting system|finance system|warehouse system)\b", t)
        ]
        if re.search(r"\b(integrat\w*|connect\w*|sync\w*|feed\w*|exchange)\b", low):
            m = re.search(r"\b(?:integrat\w*|connect\w*|sync\w*) (?:with|to) (?:our |the |their )?([A-Z][\w&-]*(?: [A-Z][\w&-]*)*)", t)
            if m and m.group(1) not in systems and m.group(1) not in ("Azure", "Microsoft"):
                systems.append(m.group(1))
        add_list("integrations", systems, t)
        if re.search(r"\.NET|must use|standardi[sz]ed on|our (?:team|developers) (?:know|use)|no kubernetes|must run on", t, re.I):
            add_list("constraints", [t.rstrip(".")], t)
        if re.search(r"\bbudget\b|per month|a month|monthly", low) and re.search(r"budget|spend|cost|afford", low) and (m := _MONEY.search(t)):
            sym = m.group(1).strip().lower()
            put("budget_month", _number(m, 2), t)
            currency = _SYMBOL.get(sym, sym.upper())
            if currency in CURRENCIES:
                put("currency", currency, t)
        needs = []
        for need, pattern in (
            ("web_app", r"\b(portal|website|web site|web app|online shop|web front end)\b"),
            ("api", r"\b(api|apis|mobile app)\b"),
            ("relational_db", r"\b(database|sql|postgres\w*)\b"),
            ("object_storage", r"\b(upload\w*|documents|files|drawings|images|photos|attachments)\b"),
            ("search", r"\bsearch\b"),
            ("llm", r"\b(ai assistant|chatbot|language model|summar\w+|generative ai|gpt)\b"),
            ("background_jobs", r"\b(nightly|scheduled|batch|background|sync\w*|queue\w*)\b"),
            ("cache", r"\bcach(e|ing)\b"),
        ):
            if re.search(pattern, low):
                needs.append(need)
        add_list("needs", needs, t)
    locations = []
    for s in sentences(text):
        if re.search(r"\b(users|customers|suppliers|patients|clients|visitors|staff|shoppers|members)\b", s.text, re.I):
            for name in _COUNTRY_REGION:
                if len(name) > 3 and re.search(rf"\b{re.escape(name)}\b", s.text, re.I):
                    locations.append(name.title() if name not in ("uk", "us", "usa") else name.upper())
                elif len(name) <= 3 and name.upper() in ("UK", "US", "USA") and re.search(rf"\b{name.upper()}\b", s.text):
                    locations.append(name.upper())
            if locations and "user_locations" not in found:
                put("user_locations", list(dict.fromkeys(locations)), s.text)
    return list(found.values())


PROMPT = (
    "You read requirements briefs and RFPs for cloud systems and fill in a workload form. Return JSON only: "
    '{"fields": [{"field": ..., "value": ..., "quote": ...}]}. Fields:\n'
    + "\n".join(f"- {k}: {v}" for k, v in FIELDS.items())
    + "\nOnly include a field the document states or clearly implies. 'quote' is the sentence it comes from, copied word for word."
)


def extract_with_model(text: str, cfg: Settings, client=None) -> list[dict]:
    from . import llm

    io = {} if client is not None else {"cache": llm.Cache(cfg.llm_cache) if cfg.llm_cache else None, "min_interval": cfg.min_interval}
    client = client or llm.client(cfg)
    resp = llm.complete(
        client, **io, model=cfg.llm_model, temperature=0, max_tokens=3000,
        messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": text[:30000]}],
    )  # fmt: skip
    raw = resp.choices[0].message.content or ""
    data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    return [f for f in data.get("fields", []) if f.get("field") in FIELDS and grounded(str(f.get("quote", "")), text)]


def _valid(fields: list[dict]) -> list[dict]:
    """Keep proposals that the workload model accepts, one field at a time."""
    out = []
    for f in fields:
        try:
            Workload.model_validate({"name": "check", "needs": ["web_app"], f["field"]: f["value"]})
        except Exception:  # noqa: BLE001  a value the form cannot take is not proposed
            if f["field"] not in ("name", "needs"):
                continue
        out.append(f)
    return out


def extract(name: str, data: bytes, cfg: Settings | None = None, client=None) -> dict:
    text = read_document(name, data)
    if cfg is not None and (client is not None or cfg.llm_configured):
        try:
            return {"fields": _valid(extract_with_model(text, cfg, client)), "method": "model", "note": f"Read by {cfg.llm_model}."}
        except Exception as exc:  # noqa: BLE001
            note = f"The model was unavailable ({str(exc)[:120]}). "
    else:
        note = ""
    return {
        "fields": _valid(extract_with_rules(text)),
        "method": "rules",
        "note": note + "No language model was used: these values come from a rule-based reader. Confirm each one.",
    }
