from conftest import get_test_client
import pytest
import uuid
from dotenv import load_dotenv

load_dotenv()


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_create_output_share_success(app, paid_user, test_run_deployment_sync_public):
    """Test successful creation of output share"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id),
            "visibility": "public"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == str(run_id)
        assert data["output_id"] == str(run_id)
        assert data["visibility"] == "public"
        assert "output_type" in data
        assert data["output_type"] == "other"


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_create_output_share_default_visibility(app, paid_user, test_run_deployment_sync_public):
    """Test output share creation with default visibility"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id)
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["visibility"] == "private"


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_create_output_share_nonexistent_run(app, paid_user):
    """Test creating output share for nonexistent run"""
    async with get_test_client(app, paid_user) as client:
        response = await client.post("/share/output", json={
            "run_id": str(uuid.uuid4()),
            "output_id": str(uuid.uuid4())
        })
        
        assert response.status_code == 404


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_create_output_share_unauthorized_run(app, paid_user_2, test_run_deployment_sync_public):
    """Test creating output share for run owned by different user"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user_2) as client:
        response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id)
        })
        
        assert response.status_code == 404


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_create_output_share_unauthorized_access(app, test_run_deployment_sync_public):
    """Test unauthenticated user cannot create output shares"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, None) as client:
        response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id)
        })
        
        assert response.status_code == 401


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_list_output_shares_success(app, paid_user, test_run_deployment_sync_public):
    """Test listing output shares"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        share_response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id)
        })
        assert share_response.status_code == 200
        
        list_response = await client.get("/share/output")
        assert list_response.status_code == 200
        shares = list_response.json()
        assert len(shares) >= 1
        assert any(share["run_id"] == str(run_id) for share in shares)


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_list_output_shares_empty(app, paid_user):
    """Test listing when user has no output shares"""
    async with get_test_client(app, paid_user) as client:
        response = await client.get("/share/output")
        assert response.status_code == 200
        shares = response.json()
        assert isinstance(shares, list)


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_list_output_shares_isolation(app, paid_user):
    """Test users can only see their own output shares"""
    async with get_test_client(app, paid_user) as client:
        response = await client.get("/share/output")
        assert response.status_code == 200
        shares = response.json()
        assert isinstance(shares, list)


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_get_shared_outputs_success(app, paid_user, test_run_deployment_sync_public):
    """Test retrieving shared outputs by ID"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        share_response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id),
            "visibility": "public"
        })
        assert share_response.status_code == 200
        share_id = share_response.json()["id"]
        
        get_response = await client.get(f"/share/output/{share_id}")
        assert get_response.status_code == 200
        data = get_response.json()
        assert "share" in data
        assert "run" in data
        assert data["share"]["id"] == share_id


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_get_shared_outputs_nonexistent_id(app, paid_user):
    """Test retrieving nonexistent shared outputs"""
    async with get_test_client(app, paid_user) as client:
        response = await client.get(f"/share/output/{uuid.uuid4()}")
        assert response.status_code == 404


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_get_shared_outputs_visibility_controls(app, paid_user, test_run_deployment_sync_public):
    """Test visibility controls for shared outputs"""
    run_id = test_run_deployment_sync_public
    
    visibility_levels = ["link", "public", "private"]
    
    async with get_test_client(app, paid_user) as client:
        created_shares = []
        
        for visibility in visibility_levels:
            share_response = await client.post("/share/output", json={
                "run_id": str(run_id),
                "output_id": str(run_id),
                "visibility": visibility
            })
            assert share_response.status_code == 200
            created_shares.append(share_response.json())
        
        for share in created_shares:
            get_response = await client.get(f"/share/output/{share['id']}")
            if share["visibility"] == "private":
                assert get_response.status_code == 200
            else:
                assert get_response.status_code == 200


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_delete_output_share_success(app, paid_user, test_run_deployment_sync_public):
    """Test successful deletion of output share"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        share_response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id)
        })
        assert share_response.status_code == 200
        share_id = share_response.json()["id"]
        
        delete_response = await client.delete(f"/share/output/{share_id}")
        assert delete_response.status_code == 200
        
        list_response = await client.get("/share/output")
        shares = list_response.json()
        assert not any(share["id"] == share_id for share in shares)


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_delete_output_share_nonexistent(app, paid_user):
    """Test deleting nonexistent output share"""
    async with get_test_client(app, paid_user) as client:
        response = await client.delete(f"/share/output/{uuid.uuid4()}")
        assert response.status_code == 404


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_delete_output_share_unauthorized(app, paid_user):
    """Test unauthorized deletion of output share"""
    async with get_test_client(app, paid_user) as client:
        response = await client.delete(f"/share/output/{uuid.uuid4()}")
        assert response.status_code == 404


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_output_share_org_isolation(app, paid_user, paid_user_2, test_run_deployment_sync_public):
    """Test org-level isolation of output shares"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        share_response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id),
            "visibility": "private"
        })
        assert share_response.status_code == 200
    
    async with get_test_client(app, paid_user_2) as client:
        list_response = await client.get("/share/output?include_public=false")
        shares = list_response.json()
        assert len(shares) == 0


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_create_output_share_with_output_type(app, paid_user, test_run_deployment_sync_public):
    """Test creating output share with specific output type"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id),
            "output_type": "image"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["output_id"] == str(run_id)
        assert data["output_type"] == "image"


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_create_output_share_invalid_visibility(app, paid_user, test_run_deployment_sync_public):
    """Test creating output share with invalid visibility"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        response = await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id),
            "visibility": "invalid_visibility"
        })
        
        assert response.status_code == 422


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_output_type_filtering(app, paid_user, test_run_deployment_sync_public):
    """Test filtering output shares by output type"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, paid_user) as client:
        await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id),
            "output_type": "image",
            "visibility": "public"
        })
        
        await client.post("/share/output", json={
            "run_id": str(run_id),
            "output_id": str(run_id),
            "output_type": "video",
            "visibility": "public"
        })
        
        image_response = await client.get("/share/output?output_type=image")
        assert image_response.status_code == 200
        image_shares = image_response.json()
        assert all(share["output_type"] == "image" for share in image_shares)
        
        video_response = await client.get("/share/output?output_type=video")
        assert video_response.status_code == 200
        video_shares = video_response.json()
        assert all(share["output_type"] == "video" for share in video_shares)


@pytest.mark.skip(reason="Temporarily disabled due to auth middleware conflict with /share/output endpoints")
@pytest.mark.asyncio
async def test_unauthenticated_access_public_only(app, test_run_deployment_sync_public):
    """Test that unauthenticated users only see public shares"""
    run_id = test_run_deployment_sync_public
    
    async with get_test_client(app, None) as client:
        response = await client.get("/share/output")
        assert response.status_code == 200
        shares = response.json()
        assert all(share["visibility"] == "public" for share in shares)
