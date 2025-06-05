import os
from api.utils.autumn import get_autumn_customer
# from api.middleware.subscriptionMiddleware import get_customer_plan_cached, get_customer_plan_cached_5_seconds
from api.utils.multi_level_cache import multi_level_cached
import logfire
from nanoid import generate
from pydantic import BaseModel
from api.database import get_db
from api.models import Machine, SubscriptionStatus, User, Workflow, GPUEvent, UserSettings
from api.routes.utils import (
    async_lru_cache,
    fetch_user_icon,
    get_user_settings as get_user_settings_util,
    update_user_settings as update_user_settings_util,
    select,
)

from api.middleware.auth import (
  get_api_keys as get_api_keys_auth,
  delete_api_key as delete_api_key_auth,
  create_api_key as create_api_key_auth
)
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date, and_, desc, func, or_, cast, extract, text, not_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi import BackgroundTasks
import aiohttp
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
import asyncio
import unicodedata
import re
import functools
from upstash_redis.asyncio import Redis
import json
import resend

redis_url = os.getenv("UPSTASH_REDIS_META_REST_URL")
redis_token = os.getenv("UPSTASH_REDIS_META_REST_TOKEN")
if redis_token is None:
    print("Redis token is None", redis_url)
    redisMeta = Redis(url="http://localhost:8079", token="example_token")
else:
    redisMeta = Redis(url=redis_url, token=redis_token)

router = APIRouter(
    tags=["Platform"],
)

# Add Autumn API configuration
AUTUMN_API_KEY = os.getenv("AUTUMN_SECRET_KEY")
AUTUMN_API_URL = "https://api.useautumn.com/v1"

@router.get("/platform/user-settings")
async def get_user_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await get_user_settings_util(request, db)


@router.get("/user/{user_id}")
async def get_user_meta(
    user_id: str,
):
    return await fetch_user_icon(user_id)


class UserSettingsUpdateRequest(BaseModel):
    api_version: Optional[str] = None
    custom_output_bucket: Optional[bool] = None
    hugging_face_token: Optional[str] = None
    output_visibility: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_bucket_name: Optional[str] = None
    s3_region: Optional[str] = None
    spend_limit: Optional[float] = None
    assumed_role_arn: Optional[str] = None

