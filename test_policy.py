"""Unit tests for the pure rules in engine/policy.py. One test per rule / edge case (EC-01..EC-08)."""
from datetime import datetime, date
from decimal import Decimal as D

from engine import policy as P
from engine.loader import Employee, Department


def T(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def line(cat, amount, receipt=True, proof=False, gst="G1", km=None, cls="", d=date(2026, 9, 1)):
    return {"category": cat, "amount": D(amount), "receipt": receipt, "payment_proof": proof,
            "gst_invoice_no": gst, "km": km, "travel_class": cls, "expense_date": d}


# ---- BR-04/05/06 partial days ------------------------------------------------
def test_ec01_departure_exactly_noon_and_return_exactly_2pm_are_half_days():      # BR-04, BR-05
    plan = P.day_plan(T("2026-09-08 12:00:00"), T("2026-09-10 14:00:00"), 330, 330)
    assert [p[2] for p in plan] == [D(50), D(100), D(50)]


def test_departure_before_noon_and_return_after_2pm_are_full_days():
    plan = P.day_plan(T("2026-09-08 11:59:00"), T("2026-09-10 14:01:00"), 330, 330)
    assert [p[2] for p in plan] == [D(100), D(100), D(100)]


def test_ec02_single_day_exactly_8_hours_pays_half():                              # BR-06
    plan = P.day_plan(T("2026-09-15 07:30:00"), T("2026-09-15 15:30:00"), 330, 330)
    assert plan == [(date(2026, 9, 15), "SINGLE_DAY", D(50))]


def test_single_day_under_8_hours_pays_nothing():
    plan = P.day_plan(T("2026-09-15 07:30:00"), T("2026-09-15 15:29:00"), 330, 330)
    assert plan[0][2] == D(0)


def test_ec08_date_line_return_date_is_earlier_in_local_time():                    # BR-17
    # Los Angeles UTC-07:00 = -420 minutes. 21 Oct 12:00 IST = 20 Oct 23:30 local.
    plan = P.day_plan(T("2026-10-14 02:15:00"), T("2026-10-21 12:00:00"), 330, -420)
    assert plan[-1][0] == date(2026, 10, 20) and plan[-1][1] == "RETURN"
    assert plan[-1][2] == D(100)           # 23:30 local is after 14:00
    assert len(plan) == 7


# ---- BR-03 / BR-08 per-diem amount ------------------------------------------
def test_per_diem_rates_by_class_and_grade():
    assert P.base_rate("A", "G1") == (D(1500), "INR")
    assert P.base_rate("B", "G4") == (D(2000), "INR")
    assert P.base_rate("C", "G6") == (D(2500), "INR")
    assert P.base_rate("INTL", "G5") == (D(120), "USD")


def test_international_per_diem_converted_and_rounded_to_paise():
    assert P.per_diem_amount(D(120), D("82.7200"), D(100)) == D("9926.40")


def test_ec04_meals_on_departure_half_day_is_25_percent():                           # BR-08
    assert P.per_diem_amount(D(1500), D(1), D(50), meal_pct=D(50)) == D("375.00")


def test_meal_and_breakfast_reductions_stack_multiplicatively():                    # BR-08
    # 1200 * 50% * 90% = 540 (not 1200 * 40% = 480)
    assert P.per_diem_amount(D(1200), D(1), D(100), D(50), D(10)) == D("540.00")


# ---- BR-07 lodging -----------------------------------------------------------
def test_ec03_lodging_exactly_at_cap_allowed_one_rupee_above_capped():
    ok = P.evaluate_line(line("LODGING", "4500.00"), "G3", 100, "B")
    capped = P.evaluate_line(line("LODGING", "4501.00"), "G3", 100, "B")
    assert ok == ("ALLOWED", "OK", D("4500.00"))
    assert capped == ("CAPPED", "CAP_LODGING", D("4500"))


def test_lodging_without_gst_invoice_is_disallowed():
    assert P.evaluate_line(line("LODGING", "2100", gst=""), "G2", 100, "A")[:2] == \
        ("DISALLOWED", "NO_GST_INVOICE")


def test_international_lodging_is_never_claimed():
    assert P.evaluate_line(line("LODGING", "5000"), "G5", 9000, "INTL")[0] == "DISALLOWED"


# ---- BR-09 mode rules --------------------------------------------------------
def test_air_needs_g3_or_distance_over_500():
    air = line("AIRFARE", "5200")
    assert P.evaluate_line(air, "G2", 500, "C")[:2] == ("DISALLOWED", "MODE_AIR_NOT_ALLOWED")
    assert P.evaluate_line(air, "G2", 501, "C")[0] == "ALLOWED"
    assert P.evaluate_line(air, "G3", 100, "C")[0] == "ALLOWED"


def test_manager_exception_waives_mode_rule():
    air = line("AIRFARE", "5200")
    assert P.evaluate_line(air, "G2", 500, "C", has_exception=True)[0] == "ALLOWED"


def test_train_class_g1_g2_ac2_only():
    assert P.evaluate_line(line("TRAIN", "900", cls="AC-1"), "G1", 300, "B")[0] == "DISALLOWED"
    assert P.evaluate_line(line("TRAIN", "900", cls="AC-2"), "G1", 300, "B")[0] == "ALLOWED"
    assert P.evaluate_line(line("TRAIN", "900", cls="AC-1"), "G3", 300, "B")[0] == "ALLOWED"


# ---- BR-10 taxi --------------------------------------------------------------
def test_taxi_with_km_capped_at_18_per_km():
    assert P.evaluate_line(line("TAXI", "600", km=30), "G3", 0, "A") == ("CAPPED", "CAP_TAXI_KM", D("540.00"))
    assert P.evaluate_line(line("TAXI", "540", km=30), "G3", 0, "A")[0] == "ALLOWED"


def test_taxi_flat_without_km_capped_at_1500_per_day():
    assert P.evaluate_line(line("TAXI", "1800"), "G3", 0, "A") == ("CAPPED", "CAP_TAXI_FLAT", D(1500))
    assert P.evaluate_line(line("TAXI", "1500"), "G3", 0, "A")[0] == "ALLOWED"


# ---- BR-13 receipts ----------------------------------------------------------
def test_receipt_needed_above_500():
    assert P.evaluate_line(line("OTHER", "500", receipt=False), "G3", 0, "A")[0] == "ALLOWED"
    assert P.evaluate_line(line("OTHER", "650", receipt=False), "G3", 0, "A")[:2] == ("DISALLOWED", "NO_RECEIPT")


def test_payment_proof_needed_above_10000():
    assert P.evaluate_line(line("AIRFARE", "12450"), "G3", 0, "A")[:2] == ("DISALLOWED", "NO_PAYMENT_PROOF")
    assert P.evaluate_line(line("AIRFARE", "12450", proof=True), "G3", 0, "A")[0] == "ALLOWED"
    assert P.evaluate_line(line("AIRFARE", "10000"), "G3", 0, "A")[0] == "ALLOWED"


# ---- BR-12 override + approval chain ----------------------------------------
def test_override_max_20_percent_of_line():
    assert P.override_amount(D(650), D(130)) == D("130.00")
    assert P.override_amount(D(650), D(500)) == D("130.00")     # clipped to 20%


def _people():
    emps = {"M": Employee("M", "m", "G4", "D1", "C", "H", "MANAGER", D(1)),
            "H": Employee("H", "h", "G6", "D1", "C", "", "DEPT_HEAD", D(1)),
            "F": Employee("F", "f", "G6", "D2", "C", "", "FINANCE_HEAD", D(1)),
            "E": Employee("E", "e", "G2", "D1", "C", "M", "EMPLOYEE", D(1))}
    return emps, {"D1": Department("D1", "x", "H")}


def test_approval_chain_by_allowed_amount():
    emps, deps = _people()
    e = emps["E"]
    assert P.approval_chain(e, emps, deps, D(25000)) == ["M"]
    assert P.approval_chain(e, emps, deps, D("25000.01")) == ["M", "H"]
    assert P.approval_chain(e, emps, deps, D(100000)) == ["M", "H"]
    assert P.approval_chain(e, emps, deps, D("100000.01")) == ["M", "H", "F"]


def test_department_head_claims_go_to_finance_head():
    emps, deps = _people()
    assert P.approval_chain(emps["H"], emps, deps, D(500000)) == ["F"]


# ---- BR-11 advance, BR-14 late claims ---------------------------------------
def test_advance_is_80_percent():
    assert P.advance_amount(D(50000)) == D("40000.00")


def test_ec06_advance_never_above_one_lakh():
    assert P.advance_amount(D(500000)) == D(100000)
    assert P.advance_amount(D(125000)) == D(100000)       # 80% = exactly 1,00,000


def test_ec05_day_15_no_penalty_day_16_penalty_day_31_rejected():
    assert P.late_status(15) == "ON_TIME"
    assert P.late_status(16) == "PENALTY"
    assert P.late_status(30) == "PENALTY"
    assert P.late_status(31) == "REJECTED"
