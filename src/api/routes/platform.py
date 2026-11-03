import os
from api.utils.autumn import get_autumn_customer, autumn_client
# from api.middleware.subscriptionMiddleware import get_customer_plan_cached, get_customer_plan_cached_5_seconds
from api.utils.multi_level_cache import multi_level_cached
import logfire
from nanoid import generate
from pydantic import BaseModel
from api.database import get_db
from api.models import Machine, SubscriptionStatus, User, Workflow, GPUEvent, UserSettings, AuthRequest
from api.routes.utils import (
    async_lru_cache,
    fetch_user_icon,
    get_user_settings as get_user_settings_util,
    update_user_settings as update_user_settings_util,
    select,
)
from api.utils.autumn import get_autumn_data

from api.middleware.auth import (
  get_api_keys as get_api_keys_auth,
  delete_api_key as delete_api_key_auth,
  create_api_key as create_api_key_auth,
  generate_jwt_token
)
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date, and_, desc, func, or_, cast, extract, text, not_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi import BackgroundTasks
import aiohttp
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta, timezone
import asyncio
import unicodedata
import re
import functools
from upstash_redis.asyncio import Redis
import json
import resend
import uuid
from sqlalchemy.exc import IntegrityError
from clerk_backend_api import Clerk

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


@router.get("/platform/autumn-data")
async def get_autumn_data_endpoint(
    request: Request,
):
    # print(request.state.current_user)
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")
    
    autumn_data = await get_autumn_customer(org_id or user_id, include_features=True)
    autumn_query = await get_autumn_data(org_id or user_id)
    
    # Get the credit schema from customer's features
    credit_schema = {}
    if autumn_data and autumn_data.get("features") and autumn_data["features"].get("gpu-credit"):
        gpu_credit_feature = autumn_data["features"]["gpu-credit"]
        if gpu_credit_feature.get("credit_schema"):
            for credit_item in gpu_credit_feature["credit_schema"]:
                feature_id = credit_item.get("feature_id") or credit_item.get("metered_feature_id")
                credit_amount = credit_item.get("credit_amount") or credit_item.get("credit_cost")
                if feature_id and credit_amount:
                    credit_schema[feature_id] = credit_amount
    
    # Transform the usage list using the credit schema
    transformed_list = []
    usage_list = autumn_query.get("list", []) if autumn_query else []
    
    # First pass: identify GPU types that have zero usage across all periods
    gpu_totals = {}
    for usage_item in usage_list:
        for gpu_type, usage_value in usage_item.items():
            if gpu_type == "period":
                continue
            if gpu_type not in gpu_totals:
                gpu_totals[gpu_type] = 0
            gpu_totals[gpu_type] += usage_value if usage_value else 0
    
    # Identify GPU types that are all zero and filter them out
    zero_gpu_types = [gpu_type for gpu_type, total in gpu_totals.items() if total == 0]
    gpu_types_to_filter = zero_gpu_types  # Filter out all zero GPU types
    
    # Second pass: transform and filter the data
    for usage_item in usage_list:
        transformed_item = {"period": usage_item.get("period")}
        
        # Transform each GPU usage value using the credit multiplier
        for gpu_type, usage_value in usage_item.items():
            if gpu_type == "period":
                continue
            
            # Skip GPU types that we're filtering out
            if gpu_type in gpu_types_to_filter:
                continue
                
            # Map the usage value using credit schema if available
            credit_multiplier = credit_schema.get(gpu_type, 1.0)
            transformed_value = usage_value * credit_multiplier if usage_value else 0
            transformed_item[gpu_type] = transformed_value
            
        transformed_list.append(transformed_item)
    
    return {
        "autumn_data": autumn_data,
        "list": transformed_list,
        "credit_schema": credit_schema,  # Include for debugging/reference
    }


