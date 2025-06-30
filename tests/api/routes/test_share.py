import pytest
from conftest import get_test_client


@pytest.mark.asyncio
async def test_share_output_prevent_duplicate(
    app, test_free_user, test_run_deployment_sync_public_with_output
):
    run_id, output_id = test_run_deployment_sync_public_with_output
    async with get_test_client(app, test_free_user) as client:
        response = await client.post(
            "/share/output",
            json={"run_id": run_id, "output_id": output_id},
        )
        assert response.status_code == 200

        # Try sharing the same output again from the same user
        response_dup = await client.post(
            "/share/output",
            json={"run_id": run_id, "output_id": output_id},
        )
        assert response_dup.status_code == 400
        assert "Output already shared" in response_dup.text
