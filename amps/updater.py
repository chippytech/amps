"""Helper utilities to self-update Amps from GitHub releases."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from importlib import metadata
from typing import Optional

from amps import __version__

DEFAULT_REPO = "CalmhostAcct/amps"


def normalize_version(tag: Optional[str]) -> Optional[str]:
    """Remove any leading ``v`` prefix from a Git tag to match PEP 440 versions."""

    if not tag:
        return None
    return tag.lstrip("v")


def get_installed_version() -> str:
    """Return the installed Amps version or fall back to the package constant."""

    try:
        return metadata.version("amps-m3u")
    except metadata.PackageNotFoundError:
        return __version__


def fetch_latest_release_tag(repo: str = DEFAULT_REPO, timeout: int = 10) -> Optional[str]:
    """Fetch the latest release tag from GitHub for the given repository."""

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "amps-update",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # type: ignore[arg-type]
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        raise RuntimeError(f"GitHub API error ({exc.code}): {exc.reason}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network dependent
        raise RuntimeError(f"Failed to reach GitHub: {exc.reason}") from exc

    tag = data.get("tag_name") or data.get("name")
    return tag


def is_newer_version(current: str, candidate: str) -> bool:
    """Simple semantic-ish comparison between two dotted version strings."""

    def as_tuple(version: str) -> tuple:
        parts = []
        for chunk in version.split("."):
            try:
                parts.append(int(chunk))
            except ValueError:
                # Non-numeric suffixes (e.g. rc1) are treated as lower priority
                parts.append(0)
        return tuple(parts)

    return as_tuple(candidate) > as_tuple(current)


def build_release_url(tag: str, repo: str = DEFAULT_REPO) -> str:
    """Construct the GitHub zip archive URL for the given release tag."""

    return f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"


def install_from_github(tag: str, repo: str = DEFAULT_REPO) -> subprocess.CompletedProcess:
    """Install the specified release tag via ``pip``."""

    download_url = build_release_url(tag, repo)
    command = [sys.executable, "-m", "pip", "install", "--upgrade", download_url]
    return subprocess.run(command, text=True, capture_output=True, check=False)

