"""The plugin's Agent Skill has the frontmatter the Agent Skills spec requires."""
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_skill_md_has_name_and_description():
    path = ROOT / "skills/skillmem/SKILL.md"
    front = yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])
    assert front["name"] == path.parent.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", front["name"])
    assert 0 < len(front["description"]) <= 1024
