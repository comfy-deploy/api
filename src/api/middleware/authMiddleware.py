import logging
from starlette.routing import Match  
from api.routes.platform import get_clerk_user
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from api.database import AsyncSessionLocal
from .auth import get_current_user
from cachetools import TTLCache
from typing import Dict
from fnmatch import fnmatch
import logfire
import time  # Ensure this import is present at the top
from api.router import app
import traceback  # Ensure this import is present at the top

logger = logging.getLogger(__name__)

class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._banned_cache: Dict[str, bool] = TTLCache(maxsize=100, ttl=60)
        self.ignored_routes = [
            "/api/gpu_event",
            "/api/machine-built",
            "/api/fal-webhook",
            "/api/models",
            "/api/clerk/webhook",
            "/api/user/*",
            "/api/platform/stripe/webhook",
            "/api/webhooks/autumn",
            "/api/platform/comfyui/auth-response",
            "/api/deployments/community",
            "/api/platform/gpu-pricing",
            "/api/platform/gpu-credit-schema",
        ]
        self.optional_auth_routes = [
            "/api/share/*",
            "/api/shared-workflows",
            "/api/shared-workflows/*",
            "/api/optimize/*",
        ]
        # print("AuthMiddleware initialized")  # Test print

    async def get_banned_status(self, user_id: str) -> bool:
        """Get banned status with caching"""
        try:
            # Check if we have any cached value (including False/None)
            if user_id in self._banned_cache:
                return self._banned_cache[user_id]

            # Cache miss - fetch from Clerk
            user_data = await get_clerk_user(user_id)
            banned = user_data.get("banned", False)
            self._banned_cache[user_id] = banned
            return banned
        except Exception as e:
            logger.error(f"Error checking banned status: {e}")
            return False

    async def dispatch(self, request: Request, call_next):
        start_time = time.time()  # Start timing here
        
        route_path = None
        full_path = request.url.path
        function_name = None

        try:
            for route in app.routes:
                match, _ = route.matches(request.scope)
                if match == Match.FULL:
                    route_path = route.path
                    function_name = route.endpoint.__name__
                    break
        except Exception as e:
            logger.error(f"Error matching route: {e}")


        optional_auth = any(fnmatch(full_path, route) for route in self.optional_auth_routes)

        if self.should_authenticate(request) and not optional_auth:
            # print("Authentication required")  # Test print
            try:
                await self.authenticate(request)

                plan = request.state.current_user.get("plan")
                if plan == "free":
                    user_id = request.state.current_user.get("user_id")
                    banned = await self.get_banned_status(user_id)
                    if banned:
                        return JSONResponse(
                            status_code=403,
                            content={
                                "detail": "Your account has been suspended. Please contact support at founders@comfydeploy.com for assistance."
                            },
                        )

            except HTTPException as e:
                # print(f"Authentication failed: {e.detail}")  # Test print
                current_user = getattr(request.state, 'current_user', None)
                user_id = current_user.get('user_id', 'unknown') if isinstance(current_user, dict) else 'unknown'
                org_id = current_user.get('org_id', 'unknown') if isinstance(current_user, dict) else 'unknown'
                logger.warning(
                    f"Authentication error: {e.detail}",
                    extra={
                        "route": route_path,
                        "full_route": full_path,
                        "function_name": function_name,
                        "method": request.method,
                        "user_id": user_id,
                        "org_id": org_id,
                    },
                )
                return JSONResponse(
                    status_code=e.status_code, content={"detail": e.detail}
                )
        elif optional_auth:
            try:
                await self.authenticate(request)
            except HTTPException:
                # Optional routes should not fail if auth is missing or invalid
                request.state.current_user = None
        else:
            # print("Skipping authentication")  # Test print
            logger.info("Skipping auth check for non-API route or ignored route")

        current_user = getattr(request.state, 'current_user', None)
        user_id = current_user.get('user_id', 'unknown') if isinstance(current_user, dict) else 'unknown'
        org_id = current_user.get('org_id', 'unknown') if isinstance(current_user, dict) else 'unknown'
        
        try:
            # # Skip logfire logging for update-run
            # if request.url.path != "/api/update-run":
            #     if org_id != "unknown":
            #         logfire.info("Organization", user_id=user_id, org_id=org_id)
            #     elif user_id != "unknown":
            #         logfire.info("User", user_id=user_id)
                
            response = await call_next(request)
            
            latency_ms = (time.time() - start_time) * 1000  # Convert latency to milliseconds

            logger.info(f"{request.method} {route_path} {response.status_code}", extra={
                "status_code": response.status_code,
                "route": route_path,  # Use low-cardinality route path
                "full_route": full_path,
                "function_name": function_name,
                "method": request.method,
                "user_id": user_id,
                "org_id": org_id,
                "latency_ms": latency_ms
            })
            return response
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000  # Convert latency to milliseconds even on error

            error_trace = traceback.format_exc()  # Capture traceback explicitly

            logger.error(str(e), extra={
                "route": route_path,  # Use low-cardinality route path
                "full_route": full_path,
                "function_name": function_name,
                "method": request.method,
                "status_code": 500,
                "user_id": user_id,
                "org_id": org_id,
                "latency_ms": latency_ms,
                "traceback": error_trace  # Include traceback explicitly in extra
            })
            raise e


    def should_authenticate(self, request: Request) -> bool:
        path = request.url.path

        # Check if path matches any of the ignored routes (including wildcards)
        for route in self.ignored_routes:
            if fnmatch(path, route):
                return False

        # Require authentication for all other /api routes
        return path.startswith("/api")

    async def authenticate(self, request: Request):
        request.state.current_user = await get_current_user(request)
        
        if request.state.current_user is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        if self.should_check_scopes(request):
            await self.check_token_scopes(request)
        # print("User authenticated and added to request state")  # Test print
        # logger.info("Added current_user to request state")
    
    # Currently only machine tokens are going to be checked
    def should_check_scopes(self, request: Request) -> bool:
        """Check if we need to validate scopes for this request"""
        user_data = request.state.current_user
        return user_data.get("token_type") == "machine" and user_data.get("scopes") is not None
    
    async def check_token_scopes(self, request: Request):
        """Validate that the token has permission to access the requested endpoint"""
        user_data = request.state.current_user
        scopes = user_data.get("scopes", [])
        path = request.url.path
        
        if scopes is None:
            return
        
        # Check if the current path matches any of the allowed scope patterns
        for scope in scopes:
            if fnmatch(path, scope):
                return
        
        raise HTTPException(
            status_code=403, 
            detail="Token does not have permission to access this endpoint"
        )
