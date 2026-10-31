"""Phase-2 asset scoping: LIKE escaping, presigned/register/delete key binding, optimize access.

DB and S3 are mocked; no network.
"""

import asyncio
import functools
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routes import files, image_optimization as opt

pytestmark = pytest.mark.unit


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper

ALICE = {"user_id": "user_alice", "org_id": "org_alice"}
MALLORY = {"user_id": "user_mallory"}
BUCKET = SimpleNamespace(
    public=True, bucket="platform", region="us-east-1", access_key="k", secret_key="s",
    session_token=None, is_custom=False,
)


def _req(user=None):
    state = SimpleNamespace() if user is None else SimpleNamespace(current_user=user)
    return SimpleNamespace(state=state)


def _sql(stmt):
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.delenv("ASSET_PATH_ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.delenv("OPTIMIZE_ENFORCE_ACCESS", raising=False)
    monkeypatch.setattr(files, "get_s3_config", AsyncMock(return_value=BUCKET))


def _enforce(monkeypatch, name):
    monkeypatch.setenv(name, "true")


# --- 1. LIKE escaping ---------------------------------------------------------


@run_async
async def test_list_assets_escapes_like_wildcards():
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    await files.list_assets(request=_req(ALICE), path="security_test", db=db)
    sql = _sql(db.execute.await_args.args[0])
    assert "assets.path LIKE 'security\\_test/%' ESCAPE '\\'" in sql
    assert "assets.path NOT LIKE 'security\\_test/%/%' ESCAPE '\\'" in sql
    assert "assets.org_id = 'org_alice'" in sql


@run_async
async def test_list_assets_escapes_percent_and_backslash():
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    await files.list_assets(request=_req(ALICE), path="a%b\\c", db=db)
    assert "LIKE 'a\\%b\\\\c/%' ESCAPE '\\'" in _sql(db.execute.await_args.args[0])


@run_async
async def test_search_assets_escapes_like_wildcards():
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    await files.search_assets(request=_req(ALICE), db=db, query="x_%\\", limit=5)
    assert "'%x\\_\\%\\\\%'" in _sql(db.execute.await_args.args[0])
    assert "ESCAPE '\\'" in _sql(db.execute.await_args.args[0])


# --- 2. presigned-url / register ---------------------------------------------


async def _presign(monkeypatch, user, parent_path="/upload", file_name="cat.png"):
    monkeypatch.setattr(files, "generate_presigned_url", MagicMock(return_value="https://signed"))
    return await files.get_asset_upload_url(
        request=_req(user), file_name=file_name, parent_path=parent_path, size=1, type="image/png", db=None
    )


async def _register(user, file_id, db_path, url):
    db = SimpleNamespace(add=MagicMock(), commit=AsyncMock(), refresh=AsyncMock())
    asset = await files.register_asset(
        request=_req(user), db=db, file_id=file_id, file_name="cat.png", file_size=1,
        db_path=db_path, url=url, mime_type="image/png",
    )
    return asset, db


@run_async
async def test_presigned_key_is_server_generated_and_register_accepts_it(monkeypatch):
    _enforce(monkeypatch, "ASSET_PATH_ENFORCE_OWNERSHIP")
    p = await _presign(monkeypatch, ALICE)
    assert p["db_path"] == f"upload/{p['file_id']}.png"
    assert p["download_url"] == f"https://platform.s3.us-east-1.amazonaws.com/assets/{p['db_path']}"
    asset, db = await _register(ALICE, p["file_id"], p["db_path"], p["download_url"])
    assert asset.path == p["db_path"]
    db.add.assert_called_once()


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@run_async
async def test_register_foreign_path(monkeypatch, mode):
    p = await _presign(monkeypatch, ALICE)  # Alice's upload; Mallory knows the key
    if mode == "enforce":
        _enforce(monkeypatch, "ASSET_PATH_ENFORCE_OWNERSHIP")
        with pytest.raises(HTTPException) as e:
            await _register(MALLORY, p["file_id"], p["db_path"], p["download_url"])
        assert e.value.status_code == 400
    else:
        asset, db = await _register(MALLORY, p["file_id"], p["db_path"], p["download_url"])
        db.add.assert_called_once()  # shadow: logged only, behaviour unchanged


@pytest.mark.parametrize(
    "file_id,db_path,url",
    [
        # legacy-style untagged id pointing at someone else's key
        ("img_AAAAAAAAAAAAAAAA", "upload/img_AAAAAAAAAAAAAAAA.png", None),
        # path traversal out of assets/
        (None, "../outputs/runs/r/ComfyUI_00001_.png", None),
        # url pointing at a different object than db_path
        (None, None, "https://platform.s3.us-east-1.amazonaws.com/outputs/runs/r/x.png"),
    ],
)
@run_async
async def test_register_rejects_bad_triples_when_enforced(monkeypatch, file_id, db_path, url):
    p = await _presign(monkeypatch, MALLORY)
    _enforce(monkeypatch, "ASSET_PATH_ENFORCE_OWNERSHIP")
    with pytest.raises(HTTPException) as e:
        await _register(MALLORY, file_id or p["file_id"], db_path or p["db_path"], url or p["download_url"])
    assert e.value.status_code == 400


@run_async
async def test_presigned_traversal_in_parent_path(monkeypatch):
    p = await _presign(monkeypatch, ALICE, parent_path="/../outputs")  # shadow: still issued
    assert p["db_path"].startswith("../outputs/")
    _enforce(monkeypatch, "ASSET_PATH_ENFORCE_OWNERSHIP")
    with pytest.raises(HTTPException):
        await _presign(monkeypatch, ALICE, parent_path="/../outputs")


# --- 2b. delete ---------------------------------------------------------------


def _s3(monkeypatch):
    s3_client = SimpleNamespace(delete_object=AsyncMock())
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=s3_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.client.return_value = ctx
    monkeypatch.setattr(files.aioboto3, "Session", MagicMock(return_value=session))
    return s3_client


async def _delete(asset_id, path):
    row = SimpleNamespace(id=asset_id, is_folder=False, path=path)
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = SimpleNamespace(execute=AsyncMock(return_value=result), delete=AsyncMock(), commit=AsyncMock())
    await files.delete_asset(request=_req(ALICE), asset_id=asset_id, db=db)
    db.delete.assert_awaited_once_with(row)  # the row is always removed


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@run_async
async def test_delete_own_asset_deletes_s3_object(monkeypatch, mode):
    if mode == "enforce":
        _enforce(monkeypatch, "ASSET_PATH_ENFORCE_OWNERSHIP")
    s3 = _s3(monkeypatch)
    await _delete("img_AAAAAAAAAAAAAAAA", "upload/img_AAAAAAAAAAAAAAAA.png")
    s3.delete_object.assert_awaited_once_with(Bucket="platform", Key="assets/upload/img_AAAAAAAAAAAAAAAA.png")


@pytest.mark.parametrize("path", ["upload/img_VICTIMVICTIMVICT.png", "../outputs/runs/r/img_x.png"])
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@run_async
async def test_delete_foreign_key(monkeypatch, mode, path):
    if mode == "enforce":
        _enforce(monkeypatch, "ASSET_PATH_ENFORCE_OWNERSHIP")
    s3 = _s3(monkeypatch)
    await _delete("img_x", path)
    if mode == "enforce":
        s3.delete_object.assert_not_awaited()
    else:
        s3.delete_object.assert_awaited_once()


@run_async
async def test_delete_on_custom_bucket_is_unchanged(monkeypatch):
    _enforce(monkeypatch, "ASSET_PATH_ENFORCE_OWNERSHIP")
    monkeypatch.setattr(files, "get_s3_config", AsyncMock(return_value=SimpleNamespace(**{**vars(BUCKET), "is_custom": True})))
    s3 = _s3(monkeypatch)
    await _delete("img_x", "upload/whatever.png")
    s3.delete_object.assert_awaited_once()


# --- 3. optimize ---------------------------------------------------------------

RUN_ID = str(uuid.uuid4())
PRIVATE_OUTPUT = f"outputs/runs/{RUN_ID}/ComfyUI_00001_.png"


def _optimize_env(monkeypatch, *, public_object, custom=False):
    cfg = SimpleNamespace(**{**vars(BUCKET), "is_custom": custom, "public": not custom})
    monkeypatch.setattr(opt, "get_user_settings_cached_as_object", AsyncMock(return_value=None))
    monkeypatch.setattr(opt, "retrieve_s3_config", AsyncMock(return_value=cfg))
    monkeypatch.setattr(opt, "check_s3_object_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(opt, "check_s3_object_public", AsyncMock(return_value=public_object))
    monkeypatch.setattr(opt, "get_optimized_image_response", AsyncMock(return_value="SERVED"))


async def _optimize(user, s3_key=PRIVATE_OUTPUT, scalars=(None, None)):
    db = SimpleNamespace(scalar=AsyncMock(side_effect=list(scalars)))
    resp = await opt.optimize_image_on_demand(transformations="auto", s3_key=s3_key, request=_req(user), db=db)
    return resp, db


@run_async
async def test_optimize_public_object_anonymous_unchanged(monkeypatch):
    _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=True)
    resp, db = await _optimize(None)
    assert resp == "SERVED"
    db.scalar.assert_not_awaited()


@run_async
async def test_optimize_public_object_other_tenant_unchanged(monkeypatch):
    _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=True)
    resp, db = await _optimize(MALLORY)
    assert resp == "SERVED"
    db.scalar.assert_not_awaited()


