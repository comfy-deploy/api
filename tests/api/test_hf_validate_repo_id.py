import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from api.routes import volumes


def test_malformed_repo_id_returns_not_exists():
    body = volumes.HuggingFaceValidateRequest(
        repo_id="https://huggingface.co/ada/example-model"
    )
    settings = MagicMock(hugging_face_token=None)
    with patch.object(volumes, "get_user_settings", AsyncMock(return_value=settings)):
        res = asyncio.run(volumes.validate_huggingface_repo(MagicMock(), body, MagicMock()))
    assert res.exists is False
