"""Data classes + readers for the input files."""
import csv
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal as D
from typing import List, Optional

TS = "%Y-%m-%d %H:%M:%S"


def ts(s):
    return datetime.strptime(s.strip(), TS)


def dt(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def flag(s):
    return (s or "").strip().lower() == "true"


@dataclass
class Employee:
    employee_id: str
    employee_name: str
    grade: str
    department_id: str
    base_city_id: str
    manager_id: str
    role: str
    monthly_basic: D


@dataclass
class Department:
    department_id: str
    department_name: str
    head_employee_id: str


@dataclass
class City:
    city_id: str
    city_name: str
    country: str
    city_class: str
    utc_offset_minutes: int


@dataclass
class Trip:
    trip_id: str
    employee_id: str
    from_city_id: str
    to_city_id: str
    departure_ts: datetime
    return_ts: datetime
    purpose: str
    mode: str
    distance_km: int
    request_ts: datetime
    # ---- state that changes while commands run
    status: str = "REQUESTED"
    approved_return_ts: Optional[datetime] = None     # the return time that was approved
    ext_unapproved: bool = False                      # BR-16
    advance_paid: D = D(0)
    advance_blocked: bool = False
    cancel_ts: Optional[datetime] = None
    cancel_reason: Optional[str] = None
    refund_ts: Optional[datetime] = None
    submitted_ts: Optional[datetime] = None
    claim_rejected: bool = False
    approvals: List[str] = field(default_factory=list)
    snapshot: Optional[dict] = None                   # frozen numbers at SETTLE


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_masters(folder):
    emps = {r["employee_id"]: Employee(r["employee_id"], r["employee_name"], r["grade"],
                                       r["department_id"], r["base_city_id"], r["manager_id"],
                                       r["role"], D(r["monthly_basic"]))
            for r in read_csv(f"{folder}/employees.csv")}
    deps = {r["department_id"]: Department(r["department_id"], r["department_name"],
                                           r["head_employee_id"])
            for r in read_csv(f"{folder}/departments.csv")}
    cities = {r["city_id"]: City(r["city_id"], r["city_name"], r["country"], r["city_class"],
                                 int(r["utc_offset_minutes"]))
              for r in read_csv(f"{folder}/cities.csv")}
    fx = {dt(r["rate_date"]): D(r["inr_per_unit"]) for r in read_csv(f"{folder}/fx_rates.csv")
          if r["currency"] == "USD"}
    trips = {}
    for r in read_csv(f"{folder}/trips.csv"):
        trips[r["trip_id"]] = Trip(r["trip_id"], r["employee_id"], r["from_city_id"],
                                   r["to_city_id"], ts(r["departure_ts"]), ts(r["return_ts"]),
                                   r["purpose"], r["mode"], int(r["distance_km"]),
                                   ts(r["request_ts"]))
        trips[r["trip_id"]].approved_return_ts = trips[r["trip_id"]].return_ts
    lines = []
    for r in read_csv(f"{folder}/claim_lines.csv"):
        lines.append({
            "line_id": r["line_id"], "trip_id": r["trip_id"], "expense_date": dt(r["expense_date"]),
            "category": r["category"], "amount": D(r["amount"]), "receipt": flag(r["receipt"]),
            "payment_proof": flag(r["payment_proof"]), "gst_invoice_no": r["gst_invoice_no"].strip(),
            "breakfast_included": flag(r["breakfast_included"]),
            "km": int(r["km"]) if r["km"].strip() else None,
            "travel_class": r["travel_class"].strip(), "description": r["description"]})
    return emps, deps, cities, fx, trips, lines


def read_commands(path):
    """Yield (line_no, command, args). Comments (#) and blank lines are skipped."""
    out = []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, start=1):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("|")
            out.append((n, parts[0].strip(), [p.strip() for p in parts[1:]]))
    return out
