import difflib
import hashlib
import io
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from regression_impact.github_client import (
    GitHubApiError,
    GitHubClient,
    GitHubNetworkError,
)
from regression_impact.release_selection import ReleaseSelection
from regression_impact.setup_flow import SetupContext


class ReleaseDiffError(Exception):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    relative_path: str
    absolute_path: Path
    sha256: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    previous_path: Optional[str] = None


@dataclass(frozen=True)
class ReleaseComparison:
    previous_tag: str
    current_tag: str
    previous_sha: str
    current_sha: str
    changed_files: List[ChangedFile]
    diff_text: str


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def safe_extract_zip(
    archive_bytes: bytes,
    destination: Path,
) -> Path:
    destination = destination.resolve()

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for member in archive.infolist():
                member_path = (destination / member.filename).resolve()

                try:
                    member_path.relative_to(destination)
                except ValueError as exc:
                    raise ReleaseDiffError(
                        "Unsafe path found in repository archive."
                    ) from exc

            archive.extractall(destination)

    except zipfile.BadZipFile as exc:
        raise ReleaseDiffError(
            "GitHub returned an invalid ZIP archive."
        ) from exc

    children = list(destination.iterdir())

    directories = [child for child in children if child.is_dir()]

    if len(directories) != 1:
        raise ReleaseDiffError("Unexpected GitHub archive structure.")

    return directories[0]


def build_file_index(
    repository_root: Path,
) -> Dict[str, FileSnapshot]:
    result: Dict[str, FileSnapshot] = {}

    for path in repository_root.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(repository_root).as_posix()

        result[relative] = FileSnapshot(
            relative_path=relative,
            absolute_path=path,
            sha256=calculate_sha256(path),
        )

    return result


def is_text_file(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:8192]

        if b"\x00" in sample:
            return False

        sample.decode("utf-8")
        return True

    except UnicodeDecodeError:
        return False


