# Evaluation

20 workloads (`eval/scenarios.yaml`), every design scored by the same deterministic assessor.

| System | Model | No silent violations | Within budget | Expectations met | High findings left (avg) | Terraform valid | LLM's own cost estimate within 20% |
|---|---|---|---|---|---|---|---|
| Rule-based architect (no model) | - | 100% | 100% | 100% | 0.05 | 100% | n/a |
| LLM alone (no tools) | gemini-flash-lite-latest | 80% | 100% | 90% | 0.4 | 100% | 25% |
| LLM agent with tools | gemini-flash-lite-latest | 85% | 85% | 75% | 0.18 | 100% | n/a |

## Per scenario

| Scenario | Rule-based architect (no model) | LLM alone (no tools) | LLM agent with tools |
|---|---|---|---|
| s01 | NZ$70 · ok | NZ$69 · ok | NZ$57 · ok |
| s02 | NZ$502 · ok | NZ$207 · ok | NZ$502 · ok |
| s03 | NZ$2,293 · ok | NZ$1,549 · ok · missed private_data | no design |
| s04 | NZ$2,900 · ok | NZ$1,730 · silent: RES-AI-ZONE | NZ$1,581 · ok |
| s05 | NZ$2,537 · ok | NZ$3,014 · ok | NZ$2,519 · ok |
| s06 | NZ$1,565 · ok | NZ$1,039 · ok | NZ$877 · ok |
| s07 | NZ$1,140 · ok | NZ$1,140 · ok | NZ$1,372 · ok · missed has:functions |
| s08 | NZ$175 · ok | NZ$69 · ok | NZ$66 · ok |
| s09 | NZ$2,663 · ok | NZ$1,919 · silent: REGION | NZ$2,560 · ok |
| s10 | NZ$347 · ok | NZ$539 · silent: REGION | NZ$733 · ok |
| s11 | NZ$2,551 · ok | NZ$3,151 · ok | no design |
| s12 | NZ$4,327 · ok | NZ$4,619 · ok · missed dr_region | NZ$3,874 · ok · missed dr_region |
| s13 | NZ$298 · ok | NZ$298 · ok | NZ$298 · ok |
| s14 | NZ$424 · ok | NZ$305 · ok | NZ$407 · ok |
| s15 | NZ$1,118 · ok | NZ$1,118 · ok | NZ$1,118 · ok |
| s16 | NZ$1,741 · ok | NZ$1,995 · ok | NZ$1,848 · ok |
| s17 | NZ$74 · ok | NZ$74 · ok | NZ$74 · ok |
| s18 | NZ$2,514 · ok | NZ$1,877 · silent: REGION | no design |
| s19 | NZ$359 · ok | NZ$533 · ok | NZ$371 · ok |
| s20 | NZ$317 · ok | NZ$405 · ok | NZ$304 · ok |
