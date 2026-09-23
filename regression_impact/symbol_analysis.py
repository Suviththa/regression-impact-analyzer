import ast
import io
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from regression_impact.github_client import (
    GitHubApiError,
    GitHubClient,
    GitHubNetworkError,
)
from regression_impact.release_diff import ReleaseComparison
from regression_impact.setup_flow import SetupContext


class SymbolAnalysisError(Exception):
    pass


@dataclass(frozen=True)
class ChangedLines:
    old_lines: Set[int]
    new_lines: Set[int]


@dataclass(frozen=True)
class SymbolSpan:
    name: str
    symbol_type: str
    start_line: int
    end_line: int

    @property
    def key(self) -> Tuple[str, str]:
        return (self.symbol_type, self.name)


@dataclass(frozen=True)
class ChangedSymbol:
    file: str
    name: Optional[str]
    symbol_type: str
    change_type: str


HUNK_PATTERN = re.compile(
    r"^@@ "
    r"-(\d+)(?:,\d+)? "
    r"\+(\d+)(?:,\d+)? "
    r"@@"
)


def normalize_diff_path(value: str) -> Optional[str]:
    value = value.strip()

    if value == "/dev/null":
        return None

    if value.startswith("a/"):
        return value[2:]

    if value.startswith("b/"):
        return value[2:]

    return value


def extract_changed_lines(
    diff_text: str,
) -> Dict[str, ChangedLines]:
    results: Dict[str, ChangedLines] = {}

    old_path: Optional[str] = None
    current_path: Optional[str] = None

    old_line = 0
    new_line = 0
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith("--- "):
            old_path = normalize_diff_path(line[4:])
            in_hunk = False
            continue

        if line.startswith("+++ "):
            new_path = normalize_diff_path(line[4:])
            current_path = new_path if new_path is not None else old_path

            if current_path is not None:
                results.setdefault(
                    current_path,
                    ChangedLines(old_lines=set(), new_lines=set()),
                )

            in_hunk = False
            continue

        hunk_match = HUNK_PATTERN.match(line)

        if hunk_match:
            old_line = int(hunk_match.group(1))
            new_line = int(hunk_match.group(2))
            in_hunk = True
            continue

        if not in_hunk or current_path is None:
            continue

        changed = results[current_path]

        # Added line.
        if line.startswith("+"):
            changed.new_lines.add(new_line)
            new_line += 1
            continue

        # Removed line.
        if line.startswith("-"):
            changed.old_lines.add(old_line)
            old_line += 1
            continue

        # "\ No newline at end of file"
        if line.startswith("\\"):
            continue

        # Context line exists in both versions.
        old_line += 1
        new_line += 1

    return results


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
                    raise SymbolAnalysisError(
                        "Unsafe path found in repository archive."
                    ) from exc

            archive.extractall(destination)

    except zipfile.BadZipFile as exc:
        raise SymbolAnalysisError(
            "GitHub returned an invalid ZIP archive."
        ) from exc

    directories = [
        item for item in destination.iterdir() if item.is_dir()
    ]

    if len(directories) != 1:
        raise SymbolAnalysisError(
            "Unexpected repository archive structure."
        )

    return directories[0]


def node_end_line(node: ast.AST) -> int:
    return getattr(node, "end_lineno", getattr(node, "lineno", 0))


def assignment_names(node: ast.AST) -> List[str]:
    names: List[str] = []

    def collect(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for child in target.elts:
                collect(child)

    if isinstance(node, ast.Assign):
        for target in node.targets:
            collect(target)
    elif isinstance(node, ast.AnnAssign):
        collect(node.target)
    elif isinstance(node, ast.AugAssign):
        collect(node.target)

    return names


def import_name(node: ast.AST) -> str:
    if isinstance(node, ast.Import):
        return ", ".join(alias.name for alias in node.names)

    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        names = ", ".join(alias.name for alias in node.names)
        return f"{module}: {names}" if module else names

    return "import"


def collect_class_symbols(
    class_node: ast.ClassDef,
    prefix: str = "",
) -> List[SymbolSpan]:
    symbols: List[SymbolSpan] = []

    class_name = f"{prefix}.{class_node.name}" if prefix else class_node.name

    symbols.append(
        SymbolSpan(
            name=class_name,
            symbol_type="class",
            start_line=class_node.lineno,
            end_line=node_end_line(class_node),
        )
    )

    for child in class_node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                SymbolSpan(
                    name=f"{class_name}.{child.name}",
                    symbol_type="method",
                    start_line=child.lineno,
                    end_line=node_end_line(child),
                )
            )

        elif isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for variable_name in assignment_names(child):
                symbols.append(
                    SymbolSpan(
                        name=f"{class_name}.{variable_name}",
                        symbol_type="class_variable",
                        start_line=child.lineno,
                        end_line=node_end_line(child),
                    )
                )

        elif isinstance(child, ast.ClassDef):
            symbols.extend(
                collect_class_symbols(child, prefix=class_name)
            )

    return symbols


