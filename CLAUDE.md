# Biosimilar Recommendation Engine (UiPath POC)

## What this is
A one-day prototype. A user enters a brand drug / J-code, region, payer and acquisition
channel (340B or GPO). The system looks up acquisition cost and reimbursement for every
biosimilar of that drug, calculates margin = reimbursement - acquisition cost, applies
business rules and returns the recommended biosimilar with supporting information.
It is a recommendation and reporting tool ONLY.

OUT OF SCOPE (never build): clinical logic, prior auth, benefit investigation,
Cerner/Athena order entry, EHR integration, fax/OCR, live payer APIs.
"Continue to ordering" only records the user's decision (Accepted / Overridden).

## Repo layout
```
/agent        Python coded agent (uipath-langchain + LangGraph), deployed to Orchestrator
/app          UiPath Coded App (React + TypeScript + @uipath/uipath-typescript)
/data         Mock input files, uploaded to the Storage Bucket "biosimilar-data"
/docs         Use case notes, test cases
```

## Tooling
- Use the installed UiPath skills (uipath-agents, uipath-coded-apps) and the `uip` CLI.
- Agent build loop: `uip codedagent setup|new|init|pack|publish`. Check the skill for
  exact flags before running; do not guess CLI flags.
- Python 3.11+, uv for dependencies. Node 22+ for the app.
- Publish to my personal workspace unless I say otherwise.
- Never hardcode secrets. Config goes in Orchestrator assets or environment variables.

## Demo drug class (mock data, verify codes before demo)
Reference: Neulasta (pegfilgrastim) J2506
Biosimilars: Fulphila Q5108, Udenyca Q5111, Ziextenzo Q5120, Nyvepria Q5122
Regions: South, West. Payers: Medicare, Aetna, UnitedHealthcare, Charity, Uninsured.

## Data sources (files in Storage Bucket)
- pharmacy_acquisition_cost.xlsx: hcpcs, ndc, product, channel (340B|GPO), cost_per_unit, effective_date
- cms_asp_pricing.csv: hcpcs, description, payment_limit, quarter   (used for Medicare)
- commercial_reimbursement.xlsx: payer, region, hcpcs, reimbursement_per_unit (used for commercial)

## Data Fabric entities
- DrugMaster: brand, jCode, biosimilar, qCode, active
- RecommendationRequest: requestId, userEmail, drugInput, region, payer, channel, status
  (New|Processing|Completed|NoRecommendation|ValidationError|Escalated), submittedAt, completedAt
- RecommendationResult: requestId, recommendedProduct, recommendedQCode, margin, acquisitionCost,
  reimbursement, ruleApplied (Standard|CharityUninsured|TieBreak|UserChoiceRequired),
  candidatesJson, rationale, userAction (Pending|Accepted|Overridden), selectedProduct
- ExceptionLog: requestId, scenario, severity (Info|Warning|Error|Critical), component (app|agent),
  message, details, timestamp
- AuditLog: requestId, userEmail, action, inputsSnapshot, fileVersionsUsed, timestamp

## Business rules (deterministic Python, NOT the LLM)
1. Candidates = active DrugMaster rows for the entered brand or J-code.
2. Acquisition cost = cost for candidate + selected channel.
3. Reimbursement = CMS ASP payment_limit if payer is Medicare, else commercial rate for payer + region.
4. margin = reimbursement - acquisition_cost.
5. Payer Charity or Uninsured: recommend LOWEST acquisition cost (ignore margin).
6. Otherwise: recommend HIGHEST margin.
7. Tie on margin: pick lower acquisition cost. Still tied: ruleApplied = UserChoiceRequired,
   return all tied options.
8. Round money to 2 decimals; compare with Decimal, not float.
The LLM only writes the `rationale` text from the already-computed numbers. It must never
change or invent numbers.

## Exception handling (must implement every row)
| Scenario | Action | Severity |
|---|---|---|
| Cost missing | Exclude product, warn user | Warning |
| Reimbursement rate missing | Exclude product, warn user | Warning |
| Invalid payer | Reject request, ask user to correct | Error (ValidationError status) |
| Equal margins | Tie-break rule, else show options | Info |
| File not found | Retry 3x with backoff (2s, 4s, 8s), then fail | Error |
| Invalid data (negative/non-numeric price, bad code) | Skip row, error message | Error |
| Unexpected exception | Log full stack trace, status Escalated, create Action Center task | Critical |
If all candidates are excluded: status NoRecommendation with reasons.
Every exception is written to ExceptionLog AND Orchestrator logs.

## Agent contract
Input:  { requestId, drugInput, region, payer, channel, userEmail }
Output: { requestId, status, recommendation, candidates[], ruleApplied, rationale, warnings[] }
LangGraph nodes: validate -> load_data -> calculate -> recommend -> write_rationale -> persist

## App screens
1. New Recommendation (form + validation)
2. Result (recommended product card, margin, comparison table, rule applied, warnings,
   Accept / Override + Continue to ordering)
3. Reports tab: Recommendation log, Usage, Margin, Audit, Exceptions (filterable table + simple chart)

## Working style
- Build in small steps; run and test after each step.
- Write unit tests for the rules engine (pytest) covering all 7 exception scenarios.
- Ask me before publishing or deleting anything in Orchestrator / Data Fabric.
