import re

HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """New-file line numbers GitHub accepts inline review comments on (added or context lines)."""
    lines: dict[str, set[int]] = {}
    path = None
    old_left = new_left = 0
    new_line = 0
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                lines.setdefault(path, set()).add(new_line)
                new_line += 1
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith(" ") or line == "":
                lines.setdefault(path, set()).add(new_line)
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif (m := HUNK.match(line)) and path:
            old_left = int(m.group(1) or 1)
            new_line = int(m.group(2))
            new_left = int(m.group(3) or 1)
    return lines
