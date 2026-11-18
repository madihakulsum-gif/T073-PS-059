"""End-to-end tests: run the whole sample pack and check the planted scenarios."""
import os
from decimal import Decimal as D

from engine.engine import Engine

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "sample_data")


def run(strict=True):
    e = Engine(DATA, strict_chain=strict)
    e.run(os.path.join(DATA, "commands.txt"))
    return e


E = run()
LINES = {r["line_id"]: r for r in E.all_line_results()}


def stmt(trip):
    return E.statement(E.trips[trip])


def test_ec03_lines_119_120():
    assert (LINES["LN-0119"]["status"], LINES["LN-0119"]["allowed_amount"]) == ("ALLOWED", D("4500.00"))
    assert (LINES["LN-0120"]["status"], LINES["LN-0120"]["allowed_amount"]) == ("CAPPED", D("4500.00"))


def test_ec04_meal_on_departure_day_is_25_percent():
    row = next(r for r in E.per_diem_rows(E.trips["TRP-012"]) if r["day_type"] == "DEPARTURE")
    assert row["per_diem_inr"] == D("375.00")


def test_ec05_day15_vs_day16():
    assert stmt("TRP-020")["penalty"] == D("0.00")
    assert stmt("TRP-021")["penalty"] == D("1036.10")          # 10% of 10,361


def test_ec06_advance_exactly_one_lakh():
    assert E.trips["TRP-023"].advance_paid == D(100000)


def test_ec07_recovery_is_capped_at_25_percent_of_basic_and_rolls_forward():
    rows = [r for r in E.recovery_rows if r["trip_id"] == "TRP-002"]
    assert all(r["cap"] == D("7500.00") for r in rows)
    assert all(r["recovered"] == D("7500.00") for r in rows)
    assert rows[0]["carried_forward"] == rows[0]["scheduled"] - D("7500.00")
    assert E.trips["TRP-002"].status == "CLAIM_REJECTED"


def test_ec08_los_angeles_trip_uses_departure_date_fx_rate():
    rows = E.per_diem_rows(E.trips["TRP-025"])
    assert all(r["fx_rate"] == D("82.7200") for r in rows)
    assert rows[-1]["day_date"].isoformat() == "2026-10-20"


def test_trp026_exception_covers_only_the_named_line():
    assert LINES["LN-0226"]["status"] == "ALLOWED"
    assert LINES["LN-0231"]["reason_code"] == "MODE_AIR_NOT_ALLOWED"


def test_trp026_no_gst_override_and_taxi_cap():
    assert LINES["LN-0227"]["reason_code"] == "NO_GST_INVOICE"
    assert LINES["LN-0229"]["override_amount"] == D("130.00")
    assert LINES["LN-0230"]["allowed_amount"] == D("1500.00")


def test_airfare_over_10000_without_payment_proof_disallowed():
    assert LINES["LN-0283"]["reason_code"] == "NO_PAYMENT_PROOF"


def test_trp030_second_advance_is_blocked_while_trp002_unsettled():
    assert E.trips["TRP-030"].advance_blocked and E.trips["TRP-030"].advance_paid == 0


def test_trp034_cancel_late_refund_starts_recovery_and_charge_capped_at_50_percent():
    assert LINES["LN-0288"]["allowed_amount"] == D("1575.00")
    assert any(r["trip_id"] == "TRP-034" for r in E.recovery_rows)


def test_trp006_unapproved_extension_gives_zero_per_diem_and_no_lodging():
    types = [r["day_type"] for r in E.per_diem_rows(E.trips["TRP-006"])]
    assert types.count("EXTENSION_UNAPPROVED") == 3
    assert all(LINES[i]["status"] == "DISALLOWED" for i in ("LN-0064", "LN-0066", "LN-0067"))


def test_trp023_and_trp025_have_the_full_three_step_chain():
    assert stmt("TRP-023")["approval_chain"] == "EMP-010;EMP-005;EMP-004"
    assert stmt("TRP-025")["approval_chain"] == "EMP-007;EMP-002;EMP-004"
    assert stmt("TRP-023")["status"] == "SETTLED"


def test_every_line_gets_exactly_one_result():
    assert len(LINES) == 296
    assert all(r["status"] in ("ALLOWED", "CAPPED", "DISALLOWED") for r in LINES.values())