def read_text_file(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def create_text_diff(
    previous_path: Optional[Path],
    current_path: Optional[Path],
    display_path: str,
) -> str:
    previous_lines: List[str] = []
    current_lines: List[str] = []

    if previous_path is not None:
        previous_lines = read_text_file(previous_path)

    if current_path is not None:
        current_lines = read_text_file(current_path)

    from_name = f"a/{display_path}" if previous_path is not None else "/dev/null"
    to_name = f"b/{display_path}" if current_path is not None else "/dev/null"

    diff = difflib.unified_diff(
        previous_lines,
        current_lines,
        fromfile=from_name,
        tofile=to_name,
    )

    return "".join(diff)


def compare_snapshots(
    previous_root: Path,
    current_root: Path,
) -> tuple[List[ChangedFile], str]:
    previous = build_file_index(previous_root)
    current = build_file_index(current_root)

    previous_paths = set(previous)
    current_paths = set(current)

    removed_paths = previous_paths - current_paths
    added_paths = current_paths - previous_paths
    common_paths = previous_paths & current_paths

    changed_files: List[ChangedFile] = []
    diff_parts: List[str] = []

    # Detect exact-content renames.
    removed_by_hash: Dict[str, List[str]] = {}

    for path in removed_paths:
        removed_by_hash.setdefault(
            previous[path].sha256,
            [],
        ).append(path)

    renamed_removed = set()
    renamed_added = set()

    for new_path in sorted(added_paths):
        new_hash = current[new_path].sha256
        candidates = removed_by_hash.get(new_hash, [])

        available_candidates = [
            candidate
            for candidate in candidates
            if candidate not in renamed_removed
        ]

        if not available_candidates:
            continue

        old_path = sorted(available_candidates)[0]
        renamed_removed.add(old_path)
        renamed_added.add(new_path)

        changed_files.append(
            ChangedFile(
                path=new_path,
                previous_path=old_path,
                status="renamed",
            )
        )

        diff_parts.append(
            f"rename from {old_path}\n"
            f"rename to {new_path}\n"
        )

    # Added files.
    for path in sorted(added_paths - renamed_added):
        snapshot = current[path]

        changed_files.append(
            ChangedFile(
                path=path,
                status="added",
            )
        )

        if is_text_file(snapshot.absolute_path):
            diff_parts.append(
                create_text_diff(
                    previous_path=None,
                    current_path=snapshot.absolute_path,
                    display_path=path,
                )
            )
        else:
            diff_parts.append(f"Binary file added: {path}\n")

    # Removed files.
    for path in sorted(removed_paths - renamed_removed):
        snapshot = previous[path]

        changed_files.append(
            ChangedFile(
                path=path,
                status="removed",
            )
        )

        if is_text_file(snapshot.absolute_path):
            diff_parts.append(
                create_text_diff(
                    previous_path=snapshot.absolute_path,
                    current_path=None,
                    display_path=path,
                )
            )
        else:
            diff_parts.append(f"Binary file removed: {path}\n")

    # Modified files.
    for path in sorted(common_paths):
        old_snapshot = previous[path]
        new_snapshot = current[path]

        if old_snapshot.sha256 == new_snapshot.sha256:
            continue

        changed_files.append(
            ChangedFile(
                path=path,
                status="modified",
            )
        )

        if is_text_file(old_snapshot.absolute_path) and is_text_file(
            new_snapshot.absolute_path
        ):
            diff_parts.append(
                create_text_diff(
                    previous_path=old_snapshot.absolute_path,
                    current_path=new_snapshot.absolute_path,
                    display_path=path,
                )
            )
        else:
            diff_parts.append(f"Binary file modified: {path}\n")

    changed_files.sort(
        key=lambda item: (
            item.path,
            item.status,
        )
    )

    return (
        changed_files,
        "\n".join(diff_parts),
    )


def run_release_comparison(
    context: SetupContext,
    selection: ReleaseSelection,
) -> ReleaseComparison:
    repository = context.repository
    client = GitHubClient(token=context.token)

    print("\nRelease Change Analysis")
    print(
        f"Comparing "
        f"{selection.previous.name} "
        f"→ "
        f"{selection.current.name}"
    )

    try:
        print(
            f"\nDownloading "
            f"{selection.previous.name} snapshot..."
        )
        previous_archive = client.download_repository_archive(
            owner=repository.owner,
            repository=repository.name,
            ref=selection.previous.commit_sha,
        )

        print(
            f"Downloading "
            f"{selection.current.name} snapshot..."
        )
        current_archive = client.download_repository_archive(
            owner=repository.owner,
            repository=repository.name,
            ref=selection.current.commit_sha,
        )

    except (
        GitHubApiError,
        GitHubNetworkError,
    ) as exc:
        raise ReleaseDiffError(
            f"Unable to download release snapshots: {exc}"
        ) from exc

    with tempfile.TemporaryDirectory(
        prefix="regression-impact-"
    ) as temp_directory:
        temp_root = Path(temp_directory)
        previous_destination = temp_root / "previous"
        current_destination = temp_root / "current"

        previous_destination.mkdir()
        current_destination.mkdir()

        previous_root = safe_extract_zip(
            previous_archive,
            previous_destination,
        )
        current_root = safe_extract_zip(
            current_archive,
            current_destination,
        )

        changed_files, diff_text = compare_snapshots(
            previous_root,
            current_root,
        )

    return ReleaseComparison(
        previous_tag=selection.previous.name,
        current_tag=selection.current.name,
        previous_sha=selection.previous.commit_sha,
        current_sha=selection.current.commit_sha,
        changed_files=changed_files,
        diff_text=diff_text,
    )


def print_comparison_summary(
    comparison: ReleaseComparison,
) -> None:
    print("\n" + "=" * 65)
    print("Release change analysis complete")
    print("=" * 65)
    print(f"Previous : {comparison.previous_tag}")
    print(f"Current  : {comparison.current_tag}")
    print(f"Files changed: {len(comparison.changed_files)}")

    print("\nChanged files:")

    if not comparison.changed_files:
        print("  No file changes detected.")
    else:
        status_labels = {
            "added": "A",
            "modified": "M",
            "removed": "D",
            "renamed": "R",
        }

        for item in comparison.changed_files:
            status = status_labels.get(
                item.status,
                "?",
            )

            if item.status == "renamed":
                print(
                    f"  {status} "
                    f"{item.previous_path} "
                    f"-> {item.path}"
                )
            else:
                print(f"  {status} {item.path}")

    print("=" * 65)


def print_diff_preview(
    comparison: ReleaseComparison,
    max_lines: int = 80,
) -> None:
    if not comparison.diff_text:
        return

    lines = comparison.diff_text.splitlines()

    print("\nDiff preview")
    print("-" * 65)

    for line in lines[:max_lines]:
        print(line)

    if len(lines) > max_lines:
        remaining = len(lines) - max_lines
        print(
            f"\n... {remaining} more diff line(s) not displayed."
        )

    print("-" * 65)

