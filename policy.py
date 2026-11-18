"""Pure business-rule functions (no state). Every function names the BR-ID it implements.

Money uses Decimal, never float. Rounding is always ROUND_HALF_UP to 2 decimals (paise).
"""
from datetime import timedelta, time
from decimal import Decimal as D, ROUND_HALF_UP

IST_OFFSET = 330  # minutes

# BR-03 per-diem Rs/day by city class -> (G1-G2, G3-G4, G5-G6)
PER_DIEM_INR = {"A": (D(1500), D(2500), D(4000)),
                "B": (D(1200), D(2000), D(3200)),
                "C": (D(900), D(1500), D(2500))}
PER_DIEM_USD = (D(50), D(75), D(120))            # international
# BR-07 lodging cap per night
LODGING_CAP = {"A": (D(3500), D(6000), D(10000)),
               "B": (D(2500), D(4500), D(7500)),
               "C": (D(2000), D(3500), D(5500))}

TAXI_PER_KM = D(18)
TAXI_FLAT_DAY = D(1500)
RECEIPT_ABOVE = D(500)
PROOF_ABOVE = D(10000)
ADVANCE_PCT = D("0.80")
ADVANCE_MAX = D(100000)
OVERRIDE_MAX_PCT = D("0.20")
CANCEL_REIMBURSE_PCT = D("0.50")


def money(x):
    return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def grade_group(grade):
    """G1-G2 -> 0, G3-G4 -> 1, G5-G6 -> 2."""
    n = int(grade[1:])
    return 0 if n <= 2 else 1 if n <= 4 else 2


def local_dt(ts, utc_offset_minutes):
    """README: local time = IST - 330 + city offset."""
    return ts - timedelta(minutes=IST_OFFSET) + timedelta(minutes=utc_offset_minutes)


# ---------------------------------------------------------------- per-diem
def day_plan(dep_ts, ret_ts, from_offset, to_offset, orig_ret_ts=None, ext_unapproved=False):
    """BR-04/BR-05/BR-06/BR-17: list of (date, day_type, pct) for every trip day.

    * departure day 100% if leaving BEFORE 12:00 else 50%      (exactly 12:00 -> 50%)
    * return day 100% if returning AFTER 14:00 else 50%        (exactly 14:00 -> 50%)
    * same-date trip: 50% if absence >= 8h else 0 (SINGLE_DAY)
    * return date is taken in the destination's local time (date-line case)
    * unapproved extension days (> 2 days, no re-approval) pay 0
    """
    dep_local = local_dt(dep_ts, from_offset)
    ret_local = local_dt(ret_ts, to_offset)
    d0, d1 = dep_local.date(), ret_local.date()
    if d1 < d0:
        raise ValueError("return date is before departure date")
    if d0 == d1:
        hours = D((ret_ts - dep_ts).total_seconds()) / D(3600)
        return [(d0, "SINGLE_DAY", D(50) if hours >= 8 else D(0))]

    orig_date = local_dt(orig_ret_ts, to_offset).date() if (ext_unapproved and orig_ret_ts) else None
    plan, d = [], d0
    while d <= d1:
        if d == d0:
            day_type, pct = "DEPARTURE", (D(100) if dep_local.time() < time(12, 0) else D(50))
        elif d == d1:
            day_type, pct = "RETURN", (D(100) if ret_local.time() > time(14, 0) else D(50))
        else:
            day_type, pct = "FULL", D(100)
        if orig_date is not None and d != d0:
            if d > orig_date:
                day_type, pct = "EXTENSION_UNAPPROVED", D(0)
            elif d == orig_date and day_type == "RETURN":
                day_type, pct = "FULL", D(100)
        plan.append((d, day_type, pct))
        d += timedelta(days=1)
    return plan


def base_rate(city_class, grade):
    """BR-03: returns (base_rate, currency)."""
    g = grade_group(grade)
    if city_class == "INTL":
        return PER_DIEM_USD[g], "USD"
    return PER_DIEM_INR[city_class][g], "INR"


def per_diem_amount(base, fx, pct, meal_pct=D(0), breakfast_pct=D(0)):
    """BR-08: reductions stack MULTIPLICATIVELY, rounded to paise once per day."""
    amt = D(base) * D(fx) * (pct / D(100))
    amt = amt * (D(1) - meal_pct / D(100)) * (D(1) - breakfast_pct / D(100))
    return money(amt)


def lodging_cap(city_class, grade):
    return LODGING_CAP[city_class][grade_group(grade)]


