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
from regression_impact.setup_flow import (
    run_setup,
)


def main() -> int:

    try:

        # STEP 1
        context = run_setup()

        # STEP 2
        selection = run_release_selection(
            context
        )

        # STEP 3
        comparison = run_release_comparison(
            context,
            selection,
        )

        print_comparison_summary(
            comparison
        )

        print_diff_preview(
            comparison
        )

        # STEP 4
        dependency_result = (
            run_dependency_analysis(
                context,
                comparison,
            )
        )

        print_dependency_summary(
            dependency_result
        )

        # STOP HERE.
        #
        # Step 5 will use:
        #
        # comparison.diff_text
        # comparison.changed_files
        # dependency_result.impacts
        #
        # to collect relevant source context.

        return 0

    except ReleaseSelectionError as exc:
        print(
            f"\nRelease setup failed: {exc}"
        )
        return 1

    except ReleaseDiffError as exc:
        print(
            f"\nRelease comparison failed: {exc}"
        )
        return 1

    except DependencyAnalysisError as exc:
        print(
            f"\nDependency analysis failed: {exc}"
        )
        return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())