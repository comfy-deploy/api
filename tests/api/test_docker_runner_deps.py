"""The v4 runner (my_app.py) imports requests at container start; the image must install it."""

import asyncio

import pytest

from api.utils.docker import DepsBody, generate_all_docker_commands

pytestmark = pytest.mark.unit


def test_base_image_installs_requests():
    result = asyncio.run(generate_all_docker_commands(DepsBody(docker_command_steps={"steps": []})))
    commands = [c for step in result.docker_commands for c in step]
    assert any("pip install" in c and " requests" in c for c in commands), commands
