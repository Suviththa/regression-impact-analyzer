from regression_impact.dependency_analysis import (
    DependencyAnalysisError,
    print_dependency_summary,
    run_dependency_analysis,
)
from regression_impact.release_diff import (
    ReleaseDiffError,
    print_comparison_summary,
    print_diff_preview,
    run_release_comparison,
)
from regression_impact.release_selection import (
    ReleaseSelectionError,
    run_release_selection,
)
from regression_impact.setup_flow import run_setup
from regression_impact.source_context import (
    SourceContextError,
    print_source_context_summary,
    run_source_context_collection,
)


def main() -> int:
    try:
        # STEP 1
        context = run_setup()

        # STEP 2
        selection = run_release_selection(context)

        # STEP 3
        comparison = run_release_comparison(context, selection)
        print_comparison_summary(comparison)
        print_diff_preview(comparison)

        # STEP 4
        dependency_result = run_dependency_analysis(context, comparison)
        print_dependency_summary(dependency_result)

        # STEP 5
        source_context = run_source_context_collection(
            context,
            comparison,
            dependency_result,
        )
        print_source_context_summary(source_context)

        # STOP HERE.
        #
        # We now have:
        #
        # comparison.diff_text
        # source_context.files
        #
        # Each source_context file contains:
        # - path
        # - why it was selected
        # - source version
        # - actual source code
        #
        # Do not call an LLM yet.
        return 0

    except ReleaseSelectionError as exc:
        print(f"\nRelease setup failed: {exc}")
        return 1

    except ReleaseDiffError as exc:
        print(f"\nRelease comparison failed: {exc}")
        return 1

    except DependencyAnalysisError as exc:
        print(f"\nDependency analysis failed: {exc}")
        return 1

    except SourceContextError as exc:
        print(f"\nSource context collection failed: {exc}")
        return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())