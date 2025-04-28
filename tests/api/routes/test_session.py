from conftest import get_test_client
import pytest  # noqa: E402

from dotenv import load_dotenv

load_dotenv()

@pytest.mark.asyncio
async def test_session_for_free_user(
    app, free_user, test_serverless_machine
):
    
    machine_id = test_serverless_machine

    async with get_test_client(app, free_user) as client:
        response = await client.post(
            "/session", json={"machine_id": machine_id, "wait_for_server": False}
        )
        print(f"Response status: {response.status_code}")
        assert response.status_code == 403
        assert("Free plan users cannot create sessions, we are mitigating abuse, please wait" in response.json()["detail"])


@pytest.mark.asyncio
async def test_session_for_paid_user(
    app, paid_user, test_serverless_machine
):
    
    machine_id = test_serverless_machine

    async with get_test_client(app, paid_user) as client:
        session_response = await client.post(
            "/session", json={"machine_id": machine_id, "wait_for_server": True}
        )
        assert session_response.status_code == 200

        session_id = session_response.json()["session_id"]

        timeout_response = await client.post("/session/increase-timeout",
                                             json={"timeout": 20, 
                                                   "machine_id": machine_id,
                                                   "session_id": session_id,
                                                   "gpu": "A100"}
                                            )

        assert timeout_response.status_code == 200
        
        delete_response = await client.delete(f"/session/{session_id}")

        assert delete_response.status_code == 200


@pytest.mark.asyncio
async def test_heavy_machine_session_1_for_paid_user(
    app, paid_user, test_heavy_serverless_machine_1
):
    
    machine_id = test_heavy_serverless_machine_1

    async with get_test_client(app, paid_user) as client:
        session_response = await client.post(
            "/session", json={"machine_id": machine_id, "wait_for_server": True}
        )
        assert session_response.status_code == 200

        session_id = session_response.json()["session_id"]

        timeout_response = await client.post("/session/increase-timeout",
                                             json={"timeout": 20, 
                                                   "machine_id": machine_id,
                                                   "session_id": session_id,
                                                   "gpu": "CPU"}
                                            )

        assert timeout_response.status_code == 200
        
        delete_response = await client.delete(f"/session/{session_id}")

        assert delete_response.status_code == 200


@pytest.mark.asyncio
async def test_heavy_machine_session_2_for_paid_user(
    app, paid_user, test_heavy_serverless_machine_2
):
    
    machine_id = test_heavy_serverless_machine_2

    async with get_test_client(app, paid_user) as client:
        session_response = await client.post(
            "/session", json={"machine_id": machine_id, "wait_for_server": True}
        )
        assert session_response.status_code == 200

        session_id = session_response.json()["session_id"]

        timeout_response = await client.post("/session/increase-timeout",
                                             json={"timeout": 20, 
                                                   "machine_id": machine_id,
                                                   "session_id": session_id,
                                                   "gpu": "CPU"}
                                            )

        assert timeout_response.status_code == 200
        
        delete_response = await client.delete(f"/session/{session_id}")

        assert delete_response.status_code == 200


'''
@pytest.mark.asyncio
async def test_free_user_session_timeout_limit(app, test_free_user, test_serverless_machine_free_user):
    """Test that free users cannot set timeout beyond 30 minutes"""
    async with get_test_client(app, test_free_user) as client:
        # Test creating session with timeout > 30 minutes
        response = await client.post(
            "/session/dynamic",
            json={
                "machine_id": test_serverless_machine_free_user,
                "timeout": 31,
                "gpu": "CPU",
                "wait_for_server": True,
            },
        )
        assert response.status_code == 400
        assert (
            "Free plan users are limited to 30 minutes timeout"
            in response.json()["detail"]
        )

        # Test creating session with valid timeout
        response = await client.post(
            "/session/dynamic",
            json={
                "machine_id": test_serverless_machine_free_user,
                "timeout": 15,
                "gpu": "CPU",
                "wait_for_server": True,
            },
        )
        print(f"Response status: {response.status_code}")
        print(f"Target Response body: {response.text}")
        assert response.status_code == 200
        session_id = response.json()["session_id"]

        # Test increasing timeout beyond 30 minutes total
        response = await client.post(
            f"/session/{session_id}/increase-timeout", json={"minutes": 20}
        )
        assert response.status_code == 400
        assert (
            "Free plan users are limited to 30 minutes total timeout"
            in response.json()["detail"]
        )

        # Test increasing timeout within limit
        response = await client.post(
            f"/session/{session_id}/increase-timeout", json={"minutes": 10}
        )
        assert response.status_code == 200
        assert response.json()["message"] == "Timeout increased successfully"

        # Cleanup: Delete the session
        response = await client.delete(f"/session/{session_id}?wait_for_shutdown=true")
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        assert response.status_code == 200


# test_serverless_machine


@pytest.mark.asyncio
async def test_free_user_concurrent_sessions(app, test_free_user, test_serverless_machine_free_user):
    """Test that free users cannot create concurrent sessions"""
    print(f"\nTest user: {test_free_user.id}, {test_free_user.username}")
    # test_serverless_machine = None
    print(f"Test machine: {test_serverless_machine_free_user}")

    async with get_test_client(app, test_free_user) as client:
        # Create first session
        print("\nCreating first session...")
        response = await client.post(
            "/session/dynamic",
            json={
                "machine_id": test_serverless_machine_free_user,
                "timeout": 15,
                "gpu": "CPU",
                "wait_for_server": True,
            },
        )
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")

        if response.status_code != 200:
            # Get all active sessions for debugging
            sessions_response = await client.get("/sessions")
            print(f"\nCurrent active sessions: {sessions_response.text}")
            print(
                f"\nError message: {response.json().get('detail', 'No detail provided')}"
            )

        assert response.status_code == 200
        session_id = response.json()["session_id"]

        # Try to create second session
        print("\nTrying to create second session...")
        response = await client.post(
            "/session/dynamic",
            json={
                "machine_id": test_serverless_machine_free_user,
                "timeout": 15,
                "gpu": "CPU",
                "wait_for_server": True,
            },
        )
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")
        assert response.status_code == 400
        assert (
            "Free plan does not support concurrent sessions"
            in response.json()["detail"]
        )

        # Cleanup: Delete the session
        response = await client.delete(f"/session/{session_id}?wait_for_shutdown=true")
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        assert response.status_code == 200
'''
