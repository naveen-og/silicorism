"""Dynamic skill resolver.

Finds skill prompt files across global and local harness dirs and concatenates
the ones a task requests, so a native pi/claude agent launches with the matching
skill instructions injected into its prompt. Local skills override global ones
of the same name (they are scanned last, and last write wins).
"""

from __future__ import annotations

import os

# Global first, local (CWD-relative) last so local overrides global.
SEARCH_DIRS = ("~/.claude/skills", "~/.pi/skills", ".claude/skills", ".pi/skills")


def _skill_dirs(cwd: str | None = None) -> list[str]:
    base = cwd or os.getcwd()
    dirs = []
    for d in SEARCH_DIRS:
        p = os.path.expanduser(d)
        dirs.append(p if os.path.isabs(p) else os.path.join(base, p))
    return dirs


def find_skill(name: str, cwd: str | None = None) -> str | None:
    """Path to a skill's markdown, or None. Accepts <name>.md, <name>/SKILL.md,
    or <name>/skill.md in any search dir; a local hit wins over a global one."""
    found = None
    for d in _skill_dirs(cwd):
        for cand in (os.path.join(d, f"{name}.md"),
                     os.path.join(d, name, "SKILL.md"),
                     os.path.join(d, name, "skill.md")):
            if os.path.isfile(cand):
                found = cand
    return found


def load_skills(names, cwd: str | None = None, max_chars: int = 4000) -> str:
    """Concatenate the requested skills' prompt text. Unknown names are skipped
    silently; returns '' when nothing resolves so callers can append blindly."""
    if not names:
        return ""
    parts = []
    for name in names:
        path = find_skill(name, cwd)
        if not path:
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read().strip()
        except OSError:
            continue
        parts.append(f"### Skill: {name}\n{text[:max_chars]}")
    return "--- Skills ---\n" + "\n\n".join(parts) if parts else ""


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".claude", "skills"))
        os.makedirs(os.path.join(d, ".pi", "skills", "tdd"))
        open(os.path.join(d, ".claude", "skills", "review.md"), "w").write("REVIEW RULES")
        open(os.path.join(d, ".pi", "skills", "tdd", "SKILL.md"), "w").write("TDD RULES")
        assert find_skill("review", d).endswith("review.md")
        assert find_skill("tdd", d).endswith("SKILL.md")
        assert find_skill("nope", d) is None
        out = load_skills(["review", "tdd", "nope"], cwd=d)
        assert "REVIEW RULES" in out and "TDD RULES" in out and "--- Skills ---" in out
        assert load_skills([], cwd=d) == ""
        assert load_skills(["nope"], cwd=d) == ""
    print("skills OK")
