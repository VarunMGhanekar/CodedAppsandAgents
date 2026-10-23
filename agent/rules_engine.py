"""
Biosimilar Recommendation Engine - rules engine (pure Python, no UiPath calls).

Everything here is deterministic and unit-tested. The UiPath layer (bucket reads,
Data Fabric writes, LLM rationale, Action Center) wraps these functions in Phase 3.

Input rows are plain dicts, exactly as read from the data files:
  DrugMaster:        brand, jCode, biosimilar, qCode, active
  Acquisition cost:  hcpcs, ndc, product, channel, cost_per_unit, effective_date
  CMS ASP:           hcpcs, description, payment_limit, quarter
  Commercial:        payer, region, hcpcs, reimbursement_per_unit
"""

from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

CHANNELS = {"340B", "GPO"}
REGIONS = {"South", "West"}
MEDICARE = "Medicare"
NON_REIMBURSED_PAYERS = {"Charity", "Uninsured"}
BASE_PAYERS = {MEDICARE} | NON_REIMBURSED_PAYERS
HCPCS_PATTERN = re.compile(r"^[A-Z]\d{4}$")
CENT = Decimal("0.01")

RETRY_DELAYS_SECONDS = (2, 4, 8)  # File not found: retry 3x with backoff


class Severity(str, Enum):
    INFO = "Info"
    WARNING = "Warning"
    ERROR = "Error"
    CRITICAL = "Critical"


class Scenario(str, Enum):
    COST_MISSING = "CostMissing"
    REIMBURSEMENT_MISSING = "ReimbursementRateMissing"
    INVALID_PAYER = "InvalidPayer"
    EQUAL_MARGINS = "EqualMargins"
    FILE_NOT_FOUND = "FileNotFound"
    INVALID_DATA = "InvalidData"
    UNEXPECTED = "UnexpectedException"


class Status(str, Enum):
    COMPLETED = "Completed"
    NO_RECOMMENDATION = "NoRecommendation"
    VALIDATION_ERROR = "ValidationError"
    ESCALATED = "Escalated"


class Rule(str, Enum):
    STANDARD = "Standard"
    CHARITY_UNINSURED = "CharityUninsured"
    TIE_BREAK = "TieBreak"
    USER_CHOICE_REQUIRED = "UserChoiceRequired"


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #

@dataclass
class Issue:
    scenario: Scenario
    severity: Severity
    message: str
    product: str | None = None
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.value,
            "severity": self.severity.value,
            "message": self.message,
            "product": self.product,
            "details": self.details,
        }


class RequestValidationError(Exception):
    """Raised when the user's input must be corrected before processing."""

    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(i.message for i in issues))
        self.issues = issues


class DataFileNotFoundError(Exception):
    """Raised when a data file is still missing after all retries."""


@dataclass
class RecommendationRequest:
    request_id: str
    drug_input: str
    region: str
    payer: str
    channel: str
    user_email: str = ""


@dataclass
class Candidate:
    product: str
    q_code: str


@dataclass
class PricedCandidate:
    product: str
    q_code: str
    acquisition_cost: Decimal
    reimbursement: Decimal | None  # None for Charity/Uninsured
    margin: Decimal | None         # None for Charity/Uninsured
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "qCode": self.q_code,
            "acquisitionCost": _money_str(self.acquisition_cost),
            "reimbursement": _money_str(self.reimbursement),
            "margin": _money_str(self.margin),
            "rank": self.rank,
        }


