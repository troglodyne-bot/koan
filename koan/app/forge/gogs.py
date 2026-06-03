"""Gogs forge implementation.

GogsForge targets self-hosted Gogs instances (https://gogs.io) via the
Gogs REST API v1.  Host and token are read from KOAN_GOGS_HOST and
KOAN_GOGS_TOKEN respectively.

Supported features:
    FEATURE_PR        — create, view, list merged PRs
    FEATURE_ISSUES    — create, list open issues

Not supported (Gogs API limitation or out of scope):
    FEATURE_CI_STATUS          — Gogs has no native CI API
    FEATURE_REACTIONS          — Gogs does not expose reaction endpoints
    FEATURE_NOTIFICATIONS      — handled by polling, not forge API
    FEATURE_PR_REVIEW_COMMENTS — Gogs PR review API is limited
"""

import logging
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from app.forge.base import FEATURE_ISSUES, FEATURE_PR, ForgeProvider

log = logging.getLogger(__name__)

class GogsForge(ForgeProvider):
    """Forge implementation for self-hosted Gogs instances.

    Uses the Gogs REST API v1 directly (no CLI wrapper required at
    runtime, though scripts/gogs provides a gh-compatible CLI for humans).

    Args:
        base_url: Gogs base URL.  Defaults to KOAN_GOGS_HOST env var.
    """

    name = "gogs"

    _SUPPORTED_FEATURES = frozenset({FEATURE_PR, FEATURE_ISSUES})

    def __init__(self, base_url: str = ""):
        from app.gogs_auth import get_gogs_host
        self.base_url = (base_url or get_gogs_host()).rstrip("/")

    # ------------------------------------------------------------------
    # CLI availability (optional scripts/gogs wrapper for human use)
    # ------------------------------------------------------------------

    def cli_name(self) -> str:
        return "gogs"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def auth_env(self) -> Dict[str, str]:
        from app.gogs_auth import get_gogs_host, get_gogs_token
        env = {}
        host = get_gogs_host()
        token = get_gogs_token()
        if host:
            env["KOAN_GOGS_HOST"] = host
        if token:
            env["KOAN_GOGS_TOKEN"] = token
        return env

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------

    def parse_pr_url(self, url: str) -> Tuple[str, str, str]:
        from app.gogs_url_parser import parse_pr_url
        return parse_pr_url(url)

    def parse_issue_url(self, url: str) -> Tuple[str, str, str]:
        from app.gogs_url_parser import parse_issue_url
        return parse_issue_url(url)

    def search_pr_url(self, text: str) -> Tuple[str, str, str]:
        from app.gogs_url_parser import search_pr_url
        return search_pr_url(text)

    def search_issue_url(self, text: str) -> Tuple[str, str, str]:
        from app.gogs_url_parser import search_issue_url
        return search_issue_url(text)

    # ------------------------------------------------------------------
    # PR operations
    # ------------------------------------------------------------------

    def pr_create(
        self,
        title: str,
        body: str,
        draft: bool = True,
        base: Optional[str] = None,
        repo: Optional[str] = None,
        head: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        """Create a pull request on the Gogs instance.

        Note: Gogs does not support draft PRs — the ``draft`` flag is
        accepted for interface compatibility but has no effect.

        Args:
            title: PR title.
            body: PR body (markdown).
            draft: Ignored (Gogs has no draft PR concept).
            base: Target branch name.
            repo: Repository in owner/repo format.
            head: Source branch name (or owner:branch for cross-repo).
            cwd: Unused (kept for interface compatibility).

        Returns:
            URL of the created PR.

        Raises:
            ValueError: If ``repo`` is not provided.
            RuntimeError: If the API call fails.
        """
        self._require_token()
        owner, repo_name = _split_repo(repo)
        payload: Dict = {"title": title, "body": body or ""}
        if base:
            payload["base"] = base
        if head:
            payload["head"] = head

        data = self._api("POST", f"repos/{owner}/{repo_name}/pulls", payload)
        html_url = data.get("html_url") or ""
        if not html_url:
            number = data.get("number")
            if not number:
                raise RuntimeError("Could not determine created PR's URL!")
            html_url = f"{self.base_url}/{owner}/{repo_name}/pulls/{number}"
        return html_url

    def pr_view(
        self,
        repo: str,
        number: int,
        cwd: Optional[str] = None,
    ) -> Dict:
        owner, repo_name = _split_repo(repo)
        data = self._api("GET", f"repos/{owner}/{repo_name}/pulls/{number}")
        return _normalise_pr(data)

    def pr_diff(
        self,
        repo: str,
        number: int,
        cwd: Optional[str] = None,
    ) -> str:
        """Fetch the unified diff for a Gogs PR via the web endpoint.

        Gogs serves diffs at /<owner>/<repo>/pulls/<number>.diff —
        this fetches that page with token authentication.
        """
        owner, repo_name = _split_repo(repo)
        url = f"{self.base_url}/{owner}/{repo_name}/pulls/{number}.diff"
        return self._raw_get(url)

    def list_merged_prs(
        self,
        repo: str,
        cwd: Optional[str] = None,
    ) -> List[str]:
        owner, repo_name = _split_repo(repo)
        items = self._api(
            "GET",
            f"repos/{owner}/{repo_name}/pulls",
            params={"state": "closed", "type": "closed", "limit": "50"},
        )
        if not isinstance(items, list):
            return []
        return [
            pr.get("head", {}).get("ref", "")
            for pr in items
            if isinstance(pr, dict) and pr.get("merged")
        ]

    # ------------------------------------------------------------------
    # Issue operations
    # ------------------------------------------------------------------

    def issue_create(
        self,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
        cwd: Optional[str] = None,
    ) -> str:
        # Translate git remote in cwd to 'repo' string to pass to issue_create_in_repo
        # XXX A bit wasteful to split/unsplit but beats refactoring
        self._require_token()
        result = _owner_repo_from_git_remote(cwd)
        if not result:
            raise RuntimeError(f"{cwd} is not a git repository, or has no remotes configured, so we cannot figure out how to file an issue thereupon")
        # XXX Irritating bit of reassignment due to above call returning None rather than empty array, principle of least astonishment violation
        owner, repo_name = result
        repo = f"{owner}/{repo_name}"
        return self.issue_create_in_repo(repo, title, body, labels)

    def issue_create_in_repo(
        self,
        repo: str,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
    ) -> str:
        """Create an issue specifying the target repo explicitly.

        Args:
            repo: Repository in owner/repo format.
            title: Issue title.
            body: Issue body (markdown).
            labels: Optional list of label names.

        Returns:
            URL of the created issue.
        """
        self._require_token()
        owner, repo_name = _split_repo(repo)
        payload: Dict = {"title": title, "body": body or ""}
        # Gogs label API uses IDs, not names — skip label resolution for now.
        data = self._api("POST", f"repos/{owner}/{repo_name}/issues", payload)
        html_url = data.get("html_url") or ""
        if not html_url:
            number = data.get("number")
            html_url = f"{self.base_url}/{owner}/{repo_name}/issues/{number}"
        return html_url

    # ------------------------------------------------------------------
    # API access
    # ------------------------------------------------------------------

    def run_api(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[Dict] = None,
        cwd: Optional[str] = None,
    ) -> str:
        result = self._api(method, endpoint, data)
        return json.dumps(result)

    # ------------------------------------------------------------------
    # Repository introspection
    # ------------------------------------------------------------------

    def get_web_url(
        self,
        repo: str,
        url_type: str,
        number: int,
    ) -> str:
        owner, repo_name = _split_repo(repo)
        path_map = {
            "pull": "pulls",
            "pr": "pulls",
            "pulls": "pulls",
            "issues": "issues",
            "issue": "issues",
        }
        path = path_map.get(url_type, url_type)
        return f"{self.base_url}/{owner}/{repo_name}/{path}/{number}"

    def detect_fork(self, project_path: str) -> Optional[str]:
        """Detect if a Gogs repo is a fork and return the parent owner/repo.

        Uses the git remote URL to derive owner/repo, then queries the
        Gogs API for the parent field.  Returns None when not a fork or
        on any error.
        """
        owner_repo = _owner_repo_from_git_remote(project_path)
        if not owner_repo:
            return None
        owner, repo_name = owner_repo
        try:
            data = self._api("GET", f"repos/{owner}/{repo_name}")
            parent = data.get("parent")
            if parent and isinstance(parent, dict):
                p_owner = parent.get("owner", {}).get("login", "")
                p_name = parent.get("name", "")
                if p_owner and p_name:
                    return f"{p_owner}/{p_name}"
        except RuntimeError:
            log.warning("GOGS fork detection failed for %s: %s", project_path, exc)
            pass
        return None

    # ------------------------------------------------------------------
    # Feature matrix
    # ------------------------------------------------------------------

    def supports(self, feature: str) -> bool:
        return feature in self._SUPPORTED_FEATURES

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_host(self) -> None:
        if not self.base_url:
            raise RuntimeError(
                "Gogs host is not configured. "
                "Set KOAN_GOGS_HOST to your Gogs base URL "
                "(e.g. https://git.example.com)."
            )

    # CUD is one of the few times we know for a fact we will need a token
    # We might also need one for read in the case of private repos though.
    # In those cases we will have to fall back to enjoying a 403 from the API.
    def _require_token(self) -> None:
        from app.gogs_auth import get_gogs_token
        if not get_gogs_token():
            raise RuntimeError(
                "GOGS token is not configured. "
                "Set KOAN_GOGS_TOKEN to a personal access token."
            )

    def _api(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        timeout: int = 30,
    ):
        """Make an authenticated Gogs API v1 request.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE).
            path: API path relative to /api/v1/ (e.g. "repos/owner/repo/pulls").
            data: Optional JSON payload for POST/PATCH.
            params: Optional query-string parameters for GET.
            timeout: Request timeout in seconds.

        Returns:
            Parsed JSON response (dict or list).

        Raises:
            RuntimeError: On HTTP error or if KOAN_GOGS_HOST is not set.
        """
        self._require_host()

        from app.gogs_auth import get_gogs_token

        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        if params:
            url = url + "?" + urllib.parse.urlencode(params)

        token = get_gogs_token()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"token {token}"

        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body, headers=headers, method=method.upper()
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Gogs API {method} {path} failed: HTTP {exc.code}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Gogs API {method} {path} error: {exc}"
            ) from exc

    def _raw_get(self, url: str, timeout: int = 30) -> str:
        """Fetch a raw URL (non-JSON) with token auth."""
        self._require_host()
        from app.gogs_auth import get_gogs_token

        token = get_gogs_token()
        headers = {}
        if token:
            headers["Authorization"] = f"token {token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Gogs fetch {url} failed: HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(f"Gogs fetch {url} error: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _split_repo(repo: Optional[str]) -> Tuple[str, str]:
    """Split an owner/repo string into (owner, repo_name).

    Raises:
        ValueError: If repo is empty or not in owner/repo format.
    """
    if not repo:
        raise ValueError("repo must be specified in owner/repo format")
    parts = repo.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid repo format: {repo!r} (expected owner/repo)")
    return parts[0], parts[1]


def _normalise_pr(data: Dict) -> Dict:
    """Map Gogs PR API fields to GitHub-compatible field names.

    Callers (e.g. ``pr_view``) expect GitHub-style field names such as
    ``headRefName`` and ``baseRefName``.  Gogs stores these under
    ``head.ref`` and ``base.ref``.
    """
    return {
        "number": data.get("number"),
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "state": data.get("state", ""),
        "headRefName": (data.get("head") or {}).get("ref", ""),
        "baseRefName": (data.get("base") or {}).get("ref", ""),
        "url": data.get("html_url", ""),
    }


def _owner_repo_from_git_remote(project_path: str) -> Optional[Tuple[str, str]]:
    """Parse the git origin remote to extract (owner, repo_name)."""
    import re
    import subprocess

    if not project_path:
        return None
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
            cwd=project_path, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    # SSH: git@host:owner/repo.git  or  git@host:owner/repo
    # HTTPS: https://host/owner/repo.git  or  https://host/owner/repo
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if match:
        return match.group(1), match.group(2)
    return None
