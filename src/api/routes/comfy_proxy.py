from fastapi import HTTPException, APIRouter, Request, Depends
from fastapi.responses import Response
import logging
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from api.database import get_db
import os
import json
import gzip
from urllib.parse import parse_qs
import email
from email.message import EmailMessage
import io
import base64
import random
import mimetypes
from pydantic import BaseModel, Field
from typing import Optional
from .utils import generate_presigned_url
from api.utils.storage_helper import get_s3_config

import logfire
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Comfy Proxy"])

COMFY_API_KEY = os.getenv("COMFY_API_KEY")


def _redact_headers(headers) -> dict:
    """Copy of headers safe for logging: credential-bearing values are masked."""
    return {
        k: "[REDACTED]"
        if any(s in k.lower() for s in ("authorization", "cookie", "token", "secret", "key"))
        else v
        for k, v in headers.items()
    }

# Utility functions copied from files.py
def custom_nanoid(size=16):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "".join(random.choice(alphabet) for _ in range(size))

prefixes = {
    "img": "img",
    "zip": "zip", 
    "vid": "vid",
    "audio": "audio",
    "file": "file",
    "folder": "folder"
}

def get_id_prefix_from_type(mime_type: str):
    if mime_type.startswith("video/"):
        return "vid"
    elif mime_type.startswith("audio/"):
        return "audio"
    elif mime_type.startswith("image/"):
        return "img"
    elif mime_type.startswith("application/zip"):
        return "zip"
    else:
        return "file"

def new_id(prefix):
    return f"{prefixes[prefix]}_{custom_nanoid(16)}"

# Pydantic models
class UploadRequest(BaseModel):
    file_name: str = Field(..., description="Filename to upload")
    content_type: Optional[str] = Field(
        None,
        description="Mime type of the file. For example: image/png, image/jpeg, video/mp4, etc.",
    )

class UploadResponse(BaseModel):
    download_url: str = Field(..., description="URL to GET uploaded file")
    upload_url: str = Field(..., description="URL to PUT file to upload")

def check_enterprise_plan(request: Request):
    """Check if user is on enterprise plan"""
    if not hasattr(request.state, 'current_user') or request.state.current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    plan = request.state.current_user.get("plan")
    if not plan:
        raise HTTPException(status_code=403, detail="No plan found")
    
    # Check for enterprise plan variants
    enterprise_plans = ["enterprise", "business", "business_monthly", "business_yearly"]
    if plan not in enterprise_plans:
        raise HTTPException(
            status_code=403, 
            detail="Enterprise plan required to access Comfy.org API proxy, use your own Comfy API key with API_KEY_COMFY_ORG in the machine secrets! Check https://docs.comfydeploy.com/docs/integration/comfy-org-api-nodes for setup!"
        )
    
    return True

