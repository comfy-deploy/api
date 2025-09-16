# import inspect
from sqlalchemy import (
    BigInteger,
    Column,
    String,
    Enum,
    DateTime,
    Boolean,
    MetaData,
    Float,
    JSON,
    ForeignKey,
    Integer,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.inspection import inspect as sqlalchemy_inspect
import uuid
from datetime import datetime
import json
from datetime import timezone
from sqlalchemy.orm import column_property
from decimal import Decimal

Base = declarative_base()

metadata = MetaData(schema="comfyui_deploy")


class SerializableMixin:
    def to_dict(self):
        try:
            # return {
            #     c.key: self._serialize_value(getattr(self, c.key))
            #     for c in sqlalchemy_inspect(self.__class__).mapper.column_attrs
            #     if hasattr(self, c.key)
            # }
            return {
                attr: self._serialize_value(getattr(self, attr))
                for attr in self.__dict__
                if not attr.startswith("_")
            }
        except Exception as e:
            print(f"Error in to_dict: {str(e)}")
            return {}

    def _serialize_value(self, value):
        if isinstance(value, uuid.UUID):
            return str(value)
        # if isinstance(value, datetime):
        #     return value
        # return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        if isinstance(value, datetime):
            # .replace(tzinfo=timezone.utc)
            return value.isoformat()[:-3] + "Z"
            # return value.isoformat() + "Z"
        if isinstance(value, list):
            return [self._serialize_value(item) for item in value]
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        if isinstance(value, Decimal):
            return str(value)
        # Check if the object has a to_dict method and is potentially a SerializableMixin
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return value.to_dict()
        return value

    def to_json(self):
        return json.dumps(self.to_dict())


class Workflow(SerializableMixin, Base):
    __tablename__ = "workflows"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="cascade"), nullable=False)
    org_id = Column(String)
    name = Column(String, nullable=False)
    selected_machine_id = Column(
        UUID(as_uuid=True), ForeignKey("machines.id", ondelete="set null")
    )
    description = Column(String)
    cover_image = Column(String)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    pinned = Column(Boolean, nullable=False, default=False)
    deleted = Column(Boolean, nullable=False, default=False)

    user = relationship("User", back_populates="workflows")
    versions = relationship("WorkflowVersion", back_populates="workflow_rel")
    deployments = relationship("Deployment", back_populates="workflow")
    # selected_machine = relationship("Machine", foreign_keys=[selected_machine_id])
    # machine = relationship("Machine", back_populates="workflows", foreign_keys=[selected_machine_id])
    runs = relationship("WorkflowRun", back_populates="workflow")


class WorkflowVersion(SerializableMixin, Base):
    __tablename__ = "workflow_versions"
    metadata = metadata

    workflow_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="cascade"),
        nullable=False,
    )
    id = Column(UUID(as_uuid=True), primary_key=True)
    workflow = Column(JSON)
    workflow_api = Column(JSON)
    user_id = Column(String, ForeignKey("users.id", ondelete="set null"))
    comment = Column(String)
    version = Column(Integer, nullable=False)
    snapshot = Column(JSON)
    dependencies = Column(JSON)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    machine_version_id = Column(UUID(as_uuid=True))
    machine_id = Column(UUID(as_uuid=True))
    comfyui_snapshot = Column(JSON)

    workflow_rel = relationship("Workflow", back_populates="versions")
    # user = relationship("User", back_populates="workflow_versions")


def lazy_utc_now():
    return datetime.now(tz=timezone.utc)


