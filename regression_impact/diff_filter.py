from __future__ import annotations

import difflib
import io
import tempfile
import tokenize
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

from regression_impact.github_client import (
    GitHubApiError,
    GitHubClient,
    GitHubNetworkError,
)
from regression_impact.release_diff import (
    ChangedFile,
    ReleaseComparison,
)
from regression_impact.setup_flow import SetupContext


class DiffFilterError(Exception):
    pass


# Only exclude things that are very strongly associated with
# generated/cache/editor artifacts.
#
# Deliberately NOT included:
#   build/
#   dist/
#   .so
#   .pyd
#   .class
#   .png / .jpg
#   .md / .rst
#
# Those can potentially matter to application behaviour.
NOISE_DIRECTORY_NAMES: Set[str] = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "htmlcov",
}

NOISE_EXTENSIONS: Set[str] = {
    ".pyc",
    ".pyo",
    ".swp",
    ".tmp",
    ".temp",
    ".bak",
}

NOISE_FILENAMES: Set[str] = {
    ".DS_Store",
    ".coverage",
}


@dataclass(frozen=True)
class FileDecision:
    file: str
    status: str
    included: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "status": self.status,
            "included": self.included,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DiffFilterResult:
    raw_diff: str
    meaningful_diff: str
    included_changed_files: List[ChangedFile]
    decisions: List[FileDecision]

    @property
    def included_paths(self) -> Set[str]:
        return {
            change.path
            for change in self.included_changed_files
        }

    @property
    def excluded_paths(self) -> Set[str]:
        return {
            decision.file
            for decision in self.decisions
            if not decision.included
        }

    def decision_for(
        self,
        path: str,
    ) -> Optional[FileDecision]:
        for decision in self.decisions:
            if decision.file == path:
                return decision

        return None

    def to_dict(self) -> dict:
        return {
            "includedFiles": len(
                self.included_changed_files
            ),
            "excludedFiles": len(
                self.excluded_paths
            ),
            "files": [
                decision.to_dict()
                for decision in self.decisions
            ],
        }


@dataclass(frozen=True)
class FileContent:
    exists: bool
    text: Optional[str]
    is_binary: bool


def _safe_extract_zip(
    archive_bytes: bytes,
    destination: Path,
) -> Path:
    destination = destination.resolve()

    try:
        with zipfile.ZipFile(
            io.BytesIO(archive_bytes)
        ) as archive:
            for member in archive.infolist():
                target = (
                    destination
                    / member.filename
                ).resolve()

                try:
                    target.relative_to(
                        destination
                    )
                except ValueError as exc:
                    raise DiffFilterError(
                        "Unsafe path found in repository archive."
                    ) from exc

            archive.extractall(destination)

    except zipfile.BadZipFile as exc:
        raise DiffFilterError(
            "GitHub returned an invalid ZIP archive."
        ) from exc

    directories = [
        item
        for item in destination.iterdir()
        if item.is_dir()
    ]

    if len(directories) != 1:
        raise DiffFilterError(
            "Unexpected repository archive structure."
        )

    return directories[0]


def _safe_repository_path(
    repository_root: Path,
    relative_path: str,
) -> Optional[Path]:
    repository_root = (
        repository_root.resolve()
    )

    path = (
        repository_root
        / relative_path
    ).resolve()

    try:
        path.relative_to(
            repository_root
        )
    except ValueError:
        return None

    return path


