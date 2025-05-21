import modal
from cd_config import config
import os

public_model_volume = modal.Volume.from_name(config["public_model_volume"], create_if_missing=True)
private_volume = modal.Volume.from_name(config["private_model_volume"], create_if_missing=True)

PUBLIC_BASEMODEL_DIR = "/public_models"
PRIVATE_BASEMODEL_DIR_SYM = "/private_models"

volumes = {
    PUBLIC_BASEMODEL_DIR: public_model_volume,
    PRIVATE_BASEMODEL_DIR_SYM: private_volume,
}

# NOTE: long/specific name to avoid conflicts in the ComfyUI model directory
# Required because we can only symlink to the volume path, which doesn't exist during image build
INPUT_DIR = f"{PRIVATE_BASEMODEL_DIR_SYM}/comfydeploy-comfyui-inputs"
OUTPUT_DIR = f"{PRIVATE_BASEMODEL_DIR_SYM}/comfydeploy-comfyui-outputs"