class WorkflowRun(SerializableMixin, Base):
    __tablename__ = "workflow_runs"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    workflow_version_id = Column(
        UUID(as_uuid=True), ForeignKey("workflow_versions.id"), nullable=True
    )
    workflow_inputs = Column(JSON)
    workflow_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="NO ACTION"),
        nullable=False,
    )
    workflow_api = Column(JSON)
    machine_id = Column(UUID(as_uuid=True), ForeignKey("machines.id"), nullable=True)
    origin = Column(
        Enum(
            "manual",
            "api",
            "public-share",
            "public-template",
            "workspace",
            "workspace-v2",
            name="workflow_run_origin",
        ),
        nullable=False,
        default="api",
    )
    status = Column(
        Enum(
            "not-started",
            "running",
            "uploading",
            "success",
            "failed",
            "started",
            "queued",
            "timeout",
            "cancelled",
            name="workflow_run_status",
        ),
        nullable=False,
        default="not-started",
    )
    ended_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lazy_utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lazy_utc_now)
    queued_at = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    gpu_event_id = Column(String)
    gpu = Column(
        Enum(
            "CPU",
            "T4",
            "L4",
            "A10G",
            "L40S",
            "A100",
            "A100-80GB",
            "H100",
            "H200",
            "B200",
            name="machine_gpu",
        )
    )
    machine_version = Column(String)
    machine_type = Column(
        Enum(
            "classic",
            "runpod-serverless",
            "modal-serverless",
            "comfy-deploy-serverless",
            "workspace",
            "workspace-v2",
            name="machine_type",
        )
    )
    modal_function_call_id = Column(String)
    user_id = Column(String)
    org_id = Column(String)
    run_log = Column(JSON)
    live_status = Column(String)
    progress = Column(Float, nullable=False, default=0)
    is_realtime = Column(Boolean, nullable=False, default=False)
    webhook = Column(String)
    webhook_status = Column(
        Enum("success", "failed", "not-started", "running", name="webhook_status")
    )
    webhook_intermediate_status = Column(Boolean, nullable=False, default=False)

    batch_id = Column(UUID(as_uuid=True))
    model_id = Column(String)
    deployment_id = Column(UUID(as_uuid=True))

    workflow = relationship("Workflow", back_populates="runs")
    outputs = relationship("WorkflowRunOutput", back_populates="run")
    version = relationship("WorkflowVersion", foreign_keys=[workflow_version_id])
    machine = relationship("Machine", foreign_keys=[machine_id])


class WorkflowRunWithExtra(WorkflowRun):
    pass


WorkflowRunWithExtra.duration = column_property(
    (
        func.extract("epoch", WorkflowRun.ended_at)
        - func.extract("epoch", WorkflowRun.created_at)
    ).label("duration")
)
WorkflowRunWithExtra.comfy_deploy_cold_start = column_property(
    (
        func.extract("epoch", WorkflowRun.queued_at)
        - func.extract("epoch", WorkflowRun.created_at)
    ).label("comfy_deploy_cold_start")
)
WorkflowRunWithExtra.cold_start_duration = column_property(
    (
        func.extract("epoch", WorkflowRun.started_at)
        - func.extract("epoch", WorkflowRun.queued_at)
    ).label("cold_start_duration")
)
WorkflowRunWithExtra.cold_start_duration_total = column_property(
    (
        func.extract("epoch", WorkflowRun.started_at)
        - func.extract("epoch", WorkflowRun.created_at)
    ).label("cold_start_duration_total")
)
WorkflowRunWithExtra.run_duration = column_property(
    (
        func.extract("epoch", WorkflowRun.ended_at)
        - func.extract("epoch", WorkflowRun.started_at)
    ).label("run_duration")
)


class WorkflowRunOutput(SerializableMixin, Base):
    __tablename__ = "workflow_run_outputs"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="cascade"),
        nullable=False,
    )
    data = Column(JSON)
    node_meta = Column(JSON)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    run = relationship("WorkflowRun", back_populates="outputs")


class APIKey(SerializableMixin, Base):
    __tablename__ = "api_keys"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    key = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    org_id = Column(String)
    revoked = Column(Boolean, nullable=False, default=False)
    scopes = Column(JSON, nullable=True)  # New field for storing endpoint permissions
    token_type = Column(
        Enum("user", "machine", "scoped", name="api_key_token_type"),
        nullable=False,
        default="user",
    )  # New field to distinguish token types
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    # user = relationship("User", back_populates="api_keys")

    def is_revoked(self):
        return self.revoked

