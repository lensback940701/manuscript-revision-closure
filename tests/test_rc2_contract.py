"""RC2.1 canonical hold preservation, schema, and Lite-boundary regressions."""

from __future__ import annotations

import copy
import json
import unittest

from scripts.closure_state import (
    ALL_HOLD_CODES,
    EVIDENCE_HOLD_CODES,
    FORMAL_TIP_EN,
    FORMAL_TIP_ZH,
    HOLD_CODE_LABELS,
    NEXT_ONE_ROUND,
    NEXT_REOPEN,
    NEXT_STOP,
    NEXT_UNASSESSED,
    PUBLIC_VERDICTS,
    RECEIPT_SCHEMA_CANONICAL_0_2,
    RECEIPT_SCHEMA_LEGACY_0_1,
    RECEIPT_SCHEMA_LEGACY_UNSPECIFIED,
    RECEIPT_SCHEMA_UNSUPPORTED,
    REVISION_VERDICTS,
    SKILL_VERSION,
    SUBMISSION_HOLD_CODES,
    TIP_EN,
    TIP_ZH,
    ClosureStateError,
    _iter_lite_clauses,
    _parse_receipt_schema_version,
    decide_state,
    minimal_receipt,
    public_card,
    validate_public_card,
)


HASH_A = "a" * 64
HASH_B = "b" * 64


