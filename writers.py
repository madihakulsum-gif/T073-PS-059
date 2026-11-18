"""Write the 5 required CSV files in the exact column order from the README."""
import csv
import os
from datetime import date
from decimal import Decimal as D

COLUMNS = {
    "per_diem.csv": ["trip_id", "day_date", "day_type", "base_rate", "currency", "fx_rate",
                     "day_pct", "meal_reduction", "breakfast_reduction", "per_diem_inr", "status"],
    "claim_line_results.csv": ["line_id", "trip_id", "status", "reason_code", "claimed_amount",
                               "allowed_amount", "override_amount"],
    "claim_statement.csv": ["trip_id", "employee_id", "status", "per_diem_total",
                            "lines_allowed_total", "penalty", "allowed_total", "advance_paid",
                            "payable", "recovery_total", "approval_chain", "approvals_received"],
    "recovery_schedule.csv": ["trip_id", "employee_id", "payroll_month", "scheduled", "cap",
                              "recovered", "carried_forward"],
    "dept_report.csv": ["month", "department_id", "trips", "per_diem", "lodging", "travel",
                        "other", "total"],
}
FOUR_DP = {"fx_rate"}
PLAIN = {"day_pct", "meal_reduction", "breakfast_reduction"}     # percentages: 100, 50, 10
INTS = {"trips"}


def fmt(col, v):
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, D):
        if col in FOUR_DP:
            return format(v.quantize(D("0.0001")), "f")
        if col in PLAIN:
            return format(v.normalize(), "f") if v != 0 else "0"
        return format(v.quantize(D("0.01")), "f")
    return str(v)


def write_all(engine, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tables = {
        "per_diem.csv": engine.all_per_diem_rows(),
        "claim_line_results.csv": engine.all_line_results(),
        "claim_statement.csv": engine.statement_rows,
        "recovery_schedule.csv": engine.recovery_rows,
        "dept_report.csv": engine.dept_rows,
    }
    for name, rows in tables.items():
        cols = COLUMNS[name]
        with open(os.path.join(out_dir, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(cols)
            for r in rows:
                w.writerow([fmt(c, r[c]) for c in cols])
    with open(os.path.join(out_dir, "engine_log.txt"), "w", encoding="utf-8") as f:
        f.write("EVENTS\n" + "\n".join(engine.events) + "\n\nERRORS (rejected commands)\n"
                + "\n".join(engine.errors) + "\n")
    return {k: len(v) for k, v in tables.items()}