class AuthRequest(SerializableMixin, Base):
    __tablename__ = "auth_requests"
    metadata = metadata
    
    request_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=True)
    org_id = Column(String, nullable=True)
    api_hash = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    expired_date = Column(DateTime(timezone=True), nullable=True)

class User(SerializableMixin, Base):
    __tablename__ = "users"
    metadata = metadata

    id = Column(String, primary_key=True)
    username = Column(String)
    name = Column(String)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    # Add other user fields as needed

    # api_keys = relationship("APIKey", back_populates="user")
    workflows = relationship("Workflow", back_populates="user")
    # workflow_versions = relationship("WorkflowVersion", back_populates="user")


class Deployment(SerializableMixin, Base):
    __tablename__ = "deployments"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="cascade"), nullable=False)
    org_id = Column(String)
    workflow_version_id = Column(
        UUID(as_uuid=True), ForeignKey("workflow_versions.id"), nullable=False
    )
    workflow_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="cascade"),
        nullable=False,
    )
    machine_id = Column(UUID(as_uuid=True), ForeignKey("machines.id"), nullable=False)
    share_slug = Column(String)
    description = Column(String)
    share_options = Column(JSON)
    showcase_media = Column(JSON)
    environment = Column(
        Enum(
            "staging",
            "production",
            "public-share",
            "private-share",
            "community-share",
            "preview",
            name="deployment_environment",
        ),
        nullable=False,
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # V2 deployment system
    machine_version_id = Column(UUID(as_uuid=True))
    modal_image_id = Column(String)
    concurrency_limit = Column(Integer, nullable=False, default=2)
    gpu = Column(
        Enum(
            "CPU",
            "T4",
            "L4",
            "A10G",
            "L40S",
            "A100",
            "A100-80GB",
            "H100",
            "H200",
            "B200",
            name="machine_gpu",
        )
    )
    run_timeout = Column(Integer, nullable=False, default=300)
    idle_timeout = Column(Integer, nullable=False, default=0)
    keep_warm = Column(Integer, nullable=False, default=0)
    activated_at = Column(DateTime(timezone=True))
    modal_app_id = Column(String)

    machine = relationship("Machine")
    version = relationship("WorkflowVersion")
    workflow = relationship("Workflow")
    # user = relationship("User")


# Shared column definitions
def get_machine_columns():
    return {
        "comfyui_version": Column(String),
        "gpu": Column(
            Enum(
                "CPU",
                "T4",
                "L4",
                "A10G",
                "L40S",
                "A100",
                "A100-80GB",
                "H100",
                "H200",
                "B200",
                name="machine_gpu",
            )
        ),
        "docker_command_steps": Column(JSON),
        "allow_concurrent_inputs": Column(Integer, default=1),
        "concurrency_limit": Column(Integer, default=2),
        "install_custom_node_with_gpu": Column(Boolean, default=False),
        "run_timeout": Column(Integer, nullable=False, default=300),
        "idle_timeout": Column(Integer, nullable=False, default=60),
        "extra_docker_commands": Column(JSON),
        "machine_builder_version": Column(
            Enum("2", "3", "4", name="machine_builder_version"), default="4"
        ),
        "base_docker_image": Column(String),
        "python_version": Column(String),
        "extra_args": Column(String),
        "prestart_command": Column(String),
        "keep_warm": Column(Integer, nullable=False, default=0),
        "status": Column(
            Enum(
                "not-started",
                "ready",
                "building",
                "error",
                "running",
                "paused",
                "starting",
                name="machine_status",
            ),
            nullable=False,
            default="ready",
        ),
        "build_log": Column(String),
        "machine_hash": Column(String),
        "disable_metadata": Column(Boolean, nullable=False, default=True),
        "cpu_request": Column(Float),
        "cpu_limit": Column(Float),
        "memory_request": Column(Integer),
        "memory_limit": Column(Integer),
        "models_to_cache": Column(JSON, default=[]),
        "enable_gpu_memory_snapshot": Column(Boolean, default=False),
    }


class MachineVersion(SerializableMixin, Base):
    __tablename__ = "machine_versions"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    machine_id = Column(
        UUID(as_uuid=True),
        ForeignKey("machines.id", ondelete="cascade"),
        nullable=False,
    )
    version = Column(Integer, nullable=False)
    user_id = Column(String, ForeignKey("users.id", ondelete="cascade"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    modal_image_id = Column(String)

    # Add shared columns
    locals().update(get_machine_columns())


class Machine(SerializableMixin, Base):
    __tablename__ = "machines"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="cascade"), nullable=False)
    name = Column(String, nullable=False)
    org_id = Column(String)
    endpoint = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    disabled = Column(Boolean, nullable=False, default=False)
    auth_token = Column(String)
    type = Column(
        Enum(
            "classic",
            "runpod-serverless",
            "modal-serverless",
            "comfy-deploy-serverless",
            "workspace",
            "workspace-v2",
            name="machine_type",
        ),
        nullable=False,
        default="classic",
    )
    static_assets_status = Column(
        Enum(
            "not-started",
            "ready",
            "building",
            "error",
            "running",
            "paused",
            "starting",
            name="machine_status",
        ),
        nullable=False,
        default="not-started",
    )
    machine_version = Column(String)
    snapshot = Column(JSON)
    models = Column(JSON)
    ws_gpu = Column(Enum("4090", name="workspace_machine_gpu"))
    pod_id = Column(String)
    legacy_mode = Column(Boolean, nullable=False, default=False)
    ws_timeout = Column(Integer, default=2)
    build_machine_instance_id = Column(String)
    modal_app_id = Column(String)
    target_workflow_id = Column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="set null")
    )
    dependencies = Column(JSON)
    deleted = Column(Boolean, nullable=False, default=False)
    allow_background_volume_commits = Column(Boolean, nullable=False, default=False)
    gpu_workspace = Column(Boolean, nullable=False, default=False)
    retrieve_static_assets = Column(Boolean, default=False)
    object_info = Column(JSON)
    object_info_str = Column(String)
    filename_list_cache = Column(JSON)
    extensions = Column(JSON)
    import_failed_logs = Column(String)
    machine_version_id = Column(
        UUID(as_uuid=True), ForeignKey("machine_versions.id", ondelete="set null")
    )
    is_workspace = Column(Boolean, nullable=False, default=False)
    optimized_runner = Column(Boolean, nullable=False, default=False)
    # Add shared columns
    locals().update(get_machine_columns())