@dataclass
class Lookups:
    cost: dict[tuple[str, str], Decimal]               # (hcpcs, channel) -> cost
    asp: dict[str, Decimal]                            # hcpcs -> payment limit
    commercial: dict[tuple[str, str, str], Decimal]    # (payer, region, hcpcs) -> rate
    issues: list[Issue] = field(default_factory=list)  # invalid rows skipped


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _money_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(CENT, ROUND_HALF_UP))


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_money(value: Any) -> Decimal:
    """Parse a price into Decimal (2 dp). Raises ValueError for blank, non-numeric or negative."""
    text = _clean(value).replace("$", "").replace(",", "")
    if text == "":
        raise ValueError("blank price")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"non-numeric price '{value}'") from exc
    if not amount.is_finite():
        raise ValueError(f"non-numeric price '{value}'")
    if amount < 0:
        raise ValueError(f"negative price '{value}'")
    return amount.quantize(CENT, ROUND_HALF_UP)


def is_valid_hcpcs(code: Any) -> bool:
    return bool(HCPCS_PATTERN.match(_clean(code).upper()))


def _is_active(value: Any) -> bool:
    return _clean(value).lower() in {"true", "1", "yes", "y"}


# --------------------------------------------------------------------------- #
# Step 1: build lookups from file rows (skips invalid rows, records InvalidData)
# --------------------------------------------------------------------------- #

def build_lookups(
    cost_rows: Iterable[dict],
    asp_rows: Iterable[dict],
    commercial_rows: Iterable[dict],
) -> Lookups:
    issues: list[Issue] = []

    def invalid(source: str, row_no: int, reason: str, product: str | None = None) -> None:
        issues.append(Issue(
            Scenario.INVALID_DATA, Severity.ERROR,
            f"Skipped invalid row in {source} (row {row_no}): {reason}",
            product=product,
        ))

    cost: dict[tuple[str, str], Decimal] = {}
    for i, row in enumerate(cost_rows, start=2):  # row 1 is the header
        code = _clean(row.get("hcpcs")).upper()
        channel = _clean(row.get("channel")).upper()
        product = _clean(row.get("product")) or None
        if not is_valid_hcpcs(code):
            invalid("acquisition cost file", i, f"bad HCPCS code '{row.get('hcpcs')}'", product)
            continue
        if channel not in CHANNELS:
            invalid("acquisition cost file", i, f"bad channel '{row.get('channel')}'", product)
            continue
        try:
            cost[(code, channel)] = parse_money(row.get("cost_per_unit"))
        except ValueError as exc:
            invalid("acquisition cost file", i, str(exc), product)

    asp: dict[str, Decimal] = {}
    for i, row in enumerate(asp_rows, start=2):
        code = _clean(row.get("hcpcs")).upper()
        if not is_valid_hcpcs(code):
            invalid("CMS ASP file", i, f"bad HCPCS code '{row.get('hcpcs')}'")
            continue
        try:
            asp[code] = parse_money(row.get("payment_limit"))
        except ValueError as exc:
            invalid("CMS ASP file", i, str(exc))

    commercial: dict[tuple[str, str, str], Decimal] = {}
    for i, row in enumerate(commercial_rows, start=2):
        payer = _clean(row.get("payer"))
        region = _clean(row.get("region"))
        code = _clean(row.get("hcpcs")).upper()
        if not payer or not region or not is_valid_hcpcs(code):
            invalid("commercial reimbursement file", i, "missing payer/region or bad HCPCS code")
            continue
        try:
            commercial[(payer.lower(), region.lower(), code)] = parse_money(
                row.get("reimbursement_per_unit"))
        except ValueError as exc:
            invalid("commercial reimbursement file", i, str(exc))

    return Lookups(cost=cost, asp=asp, commercial=commercial, issues=issues)


def valid_payers(commercial_rows: Iterable[dict]) -> list[str]:
    """Medicare, Charity, Uninsured plus every payer present in the commercial file."""
    names = {_clean(r.get("payer")) for r in commercial_rows if _clean(r.get("payer"))}
    return sorted(BASE_PAYERS | names)


# --------------------------------------------------------------------------- #
# Step 2: validate the request
# --------------------------------------------------------------------------- #

