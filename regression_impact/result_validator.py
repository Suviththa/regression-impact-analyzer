from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)


# ============================================================
# Errors
# ============================================================


class RegressionResultValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ============================================================
# Strict AI output models
# ============================================================


class StrictAIModel(BaseModel):
    """
    The AI must return only fields defined in the contract.
    """

    model_config = ConfigDict(
        extra="forbid"
    )


class AffectedArea(StrictAIModel):
    area: str = Field(min_length=1)

    impactType: Literal[
        "direct",
        "downstream",
    ]

    summary: str = Field(min_length=1)


class TestToRun(StrictAIModel):
    """
    Test metadata is copied by the Agent from REGRESSION_TESTS.

    Python does not know or query the regression catalogue.
    """

    testCaseId: str = Field(min_length=1)

    scenario: str = Field(min_length=1)

    featureArea: str = Field(min_length=1)

    cataloguePriority: str = Field(min_length=1)

    runOrder: Literal[
        "run_first",
        "run_next",
        "optional",
    ]

    predictedOutcome: Literal[
        "pass",
        "fail",
        "uncertain",
    ]

    reason: str = Field(min_length=1)


class CoverageGap(StrictAIModel):
    area: str = Field(min_length=1)

    gap: str = Field(min_length=1)

    action: str = Field(min_length=1)


class ProposedTest(StrictAIModel):
    reference: str = Field(min_length=1)

    featureArea: str = Field(min_length=1)

    scenario: str = Field(min_length=1)

    preconditions: str = Field(min_length=1)

    steps: list[str] = Field(min_length=1)

    expectedResult: str = Field(min_length=1)

    reason: str = Field(min_length=1)

    @field_validator("steps", mode="before")
    @classmethod
    def normalize_steps(cls, value):
        if not isinstance(value, list):
            return value

        normalized = []

        for step in value:
            # Already a plain string
            if isinstance(step, str):
                normalized.append(step)

            # Copilot Studio structured-output format
            elif isinstance(step, dict):
                item = step.get("item")

                if isinstance(item, str):
                    normalized.append(item)
                else:
                    raise ValueError(
                        "Each step object must contain a string 'item' field"
                    )

            else:
                raise ValueError(
                    "Each step must be either a string or an object containing 'item'"
                )

        return normalized


class RegressionAnalysisCandidate(StrictAIModel):
    summary: str = Field(min_length=1)

    affectedAreas: list[AffectedArea] = Field(
        default_factory=list
    )

    testsToRun: list[TestToRun] = Field(
        default_factory=list
    )

    coverageGaps: list[CoverageGap] = Field(
        default_factory=list
    )

    proposedTests: list[ProposedTest] = Field(
        default_factory=list
    )


# ============================================================
# API request
# ============================================================


class ValidationRequest(BaseModel):
    """
    No regression catalogue is passed here.

    Python only receives:
    1. Agent analysis
    2. Deterministic code-impact context
    """

    analysisResult: Any

    codeImpactContext: Any


# ============================================================
# JSON helpers
# ============================================================


def _strip_code_fence(
    value: str,
) -> str:

    text = value.strip()

    if not text.startswith("```"):
        return text

    lines = text.splitlines()

    if lines:
        lines = lines[1:]

    if (
        lines
        and lines[-1].strip() == "```"
    ):
        lines = lines[:-1]

    return "\n".join(lines).strip()


def _parse_json_value(
    value: Any,
    field_name: str,
    expected_type: type,
) -> Any:

    parsed = value

    if isinstance(value, str):

        cleaned = _strip_code_fence(
            value
        )

        try:
            parsed = json.loads(cleaned)

        except json.JSONDecodeError as exc:

            raise RegressionResultValidationError(
                [
                    f"{field_name} is not valid JSON: "
                    f"{exc.msg}"
                ]
            ) from exc

    if not isinstance(
        parsed,
        expected_type,
    ):

        raise RegressionResultValidationError(
            [
                f"{field_name} must be a "
                f"{expected_type.__name__}."
            ]
        )

    return parsed