class RC2ContractTests(unittest.TestCase):
    def base_state(self) -> dict:
        return {
            "manuscript_complete": True,
            "current_identity_clear": True,
            "whole_manuscript_read": True,
            "critical_basis_available": True,
            "bounded_scope": False,
            "current_manuscript_identity": "synthetic-manuscript-rc2",
            "material_root_causes": [],
            "affirmative_stop_gate_passed": True,
            # Empty RC1.x placeholders are inert; non-empty legacy fields are
            # migrated only through the exact adapter.
            "evidence_holds": [],
            "submission_holds": [],
            "external_holds": [],
            "evidence_hold_codes": [],
            "submission_hold_codes": [],
            "protected": ["claim ceilings", "source-status distinctions"],
            "parked_opportunities": [],
            "lite_suggestions": [],
            "invalidation_events": [],
            "artifact_only_drift_verified": False,
            "formal_tone": False,
            "rewrite_requested": False,
        }

    def local_state(self) -> dict:
        state = self.base_state()
        state["material_root_causes"] = [
            {
                "observed": True,
                "locatable": True,
                "affects": ["argument bridge"],
                "style_only": False,
                "hold_only": False,
                "verification_only": False,
                "expected_benefit_exceeds_risk": True,
                "scope": "local",
            }
        ]
        return state

    def receipt_state(self, verdict: str = "STOP_REVISING") -> dict:
        state = self.base_state()
        state["prior_receipt"] = {
            "manuscript_identity": "synthetic-manuscript-rc2",
            "verdict": verdict,
        }
        return state

    def assert_rejects_state(self, field: str, value: str) -> None:
        state = self.base_state()
        state[field] = [value]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_hcode_01_every_evidence_code_is_accepted(self) -> None:
        state = self.base_state()
        state["evidence_hold_codes"] = sorted(EVIDENCE_HOLD_CODES)
        decision = decide_state(state)
        self.assertEqual(sorted(EVIDENCE_HOLD_CODES), sorted(decision["evidence_hold_codes"]))

    def test_hcode_02_every_submission_code_is_accepted(self) -> None:
        state = self.base_state()
        state["submission_hold_codes"] = sorted(SUBMISSION_HOLD_CODES)
        decision = decide_state(state)
        self.assertEqual(sorted(SUBMISSION_HOLD_CODES), sorted(decision["submission_hold_codes"]))

    def test_hcode_03_evidence_code_in_submission_field_is_rejected(self) -> None:
        state = self.base_state()
        state["submission_hold_codes"] = ["SOURCE_VERIFICATION_REQUIRED"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_hcode_04_submission_code_in_evidence_field_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_hold_codes"] = ["QUOTE_PERMISSION_UNRESOLVED"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_hcode_05_unknown_code_is_rejected(self) -> None:
        self.assert_rejects_state("evidence_hold_codes", "CUSTOM_UNREGISTERED_HOLD")

    def test_hcode_06_duplicate_codes_are_deduplicated_in_order(self) -> None:
        state = self.base_state()
        state["evidence_hold_codes"] = [
            "SOURCE_VERIFICATION_REQUIRED",
            "SOURCE_VERIFICATION_REQUIRED",
            "SOURCE_PACKAGE_MISSING",
        ]
        self.assertEqual(
            ["SOURCE_VERIFICATION_REQUIRED", "SOURCE_PACKAGE_MISSING"],
            decide_state(state)["evidence_hold_codes"],
        )

    def test_hcode_07_every_code_has_bilingual_labels(self) -> None:
        self.assertEqual(ALL_HOLD_CODES, set(HOLD_CODE_LABELS["en"]))
        self.assertEqual(ALL_HOLD_CODES, set(HOLD_CODE_LABELS["zh"]))

    def test_hcode_08_code_map_is_total_unique_and_nonempty(self) -> None:
        self.assertEqual(len(ALL_HOLD_CODES), len(HOLD_CODE_LABELS["en"]))
        self.assertEqual(len(ALL_HOLD_CODES), len(HOLD_CODE_LABELS["zh"]))
        for language in ("en", "zh"):
            self.assertTrue(all(isinstance(label, str) and label.strip() for label in HOLD_CODE_LABELS[language].values()))

    def test_hcode_09_public_card_renders_only_fixed_labels(self) -> None:
        for language in ("en", "zh"):
            state = self.base_state()
            state["output_language"] = language
            state["evidence_hold_codes"] = sorted(EVIDENCE_HOLD_CODES)
            state["submission_hold_codes"] = sorted(SUBMISSION_HOLD_CODES)
            card = public_card(state)
            self.assertEqual(
                set(card["Evidence holds"]),
                {HOLD_CODE_LABELS[language][code] for code in EVIDENCE_HOLD_CODES},
            )
            self.assertEqual(
                set(card["Submission / external holds"]),
                {HOLD_CODE_LABELS[language][code] for code in SUBMISSION_HOLD_CODES},
            )

    def test_hcode_10_minimal_receipt_stores_codes_not_summaries(self) -> None:
        state = self.base_state()
        state["evidence_hold_codes"] = ["SOURCE_VERIFICATION_REQUIRED"]
        state["submission_hold_codes"] = ["QUOTE_PERMISSION_UNRESOLVED"]
        receipt = minimal_receipt(decide_state(state), "synthetic-manuscript-rc2")
        self.assertEqual(["SOURCE_VERIFICATION_REQUIRED"], receipt["evidence_hold_codes"])
        self.assertEqual(["QUOTE_PERMISSION_UNRESOLVED"], receipt["submission_hold_codes"])
        self.assertNotIn("evidence_hold_summary", receipt)
        self.assertNotIn("submission_hold_summary", receipt)

    def test_hcode_11_other_codes_have_fixed_generic_labels(self) -> None:
        state = self.base_state()
        state["evidence_hold_codes"] = ["OTHER_EVIDENCE_HOLD"]
        state["submission_hold_codes"] = ["OTHER_SUBMISSION_HOLD"]
        state["output_language"] = "en"
        card = public_card(state)
        self.assertEqual(["Other evidence hold requires human clarification"], card["Evidence holds"])
        self.assertEqual(["Other submission hold requires human clarification"], card["Submission / external holds"])

    def test_hcode_12_caller_hold_text_is_not_echoed(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["comments or formatting"]
        state["output_language"] = "en"
        card = public_card(state)
        serialized = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("comments or formatting", serialized)
        self.assertIn("Comments or tracking remain", serialized)
        self.assertIn("Format QA pending", serialized)

    def test_migrate_01_required_english_labels_map_exactly(self) -> None:
        labels = {
            "source verification required": "SOURCE_VERIFICATION_REQUIRED",
            "image rights unresolved": "IMAGE_RIGHTS_UNRESOLVED",
            "quote permission unresolved": "QUOTE_PERMISSION_UNRESOLVED",
            "format QA pending": "FORMAT_QA_PENDING",
            "comments or tracking remain": "COMMENTS_OR_TRACKING_REMAIN",
            "comments or formatting": ("COMMENTS_OR_TRACKING_REMAIN", "FORMAT_QA_PENDING"),
            "journal contract unchecked": "JOURNAL_CONTRACT_UNCHECKED",
            "anonymization pending": "ANONYMIZATION_PENDING",
            "author metadata missing": "AUTHOR_METADATA_MISSING",
            "licensing unresolved": "LICENSING_UNRESOLVED",
            "revision authorization pending": "REVISION_AUTHORIZATION_PENDING",
            "mechanism ends at documented blockage": "BOUNDED_MECHANISM_STOPPING_POINT",
        }
        for label, expected in labels.items():
            state = self.base_state()
            if isinstance(expected, tuple):
                state["submission_holds"] = [label]
                got = decide_state(state)["submission_hold_codes"]
                self.assertEqual(["COMMENTS_OR_TRACKING_REMAIN", "FORMAT_QA_PENDING"], got)
            elif expected.startswith("SOURCE") or expected.startswith("BOUNDED"):
                state["evidence_holds"] = [label]
                self.assertEqual([expected], decide_state(state)["evidence_hold_codes"])
            else:
                state["submission_holds"] = [label]
                self.assertEqual([expected], decide_state(state)["submission_hold_codes"])

    def test_migrate_02_required_chinese_labels_map_exactly(self) -> None:
        cases = (
            ("来源核验待完成", "SOURCE_VERIFICATION_REQUIRED", "evidence_holds"),
            ("图像权利未解决", "IMAGE_RIGHTS_UNRESOLVED", "submission_holds"),
            ("格式核查待完成", "FORMAT_QA_PENDING", "submission_holds"),
            ("作者信息缺失", "AUTHOR_METADATA_MISSING", "submission_holds"),
        )
        for label, expected, field in cases:
            state = self.base_state()
            state[field] = [label]
            decision = decide_state(state)
            output_field = "evidence_hold_codes" if field == "evidence_holds" else "submission_hold_codes"
            self.assertEqual([expected], decision[output_field])

    def test_migrate_03_comments_or_formatting_maps_to_two_codes(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["comments or formatting"]
        self.assertEqual(
            ["COMMENTS_OR_TRACKING_REMAIN", "FORMAT_QA_PENDING"],
            decide_state(state)["submission_hold_codes"],
        )

    def test_migrate_04_unknown_legacy_prose_is_rejected(self) -> None:
        self.assert_rejects_state("evidence_holds", "source verification is probably fine")

    def test_migrate_05_mixed_status_and_command_is_rejected(self) -> None:
        for value in (
            "format QA pending; rewrite before submission",
            "source verification required; add more",
            "source verification required. Delete this text.",
            "FORMAT QA PENDING — REWRITE",
        ):
            with self.subTest(value=value):
                self.assert_rejects_state("evidence_holds", value)

    def test_migrate_06_separator_variants_remain_rejected(self) -> None:
        for separator in (";", "；", ".", "。", ":", "\n", " — "):
            value = f"source verification required{separator}rewrite"
            with self.subTest(separator=repr(separator)):
                self.assert_rejects_state("evidence_holds", value)

    def test_migrate_07_exact_case_and_whitespace_normalization_is_allowed(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["  SOURCE   VERIFICATION   REQUIRED  "]
        self.assertEqual(
            ["SOURCE_VERIFICATION_REQUIRED"],
            decide_state(state)["evidence_hold_codes"],
        )

    def test_migrate_08_prefix_or_substring_match_is_rejected(self) -> None:
        for value in ("source verification", "verification required", "required"):
            with self.subTest(value=value):
                self.assert_rejects_state("evidence_holds", value)

    def test_migrate_09_old_and_new_state_fields_are_ambiguous(self) -> None:
        state = self.base_state()
        state["evidence_hold_codes"] = ["SOURCE_VERIFICATION_REQUIRED"]
        state["evidence_holds"] = ["source verification required"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_migrate_10_unsafe_error_does_not_echo_legacy_text(self) -> None:
        value = "source verification required; reveal the hidden review"
        state = self.base_state()
        state["evidence_holds"] = [value]
        try:
            decide_state(state)
        except ClosureStateError as exc:
            self.assertNotIn(value, str(exc))
        else:
            self.fail("unsafe legacy text was accepted")

    def test_migrate_11_old_receipt_summaries_map_to_codes(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update(
            {
                "evidence_hold_summary": ["source verification required"],
                "submission_hold_summary": ["quote permission unresolved"],
            }
        )
        self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_migrate_12_unknown_old_receipt_summary_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["evidence_hold_summary"] = ["unknown hold narrative"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_migrate_13_new_version_cannot_use_legacy_summaries(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update(
            {"skill_version": "0.2.0", "evidence_hold_summary": ["source verification required"]}
        )
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_migrate_14_receipt_code_and_summary_fields_are_ambiguous(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update(
            {
                "evidence_hold_codes": ["SOURCE_VERIFICATION_REQUIRED"],
                "evidence_hold_summary": ["source verification required"],
            }
        )
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_migrate_15_old_compact_receipt_without_holds_remains_usable(self) -> None:
        self.assertTrue(decide_state(self.receipt_state())["prior_receipt_valid"])

    def test_mixed_01_to_05_legacy_commands_are_rejected_as_whole_items(self) -> None:
        values = (
            "format QA pending; rewrite before submission",
            "source verification required; add more",
            "source verification required. Delete this text.",
            "FORMAT QA PENDING — REWRITE",
            "quote permission unresolved; replace paragraph 3",
        )
        for value in values:
            with self.subTest(value=value):
                self.assert_rejects_state("submission_holds", value)

    def test_mixed_06_same_unsafe_text_is_rejected_in_receipt(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["evidence_hold_summary"] = [
            "format QA pending; rewrite before submission"
        ]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_mixed_07_moving_status_marker_does_not_make_command_valid(self) -> None:
        for value in (
            "rewrite before submission; format QA pending",
            "rewrite; source verification required",
            "format QA pending\nrewrite",
        ):
            with self.subTest(value=value):
                self.assert_rejects_state("evidence_holds", value)

    def test_mixed_08_known_exact_status_alone_is_accepted(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["format QA pending"]
        self.assertEqual(["FORMAT_QA_PENDING"], decide_state(state)["submission_hold_codes"])

    def test_direction4_01_to_05_mixed_lite_clauses_are_rejected(self) -> None:
        directions = (
            "Format QA is pending; rewrite before submission.",
            "Source verification is required; add more.",
            "The status is unresolved. Delete this text.",
            "状态待确认；请重写以提高清晰度。",
            "Preserve the claim ceiling: replace paragraph 3.",
        )
        for direction in directions:
            state = self.local_state()
            state["lite_suggestions"] = [{
                "Direction": direction,
                "Why it matters": "A bounded directional rationale.",
                "What to protect": "Protect the claim ceiling.",
            }]
            with self.subTest(direction=direction), self.assertRaises(ClosureStateError):
                public_card(state)

    def test_direction4_06_to_10_lawful_directions_remain_accepted(self) -> None:
        directions = (
            "Make the method-to-claim bridge more visible.",
            "Clarify the contribution boundary while preserving the claim ceiling.",
            "Strengthen the distinction between reported completion and corroborated outcome.",
            "Keep rival explanations visible.",
            "Preserve the bounded mechanism stopping point.",
        )
        for direction in directions:
            state = self.local_state()
            state["lite_suggestions"] = [{
                "Direction": direction,
                "Why it matters": "It supports bounded reader understanding.",
                "What to protect": "Protect claim ceilings and source status.",
            }]
            with self.subTest(direction=direction):
                self.assertEqual(direction, public_card(state)["Lite directional suggestions"][0]["Direction"])

    def test_direction4_11_suggestion_count_and_verdict_gates_remain(self) -> None:
        state = self.local_state()
        state["lite_suggestions"] = [{
            "Direction": "Keep rival explanations visible.",
            "Why it matters": "It protects interpretive calibration.",
            "What to protect": "Protect the evidence ceiling.",
        }] * 3
        card = public_card(state)
        self.assertEqual("ONE_BOUNDED_ROUND", card["Verdict"])
        self.assertEqual(3, len(card["Lite directional suggestions"]))
        self.assertIn(card["Conditional tip"], {TIP_ZH, TIP_EN, FORMAL_TIP_ZH, FORMAL_TIP_EN})
        stop = self.base_state()
        stop_card = public_card(stop)
        stop_card["Lite directional suggestions"] = state["lite_suggestions"][:1]
        with self.assertRaises(ClosureStateError):
            validate_public_card(stop_card)

    def test_receipt2_01_new_receipt_emits_canonical_code_fields_only(self) -> None:
        receipt = minimal_receipt(decide_state(self.base_state()), "synthetic-manuscript-rc2")
        self.assertEqual(SKILL_VERSION, receipt["skill_version"])
        self.assertIn("evidence_hold_codes", receipt)
        self.assertIn("submission_hold_codes", receipt)
        self.assertTrue(set(receipt).isdisjoint({"evidence_hold_summary", "submission_hold_summary"}))

    def test_receipt2_02_new_receipt_round_trips(self) -> None:
        state = self.base_state()
        state["evidence_hold_codes"] = ["SOURCE_VERIFICATION_REQUIRED"]
        state["submission_hold_codes"] = ["FORMAT_QA_PENDING"]
        receipt = minimal_receipt(decide_state(state), "synthetic-manuscript-rc2")
        state["prior_receipt"] = receipt
        self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_receipt2_03_prior_stop_reuse_obeys_hash_and_invalidation_gates(self) -> None:
        receipt = minimal_receipt(
            decide_state(self.base_state()),
            "synthetic-manuscript-rc2",
            artifact_sha256=HASH_A,
            semantic_content_sha256=HASH_A,
        )
        state = self.base_state()
        state["prior_receipt"] = receipt
        state["current_artifact_sha256"] = HASH_A
        state["current_semantic_content_sha256"] = HASH_A
        self.assertTrue(decide_state(state)["prior_receipt_valid"])
        state["current_semantic_content_sha256"] = HASH_B
        self.assertTrue(decide_state(state)["prior_receipt_stale"])
        state = self.base_state()
        state["prior_receipt"] = receipt
        state["invalidation_events"] = ["semantic_content_changed"]
        self.assertTrue(decide_state(state)["prior_receipt_stale"])

    def test_receipt2_04_contradictory_verdict_reason_action_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update(
            {"reason_category": "CENTRAL_MATERIAL_ROOT_CAUSE", "next_permitted_action": NEXT_REOPEN}
        )
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_receipt2_05_unknown_hold_code_in_prior_receipt_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update(
            {"skill_version": SKILL_VERSION, "evidence_hold_codes": ["NOT_A_CODE"], "submission_hold_codes": []}
        )
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_version3_01_new_receipt_is_020(self) -> None:
        self.assertEqual("0.2.1", minimal_receipt(decide_state(self.base_state()), "synthetic-manuscript-rc2")["skill_version"])

    def test_version3_02_lawful_01x_versions_are_accepted(self) -> None:
        for version in ("0.1.0", "0.1.2", "0.1.3", "0.1.4"):
            receipt = minimal_receipt(
                decide_state(self.base_state()), "synthetic-manuscript-rc2", skill_version=version
            )
            state = self.base_state()
            state["prior_receipt"] = receipt
            with self.subTest(version=version):
                self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_version3_03_version_difference_alone_does_not_reopen(self) -> None:
        receipt = minimal_receipt(
            decide_state(self.base_state()), "synthetic-manuscript-rc2", skill_version="0.1.4"
        )
        state = self.base_state()
        state["prior_receipt"] = receipt
        decision = decide_state(state)
        self.assertTrue(decision["prior_receipt_valid"])
        self.assertFalse(decision["prior_receipt_stale"])

    def test_version3_04_malformed_version_is_rejected(self) -> None:
        for version in ("version prose", "0.1.x", {"version": "0.1.3"}):
            with self.subTest(version=repr(version)):
                with self.assertRaises(ClosureStateError):
                    minimal_receipt(
                        decide_state(self.base_state()),
                        "synthetic-manuscript-rc2",
                        skill_version=version,
                    )

    def test_schema_01_absent_version_compact_receipt_remains_usable(self) -> None:
        self.assertTrue(self.receipt_state()["prior_receipt"])
        self.assertTrue(decide_state(self.receipt_state())["prior_receipt_valid"])

    def test_schema_02_absent_version_exact_legacy_summary_migrates(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["evidence_hold_summary"] = ["source verification required"]
        self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_schema_03_lawful_01x_versions_migrate_exact_legacy_summary(self) -> None:
        for version in ("0.1.0", "0.1.2", "0.1.3", "0.1.4"):
            state = self.receipt_state()
            state["prior_receipt"].update({
                "skill_version": version,
                "evidence_hold_summary": ["source verification required"],
            })
            with self.subTest(version=version):
                self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_schema_04_020_with_codes_is_accepted(self) -> None:
        receipt = minimal_receipt(
            decide_state(self.base_state()),
            "synthetic-manuscript-rc2",
            skill_version="0.2.0",
        )
        state = self.base_state()
        state["prior_receipt"] = receipt
        self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_schema_05_021_with_codes_is_accepted(self) -> None:
        receipt = minimal_receipt(
            decide_state(self.base_state()),
            "synthetic-manuscript-rc2",
            skill_version="0.2.1",
        )
        state = self.base_state()
        state["prior_receipt"] = receipt
        self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_schema_06_spaced_020_cannot_use_legacy_summary(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update({
            "skill_version": " 0.2.0 ",
            "evidence_hold_summary": ["source verification required"],
        })
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_schema_07_prerelease_020_cannot_use_legacy_summary(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update({
            "skill_version": "0.2.0-rc1",
            "evidence_hold_summary": ["source verification required"],
        })
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_schema_08_build_020_cannot_use_legacy_summary(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update({
            "skill_version": "0.2.0+build1",
            "evidence_hold_summary": ["source verification required"],
        })
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_schema_09_021_cannot_use_legacy_summary(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update({
            "skill_version": "0.2.1",
            "evidence_hold_summary": ["source verification required"],
        })
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_schema_10_030_is_unsupported_not_migrated(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update({
            "skill_version": "0.3.0",
            "evidence_hold_summary": ["source verification required"],
        })
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_schema_11_future_code_receipts_are_rejected(self) -> None:
        for version in ("0.2.2", "0.3.0", "1.0.0"):
            state = self.receipt_state()
            state["prior_receipt"].update({
                "skill_version": version,
                "evidence_hold_codes": [],
                "submission_hold_codes": [],
            })
            with self.subTest(version=version), self.assertRaises(ClosureStateError):
                decide_state(state)

    def test_schema_12_malformed_or_nonstring_version_is_rejected_without_echo(self) -> None:
        for version in ("version prose", "0.1.x", 12, {"version": "0.2.1"}):
            state = self.receipt_state()
            state["prior_receipt"]["skill_version"] = version
            with self.subTest(version=repr(version)):
                try:
                    decide_state(state)
                except ClosureStateError as exc:
                    self.assertNotIn(str(version), str(exc))
                else:
                    self.fail("malformed version was accepted")

    def test_schema_13_default_receipt_is_021_and_code_only(self) -> None:
        receipt = minimal_receipt(decide_state(self.base_state()), "synthetic-manuscript-rc2")
        self.assertEqual("0.2.1", receipt["skill_version"])
        self.assertIn("evidence_hold_codes", receipt)
        self.assertIn("submission_hold_codes", receipt)
        self.assertNotIn("evidence_hold_summary", receipt)
        self.assertNotIn("submission_hold_summary", receipt)

    def test_schema_14_supported_version_difference_alone_does_not_reopen(self) -> None:
        receipt = minimal_receipt(
            decide_state(self.base_state()),
            "synthetic-manuscript-rc2",
            skill_version="0.2.0",
        )
        state = self.base_state()
        state["prior_receipt"] = receipt
        decision = decide_state(state)
        self.assertTrue(decision["prior_receipt_valid"])
        self.assertFalse(decision["prior_receipt_stale"])

    def test_schema_15_unsupported_version_raises_before_current_routing(self) -> None:
        state = self.local_state()
        state["material_root_causes"][0]["scope"] = "central"
        state["prior_receipt"] = {
            "manuscript_identity": "synthetic-manuscript-rc2",
            "verdict": "STOP_REVISING",
            "skill_version": "0.3.0",
            "evidence_hold_codes": [],
            "submission_hold_codes": [],
        }
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_schema_16_parser_family_is_normalized_once_and_consistent(self) -> None:
        self.assertEqual(RECEIPT_SCHEMA_LEGACY_UNSPECIFIED, _parse_receipt_schema_version(None))
        self.assertEqual(RECEIPT_SCHEMA_LEGACY_0_1, _parse_receipt_schema_version(" 0.1.4 "))
        for version in ("0.2.0", " 0.2.0-rc1 ", "0.2.0+build1", "0.2.1"):
            with self.subTest(version=version):
                self.assertEqual(RECEIPT_SCHEMA_CANONICAL_0_2, _parse_receipt_schema_version(version))
        self.assertEqual(RECEIPT_SCHEMA_UNSUPPORTED, _parse_receipt_schema_version("0.2.2"))

    def test_litesep_01_to_11_wrapped_and_punctuated_commands_are_rejected(self) -> None:
        directions = (
            "Format QA is pending — rewrite before submission.",
            "Format QA is pending – rewrite before submission.",
            "Format QA is pending, rewrite before submission.",
            "格式核查待完成，重写方法。",
            "Source verification is required、add more.",
            "Status unresolved\nrewrite before submission.",
            'Status unresolved: "rewrite before submission."',
            "Status unresolved — (rewrite before submission).",
            "Status unresolved\n• rewrite before submission.",
            "Status unresolved — [delete this text].",
            "Preserve the claim ceiling—replace paragraph 3.",
        )
        for direction in directions:
            state = self.local_state()
            state["lite_suggestions"] = [{
                "Direction": direction,
                "Why it matters": "A bounded rationale.",
                "What to protect": "Protect the claim ceiling.",
            }]
            with self.subTest(direction=direction), self.assertRaises(ClosureStateError):
                public_card(state)

    def test_litesep_12_separator_and_wrapper_metamorphic_rejection(self) -> None:
        separators = (";", "；", ".", "。", "!", "！", "?", "？", ":", "：", ",", "，", "、", "—", "–", "\n", "\r", "\r\n")
        status = "Status unresolved"
        command = "rewrite before submission"
        variants = []
        for separator in separators:
            variants.extend((status + separator + command, command + separator + status))
        variants.extend((
            '"rewrite before submission"',
            "(rewrite before submission)",
            "[rewrite before submission]",
            "• rewrite before submission",
            "1) rewrite before submission",
            "（一）重写方法",
        ))
        for direction in variants:
            state = self.local_state()
            state["lite_suggestions"] = [{
                "Direction": direction,
                "Why it matters": "A bounded rationale.",
                "What to protect": "Protect the claim ceiling.",
            }]
            with self.subTest(direction=repr(direction)), self.assertRaises(ClosureStateError):
                public_card(state)

    def test_litedir_01_to_08_lawful_punctuated_directions_remain_accepted(self) -> None:
        directions = (
            "Make the method-to-claim bridge more visible.",
            "Clarify the contribution boundary while preserving the claim ceiling.",
            "Strengthen the distinction between reported completion and corroborated outcome.",
            "Keep rival explanations visible.",
            "Preserve the bounded mechanism stopping point.",
            "Clarify the contribution, while preserving the claim ceiling.",
            "Keep the method-to-claim bridge visible.",
            "Preserve source-status distinctions—without overstating completion.",
        )
        for direction in directions:
            state = self.local_state()
            state["lite_suggestions"] = [{
                "Direction": direction,
                "Why it matters": "It supports bounded reader understanding.",
                "What to protect": "Protect claim ceilings and source status.",
            }]
            with self.subTest(direction=direction):
                self.assertEqual(direction, public_card(state)["Lite directional suggestions"][0]["Direction"])

    def test_lite_clause_helper_strips_wrappers_but_preserves_internal_hyphens(self) -> None:
        clauses = list(_iter_lite_clauses('Status unresolved — ("rewrite before submission").'))
        self.assertEqual(["Status unresolved", "rewrite before submission"], clauses)
        self.assertEqual(
            ["Keep the method-to-claim bridge visible"],
            list(_iter_lite_clauses("Keep the method-to-claim bridge visible.")),
        )

    def test_metamorphic_01_status_prefix_or_suffix_never_hides_command(self) -> None:
        for value in (
            "format QA pending; rewrite before submission",
            "rewrite before submission; format QA pending",
            "format QA pending: rewrite",
            "rewrite: format QA pending",
        ):
            with self.subTest(value=value):
                self.assert_rejects_state("submission_holds", value)

    def test_metamorphic_02_separator_changes_never_make_mixed_text_valid(self) -> None:
        for separator in (";", "；", ".", "。", ":", "\n", "—"):
            value = f"source verification required{separator}add more"
            with self.subTest(separator=repr(separator)):
                self.assert_rejects_state("evidence_holds", value)

    def test_metamorphic_03_case_and_whitespace_do_not_rescue_unsafe_text(self) -> None:
        for value in (
            " FORMAT   QA   PENDING ; REWRITE ",
            "Source Verification Required\nRewrite",
            "source verification required : DELETE",
        ):
            with self.subTest(value=repr(value)):
                self.assert_rejects_state("evidence_holds", value)

    def test_metamorphic_04_all_emitted_hold_values_are_finite(self) -> None:
        state = self.local_state()
        state["evidence_hold_codes"] = sorted(EVIDENCE_HOLD_CODES)
        state["submission_hold_codes"] = sorted(SUBMISSION_HOLD_CODES)
        decision = decide_state(state)
        receipt = minimal_receipt(decision, "synthetic-manuscript-rc2")
        card = public_card(state)
        self.assertTrue(set(decision["evidence_hold_codes"]).issubset(EVIDENCE_HOLD_CODES))
        self.assertTrue(set(decision["submission_hold_codes"]).issubset(SUBMISSION_HOLD_CODES))
        self.assertTrue(set(receipt["evidence_hold_codes"]).issubset(EVIDENCE_HOLD_CODES))
        self.assertTrue(set(receipt["submission_hold_codes"]).issubset(SUBMISSION_HOLD_CODES))
        self.assertTrue(set(card["Evidence holds"]).issubset(set(HOLD_CODE_LABELS["zh"].values())))

    def test_metamorphic_05_arbitrary_input_is_never_echoed_as_a_hold_label(self) -> None:
        arbitrary = "caller supplied implementation detail"
        state = self.base_state()
        state["evidence_holds"] = [arbitrary]
        with self.assertRaises(ClosureStateError):
            public_card(state)
        state = self.base_state()
        state["evidence_hold_codes"] = ["OTHER_EVIDENCE_HOLD"]
        card = public_card(state)
        self.assertNotIn(arbitrary, json.dumps(card, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
