import os
from typing import AsyncGenerator
import clickhouse_connect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool
from dotenv import load_dotenv
from contextlib import asynccontextmanager
import asyncio
from clickhouse_connect.driver import httputil

load_dotenv()

# Use environment variables for database connection
DATABASE_URL = os.getenv("DATABASE_URL")

# Ensure the URL uses the asyncpg dialect
if DATABASE_URL and not DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

MAX_EXPECTED_CONCURRENCY = 200  # Document your design target

# Configure engine with larger pool size and longer timeout
engine = create_async_engine(
    DATABASE_URL,
    poolclass=AsyncAdaptedQueuePool,
    # Neon recommended settings for serverless
    pool_size=25,  # Reduced from 100 - better for 2 workers on 4 CPUs
    max_overflow=50,  # Reduced from 200 - still allows for bursts
    pool_timeout=30,  # Shorter timeout as Neon quickly provisions connections
    pool_pre_ping=True,  # Keep enabled to verify connection health
    pool_recycle=1800,  # 30 minutes recycle to align with Neon's timeout
    pool_use_lifo=True,  # Last In First Out - better for serverless
)

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

big_pool_mgr = httputil.get_pool_manager(maxsize=16, num_pools=12)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


def init_db():
    # You can add any initialization logic here if needed
    pass


clickhouse_client = None


async def get_clickhouse_client():
    global clickhouse_client
    try:
        if clickhouse_client is None:
            settings = {
                "max_result_rows": 10000,
                "max_result_bytes": 1000000000,
                "result_overflow_mode": "break",
            }
            clickhouse_client = await clickhouse_connect.get_async_client(
                host=os.getenv("CLICKHOUSE_HOST"),
                user=os.getenv("CLICKHOUSE_USER"),
                password=os.getenv("CLICKHOUSE_PASSWORD"),
                secure=False if os.getenv("CLICKHOUSE_HOST") in ["localhost", "host.docker.internal", "clickhouse"] else True,
                pool_mgr=big_pool_mgr,
                port=os.getenv("CLICKHOUSE_PORT", None),
                settings=settings,
            )
        return clickhouse_client
    except Exception as e:
        print(f"Error creating ClickHouse client: {e}")
        raise


@asynccontextmanager
async def get_db_context():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# @asynccontextmanager
# async def get_clickhouse_client_context():
#     try:
#         client = await clickhouse_connect.get_async_client(
#             host=os.getenv("CLICKHOUSE_HOST"),
#             user=os.getenv("CLICKHOUSE_USER"),
#             password=os.getenv("CLICKHOUSE_PASSWORD"),
#             secure=True,
#             # pool_mgr=big_pool_mgr,
#         )
#         try:
#             yield client
#         finally:
#             pass
#             # await client.close()
#     except Exception as e:
#         print(f"Error creating ClickHouse client: {e}")
#         raise