def validate_request(
    req: RecommendationRequest,
    drug_master_rows: Iterable[dict],
    payers: Iterable[str],
) -> tuple[RecommendationRequest, list[Candidate]]:
    """Return a normalized request and its biosimilar candidates, or raise RequestValidationError."""
    issues: list[Issue] = []
    rows = list(drug_master_rows)

    drug_key = _clean(req.drug_input).lower()
    matches = [
        r for r in rows
        if _is_active(r.get("active"))
        and drug_key in {_clean(r.get("brand")).lower(), _clean(r.get("jCode")).lower()}
    ]
    if not drug_key:
        issues.append(Issue(Scenario.INVALID_DATA, Severity.ERROR,
                            "Please enter a brand drug name or J-code."))
    elif not matches:
        issues.append(Issue(Scenario.INVALID_DATA, Severity.ERROR,
                            f"'{req.drug_input}' is not a known brand drug or J-code "
                            "with active biosimilars."))

    payer_lookup = {p.lower(): p for p in payers}
    payer = payer_lookup.get(_clean(req.payer).lower())
    if payer is None:
        issues.append(Issue(Scenario.INVALID_PAYER, Severity.ERROR,
                            f"Payer '{req.payer}' is not recognized. Please select a valid payer.",
                            details="Valid payers: " + ", ".join(sorted(payer_lookup.values()))))

    region_lookup = {r.lower(): r for r in REGIONS}
    region = region_lookup.get(_clean(req.region).lower())
    if region is None:
        issues.append(Issue(Scenario.INVALID_DATA, Severity.ERROR,
                            f"Region '{req.region}' is not recognized."))

    channel = _clean(req.channel).upper()
    if channel not in CHANNELS:
        issues.append(Issue(Scenario.INVALID_DATA, Severity.ERROR,
                            f"Acquisition channel must be 340B or GPO, got '{req.channel}'."))

    if issues:
        raise RequestValidationError(issues)

    normalized = RecommendationRequest(
        request_id=req.request_id, drug_input=req.drug_input, region=region,
        payer=payer, channel=channel, user_email=req.user_email,
    )
    seen: set[str] = set()
    candidates: list[Candidate] = []
    for r in matches:
        code = _clean(r.get("qCode")).upper()
        if code and code not in seen:
            seen.add(code)
            candidates.append(Candidate(product=_clean(r.get("biosimilar")), q_code=code))
    return normalized, candidates


# --------------------------------------------------------------------------- #
# Step 3: price candidates (cost, reimbursement, margin)
# --------------------------------------------------------------------------- #

def price_candidates(
    req: RecommendationRequest,
    candidates: list[Candidate],
    lookups: Lookups,
) -> tuple[list[PricedCandidate], list[Issue]]:
    priced: list[PricedCandidate] = []
    issues: list[Issue] = []
    non_reimbursed = req.payer in NON_REIMBURSED_PAYERS

    for c in candidates:
        cost = lookups.cost.get((c.q_code, req.channel))
        if cost is None:
            issues.append(Issue(Scenario.COST_MISSING, Severity.WARNING,
                                f"{c.product} excluded: no {req.channel} acquisition cost found.",
                                product=c.product))
            continue

        if non_reimbursed:
            priced.append(PricedCandidate(c.product, c.q_code, cost, None, None))
            continue

        if req.payer == MEDICARE:
            rate = lookups.asp.get(c.q_code)
            source = "CMS ASP payment limit"
        else:
            rate = lookups.commercial.get((req.payer.lower(), req.region.lower(), c.q_code))
            source = f"{req.payer} rate for {req.region}"
        if rate is None:
            issues.append(Issue(Scenario.REIMBURSEMENT_MISSING, Severity.WARNING,
                                f"{c.product} excluded: no {source} found.",
                                product=c.product))
            continue

        priced.append(PricedCandidate(c.product, c.q_code, cost, rate, rate - cost))

    return priced, issues


# --------------------------------------------------------------------------- #
# Step 4: apply recommendation rules
# --------------------------------------------------------------------------- #

