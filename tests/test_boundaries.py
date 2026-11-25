"""Agent B is its own application: it must not depend on Agent A (Watch Concierge)."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT_A_PACKAGES = {"concierge", "common", "servers", "baselines", "evals", "tools"}


def imported_top_level_names(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_module_imports_agent_a():
    offenders = {str(p.relative_to(ROOT)): sorted(imported_top_level_names(p) & AGENT_A_PACKAGES)
                 for p in ROOT.rglob("*.py") if ".venv" not in p.parts and "venv" not in p.parts}
    assert {k: v for k, v in offenders.items() if v} == {}


def test_the_server_refuses_any_interface_but_loopback():
    from discovery_agent import config
    assert config.check_host("127.0.0.1") == "127.0.0.1"
    for host in ("0.0.0.0", "192.168.1.10", "localhost", "::"):
        with pytest.raises(SystemExit, match="127.0.0.1 only"):
            config.check_host(host)


def test_the_default_port_does_not_clash_with_agent_a():
    from discovery_agent import config
    assert config.PORT not in {8001, 8002, 8003, 8004}