@router.put("/platform/user-settings")
async def update_user_settings(
    request: Request,
    body: UserSettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    return await update_user_settings_util(request, db, body)


class GetApiKeysRequest(BaseModel):
    limit: Optional[int] = None
    offset: Optional[int] = None
    search: Optional[str] = None

@router.get("/platform/api-keys")
async def get_api_keys(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await get_api_keys_auth(request, db)

@router.post("/platform/api-keys")
async def create_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    plan = request.state.current_user.get("plan")
    if plan == "free":
        raise HTTPException(status_code=403, detail="Free plan users cannot create API keys")
    
    return await create_api_key_auth(request, db)

@router.delete("/platform/api-keys/{key_id}")
async def delete_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await delete_api_key_auth(request, db)


import stripe
from fastapi import HTTPException

# You'll need to configure stripe with your API key
stripe.api_key = os.getenv("STRIPE_API_KEY")

# Stripe price lookup keys for each plan
PLAN_LOOKUP_KEYS = {
    "creator_monthly": "creator_monthly",
    "creator_yearly": "creator_yearly",
    "creator_legacy_monthly": "creator_legacy_monthly",
    "business_monthly": "business_monthly",
    "business_yearly": "business_yearly",
    "deployment_monthly": "deployment_monthly",
    "deployment_yearly": "deployment_yearly",
}

@async_lru_cache(maxsize=1)
async def get_pricing_plan_mapping():
    """
    Fetches and caches Stripe pricing IDs using lookup keys.
    The cache is used to avoid repeated Stripe API calls.
    """
    try:
        prices = await stripe.Price.list_async(active=True, expand=['data.product'], limit=20)
        mapping = {}
        reverse_mapping = {}
        
        for price in prices.data:
            lookup_key = price.lookup_key
            if lookup_key in PLAN_LOOKUP_KEYS.values():
                mapping[lookup_key] = price.id
                reverse_mapping[price.id] = lookup_key
                
        return mapping, reverse_mapping
    except stripe.error.StripeError as e:
        logfire.error(f"Failed to fetch Stripe prices: {str(e)}")
        return {}, {}

async def get_price_id(plan_key: str) -> str:
    """Get Stripe price ID for a given plan key"""
    mapping, _ = await get_pricing_plan_mapping()
    return mapping.get(plan_key)

async def get_plan_key(price_id: str) -> str:
    """Get plan key for a given Stripe price ID"""
    _, reverse_mapping = await get_pricing_plan_mapping()
    return reverse_mapping.get(price_id)

# Update reverse mapping to use actual Stripe price IDs
@async_lru_cache(maxsize=1)
async def get_pricing_plan_reverse_mapping():
    """Get reverse mapping from price ID to plan key, lazily loaded and cached"""
    mapping, _ = await get_pricing_plan_mapping()
    return {v: k for k, v in mapping.items() if v is not None}

PRICING_PLAN_NAMES = {
    "creator_monthly": "Creator Monthly",
    "creator_yearly": "Creator Yearly",
    "creator_legacy_monthly": "Creator (Legacy)",
    "business_monthly": "Business Monthly",
    "business_yearly": "Business Yearly",
    "deployment_monthly": "Deployment Monthly",
    "deployment_yearly": "Deployment Yearly",
}


async def update_subscription_redis_data(
    subscription_id: Optional[str] = None,
    user_id: Optional[str] = None,
    org_id: Optional[str] = None,
    last_invoice_timestamp: Optional[int] = None,
    db: Optional[AsyncSession] = None,
) -> dict:
    """
    Helper function to create and update subscription data in Redis with versioning.
    Always fetches fresh data from Stripe if subscription_id is provided.
    Preserves existing fields if they're not in the new data.
    """
    # Generate Redis key from org_id or user_id
    if not (org_id or user_id):
        raise ValueError("Either org_id or user_id must be provided")
    redis_key = f"plan:{org_id or user_id}"
    
    # Get existing data from Redis
    redis_data = {}
    existing_data = None
    try:
        raw_data = await redisMeta.get(redis_key)
        if raw_data:
            existing_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            # Preserve fields we want to keep
            # if existing_data and "spent" in existing_data:
            #     redis_data["spent"] = existing_data["spent"]
    except Exception as e:
        logfire.error(f"Error fetching existing Redis data: {str(e)}")
            
    # If we have a subscription ID, fetch latest data from Stripe
    stripe_sub = None
    subscription_items = []
    if subscription_id:
        try:
            # Fetch the full subscription object with expanded items
            stripe_sub = await stripe.Subscription.retrieve_async(
                subscription_id,
                expand=["items.data.price.product", "discounts"]
            )
            subscription_items = await get_subscription_items(subscription_id)
            logfire.info(f"Fetched latest subscription data for {subscription_id}")
        except stripe.error.StripeError as e:
            logfire.error(f"Error fetching subscription data: {str(e)}")
            
    # If we have Stripe subscription data, update core fields
    if stripe_sub:
        # Debug logging
        logfire.info(f"Subscription items: {subscription_items}")
        
        # Determine plan from subscription items
        plan = None
        if subscription_items and len(subscription_items) > 0:
            first_item = subscription_items[0]
            plan = await get_plan_key(first_item.price.id)
            logfire.info(f"Got plan from subscription items: {plan}")
            
        # Fall back to existing data if no plan found
        if not plan and existing_data and existing_data.get("plan"):
            plan = existing_data["plan"]
            logfire.info(f"Using existing plan: {plan}")
            
        logfire.info(f"Final determined plan: {plan}")
            
        redis_data.update({
            "status": stripe_sub.get("status"),
            "stripe_customer_id": stripe_sub.get("customer"),
            "subscription_id": stripe_sub.get("id"),
            "trial_end": stripe_sub.get("trial_end"),
            "trial_start": stripe_sub.get("trial_start"),
            "cancel_at_period_end": stripe_sub.get("cancel_at_period_end"),
            "current_period_start": stripe_sub.get("current_period_start"),
            "current_period_end": stripe_sub.get("current_period_end"),
            "canceled_at": stripe_sub.get("canceled_at"),
            "payment_issue": stripe_sub.get("status") in ["past_due", "unpaid"],
            "payment_issue_reason": "Subscription payment is overdue" 
                if stripe_sub.get("status") in ["past_due", "unpaid"] else None,
            "plan": plan,
        })
    
        # Add subscription items
        redis_data["subscription_items"] = [
            {
                "id": item.id,
                "price_id": item.price.id,
                "plan_key": await get_plan_key(item.price.id),
                "unit_amount": item.price.unit_amount,
                "nickname": item.price.nickname,
            }
            for item in subscription_items
            if await get_plan_key(item.price.id) is not None
        ]
        
        # Calculate charges with discounts
        if stripe_sub.get("discounts"):
            charges = []
            for item in subscription_items:
                if not await get_plan_key(item.price.id):
                    continue
                    
                base_amount = item.price.unit_amount
                if base_amount is None:
                    continue
                    
                final_amount = float(base_amount)
                
                # Apply subscription-level discounts
                for discount in stripe_sub.get("discounts", []):
                    if discount.get("coupon", {}).get("percent_off"):
                        final_amount = final_amount * (1 - discount["coupon"]["percent_off"] / 100)
                    elif discount.get("coupon", {}).get("amount_off"):
                        total_items = len(subscription_items)
                        final_amount = max(0, final_amount - (discount["coupon"]["amount_off"] / total_items))
                            
                charges.append(round(final_amount))
                
            redis_data["charges"] = charges
    
    # Add user/org IDs
    if user_id:
        redis_data["user_id"] = user_id
    if org_id:
        redis_data["org_id"] = org_id
        
    # Handle last_invoice_timestamp
    if last_invoice_timestamp is not None:
        redis_data["last_invoice_timestamp"] = last_invoice_timestamp
    elif "last_invoice_timestamp" not in redis_data and db is not None:
        # If last_invoice_timestamp is missing, try to fetch from subscription table
        try:
            query = (
                select(SubscriptionStatus.last_invoice_timestamp)
                .where(
                    and_(
                        SubscriptionStatus.status != "deleted",
                        or_(
                            and_(
                                SubscriptionStatus.org_id.is_(None),
                                SubscriptionStatus.user_id == user_id,
                            )
                            if not org_id
                            else SubscriptionStatus.org_id == org_id
                        ),
                    )
                )
                .order_by(SubscriptionStatus.created_at.desc())
                .limit(1)
            )
            result = await db.execute(query)
            db_last_invoice = result.scalar_one_or_none()
            
            if db_last_invoice:
                redis_data["last_invoice_timestamp"] = int(db_last_invoice.timestamp())
                logfire.info(f"Retrieved last_invoice_timestamp from subscription table: {db_last_invoice}")
            else:
                logfire.info("No last_invoice_timestamp found in subscription table")
                redis_data["last_invoice_timestamp"] = subscription_items[0].get('created')
        except Exception as e:
            logfire.error(f"Error fetching last_invoice_timestamp from subscription table: {str(e)}")
        
    # Add data version
    redis_data["version"] = 2
    
    # Write to Redis
    await redisMeta.set(redis_key, redis_data)
    
    return redis_data

async def find_stripe_subscription(user_id: str, org_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Helper function to find Stripe subscription by user_id/org_id or email.
    Returns a tuple of (customer_id, subscription_id)
    Ensures proper organization context is maintained when searching by email.
    """
    try:
        # First try to search subscriptions by metadata with exact org context
        if org_id:
            query = f"metadata['orgId']:'{org_id}' AND status:'active'"
        else:
            query = f"metadata['userId']:'{user_id}' AND metadata['orgId']:null AND status:'active'"
            
        # Search for active subscriptions and sort by creation date
        subscriptions = await stripe.Subscription.search_async(
            query=query,
            limit=1,
            expand=["data.customer"],
        )
        if subscriptions.data:
            sub = subscriptions.data[0]
            return sub.customer.id, sub.id
            
        # If not found, try searching customers with exact org context
        # customer_query = f"metadata['userId']:'{user_id}'"
        # if org_id:
        #     customer_query = f"metadata['orgId']:'{org_id}'"
            
        # customers = stripe.Customer.search(
        #     query=customer_query,
        #     limit=1,
        #     expand=["data.subscriptions"]
        # )
        # if customers.data:
        #     customer = customers.data[0]
        #     if customer.subscriptions and customer.subscriptions.data:
        #         # Verify subscription has correct org context
        #         for subscription in customer.subscriptions.data:
        #             if org_id and subscription.metadata.get('orgId') == org_id:
        #                 return customer.id, subscription.id
        #             elif not org_id and not subscription.metadata.get('orgId'):
        #                 return customer.id, subscription.id
        #     return customer.id, None    
            
        # Email search as last resort, but only if we have user_id and no org_id
        # This prevents org context mixup since org subscriptions should be found by metadata
        # if user_id and user_id.startswith("user_") and not org_id:
        #     try:
        #         user_data = await get_clerk_user(user_id)
        #         if user_data:
        #             email = next(
        #                 (
        #                     email["email_address"]
        #                     for email in user_data["email_addresses"]
        #                     if email["id"] == user_data["primary_email_address_id"]
        #                 ),
        #                 None,
        #             )
        #             if email:
        #                 # Search Stripe by email but verify user context
        #                 customers = await stripe.Customer.search_async(
        #                     query=f"email:'{email}'",
        #                     limit=10,
        #                     expand=["data.subscriptions"]
        #                 )
        #                 for customer in customers.data:
        #                     # Check customer metadata first
        #                     if customer.metadata.get('userId') == user_id:
        #                         if customer.subscriptions and customer.subscriptions.data:
        #                             # Find subscription without org context
        #                             for subscription in customer.subscriptions.data:
        #                                 if not subscription.metadata.get('orgId'):
        #                                     return customer.id, subscription.id
        #                         return customer.id, None
                                
        #                 # If no exact match found, use first customer but update their metadata
        #                 if customers.data:
        #                     customer = customers.data[0]
        #                     # Update customer metadata to prevent future mixups
        #                     await stripe.Customer.modify_async(
        #                         customer.id,
        #                         metadata={'userId': user_id}
        #                     )
        #                     if customer.subscriptions and customer.subscriptions.data:
        #                         for subscription in customer.subscriptions.data:
        #                             if not subscription.metadata.get('orgId'):
        #                                 # Update subscription metadata
        #                                 await stripe.Subscription.modify_async(
        #                                     subscription.id,
        #                                     metadata={'userId': user_id}
        #                                 )
        #                                 return customer.id, subscription.id
        #                     return customer.id, None
        #     except Exception as e:
        #         logfire.error(f"Error fetching user data from Clerk: {str(e)}")
                
    except stripe.error.StripeError as e:
        logfire.error(f"Error searching Stripe: {str(e)}")
    
    return None, None

async def get_current_plan(
    db: AsyncSession, user_id: str, org_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Get the current subscription plan for a user/org"""
    
    if not user_id:
        return None

    # Check if Stripe is disabled
    if os.getenv("DISABLE_STRIPE") == "true":
        return {
            "stripe_customer_id": "id",
            "user_id": user_id,
            "org_id": org_id,
            "plan": "enterprise",
            "status": "active",
            "subscription_id": "",
            "trial_start": 0,
            "cancel_at_period_end": False,
            "created_at": None,
            "updated_at": None,
            "subscription_item_api_id": "id",
            "subscription_item_plan_id": "id",
            "trial_end": 0,
            "last_invoice_timestamp": None,
        }

    # First try to get from Redis
    redis_key = f"plan:{org_id or user_id}"
    plan_data = None
    
    try:
        raw_data = await redisMeta.get(redis_key)
        if raw_data:
            plan_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except json.JSONDecodeError:
        logfire.error(f"Failed to parse Redis data for key {redis_key}")
        plan_data = None

    # If Redis data is missing or invalid, try to recover from DB and Stripe
    if not plan_data or not plan_data.get("subscription_id"):
        # First try to get customer ID from subscription status table
        query = (
            select(SubscriptionStatus)
            .where(
                and_(
                    SubscriptionStatus.status != "deleted",
                    or_(
                        and_(
                            SubscriptionStatus.org_id.is_(None),
                            SubscriptionStatus.user_id == user_id,
                        )
                        if not org_id
                        else SubscriptionStatus.org_id == org_id
                    ),
                )
            )
            .order_by(SubscriptionStatus.created_at.desc())
            .limit(1)
        )
        result = await db.execute(query)
        subscription = result.scalar_one_or_none()

        # Get customer ID and subscription ID either from DB or by searching
        customer_id = None
        subscription_id = None
        
        if subscription and subscription.stripe_customer_id:
            # This is old data
            customer_id = subscription.stripe_customer_id
            subscription_id = subscription.subscription_id
        else:
            # Try to find customer and subscription by user_id/org_id or email
            customer_id, found_sub_id = await find_stripe_subscription(user_id, org_id)
            if customer_id:
                logfire.info(f"Found Stripe customer {customer_id} by searching")
                if found_sub_id:
                    subscription_id = found_sub_id
                    logfire.info(f"Found Stripe subscription {subscription_id} by searching")

        if customer_id:
            try:
                # If we don't have a subscription ID yet, search for active/trialing ones
                if not subscription_id:
                    # Search for active subscriptions for this customer
                    subscriptions = await stripe.Subscription.list_async(
                        customer=customer_id,
                        status="active",
                        expand=["data.items.data.price.product", "data.discounts"]
                    )
                    
                    if subscriptions.data:
                        # Use the most recent active subscription
                        active_sub = subscriptions.data[0]
                        subscription_id = active_sub.id
                    else:
                        # Also check for trialing subscriptions
                        trial_subs = await stripe.Subscription.list_async(
                            customer=customer_id,
                            status="trialing",
                            expand=["data.items.data.price.product", "data.discounts"]
                        )
                        if trial_subs.data:
                            subscription_id = trial_subs.data[0].id
                
                if subscription_id:
                    # Remove debug print
                    await update_subscription_redis_data(
                        subscription_id=subscription_id,
                        user_id=user_id,
                        org_id=org_id,
                        last_invoice_timestamp=int(subscription.last_invoice_timestamp.timestamp()) if subscription and subscription.last_invoice_timestamp else None,
                        db=db
                    )
            except stripe.error.StripeError as e:
                logfire.error(f"Error fetching Stripe subscriptions: {str(e)}")
                return None

    if not plan_data:
        return None

    # Get the first subscription item as the main plan
    subscription_items = plan_data.get("subscription_items", [])
    main_plan = next(iter(subscription_items), {}) if subscription_items else {}
    
    return {
        "stripe_customer_id": plan_data.get("stripe_customer_id"),
        "user_id": plan_data.get("user_id"),
        "org_id": plan_data.get("org_id"),
        "plan": main_plan.get("plan_key", plan_data.get("plan", "free")),  # Fallback to old plan field
        "status": plan_data.get("status"),
        "subscription_id": plan_data.get("subscription_id"),
        "trial_start": plan_data.get("trial_start"),
        "cancel_at_period_end": plan_data.get("cancel_at_period_end", False),
        "created_at": plan_data.get("current_period_start"),
        "updated_at": None,
        "subscription_item_api_id": main_plan.get("id"),
        "subscription_item_plan_id": main_plan.get("price_id"),
        "trial_end": plan_data.get("trial_end"),
        "last_invoice_timestamp": plan_data.get("last_invoice_timestamp"),
    }


async def get_stripe_plan(
    db: AsyncSession, user_id: str, org_id: str
) -> Optional[Dict]:
    """Get the Stripe plan details for a user"""
    # First get the current plan (you'll need to implement this based on your DB schema)
    plan = await get_current_plan(db, user_id, org_id)

    if not plan or not plan.get("subscription_id"):
        return None

    try:
        stripe_plan = await stripe.Subscription.retrieve_async(
            plan["subscription_id"], expand=["discounts"]
        )
        return stripe_plan
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


def get_last_invoice_timestamp_from_autumn(autumn_data: dict) -> Optional[datetime]:
    """
    Extract the last invoice timestamp from Autumn API data.
    Returns None if no invoices are found or if timestamp is invalid.
    Returns a datetime object if found.
    """
    if not autumn_data or "invoices" not in autumn_data:
        return None
        
    invoices = autumn_data.get("invoices", [])
    if not invoices:
        return None
        
    # Sort invoices by created_at timestamp in descending order
    sorted_invoices = sorted(
        invoices,
        key=lambda x: x.get("created_at", 0),
        reverse=True
    )
    
    # Get the timestamp of the most recent invoice
    latest_timestamp = sorted_invoices[0].get("created_at") / 1000
    if latest_timestamp is None:
        return None
        
    # Convert timestamp to datetime with validation
    return int(latest_timestamp)

async def get_plans(
    db: AsyncSession,
    user_id: str,
    org_id: str,
) -> Dict[str, Any]:
    """Get plans information including current subscription details"""

    # Get current plan data first so we can use it for fallback
    # Fetch subscription status directly from the database

    # Check if Autumn is enabled
    if AUTUMN_API_KEY:
        try:
            # Get the transformed data from cache or fetch and transform from Autumn
            transformed_data = await get_customer_plan_cached_5_seconds(org_id or user_id) #get_customer_plan(request, background_tasks)
            if transformed_data:
                logfire.info(
                    f"Successfully fetched transformed data for customer {org_id or user_id}"
                )
                
                # If last_invoice_timestamp is None, try to get it from subscription
                if transformed_data.get("last_invoice_timestamp") is None:
                    subscription_query = (
                        select(SubscriptionStatus)
                        .where(
                            and_(
                                SubscriptionStatus.status != "deleted",
                                or_(
                                    and_(
                                        SubscriptionStatus.org_id.is_(None),
                                        SubscriptionStatus.user_id == user_id,
                                    )
                                    if not org_id
                                    else SubscriptionStatus.org_id == org_id
                                ),
                            )
                        )
                        .order_by(SubscriptionStatus.created_at.desc())
                        .limit(1)
                    )
                    result = await db.execute(subscription_query)
                    subscription = result.scalar_one_or_none()
                    
                    if subscription and subscription.last_invoice_timestamp:
                        transformed_data["last_invoice_timestamp"] = int(
                            subscription.last_invoice_timestamp.timestamp()
                        )
                
                # Return the transformed data directly
                return transformed_data
                
        except Exception as e:
            logfire.error(f"Error calling Autumn API: {str(e)}")

    # Check if Stripe is disabled
    if os.getenv("DISABLE_STRIPE") == "true":
        return {
            "plans": ["creator"],
            "names": ["creator"],
            "prices": [os.getenv("STRIPE_PR_BUSINESS")],
            "amount": [100],
            "cancel_at_period_end": False,
            "canceled_at": None,
        }

    return {
        "plans": [],
        "names": [],
        "prices": [],
        "amount": [],
        "charges": [],
        "cancel_at_period_end": False,
        "canceled_at": None,
        "payment_issue": False,
        "payment_issue_reason": "",
    }


DEFAULT_FEATURE_LIMITS = {
    "free": {"machine": 1, "workflow": 2, "private_model": False},
    "pro": {"machine": 5, "workflow": 10, "private_model": True},
    "creator": {"machine": 10, "workflow": 30, "private_model": True},
    "business": {"machine": 20, "workflow": 100, "private_model": True},
    "enterprise": {"machine": 100, "workflow": 300, "private_model": True},
}

# Map plan keys to feature sets
PLAN_FEATURE_MAPPING = {
    # Legacy plans
    "creator_legacy_monthly": "creator",  # old pro plan
    
    # Current plans
    "creator_monthly": "creator",
    "creator_yearly": "creator",
    "business_monthly": "business",
    "business_yearly": "business",
    "deployment_monthly": "business",  # deployment plans have same limits as business
    "deployment_yearly": "business",
}

def get_feature_set_for_plan(plan_key: str) -> str:
    """Get the feature set name for a given plan key"""
    return PLAN_FEATURE_MAPPING.get(plan_key, "free")

async def get_machine_count(db: AsyncSession, request: Request) -> int:
    query = (
        select(func.count())
        .select_from(Machine)
        .where(~Machine.deleted)
        .apply_org_check_by_type(Machine, request)
    )
    result = await db.execute(query)
    return result.scalar() or 0


async def get_workflow_count(db: AsyncSession, request: Request) -> int:
    query = (
        select(func.count())
        .select_from(Workflow)
        .where(~Workflow.deleted)
        .apply_org_check_by_type(Workflow, request)
    )
    result = await db.execute(query)
    return result.scalar() or 0


@router.get("/platform/plan")
async def get_api_plan(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    # Get authenticated user info
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")

    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get plans, machines, workflows and user settings in parallel
    plans = await get_plans(db, user_id, org_id)
    machine_count = await get_machine_count(db, request)
    workflow_count = await get_workflow_count(db, request)
    user_settings = await get_user_settings_util(request, db)

    # Handle case where plans is None
    if plans is None:
        plans = {"plans": []}  # Provide default empty plans

    # Check if user has subscription by checking if any plan maps to a paid feature set
    # has_subscription = any(
    #     get_feature_set_for_plan(p) in ["creator", "business"] 
    #     for p in plans.get("plans", [])
    # )

    # Calculate limits based on plan
    # Get the highest tier feature set from all active plans
    feature_sets = [get_feature_set_for_plan(p) for p in plans.get("plans", [])]
    if "business" in feature_sets:
        machine_max_count = DEFAULT_FEATURE_LIMITS["business"]["machine"]
        workflow_max_count = DEFAULT_FEATURE_LIMITS["business"]["workflow"]
    elif "creator" in feature_sets:
        machine_max_count = DEFAULT_FEATURE_LIMITS["creator"]["machine"]
        workflow_max_count = DEFAULT_FEATURE_LIMITS["creator"]["workflow"]
    else:
        machine_max_count = DEFAULT_FEATURE_LIMITS["free"]["machine"]
        workflow_max_count = DEFAULT_FEATURE_LIMITS["free"]["workflow"]

    # Use updated limits if available
    effective_machine_limit = max(user_settings.machine_limit or 0, machine_max_count)
    effective_workflow_limit = max(
        user_settings.workflow_limit or 0, workflow_max_count
    )

    machine_limited = machine_count >= effective_machine_limit
    workflow_limited = workflow_count >= effective_workflow_limit

    # Get feature set for current plan
    plan_keys = plans.get("plans", [])
    current_plan_key = plan_keys[0] if plan_keys and len(plan_keys) > 0 else "free"
    feature_set = get_feature_set_for_plan(current_plan_key)
    target_plan = DEFAULT_FEATURE_LIMITS.get(feature_set, DEFAULT_FEATURE_LIMITS["free"])

    return {
        # "sub": plan,
        "features": {
            "machineLimited": machine_limited,
            "machineLimit": effective_machine_limit,
            "currentMachineCount": machine_count,
            "workflowLimited": workflow_limited,
            "workflowLimit": effective_workflow_limit,
            "currentWorkflowCount": workflow_count,
            "priavteModels": target_plan["private_model"],
            "alwaysOnMachineLimit": user_settings.always_on_machine_limit,
        },
        "plans": plans,
    }


@router.get("/platform/upgrade-plan")
async def get_upgrade_plan(
    request: Request,
    plan: str,
    coupon: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get upgrade or new plan details with proration calculations"""
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")
    
    if plan in ["creator", "business", "deployment"]:
        plan = f"{plan}_monthly"

    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get current stripe plan
    stripe_plan = await get_stripe_plan(db, user_id, org_id)
    if not stripe_plan:
        return None

    # Get target price ID
    target_price_id = await get_price_id(plan)
    if not target_price_id:
        raise HTTPException(status_code=400, detail="Invalid plan type: " + plan)

    # Handle coupon if provided
    promotion_code_id = None
    if coupon:
        try:
            promotion_codes = await stripe.PromotionCode.list_async(code=coupon, limit=1)
            if promotion_codes.data:
                promotion_code_id = promotion_codes.data[0].id
            else:
                raise HTTPException(status_code=400, detail="Invalid coupon")
        except stripe.error.StripeError as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        # Check if plan already exists
        has_target_price = any(
            item.get("price", {}).get("id") == target_price_id
            for item in stripe_plan.get("items", {}).get("data", [])
        )
        if has_target_price:
            return None

        # Get all price IDs first to avoid multiple awaits in list comprehension
        price_ids = []
        for plan_key in [
            "creator_monthly",
            "creator_yearly", 
            "business_monthly",
            "business_yearly",
            "deployment_monthly",
            "deployment_yearly",
            "creator_legacy_monthly"
        ]:
            price_id = await get_price_id(plan_key)
            if price_id:
                price_ids.append(price_id)

        # Find matching plan
        api_plan = next(
            (
                item
                for item in stripe_plan.get("items", {}).get("data", [])
                if item.get("price", {}).get("id") in price_ids
            ),
            None,
        )

        # Calculate proration
        if api_plan:
            return await stripe.Invoice.upcoming_async(
                subscription=stripe_plan.id,
                subscription_details={
                    "proration_behavior": "always_invoice",
                    "items": [
                        {"id": api_plan.id, "deleted": True},
                        {"price": target_price_id, "quantity": 1},
                    ],
                },
                discounts=[{"promotion_code": promotion_code_id}]
                if promotion_code_id
                else [],
            )
        else:
            return await stripe.Invoice.upcoming_async(
                subscription=stripe_plan.id,
                subscription_details={
                    "proration_behavior": "always_invoice",
                    "items": [
                        {"price": target_price_id, "quantity": 1},
                    ],
                },
                discounts=[{"promotion_code": promotion_code_id}]
                if promotion_code_id
                else [],
            )

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return None

clerk_api_key = os.getenv("CLERK_SECRET_KEY")

async def get_clerk_user(user_id: str) -> dict:
    """
    Fetch user data from Clerk's Backend API

    Args:
        user_id: The Clerk user ID

    Returns:
        dict: User data from Clerk

    Raises:
        HTTPException: If the API call fails
    """
    headers = {
        "Authorization": f"Bearer {clerk_api_key}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.clerk.com/v1/users/{user_id}", headers=headers
        ) as response:
            if response.status == 200:
                return await response.json()
            raise HTTPException(
                status_code=400,
                detail=f"Failed to fetch user data from Clerk: {await response.text()}",
            )

async def get_clerk_org(org_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {clerk_api_key}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.clerk.com/v1/organizations/{org_id}", headers=headers
        ) as response:
            if response.status == 200:
                return await response.json()
            raise HTTPException(
                status_code=400,
                detail=f"Failed to fetch org data from Clerk: {await response.text()}",
            )

async def slugify(workflow_name: str, current_user_id: str, from_nanoid: bool = True) -> str:
    if from_nanoid:
        # Use only alphanumeric characters (no underscores)
        slug_part = generate(size=16, alphabet='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
    else:
        slug_part = re.sub(
                r'[-\s]+',                                # Replace spaces or multiple dashes with single dash
                '-',
                re.sub(
                r'[^\w\s-]',                          # Remove special chars
                '',
                unicodedata.normalize('NFKD', workflow_name)    # Normalize Unicode
                .encode('ascii', 'ignore')             # Remove non-ascii
                .decode('ascii')                       # Convert back to string
            ).strip().lower()
        ).strip('-')                                  # Remove leading/trailing dashes

    user_part = None
    prefix, _, _ = current_user_id.partition("_")

    if prefix == "org":
        org_data = await get_clerk_org(current_user_id)
        user_part = org_data["slug"].lower()
    elif prefix == "user":
        user_data = await get_clerk_user(current_user_id)
        user_part = user_data["username"].lower()

    return f"{user_part}_{slug_part}"


# Define allowed Stripe webhook events
ALLOWED_STRIPE_EVENTS = [
    "customer.subscription.created",
    "checkout.session.completed",
    "customer.subscription.paused",
    "customer.subscription.resumed",
    "customer.subscription.deleted",
    "customer.subscription.updated",
    "customer.subscription.trial_will_end",
    "invoice.created",
    "invoice.finalized",
    "invoice.paid",
    "payment_intent.succeeded",
    "customer.updated",
    "invoice.payment_succeeded",
    "invoice.updated",
    "charge.succeeded"
]

# PostHog event mapping
POSTHOG_EVENT_MAPPING = {
    "customer.subscription.created": "create_subscription",
    "customer.subscription.paused": "pause_subscription",
    "customer.subscription.resumed": "resume_subscription",
    "customer.subscription.deleted": "delete_subscription",
    "customer.subscription.updated": "update_subscription"
}

async def process_all_active_subscriptions(
    db: AsyncSession,
    dry_run: bool = False,
    send_email: bool = False
) -> List[Dict]:
    processed_subscriptions = []
    try:
        # Get all active subscriptions from Stripe with pagination
        has_more = True
        starting_after = None
        
        while has_more:
            subscriptions = await stripe.Subscription.list_async(
                status="active",
                expand=["data.customer"],
                limit=20,  # Maximum allowed by Stripe
                starting_after=starting_after
            )
            
            logfire.info(f"Processing batch of {len(subscriptions.data)} active subscriptions")
            
            for subscription in subscriptions.data:
                try:
                    # Get metadata from subscription
                    metadata = subscription.metadata
                    user_id = metadata.get("userId")
                    org_id = metadata.get("orgId")
                    
                    if not user_id and not org_id:
                        logfire.warning(f"Subscription {subscription.id} has no user_id or org_id in metadata")
                        continue
                    
                    # Get current plan to ensure Redis data exists and is up to date
                    current_plan = await get_current_plan(db, user_id, org_id)
                    if not current_plan:
                        logfire.warning(f"No plan data found for subscription {subscription.id}")
                        continue
                    
                    # Calculate usage charges
                    final_cost, last_invoice_timestamp = await calculate_usage_charges(
                        user_id=user_id,
                        org_id=org_id,
                        end_time=datetime.now(),
                        db=db,
                        dry_run=dry_run
                    )
                    
                    # Extract only essential customer info
                    customer_info = {
                        "id": subscription.customer.id,
                        "email": subscription.customer.email
                    } if subscription.customer else {"id": None, "email": None}
                    
                    subscription_info = {
                        "subscription_id": subscription.id,
                        "customer": customer_info,
                        "user_id": user_id,
                        "org_id": org_id,
                        "final_cost": final_cost,
                        "last_invoice_timestamp": last_invoice_timestamp
                    }
                    
                    if final_cost > 0:
                        # Convert to cents for Stripe
                        amount = int(final_cost * 100)
                        subscription_info["amount_cents"] = amount
                        
                        if not dry_run:
                            # Create invoice immediately
                            invoice = await stripe.Invoice.create_async(
                                customer=subscription.customer.id,
                                auto_advance=True,
                                collection_method="charge_automatically",
                                subscription=subscription.id,
                            )
                            invoice_item = await stripe.InvoiceItem.create_async(
                                customer=subscription.customer.id,
                                amount=amount,
                                currency="usd",
                                description="GPU Compute Usage",
                                invoice=invoice.id,
                                period={
                                    "start": int(last_invoice_timestamp),
                                    "end": int(datetime.now().timestamp())
                                }
                            )
                            invoice = await stripe.Invoice.finalize_invoice_async(invoice.id)
                            logfire.info(f"Added GPU Compute Usage ({amount} cents) for subscription {subscription.id}")
                        else:
                            logfire.info(f"[DRY RUN] Would add GPU Compute Usage ({amount} cents) for subscription {subscription.id}")
                        
                        if send_email:
                            try:
                                # Get user email from Clerk
                                user_data = None
                                if user_id and user_id.startswith("user_"):
                                    user_data = await get_clerk_user(user_id)
                                elif org_id and org_id.startswith("org_"):
                                    user_data = await get_clerk_org(org_id)
                                    
                                if user_data:
                                    email = None
                                    if "email_addresses" in user_data:
                                        email = next(
                                            (email["email_address"] for email in user_data["email_addresses"]
                                             if email["id"] == user_data["primary_email_address_id"]),
                                            None
                                        )
                                    elif "email" in user_data:
                                        email = user_data["email"]
                                        
                                    if email:
                                        message = f"""
                                        <h2>GPU Usage Invoice</h2>
                                        <p>Here's your GPU usage summary:</p>
                                        <ul>
                                            <li>Total Usage Cost: ${final_cost:.2f}</li>
                                            <li>Period End: {datetime.fromtimestamp(last_invoice_timestamp).strftime('%Y-%m-%d %H:%M:%S')}</li>
                                        </ul>
                                        <p>{'This is a dry run notification.' if dry_run else 'An invoice will be generated for this usage.'}</p>
                                        """
                                        params: resend.Emails.SendParams = {
                                            "from": "Comfy Deploy <billing@comfydeploy.com>",
                                            "to": email,
                                            "subject": "Comfy Deploy - GPU Usage Summary" if final_cost == 0 else "Comfy Deploy - GPU Usage Invoice",
                                            "html": message
                                        }
                                        email_result = resend.Emails.send(params)
                                        subscription_info["email_sent"] = True
                                        subscription_info["email_id"] = email_result["id"]
                                        logfire.info(f"Sent usage email to {email}")
                            except Exception as e:
                                logfire.error(f"Error sending email for subscription {subscription.id}: {str(e)}")
                                subscription_info["email_error"] = str(e)
                    
                    if not dry_run:
                        # Update Redis with new last invoice timestamp
                        await update_subscription_redis_data(
                            subscription_id=subscription.id,
                            user_id=user_id,
                            org_id=org_id,
                            last_invoice_timestamp=last_invoice_timestamp,
                            db=db
                        )
                    
                    processed_subscriptions.append(subscription_info)
                    
                except Exception as e:
                    logfire.error(f"Error processing subscription {subscription.id}: {str(e)}")
                    continue
            
            has_more = subscriptions.has_more
            if has_more and subscriptions.data:
                starting_after = subscriptions.data[-1].id
                
    except stripe.error.StripeError as e:
        logfire.error(f"Error fetching subscriptions from Stripe: {str(e)}")
        raise
    except Exception as e:
        logfire.error(f"Unexpected error in process_all_active_subscriptions: {str(e)}")
        raise
        
    return processed_subscriptions

# Add Resend client
resend.api_key = os.getenv("RESEND_API_KEY")

@router.post("/platform/stripe/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Stripe webhook events"""
    try:
        # Get the raw request body
        body = await request.body()
        body_str = body.decode('utf-8')
        
        # Get Stripe signature from headers
        stripe_signature = request.headers.get('stripe-signature')
        if not stripe_signature:
            raise HTTPException(status_code=400, detail="No Stripe signature found")
            
        # Verify webhook signature
        webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
        if not webhook_secret:
            raise HTTPException(status_code=500, detail="Webhook secret not configured")
            
        # Update webhook event construction
        event = stripe.Webhook.construct_event(
            body_str,
            stripe_signature,
            webhook_secret
        )
        
        print(f"Webhook event type: {event.get('type')}")
        # print(f"Webhook metadata: {event.get('data', {}).get('object', {}).get('metadata', {})}")
        
        # Use the generic event handler
        await handle_stripe_event(event, db)
        return {"result": event, "ok": True}
        
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error processing webhook: {str(e)}"
        )
        
        
@router.get("/platform/checkout")
async def stripe_checkout(
    request: Request,
    plan: str,
    redirect_url: str = None,
    upgrade: Optional[bool] = False,
    trial: Optional[bool] = False,
    coupon: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle Stripe checkout process"""
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")
    
    if plan in ["creator", "business", "deployment"]:
        plan = f"{plan}_monthly"
        
    if not user_id:
        return {"url": redirect_url or "/"}
    if not plan:
        return {"url": redirect_url or "/pricing"}

    # Get user data from Clerk
    user_data = await get_clerk_user(user_id)
    user_email = next(
        (
            email["email_address"]
            for email in user_data["email_addresses"]
            if email["id"] == user_data["primary_email_address_id"]
        ),
        None,
    )
    
    # Call Autumn API to attach customer
    if AUTUMN_API_KEY:
        try:
            print(f"Attaching customer {user_id} to Autumn")
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {AUTUMN_API_KEY}",
                    "Content-Type": "application/json"
                }
                
                autumn_payload = {
                    "customer_id": org_id if org_id else user_id,
                    "product_id": plan,  # Using the plan as product_id
                    "force_checkout": not upgrade,
                    "customer_data": {
                        "name": (user_data.get("first_name") or "") + " " + (user_data.get("last_name") or ""),
                        "email": user_email,
                    }
                }
                
                async with session.post(
                    f"{AUTUMN_API_URL}/attach",
                    headers=headers,
                    json=autumn_payload
                ) as response:
                    response_text = await response.text()
                    
                    try:
                        response_data = json.loads(response_text)
                    except json.JSONDecodeError:
                        response_data = {}
                    
                    if response.status != 200:
                        logfire.error(f"Failed to attach customer to Autumn: {response_text}")
                        
                        # Check if error is because customer already has the product
                        error_code = response_data.get("code", "")
                        if error_code == "customer_already_has_product" or "customer_already_has_product" in response_text:
                            # Customer already has product, get billing portal URL instead
                            logfire.info(f"Customer already has product. Redirecting to billing portal.")
                            
                            customer_id = org_id if org_id else user_id
                            async with session.get(
                                f"{AUTUMN_API_URL}/customers/{customer_id}/billing_portal",
                                headers=headers
                            ) as portal_response:
                                portal_text = await portal_response.text()
                                try:
                                    portal_data = json.loads(portal_text)
                                    portal_url = portal_data.get("url")
                                    if portal_url:
                                        return {"url": portal_url}
                                except json.JSONDecodeError:
                                    logfire.error(f"Failed to parse billing portal response: {portal_text}")
                                
                                logfire.error(f"Failed to get billing portal URL: {portal_text}")
                    
                        return {"error": response_text}
                    else:
                        logfire.info(f"Successfully attached customer {user_id} to Autumn")
                        print(response_data)
                        if response_data.get("checkout_url"):
                            return {"url": response_data.get("checkout_url")}
                        else:
                            return response_data
        except Exception as e:
            logfire.error(f"Error calling Autumn API: {str(e)}")
            

def r(price: float) -> float:
    return round(price * 1.1, 6)


# GPU pricing per second with 10% margin (H200/B200 use 50% margin)
PRICING_LOOKUP_TABLE = {
    "T4": r(0.000164),
    "L4": r(0.000291),
    "A10G": r(0.000306),
    "L40S": r(0.000542),
    "A100": r(0.001036),
    "A100-80GB": r(0.001553),
    "H100": r(0.002125),
    "H200": round(0.001261 * 1.5, 6),  # 50% markup
    "B200": round(0.001736 * 1.5, 6),  # 50% markup
    "CPU": r(0.000038),
}

FREE_TIER_USAGE = 500  # in cents, $5

@router.get("/platform/gpu-pricing")
async def gpu_pricing():
    """Return the GPU pricing table"""
    return PRICING_LOOKUP_TABLE

@router.get("/platform/usage-details")
async def get_usage_details_by_day(
    request: Request,
    start_time: datetime,
    end_time: datetime,
    db: AsyncSession = Depends(get_db),
):
    """Get GPU usage details grouped by day"""
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")

    if not user_id and not org_id:
        raise HTTPException(status_code=400, detail="User or org id is required")

    # Build the base query conditions
    conditions = [
        GPUEvent.end_time >= start_time,
        GPUEvent.end_time < end_time,
    ]

    # Add org/user conditions
    if org_id:
        conditions.append(GPUEvent.org_id == org_id)
    else:
        conditions.append(
            and_(
                or_(GPUEvent.org_id.is_(None), GPUEvent.org_id == ""),
                GPUEvent.user_id == user_id,
            )
        )

    # Create the query
    query = (
        select(
            cast(GPUEvent.start_time, Date).label("date"),
            GPUEvent.gpu,
            func.sum(
                extract("epoch", GPUEvent.end_time)
                - extract("epoch", GPUEvent.start_time)
            ).label("usage_in_sec"),
            func.coalesce(func.sum(GPUEvent.cost), 0).label("cost"),
        )
        .where(and_(*conditions))
        .group_by(text("date"), GPUEvent.gpu)
        .order_by(text("date"))
    )

    result = await db.execute(query)
    usage_details = result.fetchall()

    # Transform the data into the desired format
    grouped_by_date = {}
    for row in usage_details:
        if row.date is None:
            continue  # Skip rows with None date
            
        date_str = row.date.strftime("%Y-%m-%d")
        if date_str not in grouped_by_date:
            grouped_by_date[date_str] = {}

        if row.gpu:
            unit_amount = PRICING_LOOKUP_TABLE.get(row.gpu, 0)
            usage_seconds = float(row.usage_in_sec) if row.usage_in_sec is not None else 0  # Also handle None usage_in_sec
            grouped_by_date[date_str][row.gpu] = unit_amount * usage_seconds

    # Convert to array format
    chart_data = [
        {"date": date, **gpu_costs} for date, gpu_costs in grouped_by_date.items()
    ]

    return chart_data


async def get_usage_details(
    db: AsyncSession,
    start_time: datetime,
    end_time: datetime,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict]:
    """Get usage details for a given time period"""
    if not user_id and not org_id:
        raise ValueError("User or org id is required")

    # Build the query conditions
    conditions = [
        GPUEvent.end_time >= start_time,
        GPUEvent.end_time < end_time,
    ]

    # Add org/user conditions
    if org_id:
        conditions.append(GPUEvent.org_id == org_id)
    else:
        conditions.append(
            and_(
                or_(GPUEvent.org_id.is_(None), GPUEvent.org_id == ""),
                GPUEvent.user_id == user_id,
            )
        )
        
    # Filter out public share workflows
    # conditions.append(
    #     or_(
    #         GPUEvent.environment != "public-share",
    #         GPUEvent.environment == None
    #     )
    # )

    # Create the query
    query = (
        select(
            GPUEvent.machine_id,
            Machine.name.label("machine_name"),
            GPUEvent.gpu,
            GPUEvent.ws_gpu,
            func.sum(
                extract("epoch", GPUEvent.end_time)
                - extract("epoch", GPUEvent.start_time)
            ).label("usage_in_sec"),
            GPUEvent.cost_item_title,
            func.sum(func.coalesce(GPUEvent.cost, 0)).label("cost"),
        )
        .select_from(GPUEvent)
        .outerjoin(Machine, GPUEvent.machine_id == Machine.id)
        .where(and_(*conditions))
        .group_by(
            GPUEvent.machine_id,
            Machine.name,
            GPUEvent.gpu,
            GPUEvent.ws_gpu,
            GPUEvent.cost_item_title,
        )
        .order_by(desc("usage_in_sec"))
    )

    result = await db.execute(query)
    usage_details = result.fetchall()

    # Convert to list of dicts
    return [
        {
            "machine_id": row.machine_id,
            "machine_name": row.machine_name,
            "gpu": row.gpu,
            "ws_gpu": row.ws_gpu,
            "usage_in_sec": float(row.usage_in_sec) if row.usage_in_sec is not None else 0,
            "cost_item_title": row.cost_item_title,
            "cost": float(row.cost) if row.cost else (
                # Calculate cost based on GPU type if row.cost is not available
                (float(row.usage_in_sec) / 3600) if row.ws_gpu and row.usage_in_sec is not None else  # Workspace GPU cost
                (PRICING_LOOKUP_TABLE.get(row.gpu, 0) * float(row.usage_in_sec) if row.gpu and row.usage_in_sec is not None else 0)  # Regular GPU cost
            ),
        }
        for row in usage_details
    ]


def get_gpu_event_cost(event: Dict) -> float:
    """Calculate cost for a GPU event"""
    if event.get("cost_item_title") and event.get("cost") is not None:
        return event["cost"]

    if event.get("ws_gpu"):
        return event["usage_in_sec"] / 3600

    gpu = event.get("gpu")
    if not gpu or gpu not in PRICING_LOOKUP_TABLE:
        return 0

    return PRICING_LOOKUP_TABLE[gpu] * event["usage_in_sec"]


@router.get("/platform/usage")
async def get_usage(
    request: Request,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get usage details and total cost for a given time period"""
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")

    if not user_id and not org_id:
        raise HTTPException(status_code=400, detail="User or org id is required")

    # Get current plan to get last invoice timestamp
    current_plan = await get_current_plan(db, user_id, org_id)

    # If no subscription or last invoice timestamp, get user creation date
    user_created_at = None
    if not current_plan or not current_plan.get("last_invoice_timestamp"):
        query = select(User.created_at).where(User.id == user_id)
        result = await db.execute(query)
        user_created_at = result.scalar_one_or_none()

    # Determine start time based on billing period
    effective_start_time = (
        start_time
        or (datetime.fromtimestamp(current_plan["last_invoice_timestamp"]) if current_plan and current_plan.get("last_invoice_timestamp") else None)
        or user_created_at
        or datetime.now()
    )

    effective_end_time = end_time or datetime.now()

    # Get usage details
    usage_details = await get_usage_details(
        db=db,
        start_time=effective_start_time,
        end_time=effective_end_time,
        org_id=org_id,
        user_id=user_id
    )
    
    user_settings = await get_user_settings_util(request, db)

    # Calculate total cost
    total_cost = sum(get_gpu_event_cost(event) for event in usage_details)

    # Apply free tier credit ($5 = 500 cents)
    final_cost = max(total_cost - FREE_TIER_USAGE / 100, 0)
    
    # Apply user settings credit if available
    credit_to_apply = 0
    if user_settings and user_settings.credit:
        credit_to_apply = min(user_settings.credit, final_cost)
        final_cost = max(final_cost - credit_to_apply, 0)

    return {
        "usage": usage_details,
        "total_cost": total_cost,
        "final_cost": final_cost,
        "free_tier_credit": FREE_TIER_USAGE,  # Convert to dollars
        "credit": user_settings.credit,
        "period": {
            "start": effective_start_time.isoformat(),
            "end": effective_end_time.isoformat(),
        },
    }


@router.get("/platform/invoices")
async def get_monthly_invoices(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")

    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Use the new Autumn-based cache directly
    plan_data = await get_customer_plan_cached_5_seconds(org_id or user_id)
    subscription_id = plan_data.get("subscription_id") if plan_data else None
    # Fallback: try to extract from autumn_data if not present
    if not subscription_id and plan_data and plan_data.get("autumn_data"):
        products = plan_data["autumn_data"].get("products", [])
        if products and products[0].get("subscription_ids"):
            subscription_ids = products[0]["subscription_ids"]
            if subscription_ids:
                subscription_id = subscription_ids[0]
    if not subscription_id:
        return []

    try:
        invoices = await stripe.Invoice.list_async(
            subscription=subscription_id,
            limit=12,
            expand=["data.lines"],
        )
        # Transform invoice data
        return [
            {
                "id": invoice.id,
                "period_start": datetime.fromtimestamp(invoice.period_start).strftime("%Y-%m-%d"),
                "period_end": datetime.fromtimestamp(invoice.period_end).strftime("%Y-%m-%d"),
                "period_start_timestamp": invoice.period_start,
                "period_end_timestamp": invoice.period_end,
                "amount_due": invoice.amount_due / 100,  # Convert cents to dollars
                "status": invoice.status,
                "invoice_pdf": invoice.invoice_pdf,
                "hosted_invoice_url": invoice.hosted_invoice_url,
                "line_items": [
                    {
                        "description": item.description,
                        "amount": item.amount / 100,
                        "quantity": item.quantity,
                    }
                    for item in invoice.lines.data
                ],
                "subtotal": invoice.subtotal / 100,
                "total": invoice.total / 100,
            }
            for invoice in invoices.data
        ]

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/platform/stripe/dashboard")
async def get_dashboard_url(
    request: Request,
    redirect_url: str = None,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    headers = {
        "Authorization": f"Bearer {AUTUMN_API_KEY}",
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{AUTUMN_API_URL}/customers/{org_id or user_id}/billing_portal",
            headers=headers
        ) as portal_response:
            portal_text = await portal_response.text()
            try:
                portal_data = json.loads(portal_text)
                portal_url = portal_data.get("url")
                if portal_url:
                    return {"url": portal_url}
            except json.JSONDecodeError:
                logfire.error(f"Failed to parse billing portal response: {portal_text}")
        
        logfire.error(f"Failed to get billing portal URL: {portal_text}")
        
    return

    # Get user's subscription
    sub = await get_current_plan(
        db=db,
        org_id=org_id,
        user_id=user_id
    )
    
    if not sub:
        raise HTTPException(status_code=404, detail="No subscription found")

    # Create Stripe billing portal session
    session = await stripe.billing_portal.Session.create_async(
        customer=sub["stripe_customer_id"],
        return_url=redirect_url
    )

    return JSONResponse({
        "url": session.url
    })

async def get_subscription_items(subscription_id: str) -> List[Dict]:
    """Helper function to get subscription items from Stripe"""
    try:
        items = await stripe.SubscriptionItem.list_async(
            subscription=subscription_id,
            limit=5
        )
        return items.data  # Return just the data array
    except stripe.error.StripeError as e:
        print(f"Error fetching subscription items: {e}")
        return []

async def handle_stripe_event(event: dict, db: AsyncSession):
    """Generic event handler for Stripe webhook events"""
    event_type = event.get("type")
    if event_type not in ALLOWED_STRIPE_EVENTS:
        print(f"Unhandled event type: {event_type}")
        return
        
    event_object = event.get("data", {}).get("object", {})
    
    # Extract common fields
    customer_id = event_object.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
        
    # For invoice events, get the subscription from the invoice
    subscription_id = None
    if event_type.startswith("invoice."):
        subscription_id = event_object.get("subscription")
        if subscription_id:
            try:
                # Fetch the full subscription object
                subscription = await stripe.Subscription.retrieve_async(subscription_id)
                event_object = subscription
            except stripe.error.StripeError as e:
                logfire.error(f"Error fetching subscription from invoice: {str(e)}")
                return
    else:
        subscription_id = event_object.get("id")
    
    metadata = event_object.get("metadata", {})
    
    # If metadata is empty in the event object, try to get it from the subscription
    if not metadata and customer_id:
        try:
            # Get customer to find associated subscription
            customer = await stripe.Customer.retrieve_async(customer_id, expand=['subscriptions'])
            if customer.get('subscriptions') and customer.subscriptions.data:
                latest_sub = customer.subscriptions.data[0]
                metadata = latest_sub.metadata
                if not subscription_id:
                    subscription_id = latest_sub.id
                    event_object = latest_sub
        except stripe.error.StripeError as e:
            logfire.error(f"Error fetching customer data: {str(e)}")
    
    user_id = metadata.get("userId")
    org_id = metadata.get("orgId")
    
    # Skip if we can't identify the user/org
    if not user_id and not org_id:
        logfire.error(f"Could not identify user/org from event: {event_type}")
        return
    
    try:
        await update_subscription_redis_data(
            subscription_id=subscription_id,
            user_id=user_id,
            org_id=org_id,
            # last_invoice_timestamp=int(event_object.get("current_period_end")) if event_type == "invoice.finalized" else None,
            db=db
        )
        logfire.info(f"Updated Redis data for plan:{org_id or user_id} after {event_type}")
    except Exception as e:
        logfire.error(f"Error updating Redis data: {str(e)}")

async def calculate_usage_charges(
    user_id: Optional[str] = None,
    org_id: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    db: Optional[AsyncSession] = None,
    dry_run: bool = False,
) -> Tuple[float, int]:
    """
    Calculate GPU compute usage charges for a given time period.
    Returns a tuple of (final_cost, last_invoice_timestamp).
    This function doesn't depend on Stripe and can be used for testing.
    
    Args:
        user_id: Optional user ID
        org_id: Optional organization ID
        start_time: Start time for usage calculation
        end_time: End time for usage calculation
        db: Optional database session
        dry_run: If True, simulate the calculation without updating credits
        
    Returns:
        Tuple[float, int]: (final_cost, last_invoice_timestamp)
        final_cost is in dollars (not cents)
        last_invoice_timestamp is Unix timestamp
    """
    if not user_id and not org_id:
        raise ValueError("Either user_id or org_id must be provided")
        
    if not db:
        async with AsyncSession(get_db()) as db:
            return await calculate_usage_charges(user_id, org_id, start_time, end_time, db, dry_run)
            
    # Get current plan to ensure Redis data exists and is up to date
    current_plan = await get_current_plan(db, user_id, org_id)
    if not current_plan:
        logfire.warning(f"No plan data found for user {user_id} or org {org_id}")
        return 0, int(datetime.now().timestamp())
            
    # If start_time not provided, use last_invoice_timestamp from current plan
    if not start_time and current_plan.get("last_invoice_timestamp"):
        start_time = datetime.fromtimestamp(current_plan["last_invoice_timestamp"])
            
    # If still no start_time, use subscription start or user creation date
    if not start_time:
        query = (
            select(SubscriptionStatus)
            .where(
                and_(
                    SubscriptionStatus.status != "deleted",
                    or_(
                        and_(
                            SubscriptionStatus.org_id.is_(None),
                            SubscriptionStatus.user_id == user_id,
                        )
                        if not org_id
                        else SubscriptionStatus.org_id == org_id
                    ),
                )
            )
            .order_by(SubscriptionStatus.created_at.desc())
            .limit(1)
        )
        result = await db.execute(query)
        subscription = result.scalar_one_or_none()
        
        if subscription and subscription.last_invoice_timestamp:
            start_time = subscription.last_invoice_timestamp
        else:
            # Try user creation date
            query = select(User.created_at).where(User.id == user_id)
            result = await db.execute(query)
            user_created_at = result.scalar_one_or_none()
            start_time = user_created_at or datetime.now()
            
    end_time = end_time or datetime.now()
    
    # Get usage details
    usage_details = await get_usage_details(
        db=db,
        start_time=start_time,
        end_time=end_time,
        org_id=org_id,
        user_id=user_id
    )
    
    # Calculate total cost
    total_cost = sum(get_gpu_event_cost(event) for event in usage_details)
    
    # Apply free tier credit ($5 = 500 cents)
    final_cost = max(total_cost - FREE_TIER_USAGE / 100, 0)

    # Try to get and apply user settings credit if available
    try:
        query = select(UserSettings).where(
            or_(
                and_(
                    UserSettings.org_id.is_(None),
                    UserSettings.user_id == user_id,
                )
                if not org_id
                else UserSettings.org_id == org_id
            )
        ).limit(1)
        result = await db.execute(query)
        user_settings = result.scalar_one_or_none()
        
        if user_settings and user_settings.credit:
            credit_to_apply = min(user_settings.credit, final_cost)
            final_cost = max(final_cost - credit_to_apply, 0)
            
            # Update the remaining credit if not in dry run mode
            if not dry_run and credit_to_apply > 0:
                remaining_credit = max(user_settings.credit - credit_to_apply, 0)
                user_settings.credit = remaining_credit
                await db.commit()
                logfire.info(f"Updated credit for {'org' if org_id else 'user'} {org_id or user_id} from {user_settings.credit} to {remaining_credit}")
            elif dry_run and credit_to_apply > 0:
                logfire.info(f"[DRY RUN] Would update credit for {'org' if org_id else 'user'} {org_id or user_id} from {user_settings.credit} to {max(user_settings.credit - credit_to_apply, 0)}")
                
    except Exception as e:
        logfire.warning(f"Failed to apply user settings credit: {str(e)}")
    
    return final_cost, int(end_time.timestamp())

async def get_customer_plan_v2(
    user_or_org_id: str,
):
    logfire.info(f"Fetching fresh data from Autumn for {user_or_org_id}")
    # Fetch raw data from Autumn
    autumn_data = await get_autumn_customer(user_or_org_id)
    if not autumn_data:
        logfire.warning(f"No data returned from Autumn for {user_or_org_id}")
        return None

    # Transform Autumn data to our format
    plans = []
    names = []
    prices = []
    amounts = []
    charges = []
    cancel_at_period_end = False
    canceled_at = None
    payment_issue = False
    payment_issue_reason = ""

    # Process products and add-ons
    all_products = autumn_data.get("products", []) + autumn_data.get("add_ons", [])

    for product in all_products:
        # Only process active products
        if product.get("status") == "active":
            # Get the plan name directly from product id
            plan_key = product.get("id", "")
            if plan_key:  # Only add if we have a plan key
                plans.append(plan_key)
                names.append(product.get("name", ""))

                # Process pricing information
                product_prices = product.get("prices", [])
                if product_prices:
                    # Use the first price entry
                    sorted_prices = sorted(
                        product_prices,
                        key=lambda x: x.get("amount", 0),
                        reverse=True
                    )
                    price_info = sorted_prices[0]
                    price_amount = price_info.get("amount", 0)

                    # Add price ID and amount
                    prices.append(plan_key)  # Using product ID as price ID
                    amounts.append(price_amount * 100)

                    # Calculate charges - using price amount directly for now
                    charges.append(price_amount * 100)

        # Check for canceled products for cancel status
        product_canceled_at = product.get("canceled_at")
        if product.get("status") == "canceled" or product_canceled_at:
            cancel_at_period_end = True
            canceled_at = product_canceled_at

    # Extract payment issues
    if any(product.get("status") not in ["active", "canceled"] for product in all_products):
        payment_issue = True
        payment_issue_reason = "Payment issue with subscription"

    # Check if we have invoices data to compute charges
    invoices = autumn_data.get("invoices", [])
    if invoices and not charges:  # Use invoice data if available and charges is empty
        for invoice in invoices:
            if invoice.get("status") == "paid":
                charges.append(invoice.get("total", 0))

    # --- Stripe ID Extraction from Autumn ---
    stripe_customer_id = autumn_data.get("stripe_id")
    stripe_subscription_id = None
    products = autumn_data.get("products", [])
    if products and products[0].get("subscription_ids"):
        # Use the first subscription_id from the first product
        stripe_subscription_id = products[0]["subscription_ids"][0]
    # --- End Stripe ID Extraction ---

    # Create the transformed data structure
    transformed_data = {
        "plans": plans,
        "names": names,
        "prices": prices,
        "amount": amounts,
        "charges": charges,
        "cancel_at_period_end": cancel_at_period_end,
        "canceled_at": canceled_at,
        "payment_issue": payment_issue,
        "payment_issue_reason": payment_issue_reason,
        "autumn_data": autumn_data,  # Keep the original data for reference
        "last_invoice_timestamp": get_last_invoice_timestamp_from_autumn(autumn_data),
        "source": "autumn",
        "last_update": int(datetime.now().timestamp()),
        # --- Stripe IDs for downstream use ---
        "stripe_customer_id": stripe_customer_id,
        "subscription_id": stripe_subscription_id,
    }

    # try:
    #     await redisMeta.set(redis_key, transformed_data)
    #     logfire.info(f"Cached transformed Autumn data in Redis for {user_or_org_id}")
    # except Exception as e:
    #     logfire.error(f"Error updating {user_or_org_id} plan in Redis: {str(e)}")

    return transformed_data


@multi_level_cached(
    key_prefix="plan",
    # Time for local memory cache to refresh from redis
    ttl_seconds=60,
    # Time for redis to refresh from source (autumn)
    redis_ttl_seconds=120,
    version="1.0",
    key_builder=lambda user_id_or_org_id: f"plan:{user_id_or_org_id}",
)
async def get_customer_plan_cached(user_id_or_org_id: str):
    with logfire.span("get_customer_plan"):
        return await get_customer_plan_v2(user_id_or_org_id)
    
@multi_level_cached(
    key_prefix="plan",
    # Time for local memory cache to refresh from redis
    ttl_seconds=2,
    # Time for redis to refresh from source (autumn)
    redis_ttl_seconds=5,
    version="1.0",
    key_builder=lambda user_id_or_org_id: f"plan:{user_id_or_org_id}",
)
async def get_customer_plan_cached_5_seconds(user_id_or_org_id: str):
    with logfire.span("get_customer_plan"):
        return await get_customer_plan_v2(user_id_or_org_id)

@router.patch("/platform/seats")
async def update_seats(
    request: Request,
):
    plan = request.state.current_user["plan"]
    if plan is None or plan == "free":
        raise HTTPException(
            status_code=401, detail="Unauthorized"
        )

    org_id = request.state.current_user["org_id"]
    if not org_id:
        raise HTTPException(
            status_code=403, detail="This is only available for organizations"
        )
    
    # Extract plan type from the full plan name
    plan_type = plan.split("_")[0] if "_" in plan else plan
    
    # Creator plans don't need seat updates
    if plan_type == "creator":
        return {"status": "success", "message": "No seat update needed for creator plan"}
    
    # Set minimum seats based on plan type
    minimum_seats = 10 if plan_type == "business" else 4  # 4 for deployment plans
    
    async with aiohttp.ClientSession() as session:
        headers = {
            "Authorization": f"Bearer {os.getenv('CLERK_SECRET_KEY')}",
            "Content-Type": "application/json",
        }

        # Get current organization data
        async with session.get(
            f"https://api.clerk.com/v1/organizations/{org_id}", headers=headers
        ) as response:
            if response.status != 200:
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Failed to fetch organization data from Clerk: {await response.text()}",
                )

            org_data = await response.json()
            current_max_seats = org_data["max_allowed_memberships"]
            
            # Don't update if current max is 0 (unlimited) or already meets minimum
            if current_max_seats == 0 or current_max_seats >= minimum_seats:
                return {
                    "status": "success", 
                    "message": "No update needed, seats already sufficient",
                    "current_seats": "unlimited" if current_max_seats == 0 else current_max_seats
                }
            
            # Update seats to minimum required for plan
            async with session.patch(
                f"https://api.clerk.com/v1/organizations/{org_id}",
                headers=headers,
                json={"max_allowed_memberships": minimum_seats}
            ) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=response.status, 
                        detail=f"Failed to update organization data: {await response.text()}"
                    )
                
                result = {
                    "status": "success",
                    "message": f"Updated seats from {current_max_seats} to {minimum_seats}",
                    "previous_seats": current_max_seats,
                    "new_seats": minimum_seats,
                }
                
                logfire.info(f"Seats updated from org_id {org_id}", extra=result)
                
                return result