@run_async
async def test_optimize_private_object_anonymous_still_401(monkeypatch):
    _optimize_env(monkeypatch, public_object=False)
    with pytest.raises(HTTPException) as e:
        await _optimize(None)
    assert e.value.status_code == 401


@run_async
async def test_optimize_private_object_owner(monkeypatch):
    _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=False)
    resp, db = await _optimize(ALICE, scalars=[uuid.UUID(RUN_ID)])
    assert resp == "SERVED"
    sql = _sql(db.scalar.await_args_list[0].args[0])
    assert "workflow_runs.org_id = 'org_alice'" in sql


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@run_async
async def test_optimize_private_object_non_owner(monkeypatch, mode):
    if mode == "enforce":
        _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=False)
    if mode == "enforce":
        with pytest.raises(HTTPException) as e:
            await _optimize(MALLORY)
        assert e.value.status_code == 403
    else:
        resp, _ = await _optimize(MALLORY)
        assert resp == "SERVED"


@run_async
async def test_optimize_private_output_shared_by_link(monkeypatch):
    _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=False)
    resp, db = await _optimize(MALLORY, scalars=[None, uuid.uuid4()])
    assert resp == "SERVED"
    assert "output_shares.visibility IN ('public', 'link')" in _sql(db.scalar.await_args_list[1].args[0])


