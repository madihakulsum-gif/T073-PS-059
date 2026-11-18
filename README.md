# T073-PS-059 - Employee Travel and Per-Diem Claims Engine

Team T073: Madiha Kulsum, Manoj B M. Language: Python 3.10+ (no outside libraries).

## Run it
    python main.py sample_data output            # follows the brief exactly
    python main.py sample_data output --lenient-chain   # see decision D-05

The input folder needs: employees.csv, departments.csv, cities.csv, fx_rates.csv,
trips.csv, claim_lines.csv, commands.txt. Output goes to `output/`:
per_diem.csv, claim_line_results.csv, claim_statement.csv, recovery_schedule.csv,
dept_report.csv, and engine_log.txt (blocked advances and rejected commands).

## Test it
    python -m pytest          # or:  python run_tests.py

## Code map
| File | Job |
|---|---|
| engine/policy.py | Pure rule functions (per-diem, caps, receipts, chain). Start here for the viva. |
| engine/engine.py | State machine and one handler per command. |
| engine/loader.py | Reads the CSVs and commands.txt. |
| engine/writers.py | Writes the 5 output CSVs. |
| tests/ | One test per rule / edge case (EC-01 to EC-08) plus sample-pack tests. |
| docs/design.md | Glossary, state diagram, BR register, decision log, open questions. |

## Order in which rules are applied to one claim line
1. Documents: GST invoice (lodging), receipt (> Rs 500), payment proof (> Rs 10,000)
2. Mode rule (air / train class). A manager EXCEPTION on that exact line waives it.
3. Extension rule (lodging nights of an unapproved extension)
4. Cap (lodging cap, taxi cap) -> CAPPED
5. Finance-head OVERRIDE, only on DISALLOWED lines, at most 20% of the line
Then per trip: per-diem (partial day -> meals 50% -> breakfast 10%, multiplied),
add lines, subtract late penalty (10% of allowed, days 16-30), subtract advance.