class Secret(SerializableMixin, Base):
    __tablename__ = "secrets"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="cascade"), nullable=False)
    name = Column(String, nullable=False)
    org_id = Column(String)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    environment_variables = Column(JSON)


class MachineSecret(SerializableMixin, Base):
    __tablename__ = "machine_secrets"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True)
    machine_id = Column(
        UUID(as_uuid=True),
        ForeignKey("machines.id", ondelete="cascade"),
        nullable=False,
    )
    secret_id = Column(
        UUID(as_uuid=True), ForeignKey("secrets.id", ondelete="cascade"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), nullable=False)


class UserSettings(SerializableMixin, Base):
    __tablename__ = "user_settings"
    metadata = metadata

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id = Column(String, ForeignKey("users.id", ondelete="cascade"), nullable=False)
    org_id = Column(String)
    output_visibility = Column(
        Enum("public", "private", name="output_visibility"), default="public"
    )
    custom_output_bucket = Column(Boolean, default=False)
    s3_access_key_id = Column(String)
    s3_secret_access_key = Column(String)
    encrypted_s3_key = Column(String)
    s3_bucket_name = Column(String)
    assumed_role_arn = Column(String)
    s3_region = Column(String)
    use_cloudfront = Column(Boolean, default=False)
    cloudfront_domain = Column(String)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    api_version = Column(Enum("v1", "v2", name="api_version"), default="v2")

    spend_limit = Column(Float, default=500)
    max_spend_limit = Column(Float, default=1000)

    hugging_face_token = Column(String)
    workflow_limit = Column(Float)
    machine_limit = Column(Float)
    always_on_machine_limit = Column(Integer)
    credit = Column(Float, default=0)

    max_gpu = Column(Integer, default=0)
    enable_custom_output_bucket = Column(Boolean, default=False)
    # Optionally, add relationship to User model
    # user = relationship("User", back_populates="settings")

    # target_workflow = relationship("Workflow", foreign_keys=[target_workflow_id])
    # workflows = relationship("Workflow", back_populates="machine", foreign_keys=[Workflow.selected_machine_id])

    @classmethod
    def from_dict(cls, data: dict):
        """Create a UserSettings instance from a dictionary (e.g., from cache)"""
        if data is None:
            return None
            
        # Create new instance
        instance = cls()
        
        # Set attributes that exist in the model
        for key, value in data.items():
            if hasattr(instance, key) and not key.startswith('_'):
                # Handle datetime strings
                if key in ['created_at', 'updated_at'] and isinstance(value, str):
                    try:
                        from datetime import datetime
                        # Parse ISO format datetime string
                        if value.endswith('Z'):
                            value = value[:-1] + '+00:00'
                        value = datetime.fromisoformat(value)
                    except (ValueError, TypeError):
                        pass  # Keep original value if parsing fails
                
                # Handle UUID strings
                if key == 'id' and isinstance(value, str):
                    try:
                        from uuid import UUID
                        value = UUID(value)
                    except (ValueError, TypeError):
                        pass  # Keep original value if parsing fails
                        
                setattr(instance, key, value)
                
        return instance


