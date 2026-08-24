"""
run_regression.py

Golden-file regression harness for table_to_yaml_converter.py.

WHY THIS EXISTS
---------------
The previous check only did `yaml.safe_load()` on each output and printed
"OK" if it didn't raise. That verifies SYNTAX, never CONTENT — so a bug
that silently deleted a table value still showed 23/23 passing. One did
exactly that for several sessions (an ADU value spanning all columns was
being swallowed and every following row wrongly nested under it).

This harness compares each converted table byte-for-byte against a frozen
"golden" copy of its known-correct output. Any change in content fails
loudly and shows a diff, so a fix that quietly breaks a different table
gets caught immediately instead of weeks later.

WORKFLOW
--------
    python regression_test.py              # check every case against golden
    python regression_test.py --accept     # re-freeze goldens after you have
                                          # REVIEWED the diffs and agree the
                                          # new output is correct

Never run --accept without reading the diff first: blindly accepting is
how a silent-corruption bug becomes the expected output.

Golden files live in cases/golden/<id>_yaml.txt.
"""

import os
import sys
import difflib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from html_to_yaml import (  # noqa: E402
    CASES_DIR,
    html_table_to_yaml_dict,
    to_yaml_string,
)

GOLDEN_DIR = os.path.join(CASES_DIR, "golden")


def convert_case(case_id: str) -> str:
    html_path = os.path.join(CASES_DIR, f"{case_id}_html.txt")
    with open(html_path, encoding="utf-8") as f:
        data = html_table_to_yaml_dict(f.read())
    return to_yaml_string(data) if data else ""


def all_case_ids() -> list:
    if not os.path.isdir(CASES_DIR):
        raise FileNotFoundError(f"'{CASES_DIR}/' not found.")
    ids = {
        f[: -len("_html.txt")]
        for f in os.listdir(CASES_DIR)
        if f.endswith("_html.txt")
    }
    return sorted(ids)


def main():
    accept = "--accept" in sys.argv
    os.makedirs(GOLDEN_DIR, exist_ok=True)

    case_ids = all_case_ids()
    if not case_ids:
        print(f"No cases found in '{CASES_DIR}/'.")
        return 1

    passed, failed, created, empty = [], [], [], []

    for case_id in case_ids:
        actual = convert_case(case_id)
        if not actual.strip():
            # A case that produces nothing is itself a red flag worth
            # surfacing — usually means the table shape wasn't recognised
            # at all rather than that the table was genuinely empty.
            empty.append(case_id)
            continue

        golden_path = os.path.join(GOLDEN_DIR, f"{case_id}_yaml.txt")

        if not os.path.exists(golden_path) or accept:
            with open(golden_path, "w", encoding="utf-8") as f:
                f.write(actual)
            created.append(case_id)
            continue

        with open(golden_path, encoding="utf-8") as f:
            expected = f.read()

        if actual == expected:
            passed.append(case_id)
        else:
            failed.append(case_id)
            print(f"\n{'=' * 70}\nFAIL: {case_id}\n{'=' * 70}")
            diff = difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"golden/{case_id}",
                tofile=f"actual/{case_id}",
                n=2,
            )
            # Cap the diff so one badly-broken table doesn't bury the rest.
            for i, line in enumerate(diff):
                if i > 60:
                    print("  ... (diff truncated)")
                    break
                print("  " + line.rstrip("\n"))

    print(f"\n{'-' * 70}")
    print(f"passed: {len(passed)}   failed: {len(failed)}   "
          f"{'re-frozen' if accept else 'new golden'}: {len(created)}   "
          f"empty output: {len(empty)}")
    if empty:
        print(f"  empty (converted to nothing — check these): {', '.join(empty)}")
    if failed:
        print(f"  failed: {', '.join(failed)}")
        print("\nIf these changes are CORRECT, re-freeze with: "
              "python run_regression.py --accept")
    print("-" * 70)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())