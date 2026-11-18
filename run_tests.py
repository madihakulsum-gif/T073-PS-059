"""Tiny fallback runner if pytest is not installed:  python run_tests.py"""
import importlib, sys, traceback
failed = 0
for mod in ("tests.test_policy", "tests.test_sample_data"):
    m = importlib.import_module(mod)
    for name in sorted(n for n in dir(m) if n.startswith("test_")):
        try:
            getattr(m, name)(); print("PASS", name)
        except Exception:
            failed += 1; print("FAIL", name); traceback.print_exc()
print("\n%d failed" % failed)
sys.exit(1 if failed else 0)
