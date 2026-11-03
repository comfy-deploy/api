import asyncio

import pytest
from fastapi import HTTPException

from api.routes.comfy_node import extract_repo_name


def test_valid_url():
    assert asyncio.run(extract_repo_name("https://github.com/ada/lovelace.git")) == "ada/lovelace"


@pytest.mark.parametrize("url", ["", "not-a-url", "https://github.com/ada", "https://github.com/"])
def test_invalid_url_is_400_not_index_error(url):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(extract_repo_name(url))
    assert exc.value.status_code == 400
