import ast
import io
import tempfile
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set
from regression_impact.github_client import (
    GitHubApiError,
    GitHubClient,
    GitHubNetworkError,
)
from regression_impact.release_diff import ReleaseComparison
from regression_impact.setup_flow import SetupContext

class DependencyAnalysisError(Exception):
    pass

@dataclass(frozen=True)
class DependencyEdge:
    source: str
    target: str

@dataclass(frozen=True)
class ImpactedModule:
    module: str
    file_path: str
    depth: int

@dataclass(frozen=True)
class ChangedModuleImpact:
    changed_file: str
    changed_module: str
    impacted_modules: List[ImpactedModule]

@dataclass(frozen=True)
class DependencyAnalysisResult:
    relationships: List[DependencyEdge]
    impacts: List[ChangedModuleImpact]

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
                    raise DependencyAnalysisError(
                        "Unsafe path found in repository archive.") from exc

            archive.extractall(destination)

    except zipfile.BadZipFile as exc:
        raise DependencyAnalysisError(
            "GitHub returned an invalid ZIP archive.") from exc

    directories = [item for item in destination.iterdir() if item.is_dir()]

    if len(directories) != 1:
        raise DependencyAnalysisError(
            "Unexpected repository archive structure.")

    return directories[0]

def discover_source_roots(repository_root: Path, ) -> List[Path]:
    src = repository_root / "src"

    if src.is_dir():
        return [src]

    return [repository_root]

def file_to_module(
    file_path: Path,
    source_root: Path,
) -> str:
    relative = file_path.relative_to(source_root)

    parts = list(relative.parts)

    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = Path(parts[-1]).stem

    return ".".join(parts)


def resolve_relative_module(
    current_module: str,
    level: int,
    module: Optional[str],
) -> str:
    package_parts = current_module.split(".")[:-1]

    levels_up = max(level - 1, 0)

    if levels_up:
        package_parts = package_parts[:-levels_up]

    if module:
        package_parts.extend(module.split("."))

    return ".".join(package_parts)


def extract_dependencies(
    source_code: str,
    current_module: str,
    known_modules: Set[str],
) -> Set[str]:
    dependencies: Set[str] = set()

    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return dependencies

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = alias.name
                if imported in known_modules:
                    dependencies.add(imported)

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_module = resolve_relative_module(
                    current_module,
                    node.level,
                    node.module,
                )
            else:
                base_module = node.module or ""

            if base_module in known_modules:
                dependencies.add(base_module)

            for alias in node.names:
                if alias.name == "*":
                    continue

                if base_module:
                    candidate = f"{base_module}.{alias.name}"
                else:
                    candidate = alias.name

                if candidate in known_modules:
                    dependencies.add(candidate)

    return dependencies


def build_dependency_graph(
    repository_root: Path, ) -> tuple[Dict[str, Set[str]], Dict[str, Path]]:
    source_roots = discover_source_roots(repository_root)

    module_to_file: Dict[str, Path] = {}

    for source_root in source_roots:
        for file_path in source_root.rglob("*.py"):
            module = file_to_module(file_path, source_root)
            if module:
                module_to_file[module] = file_path

    known_modules = set(module_to_file.keys())

    graph: Dict[str, Set[str]] = defaultdict(set)

    for module, file_path in module_to_file.items():
        try:
            source = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        dependencies = extract_dependencies(
            source,
            module,
            known_modules,
        )

        graph[module].update(dependencies)

    return graph, module_to_file


def build_reverse_graph(graph: Dict[str, Set[str]], ) -> Dict[str, Set[str]]:
    reverse: Dict[str, Set[str]] = defaultdict(set)

    for module, dependencies in graph.items():
        for dependency in dependencies:
            reverse[dependency].add(module)

    return reverse


def changed_path_to_module(
    changed_path: str,
    repository_root: Path,
    module_to_file: Dict[str, Path],
) -> Optional[str]:
    changed_absolute = (repository_root / changed_path).resolve()

    for module, file_path in module_to_file.items():
        if file_path.resolve() == changed_absolute:
            return module

    return None


def find_impacted_modules(
    changed_module: str,
    reverse_graph: Dict[str, Set[str]],
    module_to_file: Dict[str, Path],
    repository_root: Path,
    max_depth: int = 2,
) -> List[ImpactedModule]:
    impacted: List[ImpactedModule] = []
    visited = {changed_module}
    queue = deque([(changed_module, 0)])

    while queue:
        module, depth = queue.popleft()

        if depth >= max_depth:
            continue

        dependents = sorted(reverse_graph.get(module, set()))

        for dependent in dependents:
            if dependent in visited:
                continue

            visited.add(dependent)

            file_path = module_to_file[dependent]
            relative = file_path.relative_to(repository_root).as_posix()

            impacted.append(
                ImpactedModule(
                    module=dependent,
                    file_path=relative,
                    depth=depth + 1,
                ))

            queue.append((dependent, depth + 1))

    return impacted


def analyze_snapshot(
    repository_root: Path,
    comparison: ReleaseComparison,
) -> DependencyAnalysisResult:
    graph, module_to_file = build_dependency_graph(repository_root)
    reverse_graph = build_reverse_graph(graph)

    relationships: List[DependencyEdge] = []

    for source, dependencies in graph.items():
        for target in sorted(dependencies):
            relationships.append(DependencyEdge(
                source=source,
                target=target,
            ))

    impacts: List[ChangedModuleImpact] = []

    for changed in comparison.changed_files:
        if not changed.path.endswith(".py"):
            continue

        if changed.status == "removed":
            continue

        changed_module = changed_path_to_module(
            changed.path,
            repository_root,
            module_to_file,
        )

        if changed_module is None:
            continue

        impacted = find_impacted_modules(
            changed_module,
            reverse_graph,
            module_to_file,
            repository_root,
            max_depth=2,
        )

        impacts.append(
            ChangedModuleImpact(
                changed_file=changed.path,
                changed_module=changed_module,
                impacted_modules=impacted,
            ))

    return DependencyAnalysisResult(
        relationships=relationships,
        impacts=impacts,
    )


def run_dependency_analysis(
    context: SetupContext,
    comparison: ReleaseComparison,
) -> DependencyAnalysisResult:
    repository = context.repository
    client = GitHubClient(token=context.token)

    print("\nDependency Impact Analysis")
    print(f"Analyzing current release {comparison.current_tag}...")

    try:
        archive = client.download_repository_archive(
            owner=repository.owner,
            repository=repository.name,
            ref=comparison.current_sha,
        )
    except (GitHubApiError, GitHubNetworkError) as exc:
        raise DependencyAnalysisError(
            f"Unable to download current release snapshot: {exc}") from exc

    with tempfile.TemporaryDirectory(
            prefix="regression-impact-analysis-") as temp_directory:
        destination = Path(temp_directory)
        repository_root = safe_extract_zip(
            archive,
            destination,
        )

        result = analyze_snapshot(
            repository_root,
            comparison,
        )

    return result


def print_dependency_summary(result: DependencyAnalysisResult, ) -> None:
    print("\n" + "=" * 65)
    print("Dependency impact analysis complete")
    print("=" * 65)

    if not result.impacts:
        print("No analyzable changed Python modules were found.")
        print("=" * 65)
        return

    for impact in result.impacts:
        print(f"\nChanged file   : {impact.changed_file}")
        print(f"Changed module : {impact.changed_module}")

        if not impact.impacted_modules:
            print("Dependents     : None found")
            continue

        print("Dependent modules:")
        for dependent in impact.impacted_modules:
            print(f"  Depth {dependent.depth}: {dependent.file_path}")

    print("=" * 65)