# ---------------------------------------------------------------- claim lines
def receipt_problem(amount, receipt, proof):
    """BR-13: > Rs 500 needs receipt; > Rs 10,000 needs receipt AND payment proof."""
    if amount > RECEIPT_ABOVE and not receipt:
        return "NO_RECEIPT"
    if amount > PROOF_ABOVE and not proof:
        return "NO_PAYMENT_PROOF"
    return None


def mode_problem(line, grade, distance_km):
    """BR-09: air only for G3+ or distance > 500 km; train AC-2 for G1-G2, AC-1 for G3+.
    Returns a reason code or None."""
    n = int(grade[1:])
    if line["category"] == "AIRFARE":
        if not (n >= 3 or distance_km > 500):
            return "MODE_AIR_NOT_ALLOWED"
    elif line["category"] == "TRAIN":
        if n <= 2 and line.get("travel_class") != "AC-2":
            return "MODE_TRAIN_CLASS"
    return None


def evaluate_line(line, grade, distance_km, dest_class, *, has_exception=False,
                  ext_unapproved_dates=(), flat_taxi_used=D(0),
                  trip_cancelled=False, cancel_reason=None):
    """Return (status, reason_code, allowed_amount) for one claim line.

    Order applied: documents (GST / receipt / proof) -> mode rule (waived by manager
    exception) -> extension rule -> cap.  First failing check gives the reason.
    """
    amt, cat = line["amount"], line["category"]

    def no(reason):
        return "DISALLOWED", reason, D(0)

    if cat == "LODGING":                                            # BR-07
        if dest_class == "INTL":
            return no("INTL_LODGING_COMPANY_BOOKED")
        if not line.get("gst_invoice_no"):
            return no("NO_GST_INVOICE")
    prob = receipt_problem(amt, line["receipt"], line["payment_proof"])
    if prob:
        return no(prob)
    if cat in ("AIRFARE", "TRAIN"):
        if trip_cancelled and cat == "AIRFARE":                     # BR-15
            if cancel_reason != "BUSINESS":
                return no("CANCEL_PERSONAL")
            return "CAPPED", "CAP_CANCEL_50", money(amt * CANCEL_REIMBURSE_PCT)
        mp = mode_problem(line, grade, distance_km)
        if mp and not has_exception:
            return no(mp)
        return "ALLOWED", "OK", amt
    if cat == "LODGING":
        if line["expense_date"] in ext_unapproved_dates:            # BR-16
            return no("EXTENSION_UNAPPROVED")
        cap = lodging_cap(dest_class, grade)
        if amt > cap:
            return "CAPPED", "CAP_LODGING", cap
        return "ALLOWED", "OK", amt
    if cat == "TAXI":                                               # BR-10
        if line.get("km"):
            cap = TAXI_PER_KM * line["km"]
            if amt > cap:
                return "CAPPED", "CAP_TAXI_KM", money(cap)
            return "ALLOWED", "OK", amt
        room = max(D(0), TAXI_FLAT_DAY - flat_taxi_used)
        if amt > room:
            return "CAPPED", "CAP_TAXI_FLAT", room
        return "ALLOWED", "OK", amt
    return "ALLOWED", "OK", amt                                     # MEALS, LAUNDRY, OTHER


def override_amount(line_amount, requested):
    """BR-12: finance head may override a DISALLOWED line, at most 20% of the line."""
    return money(min(D(requested), line_amount * OVERRIDE_MAX_PCT))


# ---------------------------------------------------------------- approvals
def approval_chain(emp, employees, departments, allowed_amount):
    """BR-12: <= 25,000 manager; <= 1,00,000 manager + dept head; above: + finance head.
    Department heads (no manager) go straight to the finance head."""
    fin = next((e.employee_id for e in employees.values() if e.role == "FINANCE_HEAD"), None)
    if not emp.manager_id:
        return [fin]
    chain = [emp.manager_id]
    if allowed_amount > D(25000):
        chain.append(departments[emp.department_id].head_employee_id)
    if allowed_amount > D(100000):
        chain.append(fin)
    out = []
    for a in chain:                      # remove duplicates, keep order
        if a not in out:
            out.append(a)
    return out


# ---------------------------------------------------------------- advance / late claim
def advance_amount(estimated):
    """BR-11: 80% of the estimate, at most Rs 1,00,000."""
    return min(money(estimated * ADVANCE_PCT), ADVANCE_MAX)


def late_status(days_after_return):
    """BR-14: <=15 days OK; 16-30 -> 10% penalty; >30 -> auto-reject."""
    if days_after_return <= 15:
        return "ON_TIME"
    if days_after_return <= 30:
        return "PENALTY"
    return "REJECTED"