@router.post("/comfy-org/customers/storage", response_model=UploadResponse)
async def get_comfy_org_upload_url(
    request: Request,
    file_name: str,
    content_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get presigned upload URL for Comfy.org storage with enterprise plan validation"""
    
    # Check enterprise plan
    check_enterprise_plan(request)
    
    # Infer content type if not provided
    if not content_type:
        inferred_type, _ = mimetypes.guess_type(file_name)
        content_type = inferred_type or "application/octet-stream"
    
    # Get S3 configuration
    s3_config = await get_s3_config(request, db)
    
    # Generate file ID and object key
    file_extension = os.path.splitext(file_name)[1]
    file_id = new_id(get_id_prefix_from_type(content_type))
    
    # Use comfy-org specific path structure
    object_key = f"comfy-org/{file_id}{file_extension}"
    
    # Generate presigned upload URL
    upload_url = generate_presigned_url(
        object_key=object_key,
        expiration=3600,  # 1 hour
        http_method="PUT",
        content_type=content_type,
        public=s3_config.public,
        bucket=s3_config.bucket,
        region=s3_config.region,
        access_key=s3_config.access_key,
        secret_key=s3_config.secret_key,
        session_token=s3_config.session_token,
    )
    
    # Generate download URL
    download_url = f"https://{s3_config.bucket}.s3.{s3_config.region}.amazonaws.com/{object_key}"
    
    logfire.info("Generated Comfy.org upload URL", extra={
        "file_name": file_name,
        "content_type": content_type,
        "file_id": file_id,
        "object_key": object_key
    })
    
    return UploadResponse(
        upload_url=upload_url,
        download_url=download_url
    )


@router.api_route("/comfy-org/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_to_comfy_org(
    request: Request,
    path: str,
    db: AsyncSession = Depends(get_db),
):
    """Proxy all requests to https://api.comfy.org/ with enterprise plan validation"""
    
    # Check enterprise plan
    check_enterprise_plan(request)
    
    # Build target URL
    target_url = f"https://api.comfy.org/{path}"
    
    # Get query parameters
    query_params = dict(request.query_params)
    
    # Get request body if exists
    body = None
    decoded_body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        try:
            body = await request.body()
            
            # Try to decode the body for logging
            if body:
                content_type = request.headers.get("content-type", "").lower()
                content_encoding = request.headers.get("content-encoding", "").lower()
                
                # Handle gzip compression
                actual_body = body
                if "gzip" in content_encoding:
                    try:
                        actual_body = gzip.decompress(body)
                        logfire.info("Decompressed gzip content")
                    except Exception as e:
                        logfire.error(f"Failed to decompress gzip: {str(e)}")
                        actual_body = body
                
                try:
                    if "application/json" in content_type:
                        # Handle JSON
                        decoded_body = json.loads(actual_body.decode('utf-8'))
                        logfire.info(f"Request body (JSON): {json.dumps(decoded_body, indent=2)}")
                    
                    elif "multipart/form-data" in content_type:
                        # Handle multipart form data
                        logfire.info(f"Request body (multipart/form-data): {len(actual_body)} bytes")
                        logfire.info(f"Content-Type header: {request.headers.get('content-type')}")
                        
                        # Try to parse multipart data to log form fields and files
                        try:
                            # Extract boundary from content-type header
                            boundary = None
                            for part in content_type.split(';'):
                                if 'boundary=' in part:
                                    boundary = part.split('boundary=')[1].strip()
                                    break
                            
                            if boundary:
                                # Create a proper email message for parsing
                                msg_content = f"Content-Type: {content_type}\r\n\r\n".encode() + actual_body
                                
                                # Parse multipart data
                                msg = email.message_from_bytes(msg_content)
                                
                                logfire.info(f"Multipart boundary: {boundary}")
                                
                                if msg.is_multipart():
                                    form_fields = {}
                                    files_info = {}
                                    
                                    for part in msg.walk():
                                        if part.get_content_maintype() == 'multipart':
                                            continue
                                            
                                        disposition = part.get('Content-Disposition', '')
                                        if 'form-data' in disposition:
                                            # Extract field name
                                            name = None
                                            filename = None
                                            for disp_part in disposition.split(';'):
                                                if 'name=' in disp_part:
                                                    name = disp_part.split('name=')[1].strip().strip('"')
                                                elif 'filename=' in disp_part:
                                                    filename = disp_part.split('filename=')[1].strip().strip('"')
                                            
                                            if name:
                                                if filename:
                                                    # This is a file upload
                                                    content_type_part = part.get_content_type()
                                                    content_length = len(part.get_payload(decode=True) or b'')
                                                    files_info[name] = {
                                                        'filename': filename,
                                                        'content_type': content_type_part,
                                                        'size': content_length
                                                    }
                                                else:
                                                    # This is a form field
                                                    field_value = part.get_payload(decode=True)
                                                    if field_value:
                                                        try:
                                                            decoded_value = field_value.decode('utf-8')
                                                            form_fields[name] = decoded_value[:200] + ('...' if len(decoded_value) > 200 else '')
                                                        except UnicodeDecodeError:
                                                            form_fields[name] = f"<binary data: {len(field_value)} bytes>"
                                    
                                    if form_fields:
                                        logfire.info(f"Form fields: {form_fields}")
                                    if files_info:
                                        logfire.info(f"Files: {files_info}")
                                        
                                else:
                                    logfire.info("Failed to parse as multipart message")
                            else:
                                logfire.warning("No boundary found in multipart content-type")
                                
                        except Exception as e:
                            logfire.error(f"Failed to parse multipart data: {str(e)}")
                            # Fallback to just showing raw info
                            logfire.info("Multipart parsing failed, showing raw boundary info")
                        
                        # Don't try to decode multipart as text - it contains binary boundaries
                        
                    elif "application/x-www-form-urlencoded" in content_type:
                        # Handle form data
                        decoded_text = actual_body.decode('utf-8')
                        parsed_form = parse_qs(decoded_text)
                        logfire.info(f"Request body (form-urlencoded): {parsed_form}")
                        
                    elif "text/" in content_type or "application/xml" in content_type:
                        # Handle text content
                        decoded_text = actual_body.decode('utf-8')
                        logfire.info(f"Request body (text): {decoded_text[:500]}{'...' if len(decoded_text) > 500 else ''}")
                        
                    else:
                        # Unknown content type, try text first, then log as binary
                        try:
                            decoded_text = actual_body.decode('utf-8')
                            logfire.info(f"Request body (unknown type, decoded as text): {decoded_text[:500]}{'...' if len(decoded_text) > 500 else ''}")
                        except UnicodeDecodeError:
                            logfire.info(f"Request body (binary, content-type: {content_type}): {len(actual_body)} bytes")
                            
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logfire.error(f"Error decoding request body: {str(e)}")
                    logfire.info(f"Request body (failed to decode): {len(actual_body)} bytes, content-type: {content_type}")
            else:
                logfire.info("Request body: empty")
                
        except Exception as e:
            logfire.error(f"Error reading request body: {str(e)}")
            body = None
    
    # Log request details
    logfire.info(f"Proxying {request.method} request to: {target_url}")
    logfire.info(f"Query parameters: {query_params}")
    logfire.info(f"Request headers: {_redact_headers(request.headers)}")
    
    # Prepare headers (exclude host and connection headers)
    headers = {}
    for key, value in request.headers.items():
        if key.lower() not in ["host", "connection"]:
            headers[key] = value
            
    headers["x-api-key"] = COMFY_API_KEY
    
    # Log specific info for multipart requests
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type.lower():
        logfire.info(f"Handling multipart request with Content-Type: {content_type}")
        logfire.info(f"Body size: {len(body) if body else 0} bytes")
        logfire.info(f"Headers being sent: {_redact_headers(headers)}")

    try:
        async with httpx.AsyncClient() as client:
            # Make the proxied request
            with logfire.span("comfy-proxy-request"):
                response = await client.request(
                    method=request.method,
                    url=target_url,
                    params=query_params,
                    content=body,
                    headers=headers,
                    follow_redirects=True,
                    timeout=120.0  # Increased timeout for file uploads
                )
                
                # Prepare response headers (exclude some that shouldn't be proxied)
                response_headers = {}
                for key, value in response.headers.items():
                    if key.lower() not in ["connection", "content-encoding", "content-length"]:
                        response_headers[key] = value
                        
                logfire.info("Proxied request", extra={"response": response.status_code, "response_headers": _redact_headers(response_headers)})
                
                # Log response details for debugging
                logfire.info(f"Response status: {response.status_code}")
                logfire.info(f"Response headers: {_redact_headers(response.headers)}")
                logfire.info(f"Response content length: {len(response.content)} bytes")
                
                # Return the proxied response AS-IS (simple proxy)
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=response_headers,
                    media_type=response.headers.get("content-type")
                )
            
    except httpx.TimeoutException:
        logger.error(f"Timeout when proxying to {target_url}")
        raise HTTPException(status_code=504, detail="Gateway timeout when accessing Comfy.org API")
    except httpx.RequestError as e:
        logger.error(f"Request error when proxying to {target_url}: {str(e)}")
        raise HTTPException(status_code=502, detail="Bad gateway when accessing Comfy.org API")
    except Exception as e:
        logger.error(f"Unexpected error when proxying to {target_url}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error when proxying request")