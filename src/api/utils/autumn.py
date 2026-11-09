import os
import aiohttp
from datetime import datetime
import logfire
from typing import Optional, List, Dict, Any, Union
import json


class AutumnClient:
    """
    A comprehensive client for interacting with the Autumn API.
    
    This client provides methods for:
    - Managing customer data
    - Tracking usage events and feature usage
    - Handling checkouts and subscriptions
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize the Autumn client with an API key."""
        self.api_key = api_key or os.getenv("AUTUMN_SECRET_KEY")
        self.base_url = "https://api.useautumn.com/v1"
        
    def _get_headers(self, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Get default headers for API requests."""
        if not self.api_key:
            raise ValueError("AUTUMN_SECRET_KEY not found in environment variables")
            
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        if extra_headers:
            headers.update(extra_headers)
            
        return headers
    
    async def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Make a request to the Autumn API."""
        url = f"{self.base_url}/{endpoint}"
        headers = self._get_headers(extra_headers)
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=data if method in ["POST", "PUT", "PATCH", "DELETE"] else None,
                    params=data if method in ["GET"] else None
                ) as response:
                    response_text = await response.text()
                    
                    if 200 <= response.status < 300:  # 202 = event accepted async
                        return json.loads(response_text) if response_text else {}
                    else:
                        # Try to parse error response
                        try:
                            error_data = json.loads(response_text)
                            if error_data.get("code") == "customer_not_found":
                                logfire.warning(f"Customer not found in Autumn: {endpoint}")
                                return None
                        except json.JSONDecodeError:
                            pass
                            
                        logfire.error(f"Autumn API error ({response.status}): {response_text}")
                        return None
                        
        except Exception as e:
            logfire.error(f"Error making Autumn API request: {str(e)}")
            return None
    
    # Customer Management
    async def create_customer(
        self,
        id: str,
        email: Optional[str] = None,
        name: Optional[str] = None,
        fingerprint: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new customer in Autumn.
        
        Args:
            id: Your unique identifier for the customer
            email: Customer's email address  
            name: Customer's name
            fingerprint: Unique identifier (eg, serial number) to detect duplicate 
                        customers and prevent free trial abuse
            
        Returns:
            Created customer data or None if error
        """
        payload = {
            "id": id,
            "email": email,
            "name": name
        }
        
        if fingerprint:
            payload["fingerprint"] = fingerprint
            
        return await self._make_request("POST", "customers", data=payload)
    
    async def get_customer(self, customer_id: str, include_features: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get customer data from Autumn.
        
        Args:
            customer_id: The customer ID
            include_features: Whether to include feature data (requires API version 1.2)
            
        Returns:
            Customer data or None if not found
        """
        extra_headers = {"x-api-version": "1.2"} if include_features else None
        return await self._make_request("GET", f"customers/{customer_id}", extra_headers=extra_headers)
    
    async def delete_customer(self, customer_id: str, delete_in_stripe: bool = False) -> Optional[Dict[str, Any]]:
        """
        Delete a customer from Autumn.
        
        Args:
            customer_id: The customer ID to delete
            delete_in_stripe: Whether to also delete the linked Stripe customer (default: False)
            
        Returns:
            Deleted customer data or None if error
        """
        params = {}
        if delete_in_stripe:
            params["delete_in_stripe"] = delete_in_stripe
            
        return await self._make_request("DELETE", f"customers/{customer_id}", data=params)
    
    # Usage Tracking
    async def send_usage_event(
        self,
        customer_id: str,
        event_name: str,
        properties: Dict[str, Any],
        idempotency_key: Optional[str] = None
    ) -> bool:
        """
        Send a usage event to Autumn.
        
        Args:
            customer_id: The customer ID
            event_name: The name of the event
            properties: Event properties
            idempotency_key: Optional unique key to prevent duplicate events
            
        Returns:
            True if successful, False otherwise
        """
        payload = {
            "customer_id": customer_id,
            "event_name": event_name,
            "properties": properties
        }
        
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            
        result = await self._make_request("POST", "events", data=payload)
        return result is not None
    
    async def send_gpu_usage_event(
        self,
        customer_id: str,
        gpu_type: str,
        start_time: datetime,
        end_time: datetime,
        environment: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> bool:
        """
        Send GPU usage data to Autumn API.
        
        Args:
            customer_id: The org_id or user_id of the customer
            gpu_type: The type of GPU used (e.g., 'T4', 'A100')
            start_time: The start time of the GPU usage
            end_time: The end time of the GPU usage
            environment: Optional environment tag
            idempotency_key: Optional unique key to prevent duplicate events
            
        Returns:
            True if the event was sent successfully, False otherwise
        """
        # Calculate duration in seconds
        duration = (end_time - start_time).total_seconds()
        
        # Skip if duration is negative or zero
        if duration <= 0:
            logfire.warning(f"Invalid duration {duration} for GPU event")
            return False
            
        properties = {"value": str(duration)}
        
        if environment:
            properties["environment"] = environment
            
        return await self.send_usage_event(
            customer_id=customer_id,
            event_name=gpu_type.lower() if gpu_type else "cpu",
            properties=properties,
            idempotency_key=idempotency_key
        )
    
    async def set_feature_usage(
        self,
        customer_id: str,
        feature_id: str,
        value: Union[int, float]
    ) -> bool:
        """
        Set feature usage for a customer.
        
        Args:
            customer_id: The customer ID
            feature_id: The feature ID to set usage for
            value: The usage value to set
            
        Returns:
            True if successful, False otherwise
        """
        payload = {
            "customer_id": customer_id,
            "feature_id": feature_id,
            "value": value
        }
        
        result = await self._make_request("POST", "usage", data=payload)
        return result is not None
    
    async def track_feature_usage(
        self,
        customer_id: str,
        feature_id: Optional[str] = None,
        event_name: Optional[str] = None,
        value: Optional[Union[int, float]] = None,
        idempotency_key: Optional[str] = None
    ) -> bool:
        """
        Track feature usage increment/decrement for a customer.
        
        Args:
            customer_id: The customer ID
            feature_id: The feature ID to track usage for
            value: The usage value to track (+1 for increment, -1 for decrement)
            idempotency_key: Optional idempotency key to prevent duplicate tracking
            
        Returns:
            True if successful, False otherwise
        """
        payload = {
            "customer_id": customer_id,
            "value": value
        }
        
        if feature_id:
            payload["feature_id"] = feature_id
        if event_name:
            payload["event_name"] = event_name
        
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        
        result = await self._make_request("POST", "track", data=payload)
        return result is not None
    
    # Query and Analytics
    async def query_usage(
        self,
        customer_id: str,
        feature_ids: Optional[List[str]] = None,
        range: str = "30d"
    ) -> Optional[Dict[str, Any]]:
        """
        Query usage data for a customer.
        
        Args:
            customer_id: The customer ID
            feature_ids: List of feature IDs to query (defaults to GPU types)
            range: Time range for the query (e.g., "30d", "7d")
            
        Returns:
            Usage data or None if error
        """
        if feature_ids is None:
            feature_ids = [
                "gpu-b200",
                "gpu-rtx-pro-6000",
                "gpu-h200",
                "cpu",
                "gpu-h100",
                "gpu-a100-80gb",
                "gpu-a100",
                "gpu-l4",
                "gpu-t4",
                "gpu-l40s",
                "gpu-a10g",
                "gpu-credit"
            ]
            
        payload = {
            "customer_id": customer_id,
            "feature_id": feature_ids,
            "range": range
        }
        
        return await self._make_request("POST", "query", data=payload)
    
    # Permissions and Access Control
    async def check(
        self,
        customer_id: str,
        product_id: Optional[str] = None,
        feature_id: Optional[str] = None,
        required_balance: int = 1,
        send_event: bool = False,
        with_preview: bool = False,
        entity_id: Optional[str] = None,
        customer_data: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Check if a customer has access to a feature or product.
        
        Args:
            customer_id: The customer ID
            product_id: ID of the product to check access to (required if feature_id not provided)
            feature_id: ID of the feature to check access to (required if product_id not provided)
            required_balance: Amount of feature required (default: 1)
            send_event: If True, record a usage event with required_balance as value
            with_preview: If True, include preview object for paywall/upgrade UI
            entity_id: Entity ID for entity-based features (e.g., seats)
            customer_data: Additional customer properties
            
        Returns:
            Check response with allowed status and optional preview data
            
        Raises:
            ValueError: If neither product_id nor feature_id is provided
        """
        if not product_id and not feature_id:
            raise ValueError("Either product_id or feature_id must be provided")
            
        payload = {
            "customer_id": customer_id,
            "required_balance": required_balance,
            "send_event": send_event,
            "with_preview": with_preview
        }
        
        if product_id:
            payload["product_id"] = product_id
        if feature_id:
            payload["feature_id"] = feature_id
        if entity_id:
            payload["entity_id"] = entity_id
        if customer_data:
            payload["customer_data"] = customer_data
            
        return await self._make_request("POST", "check", data=payload)
    
    # Checkout and Billing
    async def checkout(
        self,
        customer_id: str,
        product_id: Optional[str] = None,
        product_ids: Optional[List[str]] = None,
        success_url: Optional[str] = None,
        options: Optional[List[Dict[str, Any]]] = None,
        reward: Optional[str] = None,
        entity_id: Optional[str] = None,
        customer_data: Optional[Dict[str, str]] = None,
        checkout_session_params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a checkout session for a customer.
        
        Args:
            customer_id: The customer ID
            product_id: Single product ID
            product_ids: List of product IDs (alternative to product_id)
            success_url: URL to redirect after successful purchase
            options: Quantities for prepaid features
            reward: Promo code or reward ID
            entity_id: Entity to attach the product to
            customer_data: Additional customer properties (name, email, fingerprint)
            checkout_session_params: Additional Stripe checkout parameters
            
        Returns:
            Checkout response with URL or payment confirmation
        """
        if not product_id and not product_ids:
            raise ValueError("Either product_id or product_ids must be provided")
            
        payload = {
            "customer_id": customer_id,
        }
        
        if product_id:
            payload["product_id"] = product_id
        if product_ids:
            payload["product_ids"] = product_ids
        if success_url:
            payload["success_url"] = success_url
        if options:
            payload["options"] = options
        if reward:
            payload["reward"] = reward
        if entity_id:
            payload["entity_id"] = entity_id
        if customer_data:
            payload["customer_data"] = customer_data
        if checkout_session_params:
            payload["checkout_session_params"] = checkout_session_params
            
        return await self._make_request("POST", "checkout", data=payload)
    
    async def attach(
        self,
        customer_id: str,
        product_id: Optional[str] = None,
        product_ids: Optional[List[str]] = None,
        success_url: Optional[str] = None,
        options: Optional[List[Dict[str, Any]]] = None,
        reward: Optional[str] = None,
        entity_id: Optional[str] = None,
        force_checkout: bool = False,
        customer_data: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        checkout_session_params: Optional[Dict[str, Any]] = None,
        enable_product_immediately: Optional[bool] = False,
        finalize_invoice: Optional[bool] = False,
        invoice: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Attach a product to a customer and handle payment if card is on file.
        
        Args:
            customer_id: The customer ID
            product_id: Single product ID
            product_ids: List of product IDs (alternative to product_id)
            success_url: URL to redirect after successful purchase
            options: Quantities for prepaid features
            reward: Promo code or reward ID
            entity_id: Entity to attach the product to
            force_checkout: Always return checkout URL even if card is on file
            customer_data: Additional customer properties
            metadata: Additional metadata for Stripe
            checkout_session_params: Additional Stripe checkout parameters
            
        Returns:
            Attach response with success status or checkout URL
        """
        if not product_id and not product_ids:
            raise ValueError("Either product_id or product_ids must be provided")
            
        payload = {
            "customer_id": customer_id,
            "force_checkout": force_checkout
        }
        
        if product_id:
            payload["product_id"] = product_id
        if product_ids:
            payload["product_ids"] = product_ids
        if success_url:
            payload["success_url"] = success_url
        if options:
            payload["options"] = options
        if reward:
            payload["reward"] = reward
        if entity_id:
            payload["entity_id"] = entity_id
        if customer_data:
            payload["customer_data"] = customer_data
        if metadata:
            payload["metadata"] = metadata
        if checkout_session_params:
            payload["checkout_session_params"] = checkout_session_params
        if enable_product_immediately:
            payload["enable_product_immediately"] = enable_product_immediately
        if finalize_invoice:
            payload["finalize_invoice"] = finalize_invoice
        if invoice:
            payload["invoice"] = invoice
            
        return await self._make_request("POST", "attach", data=payload)
    
    async def get_billing_portal(self, customer_id: str) -> Optional[Dict[str, Any]]:
        """
        Get billing portal URL for a customer.
        
        Args:
            customer_id: The customer ID
            
        Returns:
            Response with billing portal URL or None if error
        """
        return await self._make_request("GET", f"customers/{customer_id}/billing_portal")
    
    async def get_features(self) -> Optional[Dict[str, Any]]:
        """
        Get all features from Autumn.
        
        Returns:
            Features data or None if error
        """
        return await self._make_request("GET", "features")


# Create a default client instance
autumn_client = AutumnClient()


# Backward compatibility functions
async def send_autumn_usage_event(
    customer_id: str,
    gpu_type: str,
    start_time: datetime,
    end_time: datetime,
    environment: str = None,
    idempotency_key: str = None
) -> bool:
    """Backward compatibility wrapper for send_gpu_usage_event."""
    return await autumn_client.send_gpu_usage_event(
        customer_id=customer_id,
        gpu_type=gpu_type,
        start_time=start_time,
        end_time=end_time,
        environment=environment,
        idempotency_key=idempotency_key
    )


async def get_autumn_data(customer_id: str, range: str = "30d") -> Optional[dict]:
    """Backward compatibility wrapper for query_usage."""
    return await autumn_client.query_usage(customer_id=customer_id, range=range)


async def get_autumn_customer(customer_id: str, include_features: bool = False) -> Optional[dict]:
    """Backward compatibility wrapper for get_customer."""
    return await autumn_client.get_customer(customer_id=customer_id, include_features=include_features)