@run_async
async def test_optimize_private_asset_owner_and_non_owner(monkeypatch):
    _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=False)
    key = "assets/upload/img_AAAAAAAAAAAAAAAA.png"
    resp, db = await _optimize(ALICE, s3_key=key, scalars=["img_AAAAAAAAAAAAAAAA"])
    assert resp == "SERVED"
    assert "assets.id = 'img_AAAAAAAAAAAAAAAA'" in _sql(db.scalar.await_args.args[0])
    with pytest.raises(HTTPException):
        await _optimize(MALLORY, s3_key=key, scalars=[None])


@pytest.mark.parametrize("key", ["inputs/img_AAAAAAAAAAAAAAAA.png", f"outputs/runs/{RUN_ID}/../x/y.png"])
@run_async
async def test_optimize_private_unknown_or_traversal_denied_when_enforced(monkeypatch, key):
    _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=False)
    with pytest.raises(HTTPException) as e:
        await _optimize(ALICE, s3_key=key, scalars=[uuid.UUID(RUN_ID)])
    assert e.value.status_code == 403


@run_async
async def test_optimize_lookup_failure_does_not_break_shadow_mode(monkeypatch):
    _optimize_env(monkeypatch, public_object=False)
    db = SimpleNamespace(scalar=AsyncMock(side_effect=RuntimeError("db down")))
    resp = await opt.optimize_image_on_demand(transformations="auto", s3_key=PRIVATE_OUTPUT, request=_req(ALICE), db=db)
    assert resp == "SERVED"


@run_async
async def test_optimize_custom_bucket_skips_ownership_check(monkeypatch):
    _enforce(monkeypatch, "OPTIMIZE_ENFORCE_ACCESS")
    _optimize_env(monkeypatch, public_object=False, custom=True)
    resp, db = await _optimize(ALICE)
    assert resp == "SERVED"
    db.scalar.assert_not_awaited()


def test_temporary_download_url_only_presigns_real_s3_hosts(monkeypatch, caplog):
    import logging as _logging
    from api.routes import utils as route_utils

    calls = []
    monkeypatch.setattr(route_utils, "generate_presigned_download_url", lambda **kw: calls.append(kw) or "signed")
    creds = dict(region="us-east-1", access_key="a", secret_key="s", session_token=None)

    with caplog.at_level(_logging.WARNING):
        assert route_utils.get_temporary_download_url(
            "https://bucket.s3.us-east-1.amazonaws.com/assets/x.png", **creds
        ) == "signed"
    assert calls[-1]["bucket"] == "bucket" and calls[-1]["object_key"] == "assets/x.png"
    assert not [r for r in caplog.records if "non-S3 host" in r.getMessage()]

    look_alike = "https://bucket.attacker.tld/outputs/runs/x.png"
    monkeypatch.delenv("S3_URL_ENFORCE_HOST", raising=False)
    with caplog.at_level(_logging.WARNING):
        assert route_utils.get_temporary_download_url(look_alike, **creds) == "signed"
    assert [r for r in caplog.records if "non-S3 host" in r.getMessage()]

    monkeypatch.setenv("S3_URL_ENFORCE_HOST", "true")
    n = len(calls)
    with pytest.raises(ValueError):
        route_utils.get_temporary_download_url(look_alike, **creds)
    assert len(calls) == n
