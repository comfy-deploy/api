from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Optional, Any
import logging

from .utils import (
    select,
)

from api.database import get_db
from api.models import (
    User,
)
from api.utils.autumn import autumn_client
from api.utils.webhook import WebhookRegistry, WebhookResponse

logger = logging.getLogger(__name__)

# Configuration
AUTUMN_SEATS_FEATURE_ID = "seats"

router = APIRouter(
    tags=["Clerk Webhook"],
)

# Create Clerk webhook registry
clerk_registry = WebhookRegistry("Clerk webhook")

class EmailAddress(BaseModel):
    email_address: str
    id: str
    verification: dict


class ClerkWebhookData(BaseModel):
    data: dict
    type: str
    timestamp: int


# Organization Membership webhook data models
class OrganizationData(BaseModel):
    """Organization data from Clerk webhook."""
    model_config = ConfigDict(extra="allow")  # Allow additional fields we don't use
    
    id: str
    name: Optional[str] = "Unknown"
    members_count: int = 0


class PublicUserData(BaseModel):
    """Public user data from organization membership webhook."""
    model_config = ConfigDict(extra="allow")
    
    user_id: Optional[str] = None
    identifier: Optional[str] = "Unknown"
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class OrganizationMembershipData(BaseModel):
    """Organization membership webhook data."""
    model_config = ConfigDict(extra="allow")
    
    id: Optional[str] = None  # membership_id
    organization: OrganizationData
    public_user_data: Optional[PublicUserData] = None
    role: Optional[str] = "Unknown"


class UserData(BaseModel):
    """User webhook data for user.created/updated events."""
    model_config = ConfigDict(extra="allow")
    
    id: str
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


# Helper functions
def get_username_fallback(user_data: Dict[str, Any]) -> str:
    """Extract username with fallback to first_name + last_name."""
    username = user_data.get("username")
    if username:
        return username
    
    first_name = user_data.get("first_name") or ""
    last_name = user_data.get("last_name") or ""
    return f"{first_name}{last_name}".strip() or "Unknown"


def get_name_fallback(user_data: Dict[str, Any]) -> str:
    """Extract display name with fallback logic."""
    first_name = user_data.get("first_name") or ""
    last_name = user_data.get("last_name") or ""
    full_name = f"{first_name} {last_name}".strip()
    
    if full_name:
        return full_name
    
    return get_username_fallback(user_data)


async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
    """Fetch a user by ID from the database."""
    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    return result.scalar_one_or_none()


# Webhook handlers
@clerk_registry.handler("user.created")
async def handle_user_created(user_data: Dict[str, Any], db: AsyncSession) -> WebhookResponse:
    """Handle user.created webhook event."""
    # Parse the data with Pydantic model
    try:
        parsed_user = UserData(**user_data)
    except Exception as e:
        logger.error(f"Failed to parse user data: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid user data format")
    
    user_id = parsed_user.id
    
    # Check if user already exists
    existing_user = await get_user_by_id(db, user_id)
    if existing_user:
        return WebhookResponse(
            status="success",
            message="User already exists",
            data={"user_id": user_id}
        )
    
    # Create new user using the typed model
    new_user = User(
        id=user_id,
        username=get_username_fallback(parsed_user.dict()),
        name=get_name_fallback(parsed_user.dict()),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    
    logger.info(f"Created new user: {user_id}")
    return WebhookResponse(
        status="success",
        message="User created successfully",
        data={"user_id": user_id, "username": new_user.username}
    )


@clerk_registry.handler("user.updated")
async def handle_user_updated(user_data: Dict[str, Any], db: AsyncSession) -> WebhookResponse:
    """Handle user.updated webhook event."""
    # Parse the data with Pydantic model
    try:
        parsed_user = UserData(**user_data)
    except Exception as e:
        logger.error(f"Failed to parse user data: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid user data format")
    
    user_id = parsed_user.id
    
    # Get existing user
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User not found: {user_id}")
    
    # Update user fields using the typed model
    user.username = get_username_fallback(parsed_user.dict())
    user.name = get_name_fallback(parsed_user.dict())
    user.updated_at = datetime.now(timezone.utc)
    
    await db.commit()
    await db.refresh(user)
    
    logger.info(f"Updated user: {user_id}")
    return WebhookResponse(
        status="success",
        message="User updated successfully",
        data={"user_id": user_id, "username": user.username}
    )


@clerk_registry.handler("user.deleted")
async def handle_user_deleted(user_data: Dict[str, Any], db: AsyncSession) -> WebhookResponse:
    """Handle user.deleted webhook event."""
    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID not provided in webhook data")
    
    # Get existing user
    user = await get_user_by_id(db, user_id)
    if not user:
        return WebhookResponse(
            status="success",
            message="User already deleted or doesn't exist",
            data={"user_id": user_id}
        )
    
    # Soft delete or hard delete based on your requirements
    # For now, we'll just log and return success
    logger.info(f"User deletion requested: {user_id}")
    return WebhookResponse(
        status="success",
        message="User deletion processed",
        data={"user_id": user_id}
    )


# Organization Membership handlers - all update seat usage
@clerk_registry.handler("organizationMembership.deleted")
@clerk_registry.handler("organizationMembership.created")
@clerk_registry.handler("organizationMembership.updated")
async def handle_org_membership_events(data: Dict[str, Any], db: AsyncSession) -> WebhookResponse:
    """
    Handle all organization membership webhook events (created, updated, deleted).
    Updates seat usage in Autumn based on the current member count.
    """
    # Parse the data with Pydantic model
    try:
        membership_data = OrganizationMembershipData(**data)
    except Exception as e:
        logger.error(f"Failed to parse organization membership data: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid webhook data format")
    
    # Extract typed organization data
    org_id = membership_data.organization.id
    members_count = membership_data.organization.members_count
    org_name = membership_data.organization.name
    
    # Extract membership data
    membership_id = membership_data.id
    user_id = membership_data.public_user_data.user_id if membership_data.public_user_data else None
    user_identifier = membership_data.public_user_data.identifier if membership_data.public_user_data else "Unknown"
    role = membership_data.role
    
    logger.info(
        f"Organization membership change - Org: {org_name} ({org_id}), "
        f"User: {user_identifier} ({user_id}), Role: {role}, "
        f"Current members: {members_count}"
    )
    
    # Update seat usage in Autumn
    try:
        # Note: Make sure the feature is configured in your Autumn dashboard
        # You can change AUTUMN_SEATS_FEATURE_ID constant to match your configuration
        success = await autumn_client.set_feature_usage(
            customer_id=org_id,
            feature_id=AUTUMN_SEATS_FEATURE_ID,
            value=members_count
        )
        
        if success:
            logger.info(f"Successfully updated seat usage for org {org_id} to {members_count}")
        else:
            logger.warning(f"Failed to update seat usage for org {org_id}")
            
    except Exception as e:
        logger.error(f"Error updating seat usage in Autumn: {str(e)}")
        # Don't fail the webhook, just log the error
    
    return WebhookResponse(
        status="success",
        message="Organization membership change processed",
        data={
            "organization_id": org_id,
            "organization_name": org_name,
            "membership_id": membership_id,
            "user_id": user_id,
            "members_count": members_count
        }
    )


# Main webhook endpoint
@router.post("/clerk/webhook", include_in_schema=False, response_model=WebhookResponse)
async def handle_clerk_webhook(
    webhook_data: ClerkWebhookData,
    db: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    """Main webhook endpoint that routes to appropriate handlers."""
    return await clerk_registry.handle_webhook(
        event_type=webhook_data.type,
        data=webhook_data.data,
        db=db
    )