@router.post("/platform/check-limit")
async def check_feature_limit(
    request: Request,
    feature_id: str,
    required_balance: int = 1,
) -> Dict[str, Any]:
    """
    Check if a customer has access to a feature based on their limits.
    
    Args:
        feature_id: The feature ID to check (e.g., 'workflow_limit', 'machine_limit')
        required_balance: The amount of feature required (default: 1)
        
    Returns:
        Check response with allowed status and limit information
    """
    from api.utils.autumn import autumn_client
    
    current_user = request.state.current_user
    customer_id = current_user.get("org_id") or current_user.get("user_id")
    
    if not customer_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        # Check feature access using autumn client
        check_result = await autumn_client.check(
            customer_id=customer_id,
            feature_id=feature_id,
            required_balance=required_balance,
            with_preview=True  # Include preview data for paywall/upgrade UI
        )
        
        if not check_result:
            # If no result, assume not allowed
            return {
                "allowed": False,
                "feature_id": feature_id,
                "message": f"Unable to verify {feature_id} access"
            }
        
        return check_result
        
    except Exception as e:
        logfire.error(f"Error checking feature limit: {str(e)}")
        # Don't fail hard, return a conservative response
        return {
            "allowed": False,
            "feature_id": feature_id,
            "message": f"Error checking {feature_id} limit",
            "error": str(e)
        }


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
    use_cloudfront: Optional[bool] = None
    cloudfront_domain: Optional[str] = None

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

# PostHog event mapping
POSTHOG_EVENT_MAPPING = {
    "customer.subscription.created": "create_subscription",
    "customer.subscription.paused": "pause_subscription",
    "customer.subscription.resumed": "resume_subscription",
    "customer.subscription.deleted": "delete_subscription",
    "customer.subscription.updated": "update_subscription"
}

# Add Resend client
resend.api_key = os.getenv("RESEND_API_KEY")

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
    
    try:
        print(f"Attaching customer {user_id} to Autumn")
        
        customer_id = org_id if org_id else user_id
        response_data = await autumn_client.attach(
            customer_id=customer_id,
            product_id=plan,  # Using the plan as product_id
            force_checkout=not upgrade,
            success_url=redirect_url,
            customer_data={
                "name": (user_data.get("first_name") or "") + " " + (user_data.get("last_name") or ""),
                "email": user_email,
            }
        )
        
        if response_data:
            logfire.info(f"Successfully attached customer {user_id} to Autumn")
            print(response_data)
            if response_data.get("checkout_url"):
                return {"url": response_data.get("checkout_url")}
            else:
                return response_data
        else:
            # The autumn client logs errors internally and returns None on failure
            # Check if the customer already has the product by trying to get billing portal
            logfire.info(f"Attach failed. Attempting to get billing portal URL.")
            
            portal_data = await autumn_client.get_billing_portal(customer_id)
            if portal_data and portal_data.get("url"):
                return {"url": portal_data["url"]}
            
            return {"error": "Failed to create subscription"}
    except Exception as e:
        logfire.error(f"Error calling Autumn API: {str(e)}")
            

def r(price: float) -> float:
    return round(price * 1.1, 6)


# GPU pricing per second with 10% margin (H200/B200/RTX-PRO-6000 use 50% margin)
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
    "RTX-PRO-6000": round(0.000842 * 1.5, 6),  # 50% markup
    "CPU": r(0.000038),
}

FREE_TIER_USAGE = 500  # in cents, $5

@router.get("/platform/gpu-pricing")
async def gpu_pricing():
    """Return the GPU pricing table"""
    return PRICING_LOOKUP_TABLE


@router.get("/platform/gpu-credit-schema")
async def get_gpu_credit_schema():
    """Return the GPU credit schema for gpu-credit and gpu-credit-topup features"""
    try:
        features_data = await autumn_client.get_features()
        
        if not features_data or "list" not in features_data:
            raise HTTPException(status_code=500, detail="Failed to fetch features from Autumn")
        
        credit_schemas = {}
        
        for feature in features_data["list"]:
            if feature["id"] in ["gpu-credit", "gpu-credit-topup"] and feature["type"] == "credit_system":
                credit_schemas[feature["id"]] = {
                    "name": feature["name"],
                    "display": feature["display"],
                    "schema": feature.get("credit_schema", [])
                }
        
        return credit_schemas
        
    except Exception as e:
        logfire.error(f"Error fetching GPU credit schema: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch GPU credit schema: {str(e)}")

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
    
    customer_id = org_id or user_id
    portal_data = await autumn_client.get_billing_portal(customer_id)
    
    if portal_data and portal_data.get("url"):
        return {"url": portal_data["url"]}
    
    logfire.error(f"Failed to get billing portal URL for customer {customer_id}")
    raise HTTPException(status_code=500, detail="Failed to get billing portal URL")