@dataclass
class RuleOutcome:
    status: Status
    rule: Rule | None
    recommended: list[PricedCandidate]  # 1 item, or several when user must choose
    ranked: list[PricedCandidate]
    issues: list[Issue]


def apply_rules(req: RecommendationRequest, priced: list[PricedCandidate]) -> RuleOutcome:
    issues: list[Issue] = []
    if not priced:
        return RuleOutcome(Status.NO_RECOMMENDATION, None, [], [], issues)

    if req.payer in NON_REIMBURSED_PAYERS:
        ranked = sorted(priced, key=lambda p: (p.acquisition_cost, p.product))
        best_cost = ranked[0].acquisition_cost
        top = [p for p in ranked if p.acquisition_cost == best_cost]
        rule = Rule.CHARITY_UNINSURED
        if len(top) > 1:
            rule = Rule.USER_CHOICE_REQUIRED
            issues.append(Issue(Scenario.EQUAL_MARGINS, Severity.INFO,
                                "Multiple products share the lowest acquisition cost; "
                                "please choose one.", details=", ".join(p.product for p in top)))
    else:
        # Highest margin first; ties broken by lower acquisition cost.
        ranked = sorted(priced, key=lambda p: (-p.margin, p.acquisition_cost, p.product))
        best_margin = ranked[0].margin
        tied = [p for p in ranked if p.margin == best_margin]
        rule = Rule.STANDARD
        top = [ranked[0]]
        if len(tied) > 1:
            lowest_cost = min(p.acquisition_cost for p in tied)
            top = [p for p in tied if p.acquisition_cost == lowest_cost]
            names = ", ".join(p.product for p in tied)
            if len(top) == 1:
                rule = Rule.TIE_BREAK
                issues.append(Issue(Scenario.EQUAL_MARGINS, Severity.INFO,
                                    f"Equal margins ({names}); recommended the one with the "
                                    "lower acquisition cost.", details=names))
            else:
                rule = Rule.USER_CHOICE_REQUIRED
                issues.append(Issue(Scenario.EQUAL_MARGINS, Severity.INFO,
                                    f"Equal margins and equal acquisition cost ({names}); "
                                    "please choose one.", details=names))

    for i, p in enumerate(ranked, start=1):
        p.rank = i
    return RuleOutcome(Status.COMPLETED, rule, top, ranked, issues)


# --------------------------------------------------------------------------- #
# Rationale (template fallback; Phase 3 lets the LLM rephrase these facts)
# --------------------------------------------------------------------------- #

