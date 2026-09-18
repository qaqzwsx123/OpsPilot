from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    content: str


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root

    def load(self) -> list[Skill]:
        skills: list[Skill] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            content = path.read_text(encoding="utf-8")
            description = next((line.removeprefix("description:").strip() for line in content.splitlines() if line.startswith("description:")), "")
            skills.append(Skill(path.parent.name, description, content))
        return skills