class TopUpRequest(BaseModel):
    amount: float
    confirmed: Optional[bool] = None
    return_url: Optional[str] = None

@router.post("/platform/topup")
async def topup_credits(
    request: Request,
    body: TopUpRequest,
):
    """
    Create a checkout session for topping up GPU credits.
    $1 = 100 credits
    """
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Validate amount
    if body.amount < 10:
        raise HTTPException(status_code=400, detail="Minimum top-up amount is $10")
    
    if body.amount > 1000:
        raise HTTPException(status_code=400, detail="Maximum top-up amount is $1000")
    
    customer_id = org_id or user_id
    
    try:
        # Get user data from Clerk for checkout
        user_data = await get_clerk_user(user_id)
        user_email = next(
            (
                email["email_address"]
                for email in user_data["email_addresses"]
                if email["id"] == user_data["primary_email_address_id"]
            ),
            None,
        )
        
        # Convert dollars to credit quantity ($1 = 100 credits)
        credit_quantity = int(body.amount * 100)
        
        logfire.info(f"Creating credit checkout for customer {customer_id} - ${body.amount} ({credit_quantity} credits)")
        
        if body.confirmed:
            response_data = await autumn_client.attach(
                customer_id=customer_id,
                product_id="credit",
                options=[
                    {"feature_id": "gpu-credit-topup", "quantity": credit_quantity}
                ],
            )
            if response_data:
                logfire.info(f"Successfully attached credit for customer {customer_id}")
                return {"success": True, "message": "Credits added successfully", "data": response_data}
            else:
                logfire.error(f"Failed to attach credit for customer {customer_id}")
                raise HTTPException(status_code=500, detail="Failed to attach credit")
        else:
            response_data = await autumn_client.checkout(
                customer_id=customer_id,
                product_id="credit",  # Credit product ID in Autumn
                options=[
                    {
                        "feature_id": "gpu-credit-topup",
                        "quantity": credit_quantity
                    }
                ],
                success_url=body.return_url or f"{os.getenv('FRONTEND_URL', 'https://app.comfydeploy.com')}/usage?topup=success",
                customer_data={
                    "name": (user_data.get("first_name") or "") + " " + (user_data.get("last_name") or ""),
                    "email": user_email,
                },
            )
        
        if response_data:
            logfire.info(f"Successfully created credit checkout for customer {customer_id}")
            return response_data
        else:
            logfire.error(f"Failed to create credit checkout for customer {customer_id}")
            raise HTTPException(status_code=500, detail="Failed to create checkout session")
            
    except Exception as e:
        logfire.error(f"Error creating credit checkout: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create checkout: {str(e)}")

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
    # logfire.info(f"Fetching fresh data from Autumn for {user_or_org_id}")
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
    # with logfire.span("get_customer_plan"):
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
    # with logfire.span("get_customer_plan"):
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
    
    async with Clerk(
        bearer_auth=os.getenv("CLERK_SECRET_KEY"),
    ) as clerk:
        org_data = await clerk.organizations.retrieve(org_id)
        current_max_seats = org_data["max_allowed_memberships"]
        
        # Don't update if current max is 0 (unlimited) or already meets minimum
        if current_max_seats == 0 or current_max_seats >= minimum_seats:
            return {
                "status": "success", 
                "message": "No update needed, seats already sufficient",
                "current_seats": "unlimited" if current_max_seats == 0 else current_max_seats
            }
            
        # Update seats to minimum required for plan
        org_data = await clerk.organizations.update(org_id, max_allowed_memberships=minimum_seats)
        
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

