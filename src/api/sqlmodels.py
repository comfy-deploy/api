from datetime import datetime, timezone
import enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, field_serializer
from sqlalchemy import JSON, Column, MetaData
from sqlmodel import SQLModel, Field
import uuid
from decimal import Decimal

class APIBaseModel(BaseModel):
    """Base model for all API responses with consistent serialization"""
    
    @field_serializer('*', when_used='unless-none')
    def serialize_all_types(self, value: Any) -> Any:
        # Handle datetime - consistent with your existing logic
        if isinstance(value, datetime):
            # Ensure timezone-aware datetime is in UTC
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            # Use milliseconds precision and replace +00:00 with Z
            return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        
        # Handle UUID - convert to string
        elif isinstance(value, uuid.UUID):
            return str(value)
        
        # Handle Decimal - convert to string (more precise than float)
        elif isinstance(value, Decimal):
            return float(value)  # Changed from float to string for precision
        
        # Handle lists recursively
        elif isinstance(value, list):
            return [self.serialize_all_types(item) for item in value]
        
        # Handle dicts recursively
        elif isinstance(value, dict):
            return {k: self.serialize_all_types(v) for k, v in value.items()}
        
        # Handle objects with to_dict method (like SerializableMixin)
        elif hasattr(value, "to_dict") and callable(value.to_dict):
            return value.to_dict()
        
        # Return as-is for all other types
        return value

class WorkflowRunStatus(str, enum.Enum):
    NOT_STARTED = "not-started"
    RUNNING = "running"
    UPLOADING = "uploading"
    SUCCESS = "success"
    FAILED = "failed"
    STARTED = "started"
    QUEUED = "queued"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


metadata = MetaData(schema="comfyui_deploy")


class WorkflowRunOutput(SQLModel, table=True):
    __tablename__ = "workflow_run_outputs"
    metadata = metadata

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(foreign_key="workflow_runs.id")
    data: Dict = Field(default_factory=dict, sa_column=Column(JSON))
    node_meta: Dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field()
    updated_at: datetime = Field()


# class WorkflowRunWebhookBody(BaseModel):
#     status: WorkflowRunStatus
#     live_status: Optional[str]
#     progress: float
#     run_id: str
#     outputs: List[WorkflowRunOutput]


class WorkflowRunWebhookResponse(BaseModel):
    status: str


class OutputShareVisibility(str, enum.Enum):
    PRIVATE = "private"
    PUBLIC = "public"
    LINK = "link"


class OutputType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"
    THREE_D = "3d"
    OTHER = "other"


class OutputShare(SQLModel, table=True):
    __tablename__ = "output_shares"
    metadata = metadata

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(foreign_key="users.id")
    org_id: Optional[str] = None
    run_id: uuid.UUID = Field(foreign_key="workflow_runs.id")
    output_id: uuid.UUID = Field(foreign_key="workflow_run_outputs.id")
    deployment_id: Optional[uuid.UUID] = Field(default=None, foreign_key="deployments.id")
    output_data: dict = Field(default_factory=dict, sa_column=Column(JSON))
    inputs: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    output_type: OutputType = Field(default=OutputType.OTHER)
    visibility: OutputShareVisibility = Field(default=OutputShareVisibility.PRIVATE)
    created_at: datetime = Field()
    updated_at: datetime = Field()
