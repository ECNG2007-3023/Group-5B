# P5B test plan and results

What each automated test proves. Run everything with `python -m pytest -v -rs`
(VHDL-LS installed; for the combined tests also P5A next to this repo and
Docker with the `p5a-ghdl` image). GitHub runs the tests on every push and
pull request.

**Latest full run: 273 passed, 0 failed, 0 skipped** (VHDL-LS 0.88.0, P5A with
GHDL 4.1.0 in Docker, Python 3.13, Ubuntu 24.04, 19 Nov 2026). **Coverage: 96%**
statement + branch (coverage.py).

How each lecture technique (assertions, exceptions, invariants, white box,
black box, test case write-ups) is applied, with the full EP/BVA tables,
decision table, state transitions, loop-invariant proof and coverage figures:
**[testing-techniques.md](testing-techniques.md)**.

## Test files

| File | Tests | Needs | Level | Type |
| --- | --- | --- | --- | --- |
| `tests/test_messages.py` | 29 | nothing | Unit: JSON-RPC parsing, diagnostics/completion format, URIs, path removal | White box |
| `tests/test_session_store.py` | 40 | nothing | Unit: file name rule (checked against P5A's), session folders | White box |
| `tests/test_checks.py` | 8 | nothing | Unit: the test client's comparison helpers | White box |
| `tests/test_invariants.py` | 31 | nothing (one test: P5A) | Unit: invariants (class, state machine, loop, replies), assertions, exceptions | White box |
| `tests/test_blackbox.py` | 46 | nothing (5 tests: P5A; 3: `vhdl_ls`) | Equivalence partitions, boundary values, decision table, state transitions, error guessing | Black box |
| `tests/test_failures.py` | 12 | nothing (5 tests: P5A) | Disk, VHDL-LS and combined-backend failures (added after the coverage review) | White box |
| `tests/test_paths.py` | 5 | nothing | Statement/branch/path coverage of one function | White box |
| `tests/test_bridge.py` | 20 | nothing | LSP plumbing against a fake language server process | White box |
| `tests/test_gateway.py` | 51 | nothing | The Gateway over a WebSocket with the fake bridge | Grey box (WebSocket in/out, but VHDL-LS replaced and session folders inspected) |
| `tests/test_real_vhdl_ls.py` | 14 | `vhdl_ls` | The system-test cases with the real VHDL-LS | Black box |
| `tests/test_live_server.py` | 3 | nothing (uses `vhdl_ls` if installed) | Real uvicorn server, `tools/check_gateway.py` cases run twice | Black box |
| `tests/test_combined.py` | 14 | P5A (some: Docker, `vhdl_ls`) | P5A + P5B in one backend | Black box (end to end), grey box (fake GHDL job) |
| `tools/check_gateway.py` | 9 cases × 2 runs | Gateway running | Live system check over a real WebSocket | Black box |

## Testing approach

**White-box testing** is written from the code: it knows how each function
works and tests its branches and error paths on their own. Examples:
`test_messages.py` feeds `parse_message()` each kind of bad JSON-RPC message;
`test_session_store.py` tries every edge of the file name rule;
`test_bridge.py` drives the LSP plumbing against a fake language-server
process; `test_invariants.py` breaks each invariant on purpose.

**Black-box testing** is written from the requirements (SRS FR-P5B-xx and the
contract) only. It sends messages in and checks the replies, without knowing
how the Gateway works inside. Examples: `test_real_vhdl_ls.py` and
`tools/check_gateway.py` run the system-test cases with the real VHDL-LS over
a real WebSocket, exactly as P6A would; `test_combined.py` posts files to
`/check-and-simulate` and checks FR-P5-01 and FR-P5-02 from the outside.

**Grey box**: `test_gateway.py` talks to the Gateway over a real WebSocket like
a client, but swaps VHDL-LS for the fake bridge and looks at the session
folders and processes to prove clean-up.

## Assertions, exceptions and invariants

**Assertions** are checks that stop the program when something is not as
expected. Every test is built from them, and some assert that an exception IS
raised, e.g. `pytest.raises(InvalidFilename)` or `pytest.raises(InvariantError)`.

**Exceptions** are how the Gateway reports a problem without crashing:

| Exception | Raised when | What the client gets |
| --- | --- | --- |
| `GatewayError` | bad JSON, unknown method, bad params, not initialized, session limit | JSON-RPC error with the right code (-32700, -32600, -32601, -32602, -32002, -32603) and a category |
| `InvalidFilename`, `UnknownSession` | the session store is given a bad name or a session that does not exist (second line of defence: names are already rejected earlier as -32602) | handled inside the Gateway; a deleted session is simply skipped |
| `LanguageServerDied`, `LanguageServerTimeout`, `LanguageServerError` | VHDL-LS crashed, hung or answered with an error | -32603 `language_server_failure`; other sessions carry on |
| `InvariantError` | an invariant is broken (a bug in the Gateway) | -32603 "Internal error" (HTTP 500 on `/check-and-simulate`), never a malformed reply |
| any other `Exception` | anything unexpected | -32603 "Internal error"; the session carries on |

**Invariants** are rules that must ALWAYS be true. They are checked inside the
Gateway while it runs (`gateway/invariants.py`), not only in the tests. A
broken rule raises `InvariantError`; a real exception is used instead of
Python's `assert` because `assert` is switched off when Python runs with `-O`.

| Invariant | Checked in | Tests |
| --- | --- | --- |
| Every message sent is valid JSON-RPC 2.0: a reply has exactly one of result/error; an error has an integer code and data with status `failed`, stage `language_service`, category and detail | `check_outgoing()`, every message in `Connection.send` | `test_bad_messages_are_caught` (10), `test_broken_reply_becomes_internal_error_and_session_carries_on` |
| Diagnostics are for a file in the prescribed set, have severity 1-4 and non-negative positions, are sorted, and the summary matches ("No errors present" exactly when there is no error) | `check_publish_diagnostics()`, in the mapper and when sent | `test_bad_diagnostics_are_caught`, `test_broken_diagnostics_never_reach_the_client` |
| Completion items have labels, are unique and sorted | `check_completion()`, in the mapper | `test_bad_completion_is_caught` |
| Whatever VHDL-LS sends, the mapper's output obeys the rules above | mappers | `test_mapper_output_always_passes_for_any_input` (500 random replies) |
| At most `max_sessions` sessions are active | `check_session_limit()`, when a session opens | `test_session_limit_rule` |
| Only a folder directly inside the sessions folder is ever deleted | `check_inside()`, before every delete | `test_only_session_folders_may_be_deleted`, `test_delete_refuses_a_folder_outside_the_sessions_root` |
| `/check-and-simulate`: one diagnostics entry per file, and no GHDL job if VHDL-LS found an error (FR-P5-01) | `check_combined_result()` | `test_combined_rule_no_job_after_vhdl_ls_errors`, `test_combined_backend_returns_500_when_its_rule_breaks` |

## Requirements, one by one

Requirement numbers are the SRS's (FR-P5B-01 to FR-P5B-07, FR-P5-Joint-01).
"case n" is the n-th case printed by `tools/check_gateway.py`.

| Requirement and test case | Tests | Result |
| --- | --- | --- |
| FR-P5B-01: valid design, empty list and a "no errors" message | `test_valid_design` (real), `test_valid_design_gives_an_empty_list_and_no_errors_message`, case 1 | Pass |
| FR-P5B-01: missing semicolon, error on the assignment line, matching the recorded VHDL-LS output | `test_missing_semicolon` (real), `test_missing_semicolon_gives_an_error_on_that_line`, case 2 | Pass |
| FR-P5B-01: change with the fix, empty list and "no errors" message | `test_change_with_the_fix_clears_the_error` (real and fake), case 3 | Pass |
| FR-P5B-02: completion at the incomplete identifier includes `sig_clock` and `sig_reset` | `test_completion_after_sig` (real), `test_completion_offers_sig_clock_and_sig_reset`, case 4 | Pass |
| FR-P5B-03: valid design stored as a file set, diagnostics as expected | `test_file_is_stored_and_used_by_vhdl_ls` (real), `test_submitted_files_are_stored_in_src_and_can_go_to_p5a`, case 5 | Pass |
| FR-P5B-04: hover, go-to-definition, rename, formatting each rejected, no result | `test_unapproved_requests_never_reach_vhdl_ls` (real), `test_unapproved_requests_are_rejected_and_named`, case 6 | Pass |
| FR-P5B-04: invalid message rejected, the next valid request works | `test_invalid_message_then_valid_request` (real), `test_invalid_messages_then_a_valid_request`, case 7 | Pass |
| FR-P5B-05: VHDL-LS process killed during a session, the client gets an error response and other sessions carry on | `test_vhdl_ls_killed_mid_session_is_reported` (real), `test_vhdl_ls_dying_is_reported_and_other_sessions_carry_on`, `test_death_is_reported_even_without_a_request` | Pass |
| FR-P5B-06: no P5A or Project 6, all fixtures with the test client, twice, identical | `tools/check_gateway.py`, `test_live_server.py`, `test_two_runs_give_identical_replies` (real and fake) | Pass |
| FR-P5B-07: stored session files follow the shared model, usable by P5A | `test_initialize_creates_the_session_folder_and_starts_vhdl_ls`, `test_session_store.py`, `test_session_files_pass_p5as_own_validator` | Pass |
| FR-P5-Joint-01: one backend runs a GHDL job and a language-service case | `test_one_app_serves_both_services`, `test_session_files_run_through_p5a_simulate`, `test_real_ghdl_job_on_the_session_files` | Pass |
| UR-P5-01: success and failure can be told apart by structure | `test_success_and_failure_differ_in_structure`, case 8 | Pass |
| UR-P5-02: an error names the stage and gives the cause | `check_error` in `tests/test_gateway.py` (used on every error reply), case 9 | Pass |
| FR-P5-01 (integration): syntax error found by the Gateway, no GHDL job, diagnostics returned | `test_fr_p5_01_syntax_error_end_to_end`, `test_check_and_simulate_syntax_error_creates_no_job` | Pass |
| FR-P5-02 (integration): valid files, no diagnostics, P5A returns success and a VCD | `test_fr_p5_02_valid_design_end_to_end`, `test_check_and_simulate_clean_files_run_on_p5a` | Pass |

## Other things proven

| What | Tests | Result |
| --- | --- | --- |
| A hung VHDL-LS is stopped and reported | `test_hung_vhdl_ls_is_reported`, `test_silent_process_raises_timeout` | Pass |
| An error answer from VHDL-LS does not end the session | `test_error_answer_from_vhdl_ls_does_not_end_the_session` | Pass |
| Disconnect, shutdown, idle timeout and server stop leave no process or folder (UC-B06) | `test_disconnect_leaves_no_process_or_folder`, `test_shutdown_deletes_the_session`, `test_idle_session_is_closed`, `test_no_session_folders_are_left_behind`, `test_clean_up_finishes_even_if_the_caller_is_cancelled` | Pass |
| Design and testbench in one session: the testbench's diagnostics are updated when the entity is opened | `test_design_and_testbench_in_one_session` (real), `test_other_open_files_are_updated_when_their_diagnostics_change` | Pass |
| No real folder paths in any reply (Windows and Linux forms) | `test_no_host_paths_in_replies`, `test_scrubber_handles_windows_paths_and_uris` | Pass |
| Every name the Gateway accepts is accepted by P5A | `test_allowed_names_are_also_allowed_by_p5a` | Pass |
| P5A behaves the same inside the combined backend | `test_p5a_behaves_the_same_in_the_combined_app`, P5A's `m4_api_check.py` 13/13 | Pass |
| GHDL and VHDL-LS find the same syntax error on the same line, and agree on VHDL-2008 code | `test_syntax_error_is_found_by_both_services_on_the_same_line`, `test_vhdl_2008_code_gets_the_same_verdict_from_both` | Pass |
