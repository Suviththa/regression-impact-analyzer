import getpass
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote, urlparse

from regression_impact.github_client import (
  GitHubApiError,
  GitHubClient,
  GitHubNetworkError,
  RepositoryInfo,
)

OWNER_PATTERN = re.compile(
  r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$"
)

REPOSITORY_PATTERN = re.compile(
  r"^[A-Za-z0-9._-]+$"
)

class InvalidRepositoryInput(ValueError):
  pass

@dataclass
class SetupContext:
  repository: RepositoryInfo

  # repr=False is important:
  # accidentally printing SetupContext will not expose the PAT.
  token: Optional[str] = field(
    default=None,
    repr=False,
  )

def parse_repository_input(
  value: str,
) -> tuple[str, str]:

  value = value.strip()

  if not value:
    raise InvalidRepositoryInput(
      "Repository cannot be empty."
    )

  owner: str
  repository: str

  # Full GitHub URL
  if "://" in value:

    parsed = urlparse(value)

    if parsed.scheme not in ("http", "https"):
      raise InvalidRepositoryInput(
        "Only HTTP/HTTPS GitHub URLs are supported."
      )

    hostname = (parsed.hostname.lower() if parsed.hostname else "")

    if hostname not in ("github.com", "www.github.com"):
      raise InvalidRepositoryInput("The URL must point to github.com.")

    path_parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]

    if len(path_parts) != 2:
      raise InvalidRepositoryInput("Expected a repository URL such as https://github.com/owner/repo")

    owner, repository = path_parts
  else:
    path_parts = [part for part in value.strip("/").split("/") if part]

    if len(path_parts) != 2:
      raise InvalidRepositoryInput("Expected owner/repo or a full GitHub URL.")

    owner, repository = path_parts

  # Accept URLs such as repo.git
  if repository.endswith(".git"):
    repository = repository[:-4]

  if not owner:
    raise InvalidRepositoryInput("Repository owner is missing.")

  if not repository:
    raise InvalidRepositoryInput("Repository name is missing.")

  if not OWNER_PATTERN.fullmatch(owner):
    raise InvalidRepositoryInput(f"Invalid GitHub owner name: {owner}")

  if owner.endswith("-"):
    raise InvalidRepositoryInput("GitHub owner names cannot end with '-'.")

  if not REPOSITORY_PATTERN.fullmatch(repository):
    raise InvalidRepositoryInput(f"Invalid repository name: {repository}")

  return owner, repository

def prompt_repository() -> tuple[str, str]:
  while True:
    value = input("\nGitHub repository (owner/repo or full URL): ")

    try:
      owner, repository = (parse_repository_input(value))
      print(f"Target: {owner}/{repository}")
      return owner, repository
    except InvalidRepositoryInput as exc:
      print(f"{exc}")

def validate_with_token(
  owner: str,
  repository: str,
) -> Optional[SetupContext]:

  print("\nThe repository is not publicly accessible.")
  print("If it is private, provide a GitHub PAT with access to this repository.")
  print("Press Enter without a token to choose a different repository.")

  while True:
    token = getpass.getpass("\nGitHub PAT: ").strip()

    if not token:
      return None

    client = GitHubClient(token=token)

    try:
      info = client.get_repository(owner, repository,)
      return SetupContext(repository=info, token=token)

    except GitHubApiError as exc:
      if exc.status_code == 401:
        print("GitHub rejected the token. Check that the PAT is valid.")

      elif exc.status_code == 403:
        print("GitHub denied access. Check repository permissions, organization policies, SSO, or API rate limits")

      elif exc.status_code == 404:
        print("Repository not found, or this PAT does not have access to it.")

      else:
        print(f"GitHub API error "
          f"({exc.status_code}): "
          f"{exc.message}")

    except GitHubNetworkError as exc:
      print(f"{exc}")

def verify_repository(owner: str, repository: str) -> Optional[SetupContext]:
  # First attempt without authentication.
  client = GitHubClient()

  try:
    info = client.get_repository(owner, repository)
    return SetupContext(repository=info, token=None)
  except GitHubApiError as exc:
    if exc.status_code in (403, 404):
      return validate_with_token(owner, repository)

    print(
      f"GitHub API error "
      f"({exc.status_code}): "
      f"{exc.message}"
    )

    return None
  except GitHubNetworkError as exc:
    print(f"✗ {exc}")
    return None

def print_success(
  context: SetupContext,
) -> None:
  repo = context.repository

  visibility = ("Private" if repo.private else "Public")

  authentication = ("PAT authenticated" if context.token else "Public access")

  print("\n" + "=" * 50)
  print("Repository access verified")
  print("=" * 50)
  print(f"Owner          : {repo.owner}")
  print(f"Repository     : {repo.name}")
  print(f"Full name      : {repo.full_name}")
  print(f"Visibility     : {visibility}")
  print(f"Default branch : {repo.default_branch}")
  print(f"Authentication : {authentication}")
  print("=" * 50)

def run_setup() -> SetupContext:
  print("\nRegression Impact Analyzer")
  print("Repository Setup")

  while True:
    owner, repository = prompt_repository()

    context = verify_repository(owner, repository)

    if context is None:
      print("\nRepository verification was not completed. Please try again.")
      continue

    print_success(context)
    return context