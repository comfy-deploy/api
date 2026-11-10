import asyncio

import pytest
from fastapi import HTTPException

from api.utils.docker import DepsBody, generate_all_docker_commands


def test_malformed_docker_steps_returns_422():
    body = DepsBody(
        docker_command_steps={
            "steps": [
                {"id": "a", "type": "custom-node", "data": {"name": "Ada Node"}},
            ]
        }
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(generate_all_docker_commands(body))
    assert exc.value.status_code == 422
