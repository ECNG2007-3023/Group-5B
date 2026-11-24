"""White-box path coverage of one function, worked like the lecture's
discount() example: summarize(diagnostics) in gateway/mappers.py.

    D1: if no diagnostics:            return "No errors present."
        count each severity, build parts ("2 errors", "1 warning", ...)
    D2: if only one kind (1 part):    text = that part
        else:                         text = "a, b and c"
    D3: if any errors:                return text + " found."
        else:                         return "No errors present; " + text + " found."

    Decisions: 3 (D1, D2, D3) -> cyclomatic complexity 4
    Paths: D1 true (1) + D1 false x D2 x D3 (2 x 2 = 4) = 5 paths

    Coverage    | tests needed                  | test IDs
    statement   | P1, P2, P5 reach every line   | TC-B-P1, TC-B-P2, TC-B-P5
    branch      | D1, D2, D3 each T and F       | TC-B-P1, TC-B-P2, TC-B-P5
    path        | every route                   | TC-B-P1 .. TC-B-P5
"""
import pytest

from gateway.mappers import summarize


def d(severity):
    return {"severity": severity}


@pytest.mark.parametrize("diagnostics, expected", [
    ([], "No errors present."),                                        # P1: D1 T
    ([d(1)], "1 error found."),                                        # P2: D2 T, D3 T
    ([d(2)], "No errors present; 1 warning found."),                   # P3: D2 T, D3 F
    ([d(1), d(1), d(2)], "2 errors and 1 warning found."),             # P4: D2 F, D3 T
    ([d(2), d(4), d(4)], "No errors present; 1 warning and 2 hints found."),  # P5: D2 F, D3 F
], ids=["TC-B-P1_none", "TC-B-P2_one_error", "TC-B-P3_one_warning",
        "TC-B-P4_errors_and_warning", "TC-B-P5_no_errors_several_kinds"])
def test_every_path_of_summarize(diagnostics, expected):
    assert summarize(diagnostics) == expected
