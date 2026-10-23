"""Tests for the rules engine. Run from /agent with:  uv run pytest -v"""

from decimal import Decimal
from pathlib import Path

from data_loader import load_local_data
from rules_engine import (
    RecommendationRequest, RequestValidationError, DataFileNotFoundError,
    build_lookups, parse_money, run_recommendation, safe_run, validate_request,
    valid_payers, with_retry,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# --------------------------------------------------------------------------- #
# Small in-memory dataset
# --------------------------------------------------------------------------- #

DRUG_MASTER = [
    {"brand": "Neulasta", "jCode": "J2506", "biosimilar": "Alpha", "qCode": "Q0001", "active": "true"},
    {"brand": "Neulasta", "jCode": "J2506", "biosimilar": "Beta", "qCode": "Q0002", "active": "true"},
    {"brand": "Neulasta", "jCode": "J2506", "biosimilar": "Gamma", "qCode": "Q0003", "active": "true"},
    {"brand": "Neulasta", "jCode": "J2506", "biosimilar": "Retired", "qCode": "Q0009", "active": "false"},
]


def cost(code, channel, value):
    return {"hcpcs": code, "channel": channel, "cost_per_unit": value, "product": code}


def asp(code, value):
    return {"hcpcs": code, "payment_limit": value}


def com(payer, region, code, value):
    return {"payer": payer, "region": region, "hcpcs": code, "reimbursement_per_unit": value}


COST = [cost("Q0001", "GPO", 50), cost("Q0002", "GPO", 40), cost("Q0003", "GPO", 45),
        cost("Q0001", "340B", 30), cost("Q0002", "340B", 25)]
ASP = [asp("Q0001", 60), asp("Q0002", 55), asp("Q0003", 52)]
COM = [com("Aetna", "South", "Q0001", 70), com("Aetna", "South", "Q0002", 58),
       com("Aetna", "South", "Q0003", 61)]


def req(payer="Medicare", channel="GPO", drug="Neulasta", region="South"):
    return RecommendationRequest("REQ-1", drug, region, payer, channel, "user@test.com")


def run(r, cost_rows=COST, asp_rows=ASP, com_rows=COM, dm=DRUG_MASTER):
    return run_recommendation(r, dm, cost_rows, asp_rows, com_rows)


def scenarios(result):
    return [i["scenario"] for i in result["issues"]]


# --------------------------------------------------------------------------- #
# Core rules
# --------------------------------------------------------------------------- #

def test_standard_rule_medicare_highest_margin():
    # Medicare margins: Alpha 10, Beta 15, Gamma 7
    out = run(req("Medicare"))
    assert out["status"] == "Completed"
    assert out["ruleApplied"] == "Standard"
    assert out["recommendation"]["product"] == "Beta"
    assert out["recommendation"]["margin"] == "15.00"
    assert [c["product"] for c in out["candidates"]] == ["Beta", "Alpha", "Gamma"]


def test_standard_rule_commercial_uses_payer_region_rate():
    # Aetna South margins: Alpha 20, Beta 18, Gamma 16
    out = run(req("Aetna"))
    assert out["recommendation"]["product"] == "Alpha"
    assert out["recommendation"]["reimbursement"] == "70.00"


def test_charity_recommends_lowest_cost_ignoring_margin():
    out = run(req("Charity"))
    assert out["ruleApplied"] == "CharityUninsured"
    assert out["recommendation"]["product"] == "Beta"
    assert out["recommendation"]["margin"] is None


def test_uninsured_case_insensitive():
    out = run(req("uninsured"))
    assert out["ruleApplied"] == "CharityUninsured"
    assert out["inputs"]["payer"] == "Uninsured"


def test_jcode_input_works_and_inactive_excluded():
    out = run(req(drug="j2506"))
    assert "Retired" not in [c["product"] for c in out["candidates"]]
    assert len(out["candidates"]) == 3


def test_tie_break_lower_cost_wins():
    # Alpha margin 70-50=20, Beta 60-40=20 -> Beta (lower cost)
    com_rows = [com("Aetna", "South", "Q0001", 70), com("Aetna", "South", "Q0002", 60),
                com("Aetna", "South", "Q0003", 50)]
    out = run(req("Aetna"), com_rows=com_rows)
    assert out["ruleApplied"] == "TieBreak"
    assert out["recommendation"]["product"] == "Beta"
    assert "EqualMargins" in scenarios(out)


def test_full_tie_requires_user_choice():
    cost_rows = [cost("Q0001", "GPO", 40), cost("Q0002", "GPO", 40), cost("Q0003", "GPO", 45)]
    com_rows = [com("Aetna", "South", "Q0001", 60), com("Aetna", "South", "Q0002", 60),
                com("Aetna", "South", "Q0003", 50)]
    out = run(req("Aetna"), cost_rows=cost_rows, com_rows=com_rows)
    assert out["ruleApplied"] == "UserChoiceRequired"
    assert out["recommendation"] is None
    assert {o["product"] for o in out["options"]} == {"Alpha", "Beta"}


def test_decimal_precision_no_float_errors():
    assert parse_money("0.1") + parse_money("0.2") == Decimal("0.30")
    assert parse_money("$1,234.567") == Decimal("1234.57")


# --------------------------------------------------------------------------- #
# The 7 exception scenarios
# --------------------------------------------------------------------------- #

def test_exception_cost_missing():
    out = run(req("Medicare", channel="340B"))  # Gamma has no 340B cost
    assert "CostMissing" in scenarios(out)
    assert "Gamma" in out["excluded"]
    assert out["status"] == "Completed"


def test_exception_reimbursement_missing():
    com_rows = [com("Aetna", "South", "Q0001", 70), com("Aetna", "South", "Q0002", 58)]
    out = run(req("Aetna"), com_rows=com_rows)
    assert "ReimbursementRateMissing" in scenarios(out)
    assert "Gamma" in out["excluded"]


def test_exception_invalid_payer():
    out = run(req("Cigna"))
    assert out["status"] == "ValidationError"
    assert "InvalidPayer" in scenarios(out)
    try:
        validate_request(req("Cigna"), DRUG_MASTER, valid_payers(COM))
        assert False, "expected RequestValidationError"
    except RequestValidationError as exc:
        assert exc.issues[0].scenario.value == "InvalidPayer"


def test_exception_equal_margins_logged_as_info():
    com_rows = [com("Aetna", "South", "Q0001", 70), com("Aetna", "South", "Q0002", 60)]
    out = run(req("Aetna"), com_rows=com_rows)
    eq = [i for i in out["issues"] if i["scenario"] == "EqualMargins"]
    assert eq and eq[0]["severity"] == "Info"


def test_exception_file_not_found_retries_then_escalates():
    calls, sleeps = [], []

    def missing():
        calls.append(1)
        raise FileNotFoundError("pharmacy_acquisition_cost.xlsx")

    try:
        with_retry(missing, sleep=sleeps.append)
        assert False, "expected DataFileNotFoundError"
    except DataFileNotFoundError:
        pass
    assert len(calls) == 4          # 1 attempt + 3 retries
    assert sleeps == [2, 4, 8]      # backoff

    out = safe_run(req(), missing, sleep=lambda s: None)
    assert out["status"] == "Escalated"
    assert "FileNotFound" in scenarios(out)


def test_file_found_on_second_attempt():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 2:
            raise FileNotFoundError("x")
        return "ok"

    assert with_retry(flaky, sleep=lambda s: None) == "ok"


def test_exception_invalid_data_rows_skipped():
    cost_rows = COST + [cost("Q0003", "340B", -5), cost("BAD", "GPO", 10),
                        cost("Q0003", "XYZ", 10), {"hcpcs": "Q0001", "channel": "GPO",
                                                   "cost_per_unit": "abc"}]
    lookups = build_lookups(cost_rows, ASP, COM)
    assert len([i for i in lookups.issues if i.scenario.value == "InvalidData"]) == 4
    assert ("Q0003", "340B") not in lookups.cost
    assert lookups.cost[("Q0001", "GPO")] == Decimal("50.00")  # valid row kept


def test_invalid_input_drug_and_channel():
    out = run(req(drug="Aspirin", channel="Retail"))
    assert out["status"] == "ValidationError"
    assert scenarios(out).count("InvalidData") == 2


def test_exception_unexpected_is_escalated_with_trace():
    def broken():
        raise KeyError("boom")

    out = safe_run(req(), broken)
    assert out["status"] == "Escalated"
    issue = out["issues"][0]
    assert issue["scenario"] == "UnexpectedException"
    assert issue["severity"] == "Critical"
    assert "Traceback" in issue["details"]


def test_no_recommendation_when_all_excluded():
    out = run(req("Medicare"), asp_rows=[])
    assert out["status"] == "NoRecommendation"
    assert len(out["excluded"]) == 3


# --------------------------------------------------------------------------- #
# Demo scenarios against the mock files in /data
# --------------------------------------------------------------------------- #

def demo(payer, channel, region="South"):
    data = load_local_data(DATA_DIR)
    r = RecommendationRequest("DEMO", "Neulasta", region, payer, channel, "demo@test.com")
    return run_recommendation(r, data["drug_master"], data["cost"], data["asp"],
                              data["commercial"], data["file_versions"])


def test_demo_1_medicare_gpo_standard():
    out = demo("Medicare", "GPO")
    assert out["recommendation"]["product"] == "Udenyca"
    assert out["recommendation"]["margin"] == "12.75"
    assert scenarios(out).count("InvalidData") == 2  # two bad rows in cost file


def test_demo_2_charity_340b_lowest_cost():
    out = demo("Charity", "340B")
    assert out["recommendation"]["product"] == "Nyvepria"
    assert "Ziextenzo" in out["excluded"]


def test_demo_3_uhc_south_gpo_tie_break():
    out = demo("UnitedHealthcare", "GPO")
    assert out["ruleApplied"] == "TieBreak"
    assert out["recommendation"]["product"] == "Nyvepria"
    assert out["recommendation"]["margin"] == "18.00"


def test_demo_4_aetna_west_missing_rate():
    out = demo("Aetna", "GPO", region="West")
    assert out["recommendation"]["product"] == "Udenyca"
    assert "ReimbursementRateMissing" in scenarios(out)


def test_demo_5_aetna_south_different_winner():
    out = demo("Aetna", "GPO")
    assert out["recommendation"]["product"] == "Fulphila"
