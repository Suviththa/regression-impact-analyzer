from dataclasses import dataclass
from typing import List
from regression_impact.github_client import (
    GitHubApiError,
    GitHubClient,
    GitHubNetworkError,
    RepositoryTag,
)
from regression_impact.setup_flow import SetupContext

@dataclass(frozen=True)
class ReleaseSelection:
    previous: RepositoryTag
    current: RepositoryTag

class ReleaseSelectionError(Exception):
    pass

def display_tags(tags: List[RepositoryTag]) -> None:
    print("\nAvailable release tags")
    print("-" * 60)

    for index, tag in enumerate(tags, start=1):
        short_sha = tag.commit_sha[:8]
        print(f"{index:>3}. {tag.name:<20} {short_sha}")

    print("-" * 60)

def select_tag(prompt: str, tags: List[RepositoryTag]) -> RepositoryTag:
    tag_lookup = {tag.name: tag for tag in tags}

    while True:
        value = input(prompt).strip()

        if not value:
            print("Selection cannot be empty.")
            continue

        # Allow selection by displayed number.
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(tags):
                return tags[index - 1]
            print(f"Please enter a number between 1 and {len(tags)}.")
            continue

        # Also allow entering the actual tag name.
        tag = tag_lookup.get(value)
        if tag is not None:
            return tag

        print(f"Tag '{value}' was not found. Choose one of the displayed tags.")


def fetch_tags(context: SetupContext) -> List[RepositoryTag]:
    repo = context.repository
    client = GitHubClient(token=context.token)

    try:
        tags = client.list_repository_tags(
            owner=repo.owner,
            repository=repo.name,
        )
    except GitHubApiError as exc:
        raise ReleaseSelectionError(
            f"GitHub API error ({exc.status_code}): {exc.message}"
        ) from exc
    except GitHubNetworkError as exc:
        raise ReleaseSelectionError(str(exc)) from exc

    if not tags:
        raise ReleaseSelectionError(
            "No Git tags were found in this repository."
        )

    return tags

def print_selection(selection: ReleaseSelection) -> None:
    print("\n" + "=" * 60)
    print("Release selection confirmed")
    print("=" * 60)
    print(f"Previous release : {selection.previous.name}")
    print(f"Previous SHA     : {selection.previous.commit_sha}")
    print()
    print(f"Current release  : {selection.current.name}")
    print(f"Current SHA      : {selection.current.commit_sha}")
    print("=" * 60)

def run_release_selection(context: SetupContext) -> ReleaseSelection:
    print("\nRelease Selection")

    tags = fetch_tags(context)
    print(f"\nFound {len(tags)} tag(s) in {context.repository.full_name}.")
    display_tags(tags)

    while True:
        previous = select_tag(
            "\nPrevious release (number or tag): ",
            tags,
        )
        current = select_tag(
            "Current release (number or tag): ",
            tags,
        )

        if previous.name == current.name:
            print("\nPrevious and current releases must be different.")
            continue

        selection = ReleaseSelection(
            previous=previous,
            current=current,
        )
        print_selection(selection)
        return selection