class Model(SerializableMixin, Base):
    __tablename__ = "models"
    metadata = metadata

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id = Column(String, ForeignKey("users.id"))
    org_id = Column(String)
    description = Column(String)

    user_volume_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user_volume.id", ondelete="cascade"),
        nullable=False,
    )

    model_name = Column(String)
    folder_path = Column(String)
    target_symlink_path = Column(String)

    civitai_id = Column(String)
    civitai_version_id = Column(String)
    civitai_url = Column(String)
    civitai_download_url = Column(String)
    civitai_model_response = Column(JSON)

    hf_url = Column(String)
    s3_url = Column(String)
    download_progress = Column(Integer, default=0)

    user_url = Column(String, name="client_url")
    filehash_sha256 = Column(String, name="file_hash_sha256")

    is_public = Column(Boolean, nullable=False, default=True)
    status = Column(
        Enum("started", "success", "failed", "cancelled", name="resource_upload"),
        nullable=False,
        default="started",
    )
    upload_machine_id = Column(String)
    upload_type = Column(
        Enum(
            "civitai", "download-url", "huggingface", "other", name="model_upload_type"
        ),
        nullable=False,
    )
    model_type = Column(
        Enum(
            "checkpoint",
            "lora",
            "embedding",
            "vae",
            "clip",
            "clip_vision",
            "configs",
            "controlnet",
            "upscale_models",
            "ipadapter",
            "gligen",
            "unet",
            "custom",
            "custom_node",
            name="model_type",
        ),
        default="checkpoint",
    )
    error_log = Column(String)

    size = Column(BigInteger)

    deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserVolume(SerializableMixin, Base):
    __tablename__ = "user_volume"
    metadata = metadata

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id = Column(String, ForeignKey("users.id"))
    org_id = Column(String)
    volume_name = Column(String, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    disabled = Column(Boolean, nullable=False, default=False)


class GPUEvent(SerializableMixin, Base):
    __tablename__ = "gpu_events"
    metadata = metadata

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    org_id = Column(String)
    machine_id = Column(
        UUID(as_uuid=True), ForeignKey("machines.id", ondelete="set null")
    )
    start_time = Column(DateTime(timezone=True))
    end_time = Column(DateTime(timezone=True))
    gpu = Column(
        Enum(
            "CPU",
            "T4",
            "L4",
            "A10G",
            "L40S",
            "A100",
            "A100-80GB",
            "H100",
            "H200",
            "B200",
            name="machine_gpu",
        )
    )
    ws_gpu = Column(Enum("4090", name="workspace_machine_gpu"))
    gpu_provider = Column(
        Enum("runpod", "modal", "fal", name="gpu_provider"), nullable=False
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    cost_item_title = Column(String)
    cost = Column(Float, default=0)

    session_timeout = Column(Integer)
    session_id = Column(String)
    modal_function_id = Column(String)
    tunnel_url = Column(String)
    machine_version_id = Column(UUID(as_uuid=True))

    environment = Column(
        Enum(
            "staging",
            "production",
            "public-share",
            "private-share",
            "preview",
            name="deployment_environment",
        ),
    )


class SubscriptionStatus(SerializableMixin, Base):
    __tablename__ = "subscription_status"
    metadata = metadata

    stripe_customer_id = Column(String, primary_key=True)
    user_id = Column(String)
    org_id = Column(String)
    plan = Column(
        Enum(
            "basic",
            "pro",
            "enterprise",
            "creator",
            "business",
            "ws_basic",
            "ws_pro",
            name="subscription_plan",
        )
    )
    status = Column(
        Enum("active", "deleted", "paused", name="subscription_plan_status")
    )
    subscription_id = Column(String)
    subscription_item_plan_id = Column(String)
    subscription_item_api_id = Column(String)
    cancel_at_period_end = Column(Boolean, default=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    trial_end = Column(DateTime(timezone=True))
    trial_start = Column(DateTime(timezone=True))
    last_invoice_timestamp = Column(DateTime(timezone=True))


class FormSubmission(SerializableMixin, Base):
    __tablename__ = "form_submissions"
    metadata = metadata

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id = Column(String, ForeignKey("users.id", ondelete="set null"))
    org_id = Column(String)
    inputs = Column(JSON)
    call_booked = Column(Boolean, nullable=False, default=False)
    discord_thread_id = Column(String)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Asset(SerializableMixin, Base):
    __tablename__ = "assets"
    metadata = metadata

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"))
    org_id = Column(String)
    name = Column(String)
    is_folder = Column(Boolean, default=False)
    path = Column(String, default="/")
    file_size = Column(BigInteger)
    url = Column(String)
    mime_type = Column(String)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted = Column(Boolean, default=False)


class SharedWorkflow(SerializableMixin, Base):
    __tablename__ = "shared_workflows"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    org_id = Column(String)
    workflow_id = Column(UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False)
    workflow_version_id = Column(UUID(as_uuid=True), ForeignKey("workflow_versions.id"))
    workflow_export = Column(JSON, nullable=False)
    share_slug = Column(String, nullable=False, unique=True)
    title = Column(String, nullable=False)
    description = Column(String)
    cover_image = Column(String)
    is_public = Column(Boolean, default=True, nullable=False)
    view_count = Column(Integer, default=0, nullable=False)
    download_count = Column(Integer, default=0, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OutputShare(SerializableMixin, Base):
    __tablename__ = "output_shares"
    metadata = metadata

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    org_id = Column(String)
    run_id = Column(UUID(as_uuid=True), ForeignKey("workflow_runs.id"), nullable=False)
    output_id = Column(UUID(as_uuid=True), ForeignKey("workflow_run_outputs.id"), nullable=False)
    deployment_id = Column(UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=True)
    output_data = Column(JSON)
    inputs = Column(JSON)
    output_type = Column(
        Enum("image", "video", "3d", "other", name="output_type"),
        nullable=False,
        default="other"
    )
    visibility = Column(
        Enum("private", "public", "link", name="output_share_visibility"),
        nullable=False,
        default="private"
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    run = relationship("WorkflowRun", foreign_keys=[run_id])
    user = relationship("User", foreign_keys=[user_id])
    output = relationship("WorkflowRunOutput", foreign_keys=[output_id])
    deployment = relationship("Deployment", foreign_keys=[deployment_id])
