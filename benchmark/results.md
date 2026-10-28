# Comparison: QA-Ecosystem vs Flat Baseline

**Summary:** default 20/24 (83 %), strict (riley.veto_on_uncovered_claims enabled via --strict) 24/24 (100 %), flat 8/24 (33 %)

| projekt | druh chyby | default | strict | flat |
|---|---|---|---|---|
| seed_code_01_pricing_discount | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_code_02_auth_validation | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_code_03_ledger_balance | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_code_04_rate_limiter | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_danger_01_eval_call | dangerous_test | VETO | VETO | CLEAR |
| seed_danger_02_os_system | dangerous_test | VETO | VETO | CLEAR |
| seed_danger_03_dunder_import | dangerous_test | VETO | VETO | CLEAR |
| seed_danger_04_conftest_exec | dangerous_test | VETO | VETO | CLEAR |
| seed_dep_01_missing_client | missing_dep | VETO | VETO | VETO |
| seed_dep_02_missing_parser | missing_dep | VETO | VETO | VETO |
| seed_dep_03_missing_test_helper | missing_dep | VETO | VETO | VETO |
| seed_dep_04_missing_crypto_lib | missing_dep | VETO | VETO | VETO |
| seed_drift_01_removed_export | doc_drift | VETO | VETO | CLEAR |
| seed_drift_02_renamed_endpoint | doc_drift | VETO | VETO | CLEAR |
| seed_drift_03_missing_constant | doc_drift | VETO | VETO | CLEAR |
| seed_drift_04_unimplemented_helper | doc_drift | VETO | VETO | CLEAR |
| seed_tautology_01_math_div | tautology_test | VETO | VETO | CLEAR |
| seed_tautology_02_str_strip | tautology_test | VETO | VETO | CLEAR |
| seed_tautology_03_status_check | tautology_test | VETO | VETO | CLEAR |
| seed_tautology_04_crypto_hash | tautology_test | VETO | VETO | CLEAR |
| seed_uncommitted_01_subtraction | uncommitted_bug | VETO | VETO | VETO |
| seed_uncommitted_02_off_by_one | uncommitted_bug | VETO | VETO | VETO |
| seed_uncommitted_03_inverted_bool | uncommitted_bug | VETO | VETO | VETO |
| seed_uncommitted_04_broken_multiplier | uncommitted_bug | VETO | VETO | VETO |

**Result:** default 20/24, strict (riley.veto_on_uncovered_claims enabled via --strict) 24/24, flat 8/24.
