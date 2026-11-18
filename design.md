# Design - T073 / PS-059

## Glossary
Per-diem: fixed daily allowance by city class and grade. Grade group: G1-2 / G3-4 / G5-6.
Claim line: one expense row. Allowed amount: what policy accepts for a line.
Advance: money paid before travel. Recovery: money taken back from salary.
Chain: ordered list of people who must approve a claim.

## State diagram (trip)
REQUESTED -> APPROVED -> ADVANCE_PAID -> TRAVELLED -> CLAIM_SUBMITTED -> CLAIM_APPROVED -> SETTLED
- REQUESTED / APPROVED / ADVANCE_PAID -> CANCELLED (never after TRAVELLED)
- APPROVED -> TRAVELLED is allowed (advance is optional)
- CLAIM_SUBMITTED -> CLAIM_REJECTED (more than 30 days after return; full advance recovered)

## Classes
Employee, Department, City, Trip (holds state), claim line (dict), Recovery,
Engine (commands), policy module (pure rules), writers.

## Business-rule register
| ID | Rule | Where |
|---|---|---|
| BR-01 | Trip moves only through the allowed states; CANCELLED only before TRAVELLED | engine.py |
| BR-02 | Trip approval must come from the employee's manager (dept heads: finance head) | approve_trip |
| BR-03 | Per-diem by city class and grade group; international USD 50/75/120 at departure-date FX | policy.base_rate |
| BR-04 | Departure day 100% if before 12:00, else 50% (12:00 exactly = 50%) | policy.day_plan |
| BR-05 | Return day 100% if after 14:00, else 50% (14:00 exactly = 50%) | policy.day_plan |
| BR-06 | Same-date trip: 50% if absence >= 8h, else 0 | policy.day_plan |
| BR-07 | Lodging cap per night by class and grade group; above cap = CAPPED; no GST no. = DISALLOWED; international lodging never claimed | policy.evaluate_line |
| BR-08 | Any meal line that day: per-diem -50%; breakfast included: further -10%; multiply | policy.per_diem_amount |
| BR-09 | Air only G3+ or > 500 km; train AC-2 for G1-2, AC-1 for G3+; manager EXCEPTION on that line waives | policy.mode_problem |
| BR-10 | Taxi: Rs 18/km if km given, else Rs 1,500 per day flat | policy.evaluate_line |
| BR-11 | Advance 80% of estimate, max Rs 1,00,000, only after APPROVED; blocked while an earlier trip is unsettled | pay_advance |
| BR-12 | Chain: <= 25,000 manager; <= 1,00,000 + dept head; above + finance head. Override: finance head, DISALLOWED lines, max 20% | policy.approval_chain |
| BR-13 | Line > 500 needs receipt; > 10,000 needs receipt + payment proof | policy.receipt_problem |
| BR-14 | Submit within 15 days of return; 16-30 days: 10% penalty; > 30: auto-reject, advance recovered in 3 instalments | submit_claim |
| BR-15 | Cancel after advance: refund in 7 days or recovery starts; airfare cancel charge: 50% if BUSINESS | cancel_trip, refund_advance |
| BR-16 | Extension > 2 days without re-approval: per-diem 0, lodging nights DISALLOWED | extend_trip |
| BR-17 | Return date for per-diem is taken in the destination's local time | policy.local_dt |
| BR-18 | Payable = allowed - advance; negative -> recovery from next salary | settle |
| BR-19 | Recovery per month capped at 25% of monthly basic; rest rolls forward | payroll_run |

## Decision log
- D-01 Money is Decimal, rounded half-up to paise; international per-diem rounded once per day.
- D-02 Chain amount = per-diem + allowed lines (before the late penalty).
- D-03 Meal lines are payable (subject to receipt rules); the 50% cut is on per-diem only.
- D-04 Breakfast / meal reduction is matched by the line's expense_date, even if that line is DISALLOWED.
- D-05 Strict chain: SETTLE needs every approver in the chain. `--lenient-chain` allows SETTLE after any approval.
- D-06 Flat taxi cap is applied per day across all flat taxi lines that day.
- D-07 Override above 20% is clipped to 20%, not rejected.
- D-08 Cancellation charge = 50% of the airfare line amount (the booked fare is not given).
- D-09 Late advance refund starts a 3-instalment recovery of the full advance (same plan as auto-reject).
- D-10 Recovery from a negative settlement is one instalment (next payroll run), then cap and roll-forward.
- D-11 Recovery "scheduled" = this run's instalment + amount carried in. Cap is per employee per month.
- D-12 Trip status values added: CLAIM_REJECTED. Per-diem status: ALLOWED / NOT_PAYABLE.
- D-13 Domestic per-diem rows print currency INR and fx_rate 1.0000.
- D-14 Reason codes: OK, CAP_LODGING, CAP_TAXI_KM, CAP_TAXI_FLAT, CAP_CANCEL_50, NO_GST_INVOICE,
  NO_RECEIPT, NO_PAYMENT_PROOF, MODE_AIR_NOT_ALLOWED, MODE_TRAIN_CLASS, EXTENSION_UNAPPROVED,
  CANCEL_PERSONAL, INTL_LODGING_COMPANY_BOOKED.

## Questions to send on the team thread (the brief and data leave these open)
1. Sample commands approve many Rs 25,000-1,00,000 claims with the manager only, but BR-12 needs the
   department head too. Should SETTLE then be accepted or rejected?
2. Which reason_code strings do you expect on claim_line_results.csv?
3. Cancellation charge: "50% of fare" - is the fare the charge line, or the original ticket price?
4. Late advance refund (day 8+): if the advance is refunded, should recovery still start?
5. Per-diem `status` values, and fx_rate for domestic rows (we print 1.0000).
6. Is the recovery_schedule "scheduled" figure the instalment only, or instalment + carry-forward?
7. dept_report: which trips count (cancelled? rejected?) and is `total` before or after penalty?
