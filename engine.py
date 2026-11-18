"""The claims engine: holds the state and runs commands strictly in file order."""
from datetime import timedelta
from decimal import Decimal as D, ROUND_DOWN

from . import policy as P
from .loader import load_masters, read_commands, ts

SETTLED_LIKE = ("SETTLED", "CANCELLED")
RECOVERY_INSTALMENTS = 3


class Recovery:
    """Money to take back from salary. 'queue' holds instalments not yet due."""

    def __init__(self, trip_id, employee_id, total, instalments):
        self.trip_id, self.employee_id, self.total = trip_id, employee_id, total
        base = (total / instalments).quantize(D("0.01"), rounding=ROUND_DOWN)
        self.queue = [base] * (instalments - 1) + [total - base * (instalments - 1)]
        self.carry = D(0)

    def remaining(self):
        return sum(self.queue, D(0)) + self.carry


class Engine:
    def __init__(self, data_folder, strict_chain=True):
        # strict_chain=True : SETTLE only after the FULL approval chain (BR-12, as in the brief).
        # strict_chain=False: SETTLE allowed once at least one approval is in (see decision D-05).
        self.strict_chain = strict_chain
        (self.emps, self.deps, self.cities, self.fx,
         self.trips, self.lines) = load_masters(data_folder)
        self.lines_by_trip, self.line_by_id = {}, {}
        for ln in self.lines:
            self.lines_by_trip.setdefault(ln["trip_id"], []).append(ln)
            self.line_by_id[ln["line_id"]] = ln
        self.exceptions = set()          # line ids with a manager exception
        self.overrides = {}              # line id -> approved override amount
        self.recoveries = []
        self.recovery_rows, self.statement_rows, self.dept_rows = [], [], []
        self.events, self.errors = [], []
        self.handlers = {
            "APPROVE_TRIP": self.approve_trip, "PAY_ADVANCE": self.pay_advance,
            "MARK_TRAVELLED": self.mark_travelled, "SUBMIT_CLAIM": self.submit_claim,
            "APPROVE_CLAIM": self.approve_claim, "EXCEPTION": self.exception,
            "OVERRIDE": self.override, "EXTEND_TRIP": self.extend_trip,
            "CANCEL_TRIP": self.cancel_trip, "REFUND_ADVANCE": self.refund_advance,
            "SETTLE": self.settle, "PAYROLL_RUN": self.payroll_run,
            "PRINT_CLAIM": self.print_claim, "PRINT_DEPT_REPORT": self.print_dept_report}

    # ------------------------------------------------------------ running
    def run(self, commands_path):
        for line_no, name, args in read_commands(commands_path):
            fn = self.handlers.get(name)
            if fn is None:
                self.errors.append(f"line {line_no}: unknown command {name}")
                continue
            try:
                fn(*args)
            except (ValueError, KeyError, TypeError) as e:
                self.errors.append(f"line {line_no}: {name} failed: {e!r}")
            except Rejected as e:
                self.errors.append(f"line {line_no}: {name} rejected: {e}")

    # ------------------------------------------------------------ helpers
    def _trip(self, trip_id):
        if trip_id not in self.trips:
            raise Rejected(f"unknown trip {trip_id}")
        return self.trips[trip_id]

    def _ctx(self, t):
        return self.emps[t.employee_id], self.cities[t.from_city_id], self.cities[t.to_city_id]

    def _fx(self, t, to_city):
        if to_city.city_class != "INTL":
            return D("1.0000")
        d = t.departure_ts.date()
        usable = [x for x in self.fx if x <= d]
        if not usable:
            raise Rejected("no FX rate on or before departure date")
        return self.fx[max(usable)]

    def _ext_dates(self, t):
        """Nights of an unapproved extension (BR-16)."""
        if not t.ext_unapproved:
            return set()
        to_c = self.cities[t.to_city_id]
        d, last = (P.local_dt(t.approved_return_ts, to_c.utc_offset_minutes).date(),
                   P.local_dt(t.return_ts, to_c.utc_offset_minutes).date())
        out = set()
        while d < last:
            out.add(d)
            d += timedelta(days=1)
        return out

    # ------------------------------------------------------------ per-diem
    def per_diem_rows(self, t, with_reductions=True):
        emp, from_c, to_c = self._ctx(t)
        fx = self._fx(t, to_c)
        base, cur = P.base_rate(to_c.city_class, emp.grade)
        lines = self.lines_by_trip.get(t.trip_id, []) if with_reductions else []
        meal_days = {l["expense_date"] for l in lines if l["category"] == "MEALS"}
        bf_days = {l["expense_date"] for l in lines
                   if l["category"] == "LODGING" and l["breakfast_included"]}
        rows = []
        plan = P.day_plan(t.departure_ts, t.return_ts, from_c.utc_offset_minutes,
                          to_c.utc_offset_minutes, t.approved_return_ts, t.ext_unapproved)
        for day, day_type, pct in plan:
            meal = D(50) if day in meal_days and pct > 0 else D(0)
            bf = D(10) if day in bf_days and pct > 0 else D(0)
            amt = P.per_diem_amount(base, fx, pct, meal, bf)
            rows.append({"trip_id": t.trip_id, "day_date": day, "day_type": day_type,
                         "base_rate": base, "currency": cur, "fx_rate": fx, "day_pct": pct,
                         "meal_reduction": meal, "breakfast_reduction": bf,
                         "per_diem_inr": amt, "status": "ALLOWED" if amt > 0 else "NOT_PAYABLE"})
        return rows

    def estimate(self, t):
        """BR-11: per-diem x days + lodging cap x nights (no lodging for international)."""
        emp, from_c, to_c = self._ctx(t)
        total = sum((r["per_diem_inr"] for r in self.per_diem_rows(t, with_reductions=False)), D(0))
        if to_c.city_class != "INTL":
            nights = (P.local_dt(t.return_ts, to_c.utc_offset_minutes).date()
                      - P.local_dt(t.departure_ts, from_c.utc_offset_minutes).date()).days
            total += P.lodging_cap(to_c.city_class, emp.grade) * nights
        return total

    # ------------------------------------------------------------ claim lines
    def line_results(self, t):
        emp, _, to_c = self._ctx(t)
        ext_dates = self._ext_dates(t)
        flat_used, out = {}, []
        for ln in self.lines_by_trip.get(t.trip_id, []):
            used = flat_used.get(ln["expense_date"], D(0))
            status, reason, allowed = P.evaluate_line(
                ln, emp.grade, t.distance_km, to_c.city_class,
                has_exception=ln["line_id"] in self.exceptions,
                ext_unapproved_dates=ext_dates, flat_taxi_used=used,
                trip_cancelled=(t.status == "CANCELLED"), cancel_reason=t.cancel_reason)
            if ln["category"] == "TAXI" and not ln.get("km"):
                flat_used[ln["expense_date"]] = used + allowed
            ov = self.overrides.get(ln["line_id"], D(0)) if status == "DISALLOWED" else D(0)
            out.append({"line_id": ln["line_id"], "trip_id": t.trip_id, "status": status,
                        "reason_code": reason, "claimed_amount": ln["amount"],
                        "allowed_amount": allowed, "override_amount": ov,
                        "category": ln["category"]})
        return out

    def compute_claim(self, t):
        emp = self.emps[t.employee_id]
        pd_rows = [] if t.status == "CANCELLED" else self.per_diem_rows(t)
        lines = self.line_results(t)
        pd_total = sum((r["per_diem_inr"] for r in pd_rows), D(0))
        lines_total = sum((r["allowed_amount"] + r["override_amount"] for r in lines), D(0))
        gross = pd_total + lines_total
        penalty = D(0)
        if t.submitted_ts and self._days_late(t) is not None \
                and P.late_status(self._days_late(t)) == "PENALTY":
            penalty = P.money(gross * D("0.10"))
        return {"pd_rows": pd_rows, "lines": lines, "per_diem_total": pd_total,
                "lines_total": lines_total, "gross": gross, "penalty": penalty,
                "allowed_total": gross - penalty,
                "chain": P.approval_chain(emp, self.emps, self.deps, gross)}

    def _days_late(self, t):
        if not t.submitted_ts:
            return None
        return (t.submitted_ts.date() - t.return_ts.date()).days

    # ------------------------------------------------------------ recoveries
    def _new_recovery(self, t, total, instalments):
        if total > 0:
            self.recoveries.append(Recovery(t.trip_id, t.employee_id, total, instalments))

    def _trip_recovery_total(self, trip_id):
        return sum((r.total for r in self.recoveries if r.trip_id == trip_id), D(0))

    # ------------------------------------------------------------ commands
    def approve_trip(self, trip_id, approver_id, approved_ts):
        t = self._trip(trip_id)
        emp = self.emps[t.employee_id]
        expected = emp.manager_id or self._finance_head()
        if approver_id != expected:
            raise Rejected(f"{approver_id} is not the approver for {t.employee_id} (expected {expected})")
        if t.status == "REQUESTED":
            t.status = "APPROVED"
        elif t.ext_unapproved and t.status in ("APPROVED", "ADVANCE_PAID", "TRAVELLED"):
            t.ext_unapproved, t.approved_return_ts = False, t.return_ts      # re-approval
        else:
            raise Rejected(f"{trip_id} is {t.status}, cannot approve")

    def _finance_head(self):
        return next(e.employee_id for e in self.emps.values() if e.role == "FINANCE_HEAD")

    def pay_advance(self, trip_id, paid_ts):
        t = self._trip(trip_id)
        if t.status != "APPROVED":
            raise Rejected(f"advance needs APPROVED, trip is {t.status}")
        blockers = [o.trip_id for o in self.trips.values()
                    if o.employee_id == t.employee_id and o.trip_id < t.trip_id
                    and o.status not in SETTLED_LIKE and o.status != "REQUESTED"]
        if blockers:                                                        # BR-11
            t.advance_blocked = True
            self.events.append(f"{trip_id}: advance BLOCKED by unsettled {blockers}")
            return
        t.advance_paid = P.advance_amount(self.estimate(t))
        t.status = "ADVANCE_PAID"

    def mark_travelled(self, trip_id, when):
        t = self._trip(trip_id)
        if t.status not in ("APPROVED", "ADVANCE_PAID"):
            raise Rejected(f"cannot travel from {t.status}")
        t.status = "TRAVELLED"

    def extend_trip(self, trip_id, new_return, when):
        t = self._trip(trip_id)
        if t.status not in ("APPROVED", "ADVANCE_PAID", "TRAVELLED"):
            raise Rejected(f"cannot extend from {t.status}")
        t.return_ts = ts(new_return)
        if t.return_ts - t.approved_return_ts > timedelta(days=2):          # BR-16
            t.ext_unapproved = True
        else:
            t.ext_unapproved = False

    def cancel_trip(self, trip_id, when, reason):
        t = self._trip(trip_id)
        if t.status not in ("REQUESTED", "APPROVED", "ADVANCE_PAID"):       # BR-15
            raise Rejected(f"cancel allowed only before TRAVELLED, trip is {t.status}")
        if reason not in ("BUSINESS", "PERSONAL"):
            raise Rejected(f"bad cancel reason {reason!r}")
        t.status, t.cancel_ts, t.cancel_reason = "CANCELLED", ts(when), reason

    def refund_advance(self, trip_id, when):
        t = self._trip(trip_id)
        if t.status != "CANCELLED" or t.advance_paid <= 0 or t.refund_ts:
            raise Rejected("no refundable advance on this trip")
        t.refund_ts = ts(when)
        if t.refund_ts > t.cancel_ts + timedelta(days=7):                   # BR-15
            self._new_recovery(t, t.advance_paid, RECOVERY_INSTALMENTS)
            self.events.append(f"{trip_id}: advance refunded late -> recovery started")

    def submit_claim(self, trip_id, when):
        t = self._trip(trip_id)
        if t.status != "TRAVELLED":
            raise Rejected(f"claim needs TRAVELLED, trip is {t.status}")
        t.submitted_ts = ts(when)
        t.status = "CLAIM_SUBMITTED"
        if P.late_status(self._days_late(t)) == "REJECTED":                 # BR-14
            t.status, t.claim_rejected = "CLAIM_REJECTED", True
            self._new_recovery(t, t.advance_paid, RECOVERY_INSTALMENTS)

    def exception(self, line_id, manager_id, when):
        ln = self.line_by_id[line_id]
        t = self.trips[ln["trip_id"]]
        emp = self.emps[t.employee_id]
        if manager_id != emp.manager_id:
            raise Rejected("exception must come from the employee's manager")
        if not P.mode_problem(ln, emp.grade, t.distance_km):
            raise Rejected("line has no mode-rule violation")
        self.exceptions.add(line_id)

    def override(self, line_id, finance_head_id, amount, when):
        if self.emps[finance_head_id].role != "FINANCE_HEAD":
            raise Rejected("only the finance head can override")
        ln = self.line_by_id[line_id]
        t = self.trips[ln["trip_id"]]
        res = next(r for r in self.line_results(t) if r["line_id"] == line_id)
        if res["status"] != "DISALLOWED":
            raise Rejected("only DISALLOWED lines can be overridden")
        self.overrides[line_id] = P.override_amount(ln["amount"], D(amount))   # BR-12 (20% max)

    def approve_claim(self, trip_id, approver_id, when):
        t = self._trip(trip_id)
        if t.status != "CLAIM_SUBMITTED":
            raise Rejected(f"claim approval needs CLAIM_SUBMITTED, trip is {t.status}")
        chain = self.compute_claim(t)["chain"]
        if approver_id not in chain:
            raise Rejected(f"{approver_id} not in approval chain {chain}")
        if approver_id in t.approvals:
            raise Rejected("already approved by " + approver_id)
        t.approvals.append(approver_id)
        if all(a in t.approvals for a in chain):
            t.status = "CLAIM_APPROVED"

    def settle(self, trip_id, when):
        t = self._trip(trip_id)
        lenient_ok = (not self.strict_chain and t.status == "CLAIM_SUBMITTED" and t.approvals)
        if t.status != "CLAIM_APPROVED" and not lenient_ok:
            raise Rejected(f"settle needs CLAIM_APPROVED, trip is {t.status}")
        c = self.compute_claim(t)
        net = c["allowed_total"] - t.advance_paid
        if net < 0:                                                         # BR-18
            self._new_recovery(t, -net, 1)
        t.snapshot = c
        t.status = "SETTLED"

    def payroll_run(self, month):
        y, m = (int(x) for x in month.split("-"))
        month_end = ts(f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}-01 00:00:00")
        for t in self.trips.values():        # late refund never made -> recovery starts
            if (t.status == "CANCELLED" and t.advance_paid > 0 and not t.refund_ts
                    and t.cancel_ts + timedelta(days=7) < month_end
                    and not any(r.trip_id == t.trip_id for r in self.recoveries)):
                self._new_recovery(t, t.advance_paid, RECOVERY_INSTALMENTS)
        cap_left = {}
        for r in self.recoveries:
            if r.remaining() <= 0:
                continue
            emp = self.emps[r.employee_id]
            cap = P.money(emp.monthly_basic * D("0.25"))                    # BR-19
            left = cap_left.setdefault(r.employee_id, cap)
            scheduled = (r.queue.pop(0) if r.queue else D(0)) + r.carry
            recovered = min(scheduled, left)
            cap_left[r.employee_id] = left - recovered
            r.carry = scheduled - recovered
            self.recovery_rows.append({
                "trip_id": r.trip_id, "employee_id": r.employee_id, "payroll_month": month,
                "scheduled": scheduled, "cap": cap, "recovered": recovered,
                "carried_forward": r.carry})

    # ------------------------------------------------------------ reports
    def statement(self, t):
        emp = self.emps[t.employee_id]
        if t.claim_rejected or t.status in ("REQUESTED", "APPROVED", "ADVANCE_PAID", "TRAVELLED"):
            zero, chain = D(0), self.compute_claim(t)["chain"] if t.status == "TRAVELLED" else []
            return self._stmt(t, zero, zero, zero, zero, chain,
                              recovery=self._trip_recovery_total(t.trip_id))
        c = t.snapshot or self.compute_claim(t)
        if t.status == "CANCELLED":
            return self._stmt(t, D(0), c["lines_total"], D(0), c["lines_total"], [],
                              recovery=self._trip_recovery_total(t.trip_id))
        net = c["allowed_total"] - t.advance_paid
        recovery = self._trip_recovery_total(t.trip_id) or max(D(0), -net)
        return self._stmt(t, c["per_diem_total"], c["lines_total"], c["penalty"],
                          c["allowed_total"], c["chain"], recovery=recovery,
                          payable=max(D(0), net))

    def _stmt(self, t, pd, lines, penalty, allowed, chain, recovery, payable=None):
        if payable is None:
            payable = allowed if t.status == "CANCELLED" else D(0)
        return {"trip_id": t.trip_id, "employee_id": t.employee_id, "status": t.status,
                "per_diem_total": pd, "lines_allowed_total": lines, "penalty": penalty,
                "allowed_total": allowed, "advance_paid": t.advance_paid, "payable": payable,
                "recovery_total": recovery, "approval_chain": ";".join(chain),
                "approvals_received": ";".join(t.approvals)}

    def print_claim(self, trip_id):
        self.statement_rows.append(self.statement(self._trip(trip_id)))

    def print_dept_report(self, month):
        for dep in self.deps.values():
            row = dict(month=month, department_id=dep.department_id, trips=0,
                       per_diem=D(0), lodging=D(0), travel=D(0), other=D(0))
            for t in self.trips.values():
                if t.departure_ts.strftime("%Y-%m") != month or \
                        self.emps[t.employee_id].department_id != dep.department_id:
                    continue
                row["trips"] += 1
                if t.status in ("CLAIM_SUBMITTED", "CLAIM_APPROVED", "SETTLED", "CANCELLED"):
                    c = t.snapshot or self.compute_claim(t)
                    row["per_diem"] += c["per_diem_total"]
                    for r in c["lines"]:
                        amt = r["allowed_amount"] + r["override_amount"]
                        key = ("lodging" if r["category"] == "LODGING" else
                               "travel" if r["category"] in ("AIRFARE", "TRAIN", "TAXI") else "other")
                        row[key] += amt
            row["total"] = row["per_diem"] + row["lodging"] + row["travel"] + row["other"]
            self.dept_rows.append(row)

    # ------------------------------------------------------------ final tables
    def all_per_diem_rows(self):
        rows = []
        for t in self.trips.values():
            if t.status != "CANCELLED":
                rows += self.per_diem_rows(t)
        return rows

    def all_line_results(self):
        rows = []
        for t in self.trips.values():
            rows += (t.snapshot["lines"] if t.snapshot else self.line_results(t))
        order = {l["line_id"]: i for i, l in enumerate(self.lines)}
        return sorted(rows, key=lambda r: order[r["line_id"]])


class Rejected(Exception):
    """A command that breaks a rule. It is logged and the engine carries on."""
