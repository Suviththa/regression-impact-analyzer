from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from regression_impact.impact_service import (
    ImpactServiceError,
    analyze_release_impact,
)
from regression_impact.result_validator import (
    RegressionResultValidationError,
    ValidationRequest,
    validate_and_build_regression_result,
)

app = FastAPI(
    title="Regression Impact Analyzer",
    description=(
        "Analyzes code impact between two software "
        "release tags for regression analysis."
    ),
    version="0.1.0",
)


class ReleaseImpactRequest(BaseModel):
    owner: str = Field(description="GitHub repository owner or organization.")
    repository: str = Field(description="GitHub repository name.")
    previousRelease: str = Field(
        description="Previous release tag, for example v1.2.0."
    )
    currentRelease: str = Field(
        description="Current release tag, for example v1.3.0."
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/analyze-release-impact")
def analyze_release(
    request: ReleaseImpactRequest,
    x_github_token: str | None = Header(
        default=None,
        alias="X-GitHub-Token",
    ),
) -> dict:
    try:
        return analyze_release_impact(
            owner=request.owner,
            repository=request.repository,
            previous_release=request.previousRelease,
            current_release=request.currentRelease,
            github_token=x_github_token,
        )
    except ImpactServiceError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post("/validate-regression-result")
def validate_regression_result(
    request: ValidationRequest,
) -> dict:
    try:
        result = validate_and_build_regression_result(
            analysis_result=request.analysisResult,
            regression_tests=request.regressionTests,
            code_impact_context=request.codeImpactContext,
        )
        return {
            "valid": True,
            "errors": [],
            "result": result,
        }
    except RegressionResultValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "valid": False,
                "errors": exc.errors,
            },
        ) from exc
    except Exception as exc:
        print(
            "Unexpected regression validator error:",
            repr(exc),
        )
        raise HTTPException(
            status_code=500,
            detail={
                "valid": False,
                "errors": [
                    "Regression result validator failed unexpectedly."
                ],
            },
        ) from exc