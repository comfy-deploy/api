from conftest import get_test_client
import pytest  # noqa: E402
import asyncio
from contextlib import asynccontextmanager  # noqa: E402
from aiohttp import web 
from dotenv import load_dotenv

load_dotenv()


@pytest.mark.asyncio
async def test_get_shared_public_run_free_user(app, test_free_user, test_run_deployment_sync_public):
    """Test running a deployment"""
    async with get_test_client(app, test_free_user) as client:
        run_id = test_run_deployment_sync_public
        response = await client.get(
            "/run/" + run_id
        )
        assert response.status_code == 200

@pytest.mark.asyncio
async def test_get_shared_public_run_paid_user(app, paid_user, test_run_deployment_sync_public):
    """Test running a deployment"""
    async with get_test_client(app, paid_user) as client:
        run_id = test_run_deployment_sync_public
        response = await client.get(
            "/run/" + run_id
        )
        assert response.status_code == 200

@pytest.mark.asyncio
async def test_get_shared_public_run_paid_user_2(app, paid_user_2, test_run_deployment_sync_public):
    """Test running a deployment"""
    async with get_test_client(app, paid_user_2) as client:
        run_id = test_run_deployment_sync_public
        response = await client.get(
            "/run/" + run_id
        )
        assert response.status_code == 403



@pytest.mark.asyncio
async def test_run_deployment_sync_ensure_output_id(app, paid_user, test_create_workflow_deployment_output_id):
    """Test running a deployment"""
    async with get_test_client(app, paid_user) as client:
        deployment_id = test_create_workflow_deployment_output_id
        response = await client.post(
            "/run/deployment/sync", json={"deployment_id": deployment_id}
        )
        assert response.status_code == 200
        run_id = response.json()[0]["run_id"]
        assert run_id is not None
        assert response.json()[0]["output_id"] == "my_image"

@pytest.mark.asyncio
async def test_run_deployment_sync(app, paid_user, test_create_workflow_deployment):
    """Test running a deployment"""
    async with get_test_client(app, paid_user) as client:
        deployment_id = test_create_workflow_deployment
        response = await client.post(
            "/run/deployment/sync", json={"deployment_id": deployment_id}
        )
        assert response.status_code == 200
        run_id = response.json()[0]["run_id"]
        assert run_id is not None



@asynccontextmanager
async def create_webhook_server():
    """Create a temporary webhook server that collects received webhooks"""
    received_webhooks = []

    async def webhook_handler(request):
        payload = await request.json()
        print(f"Received webhook: {payload}")
        received_webhooks.append(payload)
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 0)  # Listen on all interfaces
    await site.start()

    # Get the assigned port
    port = site._server.sockets[0].getsockname()[1]

    # Use the service name 'app' as the hostname
    webhook_url = f"http://app:{port}/webhook"

    try:
        yield webhook_url, received_webhooks
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_run_deployment_with_webhook(
    app, paid_user, test_create_workflow_deployment
):
    """Test running a deployment with webhook notifications"""
    deployment_id = test_create_workflow_deployment

    async with create_webhook_server() as (webhook_url, received_webhooks):
        webhook_url_with_params = f"{webhook_url}?target_events=run.output,run.updated"

        async with get_test_client(app, paid_user) as client:
            response = await client.post(
                "/run/deployment/sync",
                json={
                    "deployment_id": deployment_id,
                    "webhook": webhook_url_with_params,
                },
            )
            assert response.status_code == 200
            run_id = response.json()[0]["run_id"]
            assert run_id is not None

            # Wait a bit for webhooks to be received
            for _ in range(30):
                if len(received_webhooks) >= 2:
                    break
                await asyncio.sleep(0.1)

            assert len(received_webhooks) >= 2, (
                "Did not receive expected number of webhooks"
            )

            output_webhooks = [
                w for w in received_webhooks if w["event_type"] == "run.output"
            ]
            status_webhooks = [
                w for w in received_webhooks if w["event_type"] == "run.updated"
            ]

            assert len(output_webhooks) > 0, "No run.output webhook received"
            assert len(status_webhooks) > 0, "No run.updated webhook received"

            for webhook in output_webhooks:
                assert "outputs" in webhook, "Webhook missing outputs field"
                assert isinstance(webhook["outputs"], list), "Outputs should be a list"

            for webhook in status_webhooks:
                assert "status" in webhook, "Webhook missing status field"
                assert "progress" in webhook, "Webhook missing progress field"
                assert webhook["run_id"] == run_id, "Run ID mismatch in webhook"


