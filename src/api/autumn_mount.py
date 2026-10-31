import os
from fastapi import HTTPException
from autumn.asgi import AutumnASGI
from typing import TYPE_CHECKING, Optional, TypedDict
from clerk_backend_api import Clerk
from api.utils.multi_level_cache import multi_level_cached

if TYPE_CHECKING:
    from starlette.requests import Request as StarletteRequest
    from autumn.asgi import AutumnIdentifyData


EmailAndName = TypedDict("EmailAndName", {"email": Optional[str], "name": str})


@multi_level_cached(
    key_prefix="customer_clerk_customer_name",
    ttl_seconds=60,
    redis_ttl_seconds=120,
    version="1.0",
    key_builder=lambda customer_id: f"customer_clerk_customer_name:{customer_id}",
)
async def get_customer_clerk_name_cached(customer_id: str) -> EmailAndName:
    async with Clerk(
        bearer_auth=os.getenv("CLERK_SECRET_KEY"),
    ) as clerk:
        if customer_id.startswith("org_"):
            org = await clerk.organizations.get_async(organization_id=customer_id)
            return EmailAndName(email=None, name=org.name)
        else:
            user = await clerk.users.get_async(user_id=customer_id)
            # first/last name and email are all optional in Clerk; a crash here 500s /api/autumn
            email = user.email_addresses[0].email_address if user.email_addresses else None
            name = " ".join(filter(None, [user.first_name, user.last_name])) or email or customer_id
            return EmailAndName(email=email, name=name)


async def identify(request: "StarletteRequest") -> "AutumnIdentifyData":
    """
    Identify the current user from the request state.
    
    This function extracts user information that was set by the auth middleware
    and returns it in the format expected by Autumn.
    
    Returns:
        AutumnIdentifyData: User identification data with customer_id and customer_data
    """
    
    current_user = getattr(request.state, 'current_user', None)
    
    if not current_user:
        # Return a default/anonymous user if no authentication is present
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user_id = current_user.get("user_id", "unknown")
    org_id = current_user.get("org_id")
    
    # Use org_id as customer_id if available, otherwise use user_id
    customer_id = org_id if org_id else user_id
    
    customer_data = await get_customer_clerk_name_cached(customer_id)
    
    # Extract user data for Autumn
    customer_data = {
        "name": customer_data["name"],
        "email": customer_data["email"],
    }

    return {
        "customer_id": customer_id,
        "customer_data": customer_data
    }


# Initialize Autumn ASGI app
autumn_app = AutumnASGI(
    token=os.environ.get("AUTUMN_SECRET_KEY"),
    identify=identify
)
