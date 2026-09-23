from regression_impact.release_selection import (
    ReleaseSelectionError,
    run_release_selection,
)
from regression_impact.setup_flow import run_setup

def main() -> int:
    try:
        # STEP 1
        context = run_setup()

        # STEP 2
        selection = run_release_selection(context)

        # STOP HERE.
        #
        # Step 3 will eventually use:
        #
        # selection.previous
        # selection.current
        #
        # Do not implement release comparison yet.

        return 0

    except ReleaseSelectionError as exc:
        print(f"\nRelease setup failed: {exc}")
        return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled.")
        return 130

if __name__ == "__main__":
    raise SystemExit(main())