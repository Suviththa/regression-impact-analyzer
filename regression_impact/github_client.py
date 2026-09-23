import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
USER_AGENT = "regression-impact-cli/0.1"

class GitHubApiError(Exception):
  def __init__(self, status_code: int, message: str,):
    super().__init__(message)
    self.status_code = status_code
    self.message = message

class GitHubNetworkError(Exception):
  pass

@dataclass(frozen=True)
class RepositoryInfo:
  owner: str
  name: str
  full_name: str
  private: bool
  default_branch: str
  html_url: str

@dataclass(frozen=True)
class RepositoryTag:
  name: str
  commit_sha: str
  zipball_url: str
  tarball_url: str

class GitHubClient:
  def __init__(self, token: Optional[str] = None):
    self._token = token

  def _build_headers(self) -> Dict[str, str]:
    headers = {
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": GITHUB_API_VERSION,
      "User-Agent": USER_AGENT,
    }

    if self._token:
      headers["Authorization"] = f"Bearer {self._token}"

    return headers

  def _get_json(self, url: str) -> Any:
    request = Request(
      url=url,
      headers=self._build_headers(),
      method="GET",
    )

    try:
      with urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")

        if not body:
          return {}

        return json.loads(body)

    except HTTPError as exc:
      message = "GitHub API request failed."

      try:
        body = exc.read().decode("utf-8")
        data = json.loads(body)

        if isinstance(data, dict):
          message = data.get("message", message)

      except (
        UnicodeDecodeError,
        json.JSONDecodeError,
      ):
        pass

      raise GitHubApiError(
        status_code=exc.code,
        message=message,
      ) from exc

    except URLError as exc:
      reason = getattr(
        exc,
        "reason",
        "Unknown network error",
      )

      raise GitHubNetworkError(
        f"Unable to connect to GitHub: {reason}"
      ) from exc

    except TimeoutError as exc:
      raise GitHubNetworkError(
        "GitHub API request timed out."
      ) from exc

  def get_repository(
    self,
    owner: str,
    repository: str,
  ) -> RepositoryInfo:

    safe_owner = quote(owner, safe="")
    safe_repo = quote(repository, safe="")

    url = (
      f"{GITHUB_API_BASE}/repos/"
      f"{safe_owner}/{safe_repo}"
    )

    data = self._get_json(url)

    try:
      return RepositoryInfo(
        owner=data["owner"]["login"],
        name=data["name"],
        full_name=data["full_name"],
        private=bool(data["private"]),
        default_branch=data["default_branch"],
        html_url=data["html_url"],
      )

    except (KeyError, TypeError) as exc:
      raise GitHubApiError(
        500,
        "GitHub returned an unexpected repository response.",
      ) from exc

  def list_repository_tags(
    self,
    owner: str,
    repository: str,
  ) -> List[RepositoryTag]:
    safe_owner = quote(owner, safe="")
    safe_repo = quote(repository, safe="")
    tags: List[RepositoryTag] = []
    per_page = 100
    page = 1

    while True:
      query = urlencode({
        "per_page": per_page,
        "page": page,
      })
      url = (
        f"{GITHUB_API_BASE}/repos/"
        f"{safe_owner}/{safe_repo}/tags"
        f"?{query}"
      )
      data = self._get_json(url)

      if not isinstance(data, list):
        raise GitHubApiError(
          500,
          "GitHub returned an unexpected tags response.",
        )

      for item in data:
        try:
          tags.append(
            RepositoryTag(
              name=item["name"],
              commit_sha=item["commit"]["sha"],
              zipball_url=item["zipball_url"],
              tarball_url=item["tarball_url"],
            )
          )
        except (KeyError, TypeError) as exc:
          raise GitHubApiError(
            500,
            "GitHub returned malformed tag data.",
          ) from exc

      # No more pages.
      if len(data) < per_page:
        break

      page += 1

    return tags