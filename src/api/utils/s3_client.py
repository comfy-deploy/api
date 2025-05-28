from typing import Dict, Optional
import aioboto3
from botocore.config import Config
import asyncio
from api.utils.retrieve_s3_config_helper import S3Config

class S3ClientManager:
    _instance = None
    _clients: Dict[str, aioboto3.Session] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def get_client(cls, s3_config: S3Config) -> aioboto3.Session:
        """Get or create an S3 client for the given configuration"""
        # Create a unique key for this configuration
        config_key = f"{s3_config.region}:{s3_config.access_key}"
        
        if config_key not in cls._clients:
            async with cls._lock:
                if config_key not in cls._clients:
                    session = aioboto3.Session()
                    cls._clients[config_key] = session
        
        return cls._clients[config_key]

    @classmethod
    async def get_s3_client(cls, s3_config: S3Config) -> aioboto3.Session.client:
        """Get an S3 client with the given configuration"""
        session = await cls.get_client(s3_config)
        return session.client(
            's3',
            region_name=s3_config.region,
            aws_access_key_id=s3_config.access_key,
            aws_secret_access_key=s3_config.secret_key,
            aws_session_token=s3_config.session_token,
            config=Config(signature_version="s3v4")
        ) 