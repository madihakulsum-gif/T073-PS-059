"""Run:  python main.py <input_folder> [output_folder] [--lenient-chain]

The input folder must hold employees.csv, departments.csv, cities.csv, fx_rates.csv,
trips.csv, claim_lines.csv and commands.txt.
"""
import sys
from engine.engine import Engine
from engine.writers import write_all


def main(argv):
    if len([a for a in argv[1:] if not a.startswith('--')]) < 1:
        print(__doc__)
        return 1
    args = [a for a in argv[1:] if not a.startswith("--")]
    lenient = "--lenient-chain" in argv
    folder = args[0]
    out = args[1] if len(args) > 1 else "output"
    eng = Engine(folder, strict_chain=not lenient)
    eng.run(f"{folder}/commands.txt")
    counts = write_all(eng, out)
    for name, n in counts.items():
        print(f"{name}: {n} rows")
    print(f"{len(eng.errors)} command(s) rejected - see {out}/engine_log.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