@pytest.mark.asyncio
async def test_run_deployment_with_webhook_no_target_events(
    app, paid_user, test_create_workflow_deployment
):
    """Test running a deployment with webhook notifications without specifying target_events - should receive run.updated by default but not run.output"""
    deployment_id = test_create_workflow_deployment

    async with create_webhook_server() as (webhook_url, received_webhooks):
        # Note: Not specifying target_events in the webhook URL - should get run.updated by default

        async with get_test_client(app, paid_user) as client:
            response = await client.post(
                "/run/deployment/sync",
                json={"deployment_id": deployment_id, "webhook": webhook_url},
            )
            assert response.status_code == 200
            run_id = response.json()[0]["run_id"]
            assert run_id is not None

            # Wait a bit for webhooks to be received
            for _ in range(30):
                if len(received_webhooks) >= 1:  # We expect run.updated events
                    break
                await asyncio.sleep(0.1)

            # Verify we received webhooks
            assert len(received_webhooks) > 0, "Did not receive any webhooks"

            # Verify we received run.updated events by default
            status_webhooks = [
                w for w in received_webhooks if w["event_type"] == "run.updated"
            ]
            assert len(status_webhooks) > 0, (
                "No run.updated webhooks received when they should be default"
            )

            # Verify we did not receive run.output events since they weren't requested
            output_webhooks = [
                w for w in received_webhooks if w["event_type"] == "run.output"
            ]
            assert len(output_webhooks) == 0, (
                "Received run.output webhooks when not requested"
            )

            # Verify the structure of run.updated webhooks
            for webhook in status_webhooks:
                assert "status" in webhook, "Webhook missing status field"
                assert "progress" in webhook, "Webhook missing progress field"
                assert webhook["run_id"] == run_id, "Run ID mismatch in webhook"


@pytest.mark.asyncio
async def test_run_deployment_on_a_wrong_user(
    app, paid_user, test_free_user, test_create_workflow_deployment
):
    """Test running a deployment with webhook notifications and machine"""
    deployment_id = test_create_workflow_deployment

    async with get_test_client(app, test_free_user) as client:
        response = await client.post(
            "/run/deployment/sync", json={"deployment_id": deployment_id}
        )
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        assert response.status_code == 404, "Expected 403 status code for wrong user"


@pytest.mark.asyncio
async def test_cancel_run_with_webhook(
    app, paid_user, test_create_workflow_deployment
):
    """Test cancelling a run with webhook notifications"""
    deployment_id = test_create_workflow_deployment

    async with create_webhook_server() as (webhook_url, received_webhooks):
        webhook_url_with_params = f"{webhook_url}?target_events=run.updated"

        async with get_test_client(app, paid_user) as client:
            response = await client.post(
                "/run/deployment/sync",
                json={
                    "deployment_id": deployment_id,
                    "webhook": webhook_url_with_params,
                },
            )
            assert response.status_code == 200
            run_id = response.json()[0]["run_id"]
            assert run_id is not None

            # Wait for initial webhooks to be received
            for _ in range(30):
                if len(received_webhooks) >= 1:
                    break
                await asyncio.sleep(0.1)

            received_webhooks.clear()

            cancel_response = await client.post(f"/run/{run_id}/cancel")
            assert cancel_response.status_code == 200
            assert cancel_response.json()["status"] == "success"

            # Wait for cancel webhook to be received
            for _ in range(30):
                if len(received_webhooks) >= 1:
                    break
                await asyncio.sleep(0.1)

            # Verify we received webhooks
            assert len(received_webhooks) > 0, "Did not receive any webhooks after cancellation"

            # Verify we received run.updated events with cancelled status
            cancel_webhooks = [
                w for w in received_webhooks 
                if w["event_type"] == "run.updated" and w["status"] == "cancelled"
            ]
            
            assert len(cancel_webhooks) > 0, "No cancelled status webhook received"

            # Verify the structure of cancel webhooks
            for webhook in cancel_webhooks:
                assert webhook["run_id"] == run_id, "Run ID mismatch in webhook"
                assert webhook["status"] == "cancelled", "Status should be cancelled"


