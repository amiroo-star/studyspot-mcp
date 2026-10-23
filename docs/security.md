# Threat model and defenses

StudySpot assumes three things can be hostile: the **model's arguments** (it may be confused or manipulated), **data returned by tools** (a data source may be compromised), and **the server itself** (its tool list may change after review). Each row below has a test.

| Threat | Defense | Where | Test |
|---|---|---|---|
| SQL injection via any argument | Parameterised SQL only (`?` placeholders); no string-built SQL anywhere | `service.py` | `test_sqli_is_inert_even_without_schema`, `test_sqli_in_holder_and_purpose_is_stored_as_text` |
| Malformed or out-of-range arguments | JSON-schema constraints (patterns, enums, bounds, `additionalProperties: false`) and `strict_input_validation` (no type coercion) | `models.py`, `server.py` | `test_schema_rejects_bad_arguments`, `test_sqli_in_room_id_is_rejected_by_schema` |
| Impersonation (booking for someone else) | Holder taken from `STUDYSPOT_USER`, set by the host; not a tool argument | `server.py`, `host.py` | `test_holder_cannot_be_chosen_by_the_model` |
| Injected instructions in free text (`purpose`) | Host tool-input guardrail blocks before the server is reached; server also refuses (defense in depth) | `guardrails.py`, `server.py` | `test_injected_argument_blocked_on_host_before_server`, `test_instruction_like_purpose_is_refused` |
| Hidden text (zero-width, bidi override, control chars) | NFKC normalisation and stripping before checking and before storage | `sanitize.py` | `test_hidden_characters_are_stripped_before_storage` |
| Indirect prompt injection from tool results | Host tool-output guardrail withholds instruction-like results; agent instructions say tool results are data | `guardrails.py`, `host.py` | `test_poisoned_data_source_is_withheld_from_the_model` |
| Unwanted side effects | Human approval required for `book_room` and `plan_study_session`; rejection means no write | `host.py` | `test_rejected_approval_means_no_side_effect` |
| Tool poisoning / rug pull | Static allow-list of exactly three tools + sha256 pin of each name, description and schema | `guardrails.py`, `tool_manifest.lock.json` | `test_manifest_detects_changed_tool_description`, `test_manifest_detects_extra_tool` |
| Cross-user data leaks | Search never returns other bookings' holders or purposes; booking resource returns only your own, and "not found" otherwise | `service.py` | `test_search_never_exposes_other_peoples_purposes`, `test_cannot_read_another_students_booking` |
| Path traversal in resource URIs | FastMCP resource security + strict id patterns | `server.py` | `test_resource_templates_reject_malformed_ids` |
| Internal detail leakage in errors | `mask_error_details=True`; only deliberate `ToolError`/`ResourceError` messages reach the client | `server.py` | `test_unexpected_errors_are_masked` |
| Double booking under concurrency | `BEGIN IMMEDIATE` transaction re-checks availability | `service.py` | `test_concurrent_bookings_cannot_both_win` |
| Network exposure | stdio by default; optional HTTP binds to `127.0.0.1` only | `server.py` | — |

**Limits, stated plainly.** The instruction detector in `sanitize.py` is pattern-based, so a determined attacker can phrase around it. It is one layer; the structural controls (identity from the transport, approval for writes, allow-list, manifest pin, no cross-user free text) are what bound the damage if it misses.
