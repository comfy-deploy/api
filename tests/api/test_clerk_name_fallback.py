"""Clerk sends missing names as null; they must not become the string "None"."""

import pytest

from api.routes.clerk_webhook import get_name_fallback, get_username_fallback

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "user,name,username",
    [
        ({"first_name": "Ada", "last_name": "Lovelace", "username": None}, "Ada Lovelace", "AdaLovelace"),
        ({"first_name": "Ada", "last_name": None, "username": None}, "Ada", "Ada"),
        ({"first_name": None, "last_name": None, "username": "ada"}, "ada", "ada"),
        ({"first_name": None, "last_name": None, "username": None}, "Unknown", "Unknown"),
    ],
)
def test_null_names(user, name, username):
    assert get_name_fallback(user) == name
    assert get_username_fallback(user) == username
