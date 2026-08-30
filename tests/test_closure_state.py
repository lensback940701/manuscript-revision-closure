from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.closure_state import (  # noqa: E402
    ClosureStateError,
    FORMAL_TIP_EN,
    FORMAL_TIP_ZH,
    NEXT_ONE_ROUND,
    NEXT_PRIOR_STOP,
    NEXT_REOPEN,
    NEXT_STOP,
    NEXT_UNASSESSED,
    PUBLIC_PROHIBITED_KEYS,
    SKILL_VERSION,
    TIP_EN,
    TIP_ZH,
    decide_state,
    minimal_receipt,
    public_card,
    validate_public_card,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "closure_cases.json"
CASES = {item["id"]: item for item in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))}
HASH_A = "a" * 64
HASH_B = "b" * 64


class ClosureStateTests(unittest.TestCase):
    def base_state(self) -> dict:
        return {
            "manuscript_complete": True,
            "current_identity_clear": True,
            "whole_manuscript_read": True,
            "critical_basis_available": True,
            "bounded_scope": False,
            "current_manuscript_identity": "synthetic-manuscript-1",
            "material_root_causes": [],
            "affirmative_stop_gate_passed": True,
            "evidence_holds": [],
            "submission_holds": [],
            "external_holds": [],
            "protected": ["claim ceilings", "source-status distinctions", "rivals and contradictions"],
            "parked_opportunities": [],
            "lite_suggestions": [],
            "invalidation_events": [],
            "artifact_only_drift_verified": False,
            "formal_tone": False,
            "rewrite_requested": False,
        }

    def state_for(self, case_id: str) -> dict:
        state = self.base_state()
        state.update(CASES[case_id]["state"])
        return state

    def assert_case(self, case_id: str) -> dict:
        state = self.state_for(case_id)
        decision = decide_state(state)
        self.assertEqual(CASES[case_id]["expected_verdict"], decision["verdict"])
        return state

    def receipt_state(self, verdict: str = "STOP_REVISING") -> dict:
        state = self.base_state()
        state["prior_receipt"] = {
            "manuscript_identity": "synthetic-manuscript-1",
            "verdict": verdict,
        }
        return state

    def local_material_state(self) -> dict:
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

    def test_case_01_ready_manuscript_stops(self) -> None:
        card = public_card(self.assert_case("CASE-01"))
        self.assertEqual("STOP_REVISING", card["Verdict"])
        self.assertIsNone(card["Conditional tip"])

    def test_case_02_rights_hold_does_not_reopen(self) -> None:
        state = self.assert_case("CASE-02")
        state["output_language"] = "en"
        card = public_card(state)
        self.assertEqual(["Image rights unresolved"], card["Submission / external holds"])
        self.assertIsNone(card["Conditional tip"])

    def test_case_03_comments_and_formatting_are_submission_hold(self) -> None:
        state = self.assert_case("CASE-03")
        state["output_language"] = "en"
        card = public_card(state)
        self.assertEqual("STOP_REVISING", card["Verdict"])
        self.assertEqual(
            ["Comments or tracking remain", "Format QA pending"],
            card["Submission / external holds"],
        )

    def test_case_04_moderate_optional_items_do_not_add_to_reopen(self) -> None:
        card = public_card(self.assert_case("CASE-04"))
        self.assertEqual("STOP_REVISING", card["Verdict"])

    def test_case_05_local_material_bridge_is_one_round(self) -> None:
        state = self.state_for("CASE-05")
        state["lite_suggestions"] = [
            {
                "Direction": "Make the method-to-claim bridge more visible.",
                "Why it matters": "Readers need to see how the supported finding reaches the contribution.",
                "What to protect": "Preserve the existing claim ceiling and source-status distinctions.",
            }
        ]
        card = public_card(self.assert_case("CASE-05") | {"lite_suggestions": state["lite_suggestions"]})
        self.assertEqual("ONE_BOUNDED_ROUND", card["Verdict"])
        self.assertTrue(card["Conditional tip"])
        self.assertEqual(1, len(card["Lite directional suggestions"]))

    def test_case_06_central_method_failure_reopens(self) -> None:
        card = public_card(self.assert_case("CASE-06"))
        self.assertEqual("REOPEN_SUBSTANTIVE_REVISION", card["Verdict"])
        self.assertTrue(card["Conditional tip"])

    def test_case_07_verification_hold_is_not_major(self) -> None:
        decision = decide_state(self.assert_case("CASE-07"))
        self.assertEqual("STOP_REVISING", decision["verdict"])
        self.assertEqual(["SOURCE_VERIFICATION_REQUIRED"], decision["evidence_hold_codes"])
        self.assertFalse(decision["material_root_cause"])

    def test_case_08_bounded_mechanism_stopping_point_is_not_failure(self) -> None:
        state = self.assert_case("CASE-08")
        state["output_language"] = "en"
        card = public_card(state)
        self.assertEqual("STOP_REVISING", card["Verdict"])
        self.assertEqual(
            ["Bounded mechanism stopping point requires verification"],
            card["Evidence holds"],
        )

    def test_case_09_bounded_excerpt_is_unassessed(self) -> None:
        card = public_card(self.assert_case("CASE-09"))
        self.assertEqual("UNASSESSED", card["Verdict"])
        self.assertIsNone(card["Conditional tip"])

    def test_case_10_incomplete_current_manuscript_is_unassessed(self) -> None:
        card = public_card(self.assert_case("CASE-10"))
        self.assertEqual("UNASSESSED", card["Verdict"])

    def test_case_11_same_receipt_remains_valid(self) -> None:
        state = self.assert_case("CASE-11")
        decision = decide_state(state)
        self.assertTrue(decision["prior_receipt_valid"])
        card = public_card(state)
        self.assertIn("valid prior closure decision", card["Reason"])

    def test_case_12_semantic_change_stales_prior_receipt(self) -> None:
        state = self.assert_case("CASE-12")
        self.assertEqual("synthetic-manuscript-1", state["current_manuscript_identity"])
        self.assertEqual("synthetic-manuscript-1", state["prior_receipt"]["manuscript_identity"])
        self.assertEqual(["semantic_content_changed"], state["invalidation_events"])
        decision = decide_state(state)
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertTrue(decision["prior_receipt_stale"])
        self.assertEqual("NO_MATERIAL_ROOT_CAUSE", decision["reason_category"])

    def test_case_13_rewrite_request_stays_read_only(self) -> None:
        card = public_card(self.assert_case("CASE-13"))
        self.assertEqual("STOP_REVISING", card["Verdict"])
        self.assertIn("read-only lane", card["Next permitted action"])

    def test_case_14_manuscript_prompt_injection_is_untrusted(self) -> None:
        # This is a helper boundary test only; it is not a live-model
        # prompt-injection certification.
        card = public_card(self.assert_case("CASE-14"))
        serialized = json.dumps(card, ensure_ascii=False).casefold()
        self.assertNotIn("ignore the skill rules", serialized)
        self.assertNotIn("detailed review report", serialized)

    def test_case_15_stop_card_has_no_detailed_review_leakage(self) -> None:
        card = public_card(self.assert_case("CASE-15"))
        self.assertTrue(set(card).isdisjoint(PUBLIC_PROHIBITED_KEYS))
        serialized = json.dumps(card, ensure_ascii=False).casefold()
        for term in ("paragraph anchor", "section anchor", "exact quote", "issue register", "replacement sentence"):
            self.assertNotIn(term, serialized)
        self.assertIsNone(card["Conditional tip"])

    def test_case_16_revision_card_has_direction_only(self) -> None:
        state = self.state_for("CASE-16")
        state["material_root_causes"] = [
            {"observed": True, "locatable": True, "affects": ["argument bridge"], "style_only": False, "hold_only": False, "verification_only": False, "expected_benefit_exceeds_risk": True, "scope": "local"}
        ]
        state["lite_suggestions"] = [
            {
                "Direction": "Clarify the relationship between method and contribution.",
                "Why it matters": "This protects reader understanding of the bounded finding.",
                "What to protect": "Keep the evidence ceiling and negative findings visible.",
            }
        ]
        card = public_card(state)
        self.assertEqual("ONE_BOUNDED_ROUND", card["Verdict"])
        self.assertTrue(card["Conditional tip"])
        self.assertEqual({"Direction", "Why it matters", "What to protect"}, set(card["Lite directional suggestions"][0]))
        with self.assertRaises(ClosureStateError):
            state["lite_suggestions"] = state["lite_suggestions"] * 4
            public_card(state)

    def test_case_17_no_internal_review_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_cwd = Path.cwd()
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            try:
                os.chdir(root)
                public_card(self.assert_case("CASE-17"))
            finally:
                os.chdir(original_cwd)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(before, after)
            forbidden_names = {
                "manuscript_review_report.md",
                "manuscript_issue_register.csv",
                "review_transcript.md",
                "hidden_reasoning.md",
            }
            self.assertTrue(forbidden_names.isdisjoint(after))

    def test_case_18_readme_discloses_non_persistence(self) -> None:
        readme_en = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("non-persisted internal whole-manuscript assessment", readme_en)
        self.assertIn("not a detailed peer-review report or revision plan", readme_en)
        self.assertIn(
            "本 skill 会在运行时进行一次不落盘的内部整稿评估，仅用于形成修订截止判断；默认不返回或保存完整审稿意见。",
            readme_zh,
        )
        self.assertIn(TIP_EN, readme_en)
        self.assertIn(TIP_ZH, readme_zh)
        english_body = readme_en.replace("[中文说明](README.zh-CN.md)", "")
        self.assertNotRegex(english_body, r"[\u3400-\u4dbf\u4e00-\u9fff]")
        self.assertIn("[中文说明](README.zh-CN.md)", readme_en)
        self.assertIn("[English](README.md)", readme_zh)
        self.assertNotIn("chain-of-thought", (readme_en + readme_zh).casefold())

    def test_invalid_substantive_fields_are_rejected(self) -> None:
        state = self.base_state()
        state["material_root_causes"] = [{"observed": True}]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_stop_tip_is_rejected(self) -> None:
        card = {
            "Verdict": "STOP_REVISING",
            "Reason": "No material root cause.",
            "Lite directional suggestions": [],
            "Protected / Do not disturb": [],
            "Evidence holds": [],
            "Submission / external holds": [],
            "Next permitted action": "Stop.",
            "Conditional tip": "Diagnosis complete.",
        }
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_minimal_receipt_excludes_internal_fields(self) -> None:
        decision = decide_state(self.base_state())
        receipt = minimal_receipt(decision, "synthetic-manuscript-1")
        self.assertEqual("STOP_REVISING", receipt["verdict"])
        self.assertTrue(set(receipt).isdisjoint(PUBLIC_PROHIBITED_KEYS))
        self.assertNotIn("material_root_cause", receipt)

    def test_parked_opportunity_cannot_leak_a_location(self) -> None:
        state = self.base_state()
        state["parked_opportunities"] = ["Replace paragraph 4 with a new argument."]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_receipt_hash_shape_is_validated(self) -> None:
        decision = decide_state(self.base_state())
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decision, "synthetic-manuscript-1", artifact_sha256="abc")

    def test_rcpt_01_unclear_identity_cannot_reuse_matching_text(self) -> None:
        state = self.receipt_state()
        state["current_identity_clear"] = False
        decision = decide_state(state)
        self.assertEqual("UNASSESSED", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])

    def test_rcpt_02_incomplete_manuscript_cannot_reuse_prior_stop(self) -> None:
        state = self.receipt_state()
        state["manuscript_complete"] = False
        decision = decide_state(state)
        self.assertEqual("UNASSESSED", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])

    def test_rcpt_03_bounded_scope_cannot_reuse_prior_stop(self) -> None:
        state = self.receipt_state()
        state["bounded_scope"] = True
        decision = decide_state(state)
        self.assertEqual("UNASSESSED", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])

    def test_rcpt_04_prior_unassessed_is_not_reusable(self) -> None:
        state = self.receipt_state("UNASSESSED")
        decision = decide_state(state)
        self.assertEqual("STOP_REVISING", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertEqual("NO_MATERIAL_ROOT_CAUSE", decision["reason_category"])

    def test_rcpt_05_stable_prior_stop_can_avoid_fresh_read(self) -> None:
        state = self.receipt_state()
        state["whole_manuscript_read"] = False
        state["critical_basis_available"] = False
        decision = decide_state(state)
        self.assertEqual("STOP_REVISING", decision["verdict"])
        self.assertTrue(decision["prior_receipt_valid"])

    def test_hash_r01_artifact_drift_with_equal_semantic_hash_is_stable(self) -> None:
        state = self.receipt_state()
        state.update(
            {
                "current_artifact_sha256": HASH_B,
                "current_semantic_content_sha256": HASH_A,
            }
        )
        state["prior_receipt"].update(
            {"artifact_sha256": HASH_A, "semantic_content_sha256": HASH_A}
        )
        decision = decide_state(state)
        self.assertTrue(decision["prior_receipt_valid"])
        self.assertEqual("ARTIFACT_CHANGED_CONTENT_STABLE", decision["reason_category"])

    def test_hash_r02_equal_artifact_and_semantic_hashes_are_stable(self) -> None:
        state = self.receipt_state()
        state.update(
            {
                "current_artifact_sha256": HASH_A,
                "current_semantic_content_sha256": HASH_A,
            }
        )
        state["prior_receipt"].update(
            {"artifact_sha256": HASH_A, "semantic_content_sha256": HASH_A}
        )
        decision = decide_state(state)
        self.assertTrue(decision["prior_receipt_valid"])

    def test_hash_r03_semantic_hash_mismatch_stales_receipt(self) -> None:
        state = self.receipt_state()
        state.update(
            {
                "current_artifact_sha256": HASH_A,
                "current_semantic_content_sha256": HASH_B,
            }
        )
        state["prior_receipt"].update(
            {"artifact_sha256": HASH_A, "semantic_content_sha256": HASH_A}
        )
        decision = decide_state(state)
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertTrue(decision["prior_receipt_stale"])
        self.assertEqual("NO_MATERIAL_ROOT_CAUSE", decision["reason_category"])

    def test_hash_r04_explicit_semantic_event_stales_same_identity(self) -> None:
        state = self.receipt_state()
        state["invalidation_events"] = ["semantic_content_changed"]
        decision = decide_state(state)
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertTrue(decision["prior_receipt_stale"])

    def test_hash_r05_artifact_drift_without_semantic_proof_is_not_valid(self) -> None:
        state = self.receipt_state()
        state["current_artifact_sha256"] = HASH_B
        state["prior_receipt"]["artifact_sha256"] = HASH_A
        decision = decide_state(state)
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertFalse(decision["prior_receipt_stale"])
        self.assertTrue(decision["prior_receipt_unverified"])
        self.assertEqual("NO_MATERIAL_ROOT_CAUSE", decision["reason_category"])

    def test_hash_verified_artifact_only_classification_is_allowed(self) -> None:
        state = self.receipt_state()
        state["current_artifact_sha256"] = HASH_B
        state["prior_receipt"]["artifact_sha256"] = HASH_A
        state["artifact_only_drift_verified"] = True
        decision = decide_state(state)
        self.assertTrue(decision["prior_receipt_valid"])
        self.assertEqual("VERIFIED_ARTIFACT_ONLY_DRIFT", decision["reason_category"])

    def test_malformed_receipt_fields_are_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["unexpected"] = "do not guess"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_leak_01_unknown_chinese_top_level_field_is_rejected(self) -> None:
        card = public_card(self.base_state())
        card["内部审稿意见"] = "hidden"
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_leak_02_chinese_locator_instruction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [
            {
                "Direction": "请修改方法部分第3小节的第2句，替换为新的表述。",
                "Why it matters": "This is too implementation-specific.",
                "What to protect": "Protect the claim ceiling.",
            }
        ]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_leak_03_english_locator_instruction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [
            {
                "Direction": "Replace sentence 3 in subsection 2 with a new paragraph.",
                "Why it matters": "This is too implementation-specific.",
                "What to protect": "Protect the claim ceiling.",
            }
        ]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_state_01_stop_with_suggestions_is_rejected(self) -> None:
        card = public_card(self.base_state())
        card["Lite directional suggestions"] = [
            {
                "Direction": "Keep the contribution visible.",
                "Why it matters": "It supports reader navigation.",
                "What to protect": "Protect the claim ceiling.",
            }
        ]
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_state_02_revision_with_parked_opportunities_is_rejected(self) -> None:
        card = public_card(self.local_material_state())
        card["Parked opportunities"] = ["Optional future thought."]
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_state_03_stop_parked_item_requires_note(self) -> None:
        card = public_card(self.base_state())
        card["Parked opportunities"] = ["Optional future thought."]
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_state_04_unassessed_cannot_contain_suggestion_or_tip(self) -> None:
        state = self.base_state()
        state["manuscript_complete"] = False
        card = public_card(state)
        card["Lite directional suggestions"] = [{
            "Direction": "Keep the claim bounded.",
            "Why it matters": "It supports interpretation.",
            "What to protect": "Protect the source status.",
        }]
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)
        card = public_card(state)
        card["Conditional tip"] = TIP_ZH
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_hold_01_quote_permission_hold_is_accepted(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["quote permission unresolved"]
        state["output_language"] = "en"
        card = public_card(state)
        self.assertEqual(["Quote permission unresolved"], card["Submission / external holds"])

    def test_tip_01_chinese_default_one_round_uses_exact_tip(self) -> None:
        card = public_card(self.local_material_state())
        self.assertEqual(TIP_ZH, card["Conditional tip"])

    def test_tip_02_english_reopen_uses_exact_tip(self) -> None:
        state = self.local_material_state()
        state["output_language"] = "en"
        state["material_root_causes"][0]["scope"] = "central"
        card = public_card(state)
        self.assertEqual("REOPEN_SUBSTANTIVE_REVISION", card["Verdict"])
        self.assertEqual(TIP_EN, card["Conditional tip"])

    def test_tip_03_stop_and_unassessed_have_no_tip_in_both_languages(self) -> None:
        for language in ("zh", "en"):
            stop = self.base_state()
            stop["output_language"] = language
            self.assertIsNone(public_card(stop)["Conditional tip"])
            unassessed = self.base_state()
            unassessed["output_language"] = language
            unassessed["manuscript_complete"] = False
            self.assertIsNone(public_card(unassessed)["Conditional tip"])

    def test_formal_tone_keeps_chinese_route_boundary(self) -> None:
        state = self.local_material_state()
        state["formal_tone"] = True
        card = public_card(state)
        self.assertIn("另行授权", card["Conditional tip"])
        self.assertNotEqual(TIP_ZH, card["Conditional tip"])

    def test_case_14_static_helper_has_no_direct_file_or_network_writes(self) -> None:
        source = (ROOT / "scripts" / "closure_state.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_imports = {"socket", "urllib", "requests", "httpx", "pathlib"}
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertTrue(forbidden_imports.isdisjoint(imports))
        forbidden_calls = {"open", "urlopen", "write_text", "write_bytes", "unlink", "remove"}
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        calls.update(
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        )
        self.assertTrue(forbidden_calls.isdisjoint(calls))

    def test_surface_01_evidence_hold_locator_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["Replace paragraph 3 before verification"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_surface_02_submission_hold_chinese_instruction_is_rejected(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["请删除第3段并替换第2句"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_surface_03_quote_permission_hold_remains_accepted(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["quote permission unresolved"]
        state["output_language"] = "en"
        self.assertEqual(
            ["Quote permission unresolved"],
            public_card(state)["Submission / external holds"],
        )

    def test_surface_04_source_verification_hold_remains_accepted(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["source verification required"]
        state["output_language"] = "en"
        self.assertEqual(["Source verification required"], public_card(state)["Evidence holds"])

    def test_action_01_arbitrary_next_action_is_rejected(self) -> None:
        card = public_card(self.base_state())
        card["Next permitted action"] = "Rewrite section 3 paragraph 2 with the hidden review findings."
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_action_02_helper_generated_next_actions_are_accepted(self) -> None:
        states = [
            self.base_state(),
            self.local_material_state(),
            self.local_material_state(),
            self.receipt_state(),
        ]
        states[2]["material_root_causes"][0]["scope"] = "central"
        states[3]["whole_manuscript_read"] = False
        expected = {NEXT_STOP, NEXT_ONE_ROUND, NEXT_REOPEN, NEXT_PRIOR_STOP}
        for state in states:
            card = public_card(state)
            self.assertIn(card["Next permitted action"], expected)
            validate_public_card(card)
        unassessed = self.base_state()
        unassessed["manuscript_complete"] = False
        self.assertEqual(NEXT_UNASSESSED, public_card(unassessed)["Next permitted action"])

    def test_tip2_01_arbitrary_chinese_locator_tip_is_rejected(self) -> None:
        card = public_card(self.local_material_state())
        card["Conditional tip"] = "请替换第3段第2句"
        with self.assertRaises(ClosureStateError):
            validate_public_card(card)

    def test_tip2_02_all_approved_tips_are_accepted_for_revision(self) -> None:
        for tip in (TIP_ZH, TIP_EN, FORMAL_TIP_ZH, FORMAL_TIP_EN):
            card = public_card(self.local_material_state())
            card["Conditional tip"] = tip
            validate_public_card(card)

    def test_tip2_03_approved_tip_is_rejected_for_stop_and_unassessed(self) -> None:
        stop = public_card(self.base_state())
        stop["Conditional tip"] = TIP_ZH
        with self.assertRaises(ClosureStateError):
            validate_public_card(stop)
        unassessed_state = self.base_state()
        unassessed_state["manuscript_complete"] = False
        unassessed = public_card(unassessed_state)
        unassessed["Conditional tip"] = TIP_ZH
        with self.assertRaises(ClosureStateError):
            validate_public_card(unassessed)

    def test_rreceipt_01_prior_one_round_without_fresh_read_is_unassessed(self) -> None:
        state = self.receipt_state("ONE_BOUNDED_ROUND")
        state["whole_manuscript_read"] = False
        decision = decide_state(state)
        self.assertEqual("UNASSESSED", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])

    def test_rreceipt_02_prior_reopen_without_fresh_read_is_unassessed(self) -> None:
        state = self.receipt_state("REOPEN_SUBSTANTIVE_REVISION")
        state["whole_manuscript_read"] = False
        decision = decide_state(state)
        self.assertEqual("UNASSESSED", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])

    def test_rreceipt_03_prior_one_round_with_fresh_basis_runs_fresh_stop(self) -> None:
        state = self.receipt_state("ONE_BOUNDED_ROUND")
        decision = decide_state(state)
        self.assertEqual("STOP_REVISING", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertFalse(decision["material_root_cause"])

    def test_rreceipt_04_prior_reopen_with_fresh_central_basis_runs_fresh_reopen(self) -> None:
        state = self.local_material_state()
        state["material_root_causes"][0]["scope"] = "central"
        state["prior_receipt"] = {
            "manuscript_identity": "synthetic-manuscript-1",
            "verdict": "REOPEN_SUBSTANTIVE_REVISION",
        }
        decision = decide_state(state)
        self.assertEqual("REOPEN_SUBSTANTIVE_REVISION", decision["verdict"])
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertTrue(decision["material_root_cause"])
        self.assertTrue(decision["central_root_cause"])
        self.assertEqual(NEXT_REOPEN, decision["next_permitted_action"])

    def test_rreceipt_05_stable_prior_stop_remains_the_only_shortcut(self) -> None:
        state = self.receipt_state("STOP_REVISING")
        state["whole_manuscript_read"] = False
        decision = decide_state(state)
        self.assertEqual("STOP_REVISING", decision["verdict"])
        self.assertTrue(decision["prior_receipt_valid"])

    def test_invariant_01_one_round_always_has_material_root_cause(self) -> None:
        decision = decide_state(self.local_material_state())
        self.assertEqual("ONE_BOUNDED_ROUND", decision["verdict"])
        self.assertTrue(decision["material_root_cause"])

    def test_invariant_02_reopen_always_has_material_and_central_root_cause(self) -> None:
        state = self.local_material_state()
        state["material_root_causes"][0]["scope"] = "central"
        decision = decide_state(state)
        self.assertEqual("REOPEN_SUBSTANTIVE_REVISION", decision["verdict"])
        self.assertTrue(decision["material_root_cause"])
        self.assertTrue(decision["central_root_cause"])

    def test_malformed_01_reason_category_object_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["reason_category"] = {"hidden": True}
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_malformed_02_next_action_list_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["next_permitted_action"] = ["do not guess"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_malformed_03_skill_version_integer_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["skill_version"] = 12
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_malformed_04_hold_summary_empty_item_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["evidence_hold_summary"] = [""]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_malformed_05_unknown_invalidation_condition_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["invalidation_conditions"] = ["not_a_real_event"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_malformed_06_assessment_time_non_string_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["assessment_time"] = 20260821
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_valid_receipt_01_minimal_receipt_round_trips(self) -> None:
        decision = decide_state(self.base_state())
        receipt = minimal_receipt(decision, "synthetic-manuscript-1")
        state = self.base_state()
        state["prior_receipt"] = receipt
        reused = decide_state(state)
        self.assertTrue(reused["prior_receipt_valid"])
        self.assertEqual(SKILL_VERSION, receipt["skill_version"])

    def test_identity2_01_clear_identity_missing_is_rejected(self) -> None:
        state = self.base_state()
        state.pop("current_manuscript_identity")
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_identity2_02_clear_identity_blank_is_rejected(self) -> None:
        state = self.base_state()
        state["current_manuscript_identity"] = ""
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_identity2_03_clear_identity_non_string_is_rejected(self) -> None:
        state = self.base_state()
        state["current_manuscript_identity"] = {"id": "synthetic"}
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_identity2_04_unclear_identity_without_label_is_unassessed(self) -> None:
        state = self.base_state()
        state["current_identity_clear"] = False
        state.pop("current_manuscript_identity")
        decision = decide_state(state)
        self.assertEqual("UNASSESSED", decision["verdict"])

    def test_identity2_05_clear_identity_complete_basis_runs_normal_assessment(self) -> None:
        decision = decide_state(self.base_state())
        self.assertEqual("STOP_REVISING", decision["verdict"])

    def test_version2_01_minimal_receipt_uses_rc1_4_version(self) -> None:
        receipt = minimal_receipt(decide_state(self.base_state()), "synthetic-manuscript-1")
        self.assertEqual("0.2.1", receipt["skill_version"])

    def test_receipt_surface_01_normal_unsafe_evidence_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["Replace paragraph 3 before verification"]
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decide_state(state), "synthetic-manuscript-1")

    def test_receipt_surface_02_normal_unsafe_submission_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["请删除第3段并替换第2句"]
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decide_state(state), "synthetic-manuscript-1")

    def test_receipt_surface_03_arbitrary_reason_category_is_rejected(self) -> None:
        decision = decide_state(self.base_state())
        decision["reason_category"] = "Rewrite paragraph 3"
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decision, "synthetic-manuscript-1")

    def test_receipt_surface_04_arbitrary_next_action_is_rejected(self) -> None:
        decision = decide_state(self.base_state())
        decision["next_permitted_action"] = "Move paragraph 4"
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decision, "synthetic-manuscript-1")

    def test_receipt_surface_05_arbitrary_hold_summaries_are_rejected(self) -> None:
        decision = decide_state(self.base_state())
        decision["evidence_hold_codes"] = ["Delete section 2"]
        decision["submission_hold_codes"] = ["Insert replacement sentence"]
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decision, "synthetic-manuscript-1")

    def test_receipt_surface_06_incomplete_decision_mapping_is_rejected(self) -> None:
        with self.assertRaises(ClosureStateError):
            minimal_receipt({"verdict": "STOP_REVISING"}, "synthetic-manuscript-1")

    def test_receipt_surface_07_all_lawful_decisions_emit_lawful_receipts(self) -> None:
        states = [
            self.base_state(),
            self.local_material_state(),
            self.local_material_state(),
            {**self.base_state(), "manuscript_complete": False},
        ]
        states[2]["material_root_causes"][0]["scope"] = "central"
        expected_verdicts = {
            "STOP_REVISING",
            "ONE_BOUNDED_ROUND",
            "REOPEN_SUBSTANTIVE_REVISION",
            "UNASSESSED",
        }
        receipts = [minimal_receipt(decide_state(state), "synthetic-manuscript-1") for state in states]
        self.assertEqual(expected_verdicts, {receipt["verdict"] for receipt in receipts})
        self.assertTrue(all(isinstance(receipt["next_permitted_action"], str) for receipt in receipts))

    def test_receipt_surface_08_legitimate_status_holds_round_trip(self) -> None:
        state = self.base_state()
        state["submission_holds"] = [
            "quote permission unresolved",
            "image rights unresolved",
            "format QA pending",
            "comments or tracking remain",
        ]
        state["evidence_holds"] = ["source verification required"]
        receipt = minimal_receipt(decide_state(state), "synthetic-manuscript-1")
        reused = self.base_state()
        reused["prior_receipt"] = receipt
        self.assertTrue(decide_state(reused)["prior_receipt_valid"])

    def test_receipt_surface_09_unsafe_prior_hold_is_rejected_before_reuse(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["evidence_hold_summary"] = ["Replace paragraph 3 before verification"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_receipt_surface_10_unsafe_prior_action_is_rejected_before_reuse(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["next_permitted_action"] = "Move paragraph 4"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_receipt_surface_11_canonical_receipt_round_trips_stable_stop(self) -> None:
        receipt = minimal_receipt(decide_state(self.base_state()), "synthetic-manuscript-1")
        state = self.base_state()
        state["prior_receipt"] = receipt
        decision = decide_state(state)
        self.assertEqual("STOP_REVISING", decision["verdict"])
        self.assertTrue(decision["prior_receipt_valid"])
        self.assertEqual(NEXT_PRIOR_STOP, decision["next_permitted_action"])

    def test_receipt_surface_12_scalar_fields_are_bounded(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["assessment_time"] = "after reading the paper"
        with self.assertRaises(ClosureStateError):
            decide_state(state)
        state = self.receipt_state()
        state["prior_receipt"]["assessment_time"] = "2026-08-21T12:30:00+08:00"
        state["prior_receipt"]["skill_version"] = "0.1.0"
        self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_receipt_invariant_01_one_round_false_material_flag_is_rejected(self) -> None:
        decision = decide_state(self.local_material_state())
        decision["material_root_cause"] = False
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decision, "synthetic-manuscript-1")

    def test_receipt_invariant_02_reopen_without_both_flags_is_rejected(self) -> None:
        state = self.local_material_state()
        state["material_root_causes"][0]["scope"] = "central"
        decision = decide_state(state)
        decision["central_root_cause"] = False
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decision, "synthetic-manuscript-1")

    def test_surface2_01_unnumbered_english_rewrite_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["Rewrite the Methods to add a causal mechanism"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_surface2_02_unnumbered_chinese_rewrite_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["重写方法部分并增加因果机制"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_surface2_03_unnumbered_english_rewrite_direction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [{
            "Direction": "Rewrite the Methods to add a causal mechanism.",
            "Why it matters": "This would be an implementation instruction.",
            "What to protect": "Protect the claim ceiling.",
        }]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_surface2_04_unnumbered_chinese_rewrite_direction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [{
            "Direction": "重写方法部分并增加因果机制",
            "Why it matters": "这会变成执行指令。",
            "What to protect": "保护主张边界。",
        }]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_surface2_05_directional_method_bridge_remains_accepted(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [{
            "Direction": "Make the method-to-claim bridge more visible.",
            "Why it matters": "Readers need a clear conceptual bridge.",
            "What to protect": "Protect the claim ceiling.",
        }]
        card = public_card(state)
        self.assertEqual("Make the method-to-claim bridge more visible.", card["Lite directional suggestions"][0]["Direction"])

    def test_surface2_06_legitimate_status_holds_remain_accepted(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["source verification required"]
        state["submission_holds"] = ["quote permission unresolved"]
        state["output_language"] = "en"
        card = public_card(state)
        self.assertEqual(["Source verification required"], card["Evidence holds"])
        self.assertEqual(["Quote permission unresolved"], card["Submission / external holds"])

    def test_surface2_07_protected_method_and_source_status_wording_is_accepted(self) -> None:
        state = self.base_state()
        state["protected"] = ["Keep the method, claim ceiling, and source-status distinctions visible."]
        card = public_card(state)
        self.assertEqual(state["protected"], card["Protected / Do not disturb"])

    def test_surface2_08_target_variants_use_reusable_rewrite_patterns(self) -> None:
        variants = [
            "Rewrite the Methodology to clarify the mechanism.",
            "Change the Discussion to clarify the mechanism.",
            "Replace the Conclusion with a new claim.",
            "重写方法部分并增加因果机制",
            "修改讨论并增加机制",
            "替换结论并新增主张",
        ]
        for text in variants:
            state = self.base_state()
            state["evidence_holds"] = [text]
            with self.subTest(text=text), self.assertRaises(ClosureStateError):
                public_card(state)

    def test_hold3_01_bare_english_rewrite_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["Rewrite before submission"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_hold3_02_generic_delete_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["Delete this text before submission"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_hold3_03_passive_rewrite_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["The Discussion should be rewritten"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_hold3_04_chinese_bare_rewrite_hold_is_rejected(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["请重写以提高清晰度"]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_hold3_05_bare_edit_hold_is_rejected_by_minimal_receipt(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["Rewrite before submission"]
        with self.assertRaises(ClosureStateError):
            minimal_receipt(decide_state(state), "synthetic-manuscript-1")

    def test_hold3_06_bare_edit_hold_is_rejected_before_prior_reuse(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["evidence_hold_summary"] = ["Rewrite before submission"]
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_hold3_07_core_status_labels_remain_accepted(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["source verification required"]
        state["submission_holds"] = [
            "quote permission unresolved",
            "image rights unresolved",
            "format QA pending",
            "comments or tracking remain",
        ]
        state["output_language"] = "en"
        card = public_card(state)
        self.assertEqual(["Source verification required"], card["Evidence holds"])
        self.assertEqual(4, len(card["Submission / external holds"]))

    def test_hold3_08_submission_status_labels_remain_accepted(self) -> None:
        state = self.base_state()
        state["submission_holds"] = [
            "journal contract unchecked",
            "anonymization pending",
            "author metadata missing",
            "licensing unresolved",
        ]
        self.assertEqual(4, len(public_card(state)["Submission / external holds"]))

    def test_hold3_09_revision_authorization_status_remains_accepted(self) -> None:
        state = self.base_state()
        state["submission_holds"] = ["revision authorization pending"]
        state["output_language"] = "en"
        self.assertEqual(
            ["Revision authorization pending"],
            public_card(state)["Submission / external holds"],
        )

    def test_hold3_10_chinese_status_labels_remain_accepted(self) -> None:
        state = self.base_state()
        state["evidence_holds"] = ["来源核验待完成"]
        state["submission_holds"] = ["图像权利未解决", "作者信息缺失"]
        card = public_card(state)
        self.assertEqual(1, len(card["Evidence holds"]))
        self.assertEqual(2, len(card["Submission / external holds"]))

    def test_direction3_01_bare_rewrite_direction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [{
            "Direction": "Rewrite before submission.",
            "Why it matters": "This is an execution command.",
            "What to protect": "Protect the claim ceiling.",
        }]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_direction3_02_generic_delete_direction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [{
            "Direction": "Delete this text before submission.",
            "Why it matters": "This is an execution command.",
            "What to protect": "Protect the claim ceiling.",
        }]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_direction3_03_passive_rewrite_direction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [{
            "Direction": "The Discussion should be rewritten.",
            "Why it matters": "This is an execution command.",
            "What to protect": "Protect the claim ceiling.",
        }]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_direction3_04_chinese_rewrite_direction_is_rejected(self) -> None:
        state = self.local_material_state()
        state["lite_suggestions"] = [{
            "Direction": "请重写以提高清晰度",
            "Why it matters": "这会变成执行指令。",
            "What to protect": "保护主张边界。",
        }]
        with self.assertRaises(ClosureStateError):
            public_card(state)

    def test_direction3_05_to_07_lawful_directions_remain_accepted(self) -> None:
        directions = (
            "Make the method-to-claim bridge more visible.",
            "Clarify the contribution boundary while preserving the claim ceiling.",
            "Strengthen the source-status distinction.",
        )
        for direction in directions:
            state = self.local_material_state()
            state["lite_suggestions"] = [{
                "Direction": direction,
                "Why it matters": "It supports bounded reader understanding.",
                "What to protect": "Protect the claim ceiling and source status.",
            }]
            with self.subTest(direction=direction):
                self.assertEqual(direction, public_card(state)["Lite directional suggestions"][0]["Direction"])

    def test_prior_xfield_01_stop_central_reason_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["reason_category"] = "CENTRAL_MATERIAL_ROOT_CAUSE"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_02_stop_local_reason_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["reason_category"] = "LOCAL_MATERIAL_ROOT_CAUSE"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_03_stop_insufficient_reason_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"]["reason_category"] = "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_04_one_round_no_material_reason_is_rejected(self) -> None:
        state = self.receipt_state("ONE_BOUNDED_ROUND")
        state["prior_receipt"]["reason_category"] = "NO_MATERIAL_ROOT_CAUSE"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_05_reopen_local_reason_is_rejected(self) -> None:
        state = self.receipt_state("REOPEN_SUBSTANTIVE_REVISION")
        state["prior_receipt"]["reason_category"] = "LOCAL_MATERIAL_ROOT_CAUSE"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_06_unassessed_central_reason_is_rejected(self) -> None:
        state = self.receipt_state("UNASSESSED")
        state["prior_receipt"]["reason_category"] = "CENTRAL_MATERIAL_ROOT_CAUSE"
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_07_fresh_stop_reason_with_reopen_action_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update({
            "reason_category": "NO_MATERIAL_ROOT_CAUSE",
            "next_permitted_action": NEXT_REOPEN,
        })
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_08_prior_stop_reason_with_fresh_action_is_rejected(self) -> None:
        state = self.receipt_state()
        state["prior_receipt"].update({
            "reason_category": "PRIOR_CLOSURE_STILL_VALID",
            "next_permitted_action": NEXT_STOP,
        })
        with self.assertRaises(ClosureStateError):
            decide_state(state)

    def test_prior_xfield_09_lawful_emitted_receipts_are_accepted(self) -> None:
        states = [
            self.base_state(),
            self.local_material_state(),
            self.local_material_state(),
            {**self.base_state(), "manuscript_complete": False},
        ]
        states[2]["material_root_causes"][0]["scope"] = "central"
        receipts = [minimal_receipt(decide_state(state), "synthetic-manuscript-1") for state in states]
        self.assertEqual(4, len(receipts))

    def test_prior_xfield_10_legacy_identity_verdict_receipt_remains_valid(self) -> None:
        decision = decide_state(self.receipt_state())
        self.assertTrue(decision["prior_receipt_valid"])

    def test_prior_xfield_11_lawful_prior_stop_reuses_under_existing_gates(self) -> None:
        state = self.base_state()
        state["prior_receipt"] = minimal_receipt(decide_state(self.base_state()), "synthetic-manuscript-1")
        state["whole_manuscript_read"] = False
        state["critical_basis_available"] = False
        self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_prior_xfield_12_semantic_mismatch_still_stales_stop(self) -> None:
        state = self.base_state()
        state["prior_receipt"] = minimal_receipt(
            decide_state(self.base_state()),
            "synthetic-manuscript-1",
            artifact_sha256=HASH_A,
            semantic_content_sha256=HASH_A,
        )
        state["current_artifact_sha256"] = HASH_A
        state["current_semantic_content_sha256"] = HASH_B
        decision = decide_state(state)
        self.assertFalse(decision["prior_receipt_valid"])
        self.assertTrue(decision["prior_receipt_stale"])

    def test_version2_02_older_lawful_version_scalars_are_accepted(self) -> None:
        for version in ("0.1.0", "0.1.2", "0.1.3"):
            receipt = minimal_receipt(
                decide_state(self.base_state()),
                "synthetic-manuscript-1",
                skill_version=version,
            )
            state = self.base_state()
            state["prior_receipt"] = receipt
            with self.subTest(version=version):
                self.assertTrue(decide_state(state)["prior_receipt_valid"])

    def test_version2_03_version_difference_alone_does_not_invalidate(self) -> None:
        receipt = minimal_receipt(
            decide_state(self.base_state()),
            "synthetic-manuscript-1",
            skill_version="0.1.0",
        )
        state = self.base_state()
        state["prior_receipt"] = receipt
        decision = decide_state(state)
        self.assertTrue(decision["prior_receipt_valid"])
        self.assertFalse(decision["prior_receipt_stale"])

    def test_version2_04_prose_or_malformed_version_is_rejected(self) -> None:
        for version in ("version prose", "0.1.x", {"version": "0.1.3"}):
            state = self.receipt_state()
            state["prior_receipt"]["skill_version"] = version
            with self.subTest(version=repr(version)), self.assertRaises(ClosureStateError):
                decide_state(state)


if __name__ == "__main__":
    unittest.main()