def default_rationale(req: RecommendationRequest, outcome: RuleOutcome) -> str:
    if outcome.status != Status.COMPLETED:
        return "No recommendation could be made because no biosimilar had complete cost and reimbursement data."
    if outcome.rule == Rule.USER_CHOICE_REQUIRED:
        names = ", ".join(p.product for p in outcome.recommended)
        return f"{names} are equally ranked for {req.payer} ({req.channel}); please choose one."
    best = outcome.recommended[0]
    others = len(outcome.ranked) - 1
    if outcome.rule == Rule.CHARITY_UNINSURED:
        return (f"{best.product} is recommended because {req.payer} patients are not reimbursed, "
                f"so the lowest acquisition cost applies (${_money_str(best.acquisition_cost)} "
                f"per unit via {req.channel}), compared against {others} other option(s).")
    text = (f"{best.product} is recommended with the highest margin of "
            f"${_money_str(best.margin)} per unit for {req.payer} in {req.region} via "
            f"{req.channel} (reimbursement ${_money_str(best.reimbursement)} minus acquisition "
            f"cost ${_money_str(best.acquisition_cost)}), compared against {others} other option(s).")
    if outcome.rule == Rule.TIE_BREAK:
        text += " It tied on margin and was chosen for its lower acquisition cost."
    return text


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_recommendation(
    req: RecommendationRequest,
    drug_master_rows: list[dict],
    cost_rows: list[dict],
    asp_rows: list[dict],
    commercial_rows: list[dict],
    file_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Full pipeline. Returns the agent output contract as a JSON-friendly dict."""
    try:
        normalized, candidates = validate_request(
            req, drug_master_rows, valid_payers(commercial_rows))
    except RequestValidationError as exc:
        return _result(req, Status.VALIDATION_ERROR, None, [], [], exc.issues, file_versions,
                       rationale="Please correct the highlighted inputs and resubmit.")

    lookups = build_lookups(cost_rows, asp_rows, commercial_rows)
    priced, price_issues = price_candidates(normalized, candidates, lookups)
    outcome = apply_rules(normalized, priced)
    excluded = [i.product for i in price_issues if i.product]
    issues = lookups.issues + price_issues + outcome.issues
    return _result(normalized, outcome.status, outcome.rule, outcome.recommended,
                   outcome.ranked, issues, file_versions,
                   rationale=default_rationale(normalized, outcome), excluded=excluded)


def _result(req, status, rule, recommended, ranked, issues, file_versions,
            rationale="", excluded=None) -> dict[str, Any]:
    return {
        "requestId": req.request_id,
        "status": status.value,
        "ruleApplied": rule.value if rule else None,
        "recommendation": recommended[0].to_dict() if len(recommended) == 1 else None,
        "options": [p.to_dict() for p in recommended] if len(recommended) > 1 else [],
        "candidates": [p.to_dict() for p in ranked],
        "excluded": excluded or [],
        "rationale": rationale,
        "warnings": [i.message for i in issues
                     if i.severity in (Severity.WARNING, Severity.ERROR)],
        "issues": [i.to_dict() for i in issues],
        "inputs": {"drug": req.drug_input, "region": req.region, "payer": req.payer,
                   "channel": req.channel, "userEmail": req.user_email},
        "fileVersionsUsed": file_versions or {},
    }


def with_retry(
    fn: Callable[[], Any],
    delays: Iterable[float] = RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> Any:
    """Call fn; on FileNotFoundError retry after each delay, then raise DataFileNotFoundError."""
    delays = list(delays)
    for attempt in range(len(delays) + 1):
        try:
            return fn()
        except FileNotFoundError as exc:
            if attempt == len(delays):
                raise DataFileNotFoundError(
                    f"Data file not found after {len(delays)} retries: {exc}") from exc
            if on_retry:
                on_retry(attempt + 1, exc)
            sleep(delays[attempt])


def safe_run(
    req: RecommendationRequest,
    load_data: Callable[[], dict[str, Any]],
    retry_delays: Iterable[float] = RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> dict[str, Any]:
    """
    Top-level entry point for the agent.
    load_data() returns {drug_master, cost, asp, commercial, file_versions} and may raise.
    File-not-found -> Error; anything unexpected -> Critical + Escalated (Phase 3 creates the
    Action Center task when status == Escalated).
    """
    try:
        data = with_retry(load_data, retry_delays, sleep, on_retry)
        return run_recommendation(req, data["drug_master"], data["cost"], data["asp"],
                                  data["commercial"], data.get("file_versions"))
    except DataFileNotFoundError as exc:
        issue = Issue(Scenario.FILE_NOT_FOUND, Severity.ERROR, str(exc))
        return _result(req, Status.ESCALATED, None, [], [], [issue], None,
                       rationale="Pricing data is temporarily unavailable. The issue has been "
                                 "logged and escalated.")
    except Exception as exc:  # noqa: BLE001 - last-resort handler by design
        issue = Issue(Scenario.UNEXPECTED, Severity.CRITICAL,
                      f"Unexpected error: {type(exc).__name__}: {exc}",
                      details=traceback.format_exc())
        return _result(req, Status.ESCALATED, None, [], [], [issue], None,
                       rationale="Something went wrong while processing this request. "
                                 "It has been logged and escalated.")
