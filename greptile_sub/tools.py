"""Read-only tools over a PR checkout, offered to the model as OpenAI functions."""

import fnmatch
import json
import re
from pathlib import Path
from typing import Iterator, Optional

MAX_READ_LINES = 400
MAX_LIST = 300
MAX_GREP_RESULTS = 100
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 300

SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the repository, optionally under a directory and matching a glob.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory relative to the repo root. Default: root."},
                    "pattern": {"type": "string", "description": "Glob on the relative path, e.g. '*.py' or 'src/**/test_*'."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": f"Read a file with line numbers, at most {MAX_READ_LINES} lines per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "1-based. Default 1."},
                    "end_line": {"type": "integer", "description": "Inclusive."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a Python regular expression. Returns path:line: text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "File or directory to search. Default: root."},
                    "glob": {"type": "string", "description": "Only search files whose relative path matches."},
                    "ignore_case": {"type": "boolean"},
                },
                "required": ["pattern"],
            },
        },
    },
]


class ToolError(Exception):
    pass


class RepoTools:
    """Every path is resolved and must stay inside the checkout, symlinks included: PR content is untrusted."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.calls = 0

    def call(self, name: str, raw_args: str) -> str:
        self.calls += 1
        try:
            args = json.loads(raw_args or "{}")
            handler = {"list_files": self.list_files, "read_file": self.read_file, "grep": self.grep}[name]
            return handler(**args)
        except KeyError:
            return f"error: unknown tool {name!r}"
        except (ToolError, TypeError, ValueError, re.error) as e:
            return f"error: {e}"

    def _resolve(self, path: Optional[str]) -> Path:
        target = (self.root / (path or ".")).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError(f"{path!r} is outside the repository")
        return target

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _files(self, base: Path) -> Iterator[Path]:
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for p in candidates:
            if ".git" in p.relative_to(self.root).parts or not p.is_file():
                continue
            if not p.resolve().is_relative_to(self.root):
                continue
            yield p

    def list_files(self, path: Optional[str] = None, pattern: Optional[str] = None) -> str:
        base = self._resolve(path)
        if not base.exists():
            raise ToolError(f"{path!r} does not exist")
        names = [self._rel(p) for p in self._files(base)]
        if pattern:
            names = [n for n in names if fnmatch.fnmatch(n, pattern) or fnmatch.fnmatch(Path(n).name, pattern)]
        more = f"\n... {len(names) - MAX_LIST} more" if len(names) > MAX_LIST else ""
        return "\n".join(names[:MAX_LIST]) + more if names else "(no files)"

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"{path!r} is not a file")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"{path!r} is larger than {MAX_FILE_BYTES} bytes")
        lines = target.read_text(errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line or len(lines), start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n:>5}| {lines[n - 1][:MAX_LINE_CHARS * 4]}" for n in range(start, end + 1))
        tail = f"\n(lines {start}-{end} of {len(lines)})" if end < len(lines) or start > 1 else ""
        return (body or "(empty file)") + tail

    def grep(self, pattern: str, path: Optional[str] = None, glob: Optional[str] = None,
             ignore_case: bool = False) -> str:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        hits: list[str] = []
        for p in self._files(self._resolve(path)):
            rel = self._rel(p)
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(p.name, glob)):
                continue
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            data = p.read_bytes()
            if b"\0" in data[:8192]:
                continue
            for n, line in enumerate(data.decode(errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:MAX_LINE_CHARS]}")
                    if len(hits) >= MAX_GREP_RESULTS:
                        return "\n".join(hits) + f"\n(stopped at {MAX_GREP_RESULTS} matches)"
        return "\n".join(hits) or "(no matches)"

