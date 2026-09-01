"""PARSE stage: unified diff → stable-ID hunks + split-diff rows (DESIGN.md §3)."""
from __future__ import annotations

import re

from .models import DiffRow, FileDiff, Hunk

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_diff(diff_text: str) -> tuple[list[FileDiff], list[Hunk]]:
    """Parse a git unified diff into per-file split rows and stable hunks.

    Hunk IDs (H1, H2, ...) are assigned in document order; anchors cited by
    the LLM are later validated against these hunks' new-file line ranges.
    """
    files: list[FileDiff] = []
    hunks: list[Hunk] = []
    lines = diff_text.splitlines()
    i = 0
    hunk_counter = 0
    current: FileDiff | None = None
    old_path = new_path = ""

    def flush() -> None:
        nonlocal current
        if current is not None:
            files.append(current)
            current = None

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git"):
            flush()
            m = re.match(r'^diff --git (?:a/(?:"?)(.*?)(?:"?)|"a/(.*?)") (?:b/(?:"?)(.*?)(?:"?)|"b/(.*?)")$', line)
            old_path = (m.group(1) or m.group(2) or "") if m else ""
            new_path = (m.group(3) or m.group(4) or "") if m else old_path
            current = FileDiff(path=new_path or old_path, status="mod")
            i += 1
            continue

        if current is None:
            i += 1
            continue

        if line.startswith("new file mode"):
            current.status = "new"
        elif line.startswith("deleted file mode"):
            current.status = "del"
        elif line.startswith("rename from"):
            current.status = "renamed"
        elif line.startswith("rename to"):
            current.path = line[len("rename to "):].strip()
        elif line.startswith("--- "):
            p = line[4:].strip()
            if p != "/dev/null":
                old_path = p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if p == "/dev/null":
                current.status = "del"
                current.path = old_path
            else:
                current.path = p[2:] if p.startswith("b/") else p

        m = _HUNK_RE.match(line)
        if not m:
            i += 1
            continue

        # ---- hunk ----
        old_start = int(m.group(1))
        new_start = int(m.group(3))
        new_count = int(m.group(4) or "1")
        hunk_counter += 1
        hunk_id = f"H{hunk_counter}"
        current.hunk_ids.append(hunk_id)
        current.rows.append(DiffRow(gap=line))

        patch_lines = [line]
        o_no, n_no = old_start, new_start
        pending_del: list[tuple[int, str]] = []
        pending_add: list[tuple[int, str]] = []
        i += 1

        def flush_pairs() -> None:
            nonlocal pending_del, pending_add
            for j in range(max(len(pending_del), len(pending_add))):
                current.rows.append(DiffRow(
                    o=pending_del[j] if j < len(pending_del) else None,
                    n=pending_add[j] if j < len(pending_add) else None,
                ))
            pending_del, pending_add = [], []

        while i < len(lines):
            body = lines[i]
            if body.startswith("\\ No newline"):
                i += 1
                continue
            if body.startswith("-") and not body.startswith("---"):
                patch_lines.append(body)
                pending_del.append((o_no, body[1:]))
                o_no += 1
            elif body.startswith("+") and not body.startswith("+++"):
                patch_lines.append(body)
                pending_add.append((n_no, body[1:]))
                n_no += 1
            elif body.startswith(" ") or body == "":
                patch_lines.append(body)
                flush_pairs()
                text = body[1:] if body.startswith(" ") else ""
                current.rows.append(DiffRow(o=(o_no, text), n=(n_no, text)))
                o_no += 1
                n_no += 1
            else:
                break  # next file/hunk header
            i += 1

        flush_pairs()
        hunks.append(Hunk(
            id=hunk_id,
            file=current.path,
            start=new_start,
            end=max(new_start, new_start + new_count - 1),
            patch="\n".join(patch_lines),
        ))

    flush()
    files = [f for f in files if f.rows]
    return files, hunks
