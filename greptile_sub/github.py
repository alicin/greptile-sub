import json
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

PR_FIELDS = "number,title,body,author,url,baseRefName,headRefOid,isDraft,additions,deletions,changedFiles"
# Everything greptile-sub posts carries this, since it posts as the user's own account.
MARKER = "<!-- greptile-sub -->"


class GitHubError(RuntimeError):
    pass


def gh(*args: str, input: Optional[str] = None) -> str:
    proc = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(f"gh {' '.join(args[:3])}: {proc.stderr.strip()[:1000]}")
    return proc.stdout


def pr_info(repo: str, number: int) -> dict[str, Any]:
    return json.loads(gh("pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS))


def pr_diff(repo: str, number: int) -> str:
    return gh("pr", "diff", str(number), "--repo", repo)


def open_prs(repo: str) -> list[dict[str, Any]]:
    return json.loads(
        gh("pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
           "--json", "number,headRefOid,isDraft,title")
    )


def pr_state(repo: str, number: int) -> str:
    return json.loads(gh("pr", "view", str(number), "--repo", repo, "--json", "state"))["state"]


def _paginated(path: str) -> list[dict[str, Any]]:
    pages = json.loads(gh("api", "--paginate", "--slurp", path))
    return [item for page in pages for item in page]


def pr_review_comments(repo: str, number: int) -> list[dict[str, Any]]:
    return _paginated(f"repos/{repo}/pulls/{number}/comments?per_page=100")


def review_comments(repo: str, number: int, review_id: int) -> list[dict[str, Any]]:
    return _paginated(f"repos/{repo}/pulls/{number}/reviews/{review_id}/comments?per_page=100")


def file_at(repo: str, path: str, ref: str) -> Optional[str]:
    try:
        return gh("api", f"repos/{repo}/contents/{path}?ref={ref}", "-H", "Accept: application/vnd.github.raw")
    except GitHubError:
        return None


def post_review(
    repo: str, number: int, commit_id: str, body: str, comments: list[dict]
) -> dict[str, Any]:
    payload = {"commit_id": commit_id, "event": "COMMENT", "body": body, "comments": comments}
    out = gh("api", "-X", "POST", f"repos/{repo}/pulls/{number}/reviews", "--input", "-",
             input=json.dumps(payload))
    return json.loads(out)


@contextmanager
def checkout(repo: str, number: int) -> Iterator[Path]:
    """Shallow checkout of the PR head (forks included) in a throwaway directory."""
    tmp = Path(tempfile.mkdtemp(prefix="greptile-sub-"))
    git = [
        "git", "-C", str(tmp),
        "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
        "-c", "core.hooksPath=/dev/null",
    ]
    try:
        for args in (
            ["init", "-q"],
            ["remote", "add", "origin", f"https://github.com/{repo}.git"],
            ["fetch", "-q", "--depth", "1", "origin", f"pull/{number}/head"],
            ["checkout", "-q", "FETCH_HEAD"],
        ):
            proc = subprocess.run(git + args, capture_output=True, text=True)
            if proc.returncode != 0:
                raise GitHubError(f"git {args[0]}: {proc.stderr.strip()[:1000]}")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
