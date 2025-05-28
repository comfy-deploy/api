from dotenv import load_dotenv

load_dotenv()

from typing import Optional
from api.middleware.subscriptionMiddleware import SubscriptionMiddleware
from sqlalchemy import select

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse  # Import JSONResponse from here
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
import os
from jose import JWTError, jwt
from sqlalchemy.orm import Session  # Import Session from SQLAlchemy
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import AsyncSessionLocal, init_db, get_db, engine
from api.routes import (
    run,
    volumes,
    internal,
    workflow,
    log,
    workflows,
    machines,
    comfy_node,
    deployments,
    runs,
    session,
    files,
    models,
    platform,
    search,
    form,
    admin,
    image_optimization,
)
from api.modal import builder
from api.models import APIKey


import logfire
import logging
from scalar_fastapi import get_scalar_api_reference
from fastapi.openapi.utils import get_openapi
from fastapi import APIRouter

# import all you need from fastapi-pagination
# from fastapi_pagination import Page, add_pagination, paginate
from pprint import pprint
from api.middleware.authMiddleware import AuthMiddleware
from api.middleware.spendLimitMiddleware import SpendLimitMiddleware

from opentelemetry.context import get_current
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.textmap import TextMapPropagator

from logtail import LogtailHandler
import logging
from api.router import app, public_api_router, api_router

logtail_host = os.getenv("LOGTAIL_INGESTING_HOST")
logtail_source_token = os.getenv("LOGTAIL_SOURCE_TOKEN")

if logtail_host and logtail_source_token:
    handler = LogtailHandler(
        source_token=logtail_source_token,
        host=logtail_host,
    )
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.handlers = []
    logger.addHandler(handler)


class NullPropagator(TextMapPropagator):
    def extract(self, *args, **kwargs):
        return get_current()

    def inject(self, *args, **kwargs):
        pass

    @property
    def fields(self):
        return set()


set_global_textmap(NullPropagator())
logger = logfire
logfire.configure(
    service_name="comfydeploy-api",
    # additional_span_processors=[span_processor]
)

logging.basicConfig(level=logging.INFO)

# logger = logging.getLogger(__name__)

# Replace the existing oauth2_scheme declaration with this:
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="token",
    scopes={
        # "read:runs": "Read access to runs",
        # "write:runs": "Write access to runs",
        # Add more scopes as needed
    },
)

app.webhooks.include_router(run.webhook_router)

# Include routers
api_router.include_router(run.router)
api_router.include_router(internal.router)
api_router.include_router(workflows.router)
api_router.include_router(workflow.router)
api_router.include_router(builder.router)
api_router.include_router(machines.router)
api_router.include_router(machines.public_router)
api_router.include_router(log.router)
api_router.include_router(volumes.router)
api_router.include_router(comfy_node.router)
api_router.include_router(deployments.router)
api_router.include_router(session.router)
api_router.include_router(session.beta_router)
api_router.include_router(runs.router)
api_router.include_router(files.router)
api_router.include_router(models.router)
api_router.include_router(platform.router)
api_router.include_router(search.router)
api_router.include_router(form.router)
api_router.include_router(admin.router)  # Add the admin router to internal API
api_router.include_router(image_optimization.router)

# This is for the docs generation
public_api_router.include_router(run.router)
public_api_router.include_router(session.router)
public_api_router.include_router(machines.public_router)
# public_api_router.include_router(session.beta_router)
public_api_router.include_router(deployments.router)
public_api_router.include_router(files.router)
public_api_router.include_router(models.router)
public_api_router.include_router(search.router)
public_api_router.include_router(image_optimization.router)
# public_api_router.include_router(platform.router)
# public_api_router.include_router(run.webhook_router)

app.include_router(api_router, prefix="/api")  # Add the prefix here instead


# Set up logging

# # Hide sqlalchemy logs
# logging.getLogger('sqlalchemy').setLevel(logging.ERROR)

# Add CORS middleware
# app.add_middleware(SpendLimitMiddleware)
app.add_middleware(SubscriptionMiddleware)
app.add_middleware(AuthMiddleware)

# Get frontend URL from environment variable, default to localhost:3000 for development

allow_origins = []

if os.getenv("ENV") == "development":
    allow_origins.append("http://localhost:3001")
else:
    allow_origins.extend(
        [
            # Production
            "https://app.comfydeploy.com",
            # Staging
            "https://staging.app.comfydeploy.com",
        ]
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,  # Allow all subdomains of comfydeploy.com
    allow_credentials=True,  # Allow credentials (cookies)
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

logfire.instrument_fastapi(app)
logfire.instrument_sqlalchemy(
    engine=engine.sync_engine,
)
