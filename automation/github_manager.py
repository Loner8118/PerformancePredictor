import os
import re
import shutil
import stat
import subprocess
from typing import Any, Dict, List, Optional


class GitHubManager:
    """
    Handles GitHub repository cloning and cleanup for the performance
    analysis pipeline.

    Clones are done via a direct `git` subprocess call rather than
    GitPython's Repo.clone_from(). This was a deliberate choice after
    testing: GitPython's kill_after_timeout kwarg is an *inactivity*
    timeout (it only kills a clone if git stops producing output for N
    seconds), not a total-duration cap - a slow-but-steadily-progressing
    clone of a huge repo sailed past a 1-second kill_after_timeout and
    took 34+ seconds anyway in testing. subprocess.run(timeout=N) gives
    an actual hard wall-clock deadline, which is what a pipeline that
    clones arbitrary user-supplied repos needs.
    """

    #: Max wall-clock time allowed for a single `git clone`, in seconds.
    DEFAULT_CLONE_TIMEOUT_SECONDS = 120

    #: Repo/owner names are restricted to this charset for filesystem safety,
    #: in addition to the path-traversal defense in _safe_destination().
    _SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

    _URL_PATTERN = re.compile(
        r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/?#]+))?/?$"
    )

    def __init__(self, workspace: str = "workspace", clone_timeout_seconds: int = DEFAULT_CLONE_TIMEOUT_SECONDS):
        base_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.workspace = os.path.abspath(os.path.join(base_directory, workspace))
        self.clone_timeout_seconds = clone_timeout_seconds
        os.makedirs(self.workspace, exist_ok=True)

    # --- Validation ---

    @classmethod
    def _validate_github_url(cls, github_url: Any) -> Dict[str, Optional[str]]:
        """
        Validate and normalize a GitHub repository URL.

        Accepts:
            https://github.com/owner/repository
            https://github.com/owner/repository.git
            https://github.com/owner/repository/tree/<branch>

        A branch parsed from the URL is only used if the caller doesn't
        pass an explicit branch. Branch names containing '/' (e.g.
        "feature/x") aren't resolvable from the URL alone - pass those
        via the explicit `branch` argument to clone_repository() instead.
        """
        if not isinstance(github_url, str):
            raise ValueError("GitHub URL must be a string.")

        github_url = github_url.strip()
        if not github_url:
            raise ValueError("GitHub URL is required.")

        match = cls._URL_PATTERN.match(github_url)
        if not match:
            raise ValueError(
                "Invalid GitHub repository URL. Use: https://github.com/owner/repository "
                "(optionally with /tree/<branch>)."
            )

        owner, repository, url_branch = match.group(1), match.group(2), match.group(3)

        if not owner or not repository:
            raise ValueError("Invalid GitHub repository URL.")

        for label, value in (("owner", owner), ("repository", repository)):
            if not cls._SAFE_NAME_PATTERN.match(value):
                raise ValueError(
                    f"GitHub {label} name contains unsupported characters: {value!r}. "
                    f"Only letters, digits, '.', '_', and '-' are allowed."
                )

        if url_branch is not None and not cls._SAFE_NAME_PATTERN.match(url_branch):
            raise ValueError(f"Branch name contains unsupported characters: {url_branch!r}.")

        normalized_url = f"https://github.com/{owner}/{repository}.git"
        return {"url": normalized_url, "project_name": repository, "branch": url_branch}

    @classmethod
    def _validate_project_name(cls, project_name: Any) -> str:
        """Prevent path traversal / unsafe characters when operating on repositories."""
        if not isinstance(project_name, str):
            raise ValueError("Project name must be a string.")

        project_name = project_name.strip()
        if not project_name:
            raise ValueError("Project name is required.")

        if project_name in {".", ".."} or "/" in project_name or "\\" in project_name or os.path.sep in project_name:
            raise ValueError("Invalid project name.")

        if not cls._SAFE_NAME_PATTERN.match(project_name):
            raise ValueError(
                f"Project name contains unsupported characters: {project_name!r}. "
                f"Only letters, digits, '.', '_', and '-' are allowed."
            )

        return project_name

    def _safe_destination(self, project_name: str) -> str:
        """
        Resolve project_name to an absolute path guaranteed to live inside
        the workspace. This is a second, independent layer of defense on
        top of the character whitelist above - abspath() normalizes away
        any '..' traversal attempts, and the startswith() check confirms
        the result never escaped the workspace root.
        """
        destination = os.path.abspath(os.path.join(self.workspace, project_name))
        workspace_root = os.path.abspath(self.workspace)

        if not destination.startswith(workspace_root + os.sep):
            raise ValueError("Invalid repository destination.")

        return destination

    @staticmethod
    def _is_valid_repo(path: str) -> bool:
        return os.path.isdir(path) and os.path.isdir(os.path.join(path, ".git"))

    # --- Clone ---

    def clone_repository(self, github_url: str, branch: Optional[str] = None, shallow: bool = True) -> Dict[str, Any]:
        """
        Clone a GitHub repository into the analysis workspace.

        Args:
            github_url: https://github.com/owner/repository (optionally
                with /tree/<branch>, or a .git suffix).
            branch: explicit branch/tag to clone. Overrides any branch
                parsed from the URL. Ignored if None and the URL had no
                /tree/<branch> segment - clones the repo's default branch.
            shallow: clone with --depth 1 (default). There's no use for
                full git history in this pipeline, so shallow cloning is
                the default - it's dramatically faster and avoids
                unbounded disk usage on large repos. Set False only if a
                specific reason needs full history.

        Returns a dict with exists/path/project_name/url/branch.
        """
        parsed = self._validate_github_url(github_url)
        normalized_url = parsed["url"]
        project_name = parsed["project_name"]
        resolved_branch = branch.strip() if isinstance(branch, str) and branch.strip() else parsed["branch"]

        if resolved_branch is not None and not self._SAFE_NAME_PATTERN.match(resolved_branch):
            raise ValueError(f"Branch name contains unsupported characters: {resolved_branch!r}.")

        destination = self._safe_destination(project_name)

        if os.path.exists(destination):
            if self._is_valid_repo(destination):
                return {
                    "exists": True,
                    "path": destination,
                    "project_name": project_name,
                    "url": normalized_url,
                    "branch": resolved_branch,
                }
            raise RuntimeError(f"Destination already exists but is not a valid Git repository: {destination}")

        cmd = ["git", "clone"]
        if shallow:
            cmd += ["--depth", "1"]
        if resolved_branch:
            cmd += ["--branch", resolved_branch]
        cmd += [normalized_url, destination]

        try:
            print(f"Cloning repository: {normalized_url}" + (f" (branch: {resolved_branch})" if resolved_branch else ""))
            subprocess.run(
                cmd,
                timeout=self.clone_timeout_seconds,
                capture_output=True,
                text=True,
                check=True,
            )
            print("Repository cloned successfully.")

            return {
                "exists": False,
                "path": destination,
                "project_name": project_name,
                "url": normalized_url,
                "branch": resolved_branch,
            }

        except subprocess.TimeoutExpired:
            self._cleanup_partial_clone(destination)
            raise RuntimeError(
                f"Repository clone timed out after {self.clone_timeout_seconds}s. "
                f"The repository may be too large, the branch may not exist, or the connection is too slow."
            )

        except subprocess.CalledProcessError as e:
            self._cleanup_partial_clone(destination)
            raise RuntimeError(f"Failed to clone GitHub repository: {self._clean_git_error(e.stderr)}")

        except FileNotFoundError:
            raise RuntimeError("git is not installed or not available on PATH.")

        except Exception as e:
            self._cleanup_partial_clone(destination)
            raise RuntimeError(f"Failed to clone repository: {e}")

    def _cleanup_partial_clone(self, destination: str) -> None:
        if os.path.exists(destination):
            self._remove_directory(destination)

    @staticmethod
    def _clean_git_error(stderr: Optional[str]) -> str:
        """
        git's stderr is often multi-line and includes noise (hints, retry
        suggestions). Keep just the most relevant line(s) for a cleaner
        error message back to the API caller.
        """
        if not stderr:
            return "unknown git error"

        lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
        if not lines:
            return "unknown git error"

        meaningful = [line for line in lines if not line.lower().startswith("hint:")]
        return meaningful[-1] if meaningful else lines[-1]

    # --- Delete ---

    def delete_repository(self, project_name: str) -> bool:
        """Delete a cloned repository from the workspace."""
        project_name = self._validate_project_name(project_name)
        destination = self._safe_destination(project_name)

        if not os.path.exists(destination):
            raise ValueError("Repository does not exist.")

        if not self._is_valid_repo(destination):
            raise ValueError(
                "This directory was not created by GitHubManager (no .git folder found); refusing to delete it."
            )

        try:
            self._remove_directory(destination)
            print("Repository deleted successfully.")
            return True

        except Exception as e:
            raise RuntimeError(f"Failed to delete repository: {e}")

    # --- Listing (for the frontend / pipeline to see what's already cloned) ---

    def list_repositories(self) -> List[Dict[str, Any]]:
        """List valid repositories currently sitting in the workspace."""
        repositories = []

        for entry in sorted(os.listdir(self.workspace)):
            path = os.path.join(self.workspace, entry)
            if self._is_valid_repo(path):
                repositories.append({
                    "project_name": entry,
                    "path": path,
                    "size_bytes": self._directory_size(path),
                })

        return repositories

    @staticmethod
    def _directory_size(path: str) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                try:
                    total += os.path.getsize(file_path)
                except OSError:
                    pass  # broken symlink or file removed mid-walk - skip it
        return total

    # --- Filesystem helpers ---

    @staticmethod
    def _remove_directory(path: str) -> None:
        """Remove a directory even when it contains read-only files (common on Windows, and
        common in .git/objects which Git marks read-only)."""

        def remove_readonly(func, file_path, exc_info):
            try:
                os.chmod(file_path, stat.S_IWRITE)
                func(file_path)
            except Exception:
                pass

        shutil.rmtree(path, onerror=remove_readonly)