"""Forge provider factory and auto-detection.

Primary entry point for the forge package.  Callers use get_forge() to
obtain a ForgeProvider for a project without caring about the concrete type.

Resolution order in get_forge(project_name):
  1. 'forge' field in projects.yaml for the project
  2. Auto-detect from 'forge_url' / 'github_url' domain
  3. Default: GitHubForge

Phase roadmap:
  Phase 1 (done): GitHub, base class, registry, factory
  Phase 2 (done): GogsForge (self-hosted Gogs instances)
  Phase 3a: GitLabForge
  Phase 3b: GiteaForge (Codeberg / Forgejo)
  Phase 4: forge_url config field + auto-detection from git remotes
"""

import logging
from typing import Optional

from app.forge.base import ForgeProvider
from app.forge.github import GitHubForge
from app.forge.registry import DEFAULT_FORGE, get_forge_class

log = logging.getLogger(__name__)


def get_forge(project_name: Optional[str] = None) -> ForgeProvider:
    """Return a ForgeProvider for the given project.

    Falls back to GitHubForge for any unconfigured or unknown project so
    that all existing callers work without change during the Phase 1→5
    migration period.

    Args:
        project_name: Project name as declared in projects.yaml.
                      Pass None to get the default forge.

    Returns:
        A ForgeProvider instance appropriate for the project.
    """
    forge_type, forge_url = _resolve_forge_config(project_name)

    try:
        cls = get_forge_class(forge_type)
    except ValueError:
        # Unknown forge type — fall back to GitHub to avoid breaking callers.
        cls = GitHubForge

    # If we have a forge url, pass it
    if forge_url:
        return cls(base_url=forge_url)
    return cls()


def detect_forge_from_url(url: str) -> ForgeProvider:
    """Infer a ForgeProvider from a URL domain.

    Used when a user pastes a PR/issue URL for a project that is not in
    projects.yaml.  Falls back to GitHubForge for unknown domains.

    Args:
        url: A forge URL (PR, MR, issue, or repo).

    Returns:
        A ForgeProvider whose domain matches the URL.
    """
    if not url:
        return GitHubForge()

    lower = url.lower()

    from urllib.parse import urlparse
    parsed=urlparse(lower);

    netloc = parsed.netloc();

    # While this still allows for nefarious github.enterprise.whatever,
    # we presume that is intentional subdomain design in that case
    if netloc == "github.com" or "github.enterprise" in netloc:
        return GitHubForge()

    # Phase 2: self-hosted Gogs — detected by KOAN_GOGS_HOST match
    gogs_host = _gogs_host_for_detection()
    if netloc == gogs_host:
        from app.forge.gogs import GogsForge
        return GogsForge()

    # Phase 3a: gitlab.com and self-hosted GitLab
    # if "gitlab.com" in lower or _is_gitlab_url(lower):
    #     return GitLabForge()

    # Phase 3b: Codeberg / Forgejo / Gitea
    # if "codeberg.org" in lower or "gitea.io" in lower:
    #     return GiteaForge()

    # Unknown domain — default to GitHub to avoid breaking callers.
    return GitHubForge()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_forge_config(project_name: Optional[str]) -> tuple:
    """Read forge type and URL from projects.yaml for the given project.

    Returns:
        (forge_type: str, forge_url: str | None)
    """
    if not project_name:
        return DEFAULT_FORGE, None

    try:
        import os
        from app.projects_config import load_projects_config, get_project_config

        koan_root = os.environ.get("KOAN_ROOT", "")
        if not koan_root:
            log.warning("KOAN_ROOT not set — cannot resolve forge for project %r", project_name)
            return DEFAULT_FORGE, None
        config = load_projects_config(koan_root)
        if not config:
            return DEFAULT_FORGE, None

        project_cfg = get_project_config(config, project_name)
        forge_type = project_cfg.get("forge", DEFAULT_FORGE)
        # Support both 'forge_url' (new) and 'github_url' (legacy alias)
        forge_url = project_cfg.get("forge_url") or project_cfg.get("github_url")
        return forge_type, forge_url

    except Exception:
        log.warning("Failed to resolve forge config for project %r, "
                    "falling back to default", project_name, exc_info=True)
        return DEFAULT_FORGE, None


def _known_forge_types() -> set:
    """Return the set of currently recognised forge type strings."""
    from app.forge.registry import FORGE_TYPES
    return set(FORGE_TYPES.keys())


def _gogs_host_for_detection() -> str:
    """Return the lowercase Gogs host for URL detection, or empty string."""
    try:
        from app.gogs_auth import get_gogs_host
        host = get_gogs_host()
        # Strip scheme for comparison since lower() operates on the full URL
        host = host.replace("https://", "").replace("http://", "")
        return host.lower()
    except Exception:
        log.warning("Could not resolve Gogs host for URL detection", exc_info=True)
        return ""
