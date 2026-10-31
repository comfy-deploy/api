"""Regression tests for two phase-1 security hotfixes.

A. DELETE /assets/{id} on a folder must only delete the caller's own children,
   and the folder path must be matched as a literal prefix (LIKE wildcards escaped).
B. Platform HuggingFace / Civitai tokens may only be attached when the download
   URL's parsed host really is huggingface.co / civitai.com (not a substring match).
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import files, volumes
from api.routes.volumes import host_matches

pytestmark = pytest.mark.unit

DOWNLOADER = Path(__file__).resolve().parents[2] / "src/modal_apps/modal_downloader.py"


# --- Bug A -----------------------------------------------------------------


def _run_delete(folder_path, current_user):
    folder = SimpleNamespace(is_folder=True, path=folder_path)
    first = MagicMock()
    first.scalar_one_or_none.return_value = folder
    second = MagicMock()
    second.scalars.return_value = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[first, second]),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    request = SimpleNamespace(state=SimpleNamespace(current_user=current_user))
    asyncio.run(files.delete_asset(request=request, asset_id="folder_x", db=db))
    children_stmt = db.execute.await_args_list[1].args[0]
    return str(children_stmt.compile(compile_kwargs={"literal_binds": True}))


def test_folder_children_query_is_org_scoped():
    sql = _run_delete("images", {"user_id": "user_1", "org_id": "org_1"})
    assert "assets.org_id = 'org_1'" in sql
    assert "assets.path LIKE 'images/%' ESCAPE '\\'" in sql


def test_folder_children_query_is_user_scoped_without_org():
    sql = _run_delete("a/images", {"user_id": "user_1"})
    assert "assets.user_id = 'user_1'" in sql
    assert "assets.org_id IS NULL" in sql
    assert "assets.path LIKE 'a/images/%' ESCAPE '\\'" in sql


def test_folder_children_query_escapes_wildcards():
    sql = _run_delete("my_folder/%\\x", {"user_id": "user_1", "org_id": "org_1"})
    assert "assets.path LIKE 'my\\_folder/\\%\\\\x/%' ESCAPE '\\'" in sql


# --- Bug B -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url,domain,expected",
    [
        # legit
        ("https://huggingface.co/org/repo/resolve/main/model.safetensors", "huggingface.co", True),
        ("https://cdn-lfs.huggingface.co/repos/ab/cd/file", "huggingface.co", True),
        ("https://HuggingFace.CO/org/repo/resolve/main/x.bin", "huggingface.co", True),
        ("https://huggingface.co:443/org/repo/resolve/main/x.bin", "huggingface.co", True),
        ("https://civitai.com/api/download/models/12345", "civitai.com", True),
        ("https://civitai.com/api/download/models/12345?type=Model&format=SafeTensor", "civitai.com", True),
        # attacks
        ("https://evil.tld/huggingface.co/x", "huggingface.co", False),
        ("https://evil.tld/?huggingface.co", "huggingface.co", False),
        ("https://evil.tld/?civitai.com", "civitai.com", False),
        ("https://evil.tld/#civitai.com", "civitai.com", False),
        ("https://huggingface.co.evil.tld/x", "huggingface.co", False),
        ("https://civitai.com.evil.tld/api/download/models/1", "civitai.com", False),
        ("https://evilhuggingface.co/x", "huggingface.co", False),
        ("https://notcivitai.com/x", "civitai.com", False),
        ("https://huggingface.co@evil.tld/x", "huggingface.co", False),
        ("https://huggingface.co:443@evil.tld/x", "huggingface.co", False),
        ("https://user:pass@civitai.com.evil.tld/x", "civitai.com", False),
        ("https://huggingface.co\\@evil.tld/x", "huggingface.co", False),
        ("https://evil.tld#@huggingface.co/x", "huggingface.co", False),
        # wrong scheme / garbage
        ("http://huggingface.co/org/repo/resolve/main/x.bin", "huggingface.co", False),
        ("http://civitai.com/api/download/models/12345", "civitai.com", False),
        ("ftp://huggingface.co/x", "huggingface.co", False),
        ("huggingface.co/org/repo", "huggingface.co", False),
        ("https://[huggingface.co/x", "huggingface.co", False),
        ("", "huggingface.co", False),
    ],
)
def test_host_matches(url, domain, expected):
    assert host_matches(url, domain) is expected


def test_downloader_host_matches_is_identical_copy():
    """modal_downloader.py can't import from api (separate container), so it
    carries a copy of the helper; keep it byte-identical to the tested one."""

    def source_of(path):
        src = path.read_text()
        node = next(
            n for n in ast.parse(src).body
            if isinstance(n, ast.FunctionDef) and n.name == "host_matches"
        )
        return ast.get_source_segment(src, node)

    assert source_of(DOWNLOADER) == source_of(Path(volumes.__file__))
