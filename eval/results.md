# Evaluation

20 workloads in 7 jurisdictions and 6 currencies (`eval/scenarios.yaml`), every design scored by the same deterministic assessor.

| System | Model | No silent violations | Within budget | Expectations met | High findings left (avg) | Terraform valid | LLM's own cost estimate within 20% |
|---|---|---|---|---|---|---|---|
| Rule-based architect (no model) | - | 100% | 100% | 100% | 0.05 | 100% | n/a |
| LLM alone (no tools) | gemini-flash-lite-latest | 85% | 100% | 60% | 0.7 | 100% | 15% |
| LLM agent with tools | gemini-flash-lite-latest | 100% | 100% | 65% | 0.3 | 100% | n/a |

## Per scenario

| Scenario | Residency | Rule-based architect (no model) | LLM alone (no tools) | LLM agent with tools |
|---|---|---|---|---|
| s01 Bakery click-and-collect (dev) | uk | £24 · ok | £24 · ok | £24 · ok |
| s02 Plumbing business booking site | us | US$219 · ok | US$36 · ok | US$33 · ok |
| s03 Physiotherapy chain patient portal | uk | £831 · ok | £724 · ok · missed waf | £670 · ok |
| s04 HR policy assistant for a Dutch employer | eu | €1,670 · ok | €700 · silent: NOT-SOLD, NEED | €559 · ok · missed region |
| s05 Australian retail chain e-commerce | australia | A$2,640 · ok | A$2,049 · ok · missed no_tier:D2ds_v5 | A$2,275 · ok · missed no_tier:D2ds_v5 |
| s06 Land records archive for a New Zealand trust | nz | NZ$1,565 · ok | NZ$1,217 · ok | NZ$1,621 · ok |
| s07 Canadian freight tracking API | canada | C$803 · ok | C$829 · ok | C$439 · ok · missed has:functions |
| s08 Startup SaaS MVP | any | US$123 · ok | US$28 · ok | US$27 · ok |
| s09 UK insurance claims triage with AI | uk | £976 · ok | £571 · silent: NOT-SOLD, NEED | £462 · ok · missed no_tier:gpt-4o-mini_global |
| s10 Marketing site for a global audience | any | US$341 · ok | US$234 · ok · missed autoscale | US$417 · ok · missed autoscale |
| s11 Government grants portal (critical, New Zealand only) | nz | NZ$2,551 · ok | NZ$1,849 · ok | NZ$2,320 · ok |
| s12 German bank customer chatbot (critical) | eu | €1,949 · ok | €1,494 · silent: NOT-SOLD, NEED · missed dr_region | €1,368 · ok · missed dr_region |
| s13 Internal HR knowledge search | any | US$146 · ok | US$134 · ok | US$90 · ok |
| s14 Batch invoice processing for an Australian wholesaler | australia | A$350 · ok | A$205 · ok | A$336 · ok |
| s15 Legacy app lift-and-shift for a Quebec insurer | canada | C$723 · ok | C$859 · ok | C$477 · ok |
| s16 Logistics microservices for a French carrier | eu | €754 · ok | €729 · ok · missed has:azure_sql | €451 · ok · missed has:azure_sql |
| s17 School newsletter app (tight budget) | nz | NZ$74 · ok | NZ$74 · ok | NZ$61 · ok |
| s18 US online store taking card payments | us | US$1,128 · ok | US$1,158 · ok · missed log_days | US$1,478 · ok |
| s19 US clinic records with HIPAA | us | US$1,036 · ok | US$740 · ok · missed log_days | US$526 · ok |
| s20 Irish payments ledger (5-minute RPO) | eu | €913 · ok | €729 · ok · missed db_replica | €805 · ok |

# Policy extraction

44 hand-labelled rules in 8 documents (`eval/policy_labels.yaml`). A proposed rule is correct when its check type and parameters match a label (strict); the lenient score only asks for the right check type.

| Extractor | Documents | Labelled | Proposed | Correct | Precision | Recall | Precision (lenient) | Recall (lenient) |
|---|---|---|---|---|---|---|---|---|
| Model (gemini-flash-lite-latest) | development (5 documents) | 27 | 27 | 27 | 100% | 100% | 100% | 100% |
| Model (gemini-flash-lite-latest) | held-out (3 documents) | 17 | 17 | 17 | 100% | 100% | 100% | 100% |
| Rule-based extractor (no model) | development (5 documents) | 27 | 27 | 27 | 100% | 100% | 100% | 100% |
| Rule-based extractor (no model) | held-out (3 documents) | 17 | 7 | 6 | 86% | 35% | 100% | 41% |
