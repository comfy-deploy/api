from fastapi import HTTPException, APIRouter, Request, Depends
from fastapi.responses import Response
import logging
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from api.database import get_db
import os
from api.utils.storage_helper import get_s3_config
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import logfire

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Model Proxy"])

@router.get("/proxy/model/{path:path}")
async def proxy_s3_model(
    request: Request,
    path: str,
    db: AsyncSession = Depends(get_db),
):
    """Proxy S3 model requests to maintain authentication for private buckets"""
    
    try:
        s3_config = await get_s3_config(request, db)
        
        s3_client = boto3.client(
            's3',
            aws_access_key_id=s3_config.access_key,
            aws_secret_access_key=s3_config.secret_key,
            aws_session_token=s3_config.session_token,
            region_name=s3_config.region
        )
        
        path_parts = path.lstrip('/').split('/', 1)
        if len(path_parts) < 2:
            raise HTTPException(status_code=400, detail="Invalid S3 path format")
        
        bucket_name = path_parts[0]
        object_key = path_parts[1]
        
        logfire.info(f"Proxying S3 model request: bucket={bucket_name}, key={object_key}")
        
        try:
            response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
            content = response['Body'].read()
            content_type = response.get('ContentType', 'application/octet-stream')
            
            logfire.info(f"Successfully retrieved S3 object: {len(content)} bytes, content-type: {content_type}")
            
            return Response(
                content=content,
                media_type=content_type,
                headers={
                    'Content-Length': str(len(content)),
                    'Cache-Control': 'public, max-age=3600',  # Cache for 1 hour
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET',
                    'Access-Control-Allow-Headers': '*'
                }
            )
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'NoSuchKey':
                logfire.error(f"S3 object not found: {bucket_name}/{object_key}")
                raise HTTPException(status_code=404, detail="Model file not found")
            elif error_code == 'NoSuchBucket':
                logfire.error(f"S3 bucket not found: {bucket_name}")
                raise HTTPException(status_code=404, detail="S3 bucket not found")
            elif error_code == 'AccessDenied':
                logfire.error(f"Access denied to S3 object: {bucket_name}/{object_key}")
                raise HTTPException(status_code=403, detail="Access denied to model file")
            else:
                logfire.error(f"S3 client error: {str(e)}")
                raise HTTPException(status_code=500, detail="Error accessing S3 storage")
                
    except NoCredentialsError:
        logfire.error("No AWS credentials available")
        raise HTTPException(status_code=500, detail="S3 credentials not configured")
    except Exception as e:
        logfire.error(f"Unexpected error in model proxy: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")
