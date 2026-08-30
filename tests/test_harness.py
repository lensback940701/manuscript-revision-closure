from __future__ import annotations

import copy
import unittest

from standalone.harness import (
    AFFIRMATIVE_STOP_DIMENSIONS,
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_DIMENSIONS,
    HarnessContractError,
    analyze_intake_structure,
    canonical_digest,
    context_budget,
    coverage_is_complete,
    validate_adjudication_binding,
    validate_coverage,
    validate_cross_stage_consistency,
)


def complete_text() -> str:
    return (
        "A Complete Manuscript\n\nAbstract\n"
        + ("Bounded argument, evidence, and scope condition.\n" * 80)
        + "\nConclusion\nThe contribution remains bounded.\n\nReferences\nReference A."
    )


def coverage_state(*, candidate: str | None = None) -> dict:
    return {
        "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
        "whole_manuscript_basis": "SUFFICIENT",
        "basis_reason_codes": ["SUFFICIENT_SUBSTANTIVE_WHOLE_MANUSCRIPT"],
        "basis_explanation": "The supplied text contains sufficient substantive whole-manuscript material.",
        "manuscript_identity_confirmed": True,
        "full_span_covered": True,
        "dimensions": [
            {
                "dimension": dimension,
                "applicability": "APPLICABLE",
                "assessed": True,
                "status": "POTENTIAL_MATERIAL_ROOT_CAUSE" if dimension == candidate else "CLEAR",
                "affirmative_sufficiency": True,
                "sufficiency_reason_code": "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
            }
            for dimension in COVERAGE_DIMENSIONS
        ],
        "root_cause_candidate_dimensions": [candidate] if candidate else [],
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected_invariants": {
            "claim_ceiling_preserved": True,
            "evidence_status_distinctions_preserved": True,
            "rivals_and_negative_findings_preserved": True,
        },
    }


def insufficient_coverage_state() -> dict:
    value = coverage_state()
    value.update(
        {
            "whole_manuscript_basis": "INSUFFICIENT",
            "basis_reason_codes": ["FRAGMENT_OR_EXCERPT_ONLY"],
            "basis_explanation": "Only a bounded fragment is available for substantive assessment.",
            "full_span_covered": False,
        }
    )
    for row in value["dimensions"]:
        row["assessed"] = False
        row["status"] = "UNASSESSED"
        row["affirmative_sufficiency"] = False
        row["sufficiency_reason_code"] = "UNASSESSED"
    value["protected_invariants"] = {
        key: False for key in value["protected_invariants"]
    }
    return value


def model_state(*, candidate: str | None = None) -> dict:
    causes = []
    if candidate:
        causes.append(
            {
                "observed": True,
                "locatable": True,
                "affects": [candidate],
                "origin": "COVERAGE_CANDIDATE",
                "coverage_disagreement": False,
                "disposition_reason_code": "MATERIAL_CONCERN_CONFIRMED",
                "author_decision_required": False,
                "style_only": False,
                "hold_only": False,
                "verification_only": False,
                "expected_benefit_exceeds_risk": True,
                "scope": "local",
            }
        )
    return {
        "material_root_causes": causes,
        "affirmative_sufficiency": [
            {
                "dimension": dimension,
                "assessed": True,
                "affirmative_sufficiency": dimension != candidate,
                "unresolved_material_concern": dimension == candidate,
                "sufficiency_reason_code": (
                    "UNRESOLVED_MATERIAL_CONCERN"
                    if dimension == candidate
                    else "AFFIRMATIVE_MANUSCRIPT_SUPPORT"
                ),
            }
            for dimension in AFFIRMATIVE_STOP_DIMENSIONS
        ],
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected": ["保持当前论点上限。"],
        "parked_opportunities": [],
        "lite_suggestions": [],
    }


