"""
Structural checks for the web login gate — the properties B4/B5 depend on
that a unit test of any one function can't see:

  - every page calls require_login() before touching portfolio_agent data
  - nothing under web/ still uses the pre-auth local_context() path
  - portfolio_agent/ stays Streamlit-free (auth lives in web/ only)
  - identity_from_verified_login() is reachable only from web/auth.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "web" / "app.py"] + sorted((ROOT / "web" / "pages").glob("*.py"))


def _imported_names(tree: ast.Module, module_prefix: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(module_prefix):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _find_top_level_call(tree: ast.Module, func_name: str) -> int | None:
    for i, node in enumerate(tree.body):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == func_name
        ):
            return i
    return None


def _calls_before(node: ast.AST, names: set[str], found: list[str]) -> None:
    """Collect calls to `names` that execute immediately — skips into
    function/class bodies (not run until called later), but not decorators
    or default-argument expressions (which do run at def time)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for dec in getattr(node, "decorator_list", []):
            _calls_before(dec, names, found)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
        found.append(node.func.id)
    for child in ast.iter_child_nodes(node):
        _calls_before(child, names, found)


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.name)
def test_page_calls_require_login_before_any_portfolio_agent_call(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pa_names = _imported_names(tree, "portfolio_agent")

    idx = _find_top_level_call(tree, "require_login")
    assert idx is not None, f"{path.relative_to(ROOT)} never calls require_login() at module level"

    found: list[str] = []
    for node in tree.body[:idx]:
        _calls_before(node, pa_names, found)
    assert not found, (
        f"{path.relative_to(ROOT)} calls {found} from portfolio_agent before require_login()"
    )


def test_no_file_under_web_calls_local_context():
    hits = []
    for path in (ROOT / "web").rglob("*.py"):
        if "local_context(" in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(ROOT)))
    assert not hits, f"local_context( still used under web/: {hits}"


def test_no_file_under_portfolio_agent_imports_streamlit():
    hits = []
    for path in (ROOT / "portfolio_agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "streamlit" for a in node.names):
                hits.append(str(path.relative_to(ROOT)))
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "streamlit":
                hits.append(str(path.relative_to(ROOT)))
    assert not hits, f"streamlit imported under portfolio_agent/: {hits}"


def test_identity_from_verified_login_referenced_only_in_web_auth_and_tests():
    """Code reference (import or call), not prose — a docstring may still
    mention the function by name to explain what a caller becomes."""
    allowed_files = {
        ROOT / "portfolio_agent" / "services" / "context.py",  # its own definition
        ROOT / "web" / "auth.py",
    }
    hits = []
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        if path in allowed_files or path.parts[len(ROOT.parts)] == "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            is_import = (
                isinstance(node, ast.ImportFrom)
                and any((a.asname or a.name) == "identity_from_verified_login" for a in node.names)
            )
            is_reference = isinstance(node, ast.Name) and node.id == "identity_from_verified_login"
            if is_import or is_reference:
                hits.append(str(path.relative_to(ROOT)))
                break
    assert not hits, f"identity_from_verified_login referenced outside web/auth.py and tests/: {hits}"
