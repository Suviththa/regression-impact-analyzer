from typing import Optional

from regression_impact.dependency_analysis import (
    DependencyAnalysisError,
    run_dependency_analysis,
)
from regression_impact.github_client import (
    GitHubApiError,
    GitHubClient,
    GitHubNetworkError,
)
from regression_impact.release_diff import (
    ReleaseDiffError,
    run_release_comparison,
)
from regression_impact.release_selection import ReleaseSelection
from regression_impact.setup_flow import SetupContext
from regression_impact.source_context import (
    SourceContextError,
    run_source_context_collection,
)


class ImpactServiceError(Exception):
    pass

def analyze_release_impact(
    owner: str,
    repository: str,
    previous_release: str,
    current_release: str,
    github_token: Optional[str] = None,
) -> dict:
    owner = owner.strip()
    repository = repository.strip()
    previous_release = previous_release.strip()
    current_release = current_release.strip()

    if not owner:
        raise ImpactServiceError("Repository owner is required.")

    if not repository:
        raise ImpactServiceError("Repository name is required.")

    if not previous_release:
        raise ImpactServiceError("Previous release is required.")

    if not current_release:
        raise ImpactServiceError("Current release is required.")

    if previous_release == current_release:
        raise ImpactServiceError(
            "Previous and current releases must be different."
        )

    client = GitHubClient(token=github_token)

    # Resolve repository and release tags.
    try:
        repository_info = client.get_repository(
            owner,
            repository,
        )

        tags = client.list_repository_tags(
            owner,
            repository,
        )

    except (
        GitHubApiError,
        GitHubNetworkError,
    ) as exc:
        raise ImpactServiceError(
            f"Unable to access repository: {exc}"
        ) from exc

    tags_by_name = {tag.name: tag for tag in tags}

    previous_tag = tags_by_name.get(previous_release)

    if previous_tag is None:
        raise ImpactServiceError(
            f"Previous release tag '{previous_release}' was not found."
        )

    current_tag = tags_by_name.get(current_release)

    if current_tag is None:
        raise ImpactServiceError(
            f"Current release tag '{current_release}' was not found."
        )

    # Build the same objects our existing CLI uses.
    context = SetupContext(
        repository=repository_info,
        token=github_token,
    )

    selection = ReleaseSelection(
        previous=previous_tag,
        current=current_tag,
    )

    try:
        # STEP 3
        comparison = run_release_comparison(
            context,
            selection,
        )

        # STEP 4
        dependency_result = run_dependency_analysis(
            context,
            comparison,
        )

        # STEP 5
        source_context = run_source_context_collection(
            context,
            comparison,
            dependency_result,
        )

    except (
        ReleaseDiffError,
        DependencyAnalysisError,
        SourceContextError,
    ) as exc:
        raise ImpactServiceError(
            f"Release impact analysis failed: {exc}"
        ) from exc

    # Convert our internal Python objects into a JSON-friendly dictionary.
    return {
        "repository": repository_info.full_name,
        "previousRelease": {
            "tag": previous_tag.name,
            "sha": previous_tag.commit_sha,
        },
        "currentRelease": {
            "tag": current_tag.name,
            "sha": current_tag.commit_sha,
        },
        "changedFiles": [
            {
                "path": changed.path,
                "status": changed.status,
                "previousPath": changed.previous_path,
            }
            for changed in comparison.changed_files
        ],
        "diff": comparison.diff_text,
        "dependencyImpacts": [
            {
                "changedFile": impact.changed_file,
                "changedModule": impact.changed_module,
                "dependents": [
                    {
                        "module": dependent.module,
                        "filePath": dependent.file_path,
                        "depth": dependent.depth,
                    }
                    for dependent in impact.impacted_modules
                ],
            }
            for impact in dependency_result.impacts
        ],
        "sourceContext": [
            {
                "path": source_file.path,
                "sourceVersion": source_file.source_version,
                "roles": list(source_file.roles),
                "changeStatus": source_file.change_status,
                "dependencyDepth": source_file.dependency_depth,
                "content": source_file.content,
            }
            for source_file in source_context.files
        ],
    }