class HarnessTests(unittest.TestCase):
    def test_local_preflight_is_format_nonblocking_with_advisories(self) -> None:
        receipt = analyze_intake_structure(complete_text())
        self.assertTrue(receipt.complete_structure)
        broken = analyze_intake_structure(complete_text().replace("Conclusion", "Closing remarks"))
        self.assertTrue(broken.complete_structure)
        self.assertTrue(broken.format_advisory_only)
        self.assertIn("STRUCTURE_FORMAT_REVIEW", broken.advisory_codes)
        reversed_sections = complete_text().replace(
            "Conclusion\nThe contribution remains bounded.\n\nReferences\nReference A.",
            "References\nReference A.\n\nConclusion\nThe contribution remains bounded.",
        )
        reversed_receipt = analyze_intake_structure(reversed_sections)
        self.assertTrue(reversed_receipt.complete_structure)
        self.assertIn("STRUCTURE_FORMAT_REVIEW", reversed_receipt.advisory_codes)

    def test_context_budget_is_model_bound_and_fails_before_truncation(self) -> None:
        short = context_budget(
            [{"role": "user", "content": complete_text()}],
            provider="kimi",
            model="kimi-k2.6",
        )
        self.assertTrue(short.passed)
        self.assertLessEqual(short.requested_max_output_tokens, 131072)
        huge = context_budget(
            [{"role": "user", "content": "中" * 300_000}],
            provider="kimi",
            model="kimi-k2.6",
        )
        self.assertFalse(huge.passed)

    def test_coverage_exact_dimensions_and_candidate_projection(self) -> None:
        value = coverage_state(candidate="contribution")
        clean = validate_coverage(value)
        self.assertTrue(coverage_is_complete(clean))
        missing = copy.deepcopy(value)
        missing["dimensions"].pop()
        with self.assertRaises(HarnessContractError):
            validate_coverage(missing)
        stale = copy.deepcopy(value)
        stale["root_cause_candidate_dimensions"] = []
        with self.assertRaises(HarnessContractError):
            validate_coverage(stale)

    def test_unassessed_coverage_is_valid_but_not_complete(self) -> None:
        value = coverage_state()
        value["dimensions"][0]["assessed"] = False
        value["dimensions"][0]["status"] = "UNASSESSED"
        value["dimensions"][0]["affirmative_sufficiency"] = False
        value["dimensions"][0]["sufficiency_reason_code"] = "UNASSESSED"
        self.assertFalse(coverage_is_complete(validate_coverage(value)))

    def test_semantic_basis_truth_table_and_explanation_privacy(self) -> None:
        insufficient = insufficient_coverage_state()
        clean = validate_coverage(insufficient)
        self.assertFalse(coverage_is_complete(clean))
        self.assertEqual("INSUFFICIENT", clean["whole_manuscript_basis"])

        suspicious = copy.deepcopy(insufficient)
        suspicious["basis_explanation"] = (
            "Authorization: Bearer sk-secret at C:\\private\\paper.md line 99"
        )
        sanitized = validate_coverage(suspicious)
        serialized = str(sanitized)
        self.assertNotIn("sk-secret", serialized)
        self.assertNotIn("C:\\private", serialized)
        self.assertNotIn("line 99", serialized)

        invalid_variants = []
        full_span = copy.deepcopy(insufficient)
        full_span["full_span_covered"] = True
        invalid_variants.append(full_span)
        assessed = copy.deepcopy(insufficient)
        assessed["dimensions"][0].update({"assessed": True, "status": "CLEAR"})
        invalid_variants.append(assessed)
        invariant = copy.deepcopy(insufficient)
        invariant["protected_invariants"]["claim_ceiling_preserved"] = True
        invalid_variants.append(invariant)
        hold = copy.deepcopy(insufficient)
        hold["evidence_hold_codes"] = ["SOURCE_VERIFICATION_REQUIRED"]
        invalid_variants.append(hold)
        wrong_reason = coverage_state()
        wrong_reason["basis_reason_codes"] = ["FRAGMENT_OR_EXCERPT_ONLY"]
        invalid_variants.append(wrong_reason)
        for invalid in invalid_variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(HarnessContractError):
                    validate_coverage(invalid)

    def test_adjudication_hash_binding_and_cross_stage_accounting(self) -> None:
        coverage = validate_coverage(coverage_state(candidate="contribution"))
        state = model_state(candidate="contribution")
        envelope = {
            "coverage_digest_sha256": canonical_digest(coverage),
            **state,
        }
        self.assertEqual(state, validate_adjudication_binding(envelope, coverage))
        validate_cross_stage_consistency(coverage, state)
        stale = dict(envelope)
        stale["coverage_digest_sha256"] = "0" * 64
        with self.assertRaises(HarnessContractError):
            validate_adjudication_binding(stale, coverage)
        dropped = model_state()
        with self.assertRaises(HarnessContractError):
            validate_cross_stage_consistency(coverage, dropped)

    def test_cross_stage_gate_rejects_dropped_holds_and_unbound_invariants(self) -> None:
        coverage = validate_coverage(coverage_state())
        coverage["evidence_hold_codes"] = ["SOURCE_VERIFICATION_REQUIRED"]
        with self.assertRaises(HarnessContractError):
            validate_cross_stage_consistency(coverage, model_state())
        invariant = validate_coverage(coverage_state())
        invariant["protected_invariants"]["claim_ceiling_preserved"] = False
        with self.assertRaises(HarnessContractError):
            validate_cross_stage_consistency(invariant, model_state())


if __name__ == "__main__":
    unittest.main()