def _format_model_errors(
    prefix: str,
    exc: ValidationError,
) -> list[str]:

    errors: list[str] = []

    for error in exc.errors():

        location = ".".join(
            str(part)
            for part in error["loc"]
        )

        errors.append(
            f"{prefix}.{location}: "
            f"{error['msg']}"
        )

    return errors


# ============================================================
# Code-impact helpers
# ============================================================


def _get_release_tag(
    context: dict,
    property_name: str,
) -> str:

    release = context.get(
        property_name
    )

    if not isinstance(
        release,
        dict,
    ):
        return ""

    tag = release.get("tag")

    if not isinstance(tag, str):
        return ""

    return tag


def _extract_changed_files(
    context: dict,
) -> list[str]:

    result: list[str] = []

    for changed in context.get(
        "changedFiles",
        [],
    ):

        if not isinstance(
            changed,
            dict,
        ):
            continue

        if (
            changed.get(
                "includedInAnalysis",
                True,
            )
            is False
        ):
            continue

        path = changed.get("path")

        if (
            isinstance(path, str)
            and path not in result
        ):
            result.append(path)

    return result


def _extract_changed_symbols(
    context: dict,
) -> list[str]:

    result: list[str] = []

    for changed in context.get(
        "changedSymbols",
        [],
    ):

        if not isinstance(
            changed,
            dict,
        ):
            continue

        name = changed.get("name")

        if (
            isinstance(name, str)
            and name not in result
        ):
            result.append(name)

    return result


def _extract_dependency_candidates(
    context: dict,
) -> list[str]:

    result: list[str] = []

    for impact in context.get(
        "dependencyImpacts",
        [],
    ):

        if not isinstance(
            impact,
            dict,
        ):
            continue

        for dependent in impact.get(
            "dependents",
            [],
        ):

            if not isinstance(
                dependent,
                dict,
            ):
                continue

            module = dependent.get(
                "module"
            )

            if (
                isinstance(module, str)
                and module not in result
            ):
                result.append(module)

    return result


# ============================================================
# Main validation
# ============================================================