def collect_symbol_spans(source: str) -> List[SymbolSpan]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    symbols: List[SymbolSpan] = []

    for node in tree.body:
        # Top-level function.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                SymbolSpan(
                    name=node.name,
                    symbol_type="function",
                    start_line=node.lineno,
                    end_line=node_end_line(node),
                )
            )

        # Class and its methods.
        elif isinstance(node, ast.ClassDef):
            symbols.extend(collect_class_symbols(node))

        # Module-level variables.
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for variable_name in assignment_names(node):
                symbols.append(
                    SymbolSpan(
                        name=variable_name,
                        symbol_type="module_variable",
                        start_line=node.lineno,
                        end_line=node_end_line(node),
                    )
                )

        # Module-level imports.
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            symbols.append(
                SymbolSpan(
                    name=import_name(node),
                    symbol_type="import",
                    start_line=node.lineno,
                    end_line=node_end_line(node),
                )
            )

    return symbols


def symbols_for_changed_lines(
    changed_lines: Set[int],
    symbols: List[SymbolSpan],
) -> Set[Tuple[str, str]]:
    affected: Set[Tuple[str, str]] = set()

    for line_number in changed_lines:
        containing = [
            symbol
            for symbol in symbols
            if symbol.start_line <= line_number <= symbol.end_line
        ]

        if not containing:
            continue

        # Select the smallest enclosing meaningful construct.
        smallest_size = min(
            (symbol.end_line - symbol.start_line)
            for symbol in containing
        )

        smallest = [
            symbol
            for symbol in containing
            if (symbol.end_line - symbol.start_line) == smallest_size
        ]

        for symbol in smallest:
            affected.add(symbol.key)

    return affected


def read_source(
    repository_root: Path,
    relative_path: str,
) -> Optional[str]:
    repository_root = repository_root.resolve()
    path = (repository_root / relative_path).resolve()

    try:
        path.relative_to(repository_root)
    except ValueError:
        return None

    if not path.is_file():
        return None

    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def analyze_file_symbols(
    file_path: str,
    previous_source: Optional[str],
    current_source: Optional[str],
    changed_lines: ChangedLines,
) -> List[ChangedSymbol]:
    previous_symbols = (
        collect_symbol_spans(previous_source)
        if previous_source is not None
        else []
    )

    current_symbols = (
        collect_symbol_spans(current_source)
        if current_source is not None
        else []
    )

    previous_by_key = {symbol.key: symbol for symbol in previous_symbols}
    current_by_key = {symbol.key: symbol for symbol in current_symbols}

    old_affected = symbols_for_changed_lines(
        changed_lines.old_lines,
        previous_symbols,
    )

    new_affected = symbols_for_changed_lines(
        changed_lines.new_lines,
        current_symbols,
    )

    affected_keys = old_affected | new_affected

    results: List[ChangedSymbol] = []

    for key in sorted(affected_keys):
        previous = previous_by_key.get(key)
        current = current_by_key.get(key)

        if previous is None and current is not None:
            change_type = "added"
            symbol = current
        elif current is None and previous is not None:
            change_type = "removed"
            symbol = previous
        else:
            change_type = "modified"
            symbol = current if current is not None else previous

        if symbol is None:
            continue

        results.append(
            ChangedSymbol(
                file=file_path,
                name=symbol.name,
                symbol_type=symbol.symbol_type,
                change_type=change_type,
            )
        )

    # Changed lines existed, but none could safely be mapped to a known symbol.
    if (changed_lines.old_lines or changed_lines.new_lines) and not results:
        results.append(
            ChangedSymbol(
                file=file_path,
                name=None,
                symbol_type="module_level_change",
                change_type="modified",
            )
        )

    return results


def run_symbol_analysis(
    context: SetupContext,
    comparison: ReleaseComparison,
) -> List[ChangedSymbol]:
    changed_line_map = extract_changed_lines(comparison.diff_text)

    repository = context.repository
    client = GitHubClient(token=context.token)

    try:
        previous_archive = client.download_repository_archive(
            owner=repository.owner,
            repository=repository.name,
            ref=comparison.previous_sha,
        )

        current_archive = client.download_repository_archive(
            owner=repository.owner,
            repository=repository.name,
            ref=comparison.current_sha,
        )

    except (GitHubApiError, GitHubNetworkError) as exc:
        raise SymbolAnalysisError(
            f"Unable to download release snapshots: {exc}"
        ) from exc

    results: List[ChangedSymbol] = []

    with tempfile.TemporaryDirectory(
        prefix="regression-impact-symbols-"
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

        for changed_file in comparison.changed_files:
            # POC symbol analyzer currently supports Python source only.
            if not changed_file.path.endswith(".py"):
                continue

            changed_lines = changed_line_map.get(changed_file.path)

            if changed_lines is None:
                continue

            previous_path = changed_file.previous_path or changed_file.path

            previous_source = read_source(
                previous_root,
                previous_path,
            )

            current_source = read_source(
                current_root,
                changed_file.path,
            )

            results.extend(
                analyze_file_symbols(
                    file_path=changed_file.path,
                    previous_source=previous_source,
                    current_source=current_source,
                    changed_lines=changed_lines,
                )
            )

    return sorted(
        results,
        key=lambda item: (
            item.file,
            item.symbol_type,
            item.name or "",
        ),
    )

