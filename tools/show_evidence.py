"""Print the real output saved by an STD test, ready to screenshot.

    python tools/show_evidence.py UT-02          every UT-02 case, one after another
    python tools/show_evidence.py UT-02_empty    just that case
    python tools/show_evidence.py --list         what evidence exists

Run the STD tests first (python -m pytest tests/test_std.py): each test saves
what it saw to tests/evidence/<ID>.json, and this script shows it.
"""
import json
import os
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent.parent / "tests" / "evidence"

os.system("")  # turns on colours in the Windows terminal
CYAN, GREEN, YELLOW, RED, DIM, BOLD, END = ("\033[96m", "\033[92m", "\033[93m", "\033[91m",
                                            "\033[2m", "\033[1m", "\033[0m")


def colour(value):
    if value is None:
        return f"{DIM}null{END}"
    if isinstance(value, bool):
        return f"{YELLOW}{str(value).lower()}{END}"
    if isinstance(value, (int, float)):
        return f"{YELLOW}{value}{END}"
    text = str(value)
    if text in ("success", "ok", "present", "No errors present.", "BLOCKED"):
        return f"{GREEN}{text}{END}"
    if text in ("failed",) or text.endswith("_error") or text in ("timeout", "memory_limit"):
        return f"{RED}{text}{END}"
    return text


def show(value, indent=0):
    pad = "  " * indent
    if isinstance(value, dict):
        for key, v in value.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{CYAN}{key}{END}:")
                show(v, indent + 1)
            elif isinstance(v, str) and "\n" in v.strip():
                print(f"{pad}{CYAN}{key}{END}:")
                for line in v.rstrip().splitlines():
                    print(f"{pad}  {DIM}|{END} {line}")
            else:
                print(f"{pad}{CYAN}{key}{END}: {colour(v)}")
    elif isinstance(value, list):
        simple = all(not isinstance(v, (dict, list)) for v in value)
        if simple and len(json.dumps(value)) < 100:
            print(f"{pad}[{', '.join(colour(v) for v in value)}]")
            return
        for v in value:
            if isinstance(v, (dict, list)):
                print(f"{pad}-")
                show(v, indent + 1)
            else:
                print(f"{pad}- {colour(v)}")
    else:
        print(f"{pad}{colour(value)}")


def main(args):
    if not EVIDENCE.is_dir():
        sys.exit(f"No evidence folder at {EVIDENCE}. Run the STD tests first.")
    files = sorted(EVIDENCE.glob("*.json"))
    if not args or args[0] == "--list":
        for f in files:
            print(f.stem)
        return
    wanted = [f for f in files if f.stem.upper().startswith(args[0].upper().replace("_", "-", 1)
                                                            if "_" not in args[0][:5] else args[0].upper())
              or f.stem.upper().startswith(args[0].upper())]
    if not wanted:
        sys.exit(f"No evidence matching {args[0]!r}. Try --list.")
    for f in wanted:
        record = json.loads(f.read_text(encoding="utf-8"))
        test_id, _, case = f.stem.partition("_")
        line = "=" * 78
        print(f"\n{BOLD}{line}{END}")
        print(f"{BOLD} STD {test_id}{END}" + (f"   case: {BOLD}{case}{END}" if case else ""))
        print(f"{DIM} run {record.get('time', '?')} on {record.get('platform', '?')}{END}")
        print(f"{BOLD}{line}{END}")
        show(record.get("observed", record))
    vcd = [p for p in EVIDENCE.glob("*.vcd") if p.stem.upper().startswith(args[0].upper()[:5])]
    for p in vcd:
        print(f"\n{BOLD}--- {p.name} (first 30 lines) ---{END}")
        print("\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[:30]))


if __name__ == "__main__":
    main(sys.argv[1:])