def validate_and_build_regression_result(
    analysis_result: Any,
    code_impact_context: Any,
) -> dict:

    errors: list[str] = []

    # ========================================================
    # 1. Parse and validate AI result
    # ========================================================

    analysis_payload = (
        _parse_json_value(
            analysis_result,
            "analysisResult",
            dict,
        )
    )

    try:

        analysis = (
            RegressionAnalysisCandidate
            .model_validate(
                analysis_payload
            )
        )

    except ValidationError as exc:

        raise RegressionResultValidationError(
            _format_model_errors(
                "analysisResult",
                exc,
            )
        ) from exc

    # ========================================================
    # 2. Parse deterministic code-impact context
    # ========================================================

    context = _parse_json_value(
        code_impact_context,
        "codeImpactContext",
        dict,
    )

    repository = context.get(
        "repository"
    )

    previous_release = (
        _get_release_tag(
            context,
            "previousRelease",
        )
    )

    current_release = (
        _get_release_tag(
            context,
            "currentRelease",
        )
    )

    if (
        not isinstance(
            repository,
            str,
        )
        or not repository.strip()
    ):
        errors.append(
            "codeImpactContext.repository "
            "is missing."
        )

    if not previous_release:
        errors.append(
            "codeImpactContext."
            "previousRelease.tag is missing."
        )

    if not current_release:
        errors.append(
            "codeImpactContext."
            "currentRelease.tag is missing."
        )

    # ========================================================
    # 3. Detect duplicate selected test references
    # ========================================================

    selected_ids = [
        test.testCaseId
        for test
        in analysis.testsToRun
    ]

    if len(selected_ids) != len(
        set(selected_ids)
    ):

        errors.append(
            "Duplicate testCaseIds found "
            "in testsToRun."
        )

    # ========================================================
    # 4. Validate proposed-test references
    # ========================================================

    proposed_refs: list[str] = []

    proposed_pattern = re.compile(
        r"^PROPOSED-\d{3}$"
    )

    for proposed in (
        analysis.proposedTests
    ):

        reference = (
            proposed.reference.strip()
        )

        proposed_refs.append(
            reference
        )

        if not proposed_pattern.fullmatch(
            reference
        ):

            errors.append(
                "Invalid proposed test "
                f"reference: {reference}. "
                "Expected format "
                "PROPOSED-001."
            )

        for step in proposed.steps:

            if not step.strip():

                errors.append(
                    f"{reference} contains "
                    "an empty test step."
                )

    if len(proposed_refs) != len(
        set(proposed_refs)
    ):

        errors.append(
            "Duplicate proposed test "
            "references found."
        )

    # ========================================================
    # 5. Stop if invalid
    # ========================================================

    if errors:

        raise (
            RegressionResultValidationError(
                errors
            )
        )

    # ========================================================
    # 6. Deterministically calculate selected-test statistics
    # ========================================================

    recommended_tests = len(
        analysis.testsToRun
    )

    predicted_pass = sum(
        1
        for test
        in analysis.testsToRun
        if test.predictedOutcome
        == "pass"
    )

    predicted_fail = sum(
        1
        for test
        in analysis.testsToRun
        if test.predictedOutcome
        == "fail"
    )

    uncertain = sum(
        1
        for test
        in analysis.testsToRun
        if test.predictedOutcome
        == "uncertain"
    )

    predicted_pass_rate = (
        round(
            (
                predicted_pass
                / recommended_tests
            )
            * 100,
            2,
        )
        if recommended_tests > 0
        else 0
    )

    # ========================================================
    # 7. Sort selected tests
    # ========================================================

    run_order_rank = {
        "run_first": 0,
        "run_next": 1,
        "optional": 2,
    }

    final_tests = [
        test.model_dump()
        for test
        in analysis.testsToRun
    ]

    final_tests.sort(
        key=lambda test: (
            run_order_rank.get(
                test["runOrder"],
                99,
            )
        )
    )

    # ========================================================
    # 8. Derive failure candidates
    # ========================================================

    likely_failure_candidates = [
        {
            "testCaseId":
                test["testCaseId"],

            "scenario":
                test["scenario"],

            "reason":
                test["reason"],
        }

        for test in final_tests

        if test["predictedOutcome"]
        == "fail"
    ]

    # ========================================================
    # 9. Build technical metadata from trusted Python context
    # ========================================================

    technical_details = {

        "changedFiles":
            _extract_changed_files(
                context
            ),

        "changedSymbols":
            _extract_changed_symbols(
                context
            ),

        "dependencyCandidates":
            _extract_dependency_candidates(
                context
            ),
    }

    # ========================================================
    # 10. Final frontend result
    # ========================================================

    return {

        "release": {

            "repository":
                repository,

            "from":
                previous_release,

            "to":
                current_release,
        },

        "summary":
            analysis.summary,

        "regressionOverview": {

            "recommendedTests":
                recommended_tests,

            "predictedPass":
                predicted_pass,

            "predictedFail":
                predicted_fail,

            "uncertain":
                uncertain,

            "predictedPassRate":
                predicted_pass_rate,
        },

        "affectedAreas": [
            area.model_dump()
            for area
            in analysis.affectedAreas
        ],

        "testsToRun":
            final_tests,

        "likelyFailureCandidates":
            likely_failure_candidates,

        "coverageGaps": [
            gap.model_dump()
            for gap
            in analysis.coverageGaps
        ],

        "proposedTests": [
            proposed.model_dump()
            for proposed
            in analysis.proposedTests
        ],

        "technicalDetails":
            technical_details,
    }