def _read_file(
    repository_root: Path,
    relative_path: str,
) -> FileContent:
    path = _safe_repository_path(
        repository_root,
        relative_path,
    )

    if (
        path is None
        or not path.is_file()
    ):
        return FileContent(
            exists=False,
            text=None,
            is_binary=False,
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DiffFilterError(
            f"Unable to read {relative_path}: {exc}"
        ) from exc

    # NUL bytes are a strong indication that the
    # file should not be treated as ordinary text.
    if b"\x00" in data:
        return FileContent(
            exists=True,
            text=None,
            is_binary=True,
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return FileContent(
            exists=True,
            text=None,
            is_binary=True,
        )

    return FileContent(
        exists=True,
        text=text,
        is_binary=False,
    )


def _is_definite_noise_path(
    path: str,
) -> bool:
    parsed = Path(path)

    if any(
        part in NOISE_DIRECTORY_NAMES
        for part in parsed.parts
    ):
        return True

    if parsed.name in NOISE_FILENAMES:
        return True

    if (
        parsed.suffix.lower()
        in NOISE_EXTENSIONS
    ):
        return True

    return False


def _multiline_string_lines(
    source: str,
) -> Optional[Set[int]]:
    """
    Find lines belonging to multi-line Python string
    literals.

    We protect these lines because whitespace inside a
    triple-quoted string can be real application data.
    """
    protected: Set[int] = set()

    try:
        tokens = tokenize.generate_tokens(
            io.StringIO(source).readline
        )

        for token in tokens:
            if (
                token.type == tokenize.STRING
                and token.start[0]
                < token.end[0]
            ):
                protected.update(
                    range(
                        token.start[0],
                        token.end[0] + 1,
                    )
                )

    except (
        tokenize.TokenError,
        IndentationError,
        SyntaxError,
    ):
        # If tokenization fails, do NOT guess.
        # The caller will retain the exact source.
        return None

    return protected


def _normalize_python_source(
    source: str,
) -> List[str]:
    """
    Normalize only whitespace that is safe to ignore
    for Python source.

    Important:
      - leading indentation is PRESERVED
      - comments are PRESERVED
      - string contents are PRESERVED
      - multi-line strings are protected
      - only trailing spaces/tabs are removed
      - whitespace-only lines become empty lines

    We DO NOT delete blank lines, so original line
    positions remain stable.
    """
    lines = source.splitlines()

    protected_lines = (
        _multiline_string_lines(
            source
        )
    )

    # Unable to safely understand the file.
    # Be conservative and keep everything exactly.
    if protected_lines is None:
        return lines

    normalized: List[str] = []

    for line_number, line in enumerate(
        lines,
        start=1,
    ):
        if (
            line_number
            in protected_lines
        ):
            normalized.append(line)
            continue

        # Only remove TRAILING spaces/tabs.
        #
        # Leading indentation is intentionally
        # untouched because it is significant in
        # Python.
        normalized.append(
            line.rstrip(" \t")
        )

    return normalized


def _normal_text_lines(
    source: str,
) -> List[str]:
    return source.splitlines()


def _build_unified_diff(
    previous_lines: List[str],
    current_lines: List[str],
    previous_path: Optional[str],
    current_path: Optional[str],
) -> str:
    from_file = (
        f"a/{previous_path}"
        if previous_path
        else "/dev/null"
    )

    to_file = (
        f"b/{current_path}"
        if current_path
        else "/dev/null"
    )

    diff_lines = difflib.unified_diff(
        previous_lines,
        current_lines,
        fromfile=from_file,
        tofile=to_file,
        lineterm="",
    )

    return "\n".join(diff_lines)


def _build_rename_description(
    previous_path: str,
    current_path: str,
) -> str:
    return (
        f"rename from {previous_path}\n"
        f"rename to {current_path}"
    )


def _build_binary_description(
    changed_file: ChangedFile,
) -> str:
    if (
        changed_file.status == "renamed"
        and changed_file.previous_path
    ):
        return (
            f"Binary/non-text file renamed:\n"
            f"rename from "
            f"{changed_file.previous_path}\n"
            f"rename to "
            f"{changed_file.path}"
        )

    return (
        f"Binary/non-text file "
        f"{changed_file.status}: "
        f"{changed_file.path}"
    )


def _build_empty_file_description(
    changed_file: ChangedFile,
) -> str:
    return (
        f"Empty file {changed_file.status}: "
        f"{changed_file.path}"
    )


def _analyze_file(
    changed_file: ChangedFile,
    previous_root: Path,
    current_root: Path,
) -> Tuple[
    FileDecision,
    Optional[str],
]:
    current_path = (
        changed_file.path
    )

    previous_path = (
        changed_file.previous_path
        or changed_file.path
    )

    #
    # For a rename, only exclude it automatically
    # when BOTH old and new locations are clearly
    # generated/noise locations.
    #
    if changed_file.status == "renamed":
        if (
            _is_definite_noise_path(
                previous_path
            )
            and _is_definite_noise_path(
                current_path
            )
        ):
            return (
                FileDecision(
                    file=current_path,
                    status=changed_file.status,
                    included=False,
                    reason=(
                        "Rename occurred entirely "
                        "inside generated/cache "
                        "content."
                    ),
                ),
                None,
            )

    elif _is_definite_noise_path(
        current_path
    ):
        return (
            FileDecision(
                file=current_path,
                status=changed_file.status,
                included=False,
                reason=(
                    "Generated/cache/editor artifact."
                ),
            ),
            None,
        )

    previous = _read_file(
        previous_root,
        previous_path,
    )

    current = _read_file(
        current_root,
        current_path,
    )

    #
    # Binary or unknown encoding:
    #
    # Do NOT discard it just because we cannot
    # understand it.
    #
    if (
        previous.is_binary
        or current.is_binary
    ):
        return (
            FileDecision(
                file=current_path,
                status=changed_file.status,
                included=True,
                reason=(
                    "Binary or non-UTF8 change "
                    "retained conservatively."
                ),
            ),
            _build_binary_description(
                changed_file
            ),
        )

    previous_text = (
        previous.text
        if previous.exists
        and previous.text is not None
        else ""
    )

    current_text = (
        current.text
        if current.exists
        and current.text is not None
        else ""
    )

    is_python = (
        current_path.lower().endswith(
            ".py"
        )
        or previous_path.lower().endswith(
            ".py"
        )
    )

    if is_python:
        previous_lines = (
            _normalize_python_source(
                previous_text
            )
        )

        current_lines = (
            _normalize_python_source(
                current_text
            )
        )

    else:
        #
        # For non-Python source we do NOT attempt
        # aggressive whitespace cleaning.
        #
        # YAML, shell scripts, templates, Markdown,
        # config files etc. can have different
        # whitespace semantics.
        #
        previous_lines = (
            _normal_text_lines(
                previous_text
            )
        )

        current_lines = (
            _normal_text_lines(
                current_text
            )
        )

    previous_diff_path = (
        previous_path
        if previous.exists
        else None
    )

    current_diff_path = (
        current_path
        if current.exists
        else None
    )

    meaningful_diff = (
        _build_unified_diff(
            previous_lines,
            current_lines,
            previous_diff_path,
            current_diff_path,
        )
    )

    #
    # Exact-content rename:
    #
    # difflib will produce nothing because the
    # contents are identical, but the path change
    # itself can affect imports and therefore IS
    # meaningful.
    #
    if (
        changed_file.status == "renamed"
        and changed_file.previous_path
    ):
        rename_description = (
            _build_rename_description(
                changed_file.previous_path,
                changed_file.path,
            )
        )

        if meaningful_diff:
            meaningful_diff = (
                rename_description
                + "\n"
                + meaningful_diff
            )
        else:
            meaningful_diff = (
                rename_description
            )

        return (
            FileDecision(
                file=current_path,
                status=changed_file.status,
                included=True,
                reason=(
                    "File rename retained because "
                    "module/path changes can affect "
                    "dependencies."
                ),
            ),
            meaningful_diff,
        )

    #
    # Added or removed empty files should NOT be
    # discarded automatically.
    #
    # Example: an empty __init__.py can still affect
    # Python package behaviour.
    #
    if (
        not meaningful_diff
        and changed_file.status
        in {"added", "removed"}
    ):
        return (
            FileDecision(
                file=current_path,
                status=changed_file.status,
                included=True,
                reason=(
                    "Added/removed file retained "
                    "even though it contains no "
                    "textual content."
                ),
            ),
            _build_empty_file_description(
                changed_file
            ),
        )

    #
    # This is the main noise-removal case.
    #
    # For a modified Python file, if its normalized
    # old/current contents are identical, then the
    # difference was only safe cosmetic whitespace
    # such as trailing spaces.
    #
    if (
        not meaningful_diff
        and changed_file.status == "modified"
        and is_python
    ):
        return (
            FileDecision(
                file=current_path,
                status=changed_file.status,
                included=False,
                reason=(
                    "Python file contains only "
                    "safe cosmetic whitespace "
                    "changes."
                ),
            ),
            None,
        )

    #
    # For a non-Python file whose raw text changed
    # but the line-based diff became empty, retain
    # it conservatively. This can happen with line
    # ending-only changes.
    #
    if (
        not meaningful_diff
        and previous_text != current_text
    ):
        return (
            FileDecision(
                file=current_path,
                status=changed_file.status,
                included=True,
                reason=(
                    "Text changed in a way the "
                    "normal diff did not represent; "
                    "retained conservatively."
                ),
            ),
            (
                "Formatting or line-ending change "
                f"retained: {current_path}"
            ),
        )

    if not meaningful_diff:
        return (
            FileDecision(
                file=current_path,
                status=changed_file.status,
                included=False,
                reason=(
                    "No meaningful textual change "
                    "remained after safe filtering."
                ),
            ),
            None,
        )

    return (
        FileDecision(
            file=current_path,
            status=changed_file.status,
            included=True,
            reason=(
                "Meaningful source change retained."
            ),
        ),
        meaningful_diff,
    )


def run_diff_filter(
    context: SetupContext,
    comparison: ReleaseComparison,
) -> DiffFilterResult:
    repository = context.repository

    client = GitHubClient(
        token=context.token
    )

    try:
        previous_archive = (
            client.download_repository_archive(
                owner=repository.owner,
                repository=repository.name,
                ref=comparison.previous_sha,
            )
        )

        current_archive = (
            client.download_repository_archive(
                owner=repository.owner,
                repository=repository.name,
                ref=comparison.current_sha,
            )
        )

    except (
        GitHubApiError,
        GitHubNetworkError,
    ) as exc:
        raise DiffFilterError(
            "Unable to download release "
            f"snapshots: {exc}"
        ) from exc

    decisions: List[
        FileDecision
    ] = []

    included_changed_files: List[
        ChangedFile
    ] = []

    meaningful_parts: List[str] = []

    with tempfile.TemporaryDirectory(
        prefix="regression-impact-filter-"
    ) as temporary_directory:
        temporary_root = Path(
            temporary_directory
        )

        previous_destination = (
            temporary_root / "previous"
        )

        current_destination = (
            temporary_root / "current"
        )

        previous_destination.mkdir()
        current_destination.mkdir()

        previous_root = (
            _safe_extract_zip(
                previous_archive,
                previous_destination,
            )
        )

        current_root = (
            _safe_extract_zip(
                current_archive,
                current_destination,
            )
        )

        for changed_file in (
            comparison.changed_files
        ):
            decision, meaningful = (
                _analyze_file(
                    changed_file,
                    previous_root,
                    current_root,
                )
            )

            decisions.append(
                decision
            )

            if decision.included:
                included_changed_files.append(
                    changed_file
                )

            if meaningful:
                meaningful_parts.append(
                    meaningful
                )

    meaningful_diff = (
        "\n\n".join(
            meaningful_parts
        )
    )

    return DiffFilterResult(
        raw_diff=comparison.diff_text,
        meaningful_diff=meaningful_diff,
        included_changed_files=(
            included_changed_files
        ),
        decisions=decisions,
    )

