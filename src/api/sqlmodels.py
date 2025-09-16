from datetime import datetime
import enum
from typing import List, Optional, Dict
from pydantic import BaseModel
from sqlalchemy import JSON, Column, MetaData
from sqlmodel import SQLModel, Field
import uuid

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
