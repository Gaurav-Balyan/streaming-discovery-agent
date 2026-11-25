"""
Agent B's discovery skills: a skill = instructions + the catalog tools it may use + a turn budget,
described by a file in discovery_agent/skills/ (same JSON-valued front-matter style as Agent A's skills).

PROGRESSIVE DISCLOSURE
    The registry reads only the HEADERS at startup (name, version, description, tools, keywords, budget).
    A skill's instructions (the body) are read only when a request selects it, and cached after that.
    The model sees only the selected skills' instructions and tools.

ROUTING (deterministic, no model call; ADR-002 / ADR-004)
    route(question) matches each skill's `keywords` as whole words, case-insensitive:
        cues for one skill  -> that skill
        cues for several    -> all of them (one agent run, e.g. "a title on Prime and a song on YouTube")
        no cue at all       -> all skills (ambiguous: better a little more context than a missed search)

FAIL CLOSED
    Any invalid skill file stops the registry from loading (SkillError) — the server does not start
    with a half-understood configuration. Unknown skill names raise SkillError.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
CATALOG_TOOLS = frozenset({"search_title", "search_youtube"})       # what Agent B's catalog-mcp offers
REQUIRED = ("name", "version", "description", "tools", "max_turns", "keywords")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class SkillError(Exception):
    pass


@dataclass(frozen=True)
class SkillMeta:
    name: str
    version: str
    description: str
    tools: tuple[str, ...]
    max_turns: int
    keywords: tuple[str, ...]
    path: Path


def _header_lines(path: Path) -> list[str]:
    """The front-matter lines only. Stops at the closing '---': the body is NOT read here."""
    lines = []
    with path.open(encoding="utf-8") as f:
        if f.readline().rstrip("\r\n") != "---":
            raise SkillError(f"{path.name}: must start with a '---' header line")
        for line in f:
            line = line.rstrip("\r\n")
            if line == "---":
                return lines
            lines.append(line)
    raise SkillError(f"{path.name}: the header is not closed with '---'")


def parse_header(path: Path) -> SkillMeta:
    meta = {}
    for n, line in enumerate(_header_lines(path), 2):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise SkillError(f"{path.name} line {n}: expected 'key: <json value>'")
        try:
            meta[key.strip()] = json.loads(value.strip())
        except json.JSONDecodeError as e:
            raise SkillError(f"{path.name} line {n}: value of {key.strip()!r} is not JSON ({e.msg})") from None
    missing = [k for k in REQUIRED if k not in meta]
    if missing:
        raise SkillError(f"{path.name}: missing {missing}")
    name, tools, keywords, turns = meta["name"], meta["tools"], meta["keywords"], meta["max_turns"]
    if not isinstance(name, str) or not NAME.match(name) or path.stem != name:
        raise SkillError(f"{path.name}: name must be lowercase-with-dashes and match the file name")
    if not isinstance(meta["version"], str) or not VERSION.match(meta["version"]):
        raise SkillError(f"{path.name}: version must look like 1.0.0")
    if not isinstance(meta["description"], str) or not 1 <= len(meta["description"]) <= 200:
        raise SkillError(f"{path.name}: description must be 1-200 characters")
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
        raise SkillError(f"{path.name}: tools must be a non-empty list of names")
    unknown = sorted(set(tools) - CATALOG_TOOLS)
    if unknown:
        raise SkillError(f"{path.name}: unknown tools {unknown}; allowed: {sorted(CATALOG_TOOLS)}")
    if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 6:
        raise SkillError(f"{path.name}: max_turns must be a whole number 1-6")
    if not isinstance(keywords, list) or not keywords or not all(isinstance(k, str) and k.strip() for k in keywords):
        raise SkillError(f"{path.name}: keywords must be a non-empty list of words")
    return SkillMeta(name, meta["version"], meta["description"], tuple(tools), turns,
                     tuple(k.lower() for k in keywords), path)


class Registry:
    def __init__(self, directory: Path | None = None):
        directory = directory or SKILLS_DIR
        files = sorted(directory.glob("*.md"))
        if not files:
            raise SkillError(f"no skill files in {directory}")
        self._meta: dict[str, SkillMeta] = {}
        for path in files:
            m = parse_header(path)
            if m.name in self._meta:
                raise SkillError(f"duplicate skill name {m.name!r}")
            self._meta[m.name] = m
        self._bodies: dict[str, str] = {}
        self._patterns = {n: re.compile(r"\b(" + "|".join(re.escape(k) for k in m.keywords) + r")\b", re.I)
                          for n, m in self._meta.items()}
        self.bodies_read = 0               # for tests and the disclosure measurement

    def metadata(self) -> list[SkillMeta]:
        return list(self._meta.values())

    def get(self, name: str) -> SkillMeta:
        if name not in self._meta:
            raise SkillError(f"unknown skill {name!r}")
        return self._meta[name]

    def instructions(self, name: str) -> str:
        """The body of one skill, read on first use (progressive disclosure)."""
        meta = self.get(name)
        if name not in self._bodies:
            text = meta.path.read_text(encoding="utf-8").replace("\r\n", "\n")
            body = text[4:].split("\n---\n", 1)[1]
            self._bodies[name] = " ".join(body.split())
            self.bodies_read += 1
        return self._bodies[name]

    def route(self, question: str) -> tuple[list[str], str]:
        """(selected skill names in registry order, reason)."""
        hits = [n for n, p in self._patterns.items() if p.search(question)]
        if hits:
            return hits, "keywords: " + ", ".join(hits)
        return list(self._meta), "no cue: all skills"


_registry: Registry | None = None


def registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry
