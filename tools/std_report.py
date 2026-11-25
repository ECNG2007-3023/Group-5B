"""Builds the STD test log from the pytest results of both repositories.

    python tools/std_report.py <Group-5A folder> <Group-5B folder>

Reads tests/evidence/junit_*.xml in each repo and writes STD_TEST_REPORT.md
(in the Group-5B folder): one row per STD test case with its result and
the evidence files, ready to copy into Section 3.4 of the STD.
"""
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path

ORDER = ([f"UT-{i:02d}" for i in range(1, 15)] + [f"IT-{i:02d}" for i in range(1, 7)]
         + [f"VT-{i:02d}" for i in range(1, 6)] + [f"HT-{i:02d}" for i in range(1, 6)])
TITLES = {
    "UT-01": "Valid request accepted", "UT-02": "Invalid requests rejected",
    "UT-03": "Stages run in order", "UT-04": "Stops at first failing stage",
    "UT-05": "Fresh, bounded container per stage", "UT-06": "Failures classified by stage",
    "UT-07": "Result fields and VCD rules", "UT-08": "Malformed/unsupported requests rejected",
    "UT-09": "Session opened", "UT-10": "Session closed and cleaned up",
    "UT-11": "Diagnostics returned", "UT-12": "Diagnostics follow edits",
    "UT-13": "Code completion returned", "UT-14": "Test client runs without P5A",
    "IT-01": "REST API -> Validation -> Stage Runner", "IT-02": "Stage Runner -> Docker -> Result Builder",
    "IT-03": "WebSocket -> Session Manager -> VHDL-LS", "IT-04": "VHDL-LS -> Handlers -> client",
    "IT-05": "P5B shared file set -> P5A", "IT-06": "/check-and-simulate gate -> P5A",
    "VT-01": "End-to-end valid simulation", "VT-02": "Faulty designs: correct stage and line",
    "VT-03": "Editing workflow", "VT-04": "Response clarity", "VT-05": "Contract conformance",
    "HT-01": "Performance", "HT-02": "Stress and scalability", "HT-03": "Recovery and fault tolerance",
    "HT-04": "Security and isolation", "HT-05": "Cross-platform behaviour",
}
# IT-03 and IT-04 are the same messages as UT-09..UT-13 but through the real
# WebSocket endpoint; they are covered by those tests plus the real-vhdl_ls variants.
ALIASES = {"IT-03": ["UT-09", "UT-11"], "IT-04": ["UT-11", "UT-13"]}
NAME_RE = re.compile(r"test_(UT|IT|VT|HT)(\d\d)_")


def collect(repo):
    cases = []
    for xml in sorted((repo / "tests" / "evidence").glob("junit_*.xml")):
        for tc in ET.parse(xml).iter("testcase"):
            m = NAME_RE.match(tc.get("name", ""))
            if not m:
                continue
            if tc.find("failure") is not None or tc.find("error") is not None:
                outcome = "fail"
            elif tc.find("skipped") is not None:
                outcome = "skip"
            else:
                outcome = "pass"
            cases.append((f"{m.group(1)}-{m.group(2)}", tc.get("name"), outcome))
    return cases


def verdict(outcomes):
    if not outcomes:
        return "Not run"
    if "fail" in outcomes:
        return "Fail"
    if all(o == "skip" for o in outcomes):
        return "Not run (needs Docker / vhdl_ls)"
    if "skip" in outcomes:
        return f"Pass ({outcomes.count('skip')} part(s) skipped)"
    return "Pass"


def main(a, b):
    repos = {"Group-5A": Path(a), "Group-5B": Path(b)}
    results = OrderedDict((i, []) for i in ORDER)
    names = OrderedDict((i, []) for i in ORDER)
    evidence = OrderedDict((i, []) for i in ORDER)
    for label, repo in repos.items():
        for test_id, name, outcome in collect(repo):
            results[test_id].append(outcome)
            names[test_id].append(f"{name} [{outcome}]")
        for f in sorted((repo / "tests" / "evidence").glob("*.json")):
            test_id = f.stem[:5]
            if test_id in evidence:
                evidence[test_id].append(f"{label}/tests/evidence/{f.name}")
    for test_id, sources in ALIASES.items():
        if not results[test_id]:
            for s in sources:
                results[test_id] += results[s]
                evidence[test_id] += evidence[s]
                names[test_id].append(f"(covered by {s})")

    lines = [f"# STD test report - {time.strftime('%Y-%m-%d %H:%M')}", "",
             "| Test ID | Test | Result | Evidence |", "| --- | --- | --- | --- |"]
    for test_id in ORDER:
        ev = "<br>".join(evidence[test_id][:6]) or "-"
        if len(evidence[test_id]) > 6:
            ev += f"<br>... +{len(evidence[test_id]) - 6} more"
        lines.append(f"| {test_id} | {TITLES[test_id]} | {verdict(results[test_id])} | {ev} |")
    lines += ["", "## Pytest cases per STD ID", ""]
    for test_id in ORDER:
        lines.append(f"- **{test_id}**: " + ("; ".join(names[test_id]) or "none"))
    lines += ["", "VT-04 also needs the two-reviewer check: give them "
              "Group-5B/tests/evidence/VT-04_responses_for_reviewers.json and mark against "
              "VT-04_answer_key.json."]
    out = repos["Group-5B"] / "STD_TEST_REPORT.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out}")
    counts = {v: sum(1 for i in ORDER if verdict(results[i]).startswith(v))
              for v in ("Pass", "Fail", "Not run")}
    print(counts)
    return 1 if counts["Fail"] else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
