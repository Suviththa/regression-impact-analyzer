import io
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

from regression_impact.dependency_analysis import DependencyAnalysisResult
from regression_impact.github_client import (
    GitHubApiError,
    GitHubClient,
    GitHubNetworkError,
)
from regression_impact.release_diff import ReleaseComparison
from regression_impact.setup_flow import SetupContext

class SourceContextError(Exception):
    pass

@dataclass(frozen=True)
class SourceContextFile:
    path: str
    source_version: str
    roles: tuple[str, ...]
    change_status: Optional[str]
    dependency_depth: Optional[int]
    content: str

@dataclass(frozen=True)
class SourceContextResult:
    files: List[SourceContextFile]

@dataclass
class ContextFileRequest:
    path: str
    source_version: str
    roles: Set[str]
    change_status: Optional[str] = None
    dependency_depth: Optional[int] = None

def safe_extract_zip(
    archive_bytes: bytes,
    destination: Path,
) -> Path:
    destination = destination.resolve()

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()

                try:
                    target.relative_to(destination)
                except ValueError as exc:
                    raise SourceContextError(
                        "Unsafe path found in repository archive."
                    ) from exc

            archive.extractall(destination)

    except zipfile.BadZipFile as exc:
        raise SourceContextError(
            "GitHub returned an invalid ZIP archive."
        ) from exc

    directories = [
        item for item in destination.iterdir() if item.is_dir()
    ]

    if len(directories) != 1:
        raise SourceContextError(
            "Unexpected repository archive structure."
        )

    return directories[0]


def build_context_requests(
    comparison: ReleaseComparison,
    dependency_result: DependencyAnalysisResult,
) -> Dict[str, ContextFileRequest]:
    requests: Dict[str, ContextFileRequest] = {}

    # First include every changed file.
    for changed in comparison.changed_files:
        # A removed file no longer exists in the current release,
        # so retrieve it from the previous release.
        source_version = (
            "previous" if changed.status == "removed" else "current"
        )

        requests[changed.path] = ContextFileRequest(
            path=changed.path,
            source_version=source_version,
            roles={"changed"},
            change_status=changed.status,
        )

    # Then include modules discovered through dependency analysis.
    for impact in dependency_result.impacts:
        for dependent in impact.impacted_modules:
            existing = requests.get(dependent.file_path)

            if existing is not None:
                existing.roles.add("dependent")
                if (
                    existing.dependency_depth is None
                    or dependent.depth < existing.dependency_depth
                ):
                    existing.dependency_depth = dependent.depth
                continue

            requests[dependent.file_path] = ContextFileRequest(
                path=dependent.file_path,
                source_version="current",
                roles={"dependent"},
                dependency_depth=dependent.depth,
            )

    return requests


def resolve_source_file(
    repository_root: Path,
    relative_path: str,
) -> Path:
    repository_root = repository_root.resolve()
    source_path = (repository_root / relative_path).resolve()

    try:
        source_path.relative_to(repository_root)
    except ValueError as exc:
        raise SourceContextError(
            f"Unsafe source path: {relative_path}"
        ) from exc

    return source_path


def read_source_file(
    repository_root: Path,
    relative_path: str,
) -> str:
    source_path = resolve_source_file(
        repository_root,
        relative_path,
    )

    if not source_path.is_file():
        raise SourceContextError(
            f"Source file was not found in the selected release: {relative_path}"
        )

    try:
        return source_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SourceContextError(
            f"Source context file is not UTF-8 text: {relative_path}"
        ) from exc
    except OSError as exc:
        raise SourceContextError(
            f"Unable to read source file: {relative_path}"
        ) from exc


def run_source_context_collection(
    context: SetupContext,
    comparison: ReleaseComparison,
    dependency_result: DependencyAnalysisResult,
) -> SourceContextResult:
    repository = context.repository

    requests = build_context_requests(
        comparison,
        dependency_result,
    )

    print("\nRelevant Source Context")
    print(f"Collecting source for {len(requests)} relevant file(s)...")

    if not requests:
        return SourceContextResult(files=[])

    client = GitHubClient(token=context.token)

    needs_current = any(
        request.source_version == "current"
        for request in requests.values()
    )
    needs_previous = any(
        request.source_version == "previous"
        for request in requests.values()
    )

    current_archive: Optional[bytes] = None
    previous_archive: Optional[bytes] = None

    try:
        if needs_current:
            print(
                f"Downloading current source snapshot ({comparison.current_tag})..."
            )
            current_archive = client.download_repository_archive(
                owner=repository.owner,
                repository=repository.name,
                ref=comparison.current_sha,
            )

        if needs_previous:
            print(
                f"Downloading previous source snapshot ({comparison.previous_tag})..."
            )
            previous_archive = client.download_repository_archive(
                owner=repository.owner,
                repository=repository.name,
                ref=comparison.previous_sha,
            )

    except (GitHubApiError, GitHubNetworkError) as exc:
        raise SourceContextError(
            f"Unable to download source context snapshot: {exc}"
        ) from exc

    collected: List[SourceContextFile] = []

    with tempfile.TemporaryDirectory(
        prefix="regression-impact-context-"
    ) as temp_directory:
        temp_root = Path(temp_directory)
        current_root: Optional[Path] = None
        previous_root: Optional[Path] = None

        if current_archive is not None:
            current_destination = temp_root / "current"
            current_destination.mkdir()
            current_root = safe_extract_zip(
                current_archive,
                current_destination,
            )

        if previous_archive is not None:
            previous_destination = temp_root / "previous"
            previous_destination.mkdir()
            previous_root = safe_extract_zip(
                previous_archive,
                previous_destination,
            )

        for path in sorted(requests):
            request = requests[path]

            if request.source_version == "current":
                repository_root = current_root
            else:
                repository_root = previous_root

            if repository_root is None:
                raise SourceContextError(
                    f"Required source snapshot was not available for {path}."
                )

            content = read_source_file(
                repository_root,
                request.path,
            )

            collected.append(
                SourceContextFile(
                    path=request.path,
                    source_version=request.source_version,
                    roles=tuple(sorted(request.roles)),
                    change_status=request.change_status,
                    dependency_depth=request.dependency_depth,
                    content=content,
                )
            )

    return SourceContextResult(files=collected)


def print_source_context_summary(
    result: SourceContextResult,
) -> None:
    print("\n" + "=" * 65)
    print("Relevant source context collected")
    print("=" * 65)
    print(f"Files collected: {len(result.files)}")

    if not result.files:
        print("No source files were selected.")
        print("=" * 65)
        return

    for source_file in result.files:
        roles = ", ".join(role.upper() for role in source_file.roles)

        details = []
        if source_file.change_status:
            details.append(source_file.change_status)
        if source_file.dependency_depth is not None:
            details.append(f"depth {source_file.dependency_depth}")

        detail_text = f" ({', '.join(details)})" if details else ""
        line_count = len(source_file.content.splitlines())

        print(
            f"  [{roles}] {source_file.path}{detail_text} - {line_count} lines"
        )

    print("=" * 65)

