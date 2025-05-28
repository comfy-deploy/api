import modal
from typing import Dict, Any, Optional
import aiohttp
import asyncio
import io
import hashlib
import logging

# Create the Modal app
app = modal.App("image-optimizer")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define image with dependencies
image = modal.Image.debian_slim().pip_install([
    "pillow",
    "aiohttp",
    "logfire"
])

# Default optimization configuration
DEFAULT_CONFIG = {
    "format": "webp",
    "quality": 85,
    "max_width": 2048,
    "max_height": 2048,
    "progressive": True,
    "strip_metadata": True,
    "optimize": True,
}

@app.function(
    image=image,
    memory=2048,
    timeout=300,
    max_containers=10,
)
async def optimize_image(
    input_url: str,
    output_url: str,
    optimization_config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Download image from input_url, optimize it, upload to output_url
    """
    try:
        logger.info("Starting image optimization", extra={
            "has_input_url": bool(input_url),
            "has_output_url": bool(output_url),
            "config": optimization_config
        })
        
        # Download image
        image_data = await download_image(input_url)
        original_size = len(image_data)
        
        # Optimize image
        optimized_data = await process_image(image_data, optimization_config)
        optimized_size = len(optimized_data)
        
        # Upload optimized image
        await upload_image(optimized_data, output_url, optimization_config)
        
        compression_ratio = optimized_size / original_size if original_size > 0 else 1.0
        
        logger.info("Image optimization completed", extra={
            "original_size": original_size,
            "optimized_size": optimized_size,
            "compression_ratio": compression_ratio
        })
        
        return {
            "status": "success",
            "original_size": original_size,
            "optimized_size": optimized_size,
            "compression_ratio": compression_ratio
        }
        
    except Exception as e:
        logger.error("Image optimization failed", extra={
            "error": str(e),
            "config": optimization_config
        })
        return {
            "status": "error",
            "error": str(e)
        }


async def download_image(url: str) -> bytes:
    """Download image from presigned URL"""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Failed to download image: {response.status}")
            return await response.read()


async def upload_image(data: bytes, url: str, config: Dict[str, Any]) -> None:
    """Upload optimized image to presigned URL"""
    headers = {}
    if config.get('is_public', False):
        headers['x-amz-acl'] = 'public-read'
    
    async with aiohttp.ClientSession() as session:
        async with session.put(url, data=data, headers=headers) as response:
            if response.status not in [200, 201]:
                raise Exception(f"Failed to upload image: {response.status}")


async def process_image(image_data: bytes, config: Dict[str, Any]) -> bytes:
    """Process image with given configuration"""
    from PIL import Image
    
    # Merge with defaults
    opts = {**DEFAULT_CONFIG, **config}
    
    # Load image
    image = Image.open(io.BytesIO(image_data))
    
    # Handle auto-optimization
    if opts.get("auto_optimize", False):
        opts = auto_optimize_config(image, opts)
    
    # Convert color mode if needed
    if opts["format"].lower() in ["jpeg", "jpg"] and image.mode in ["RGBA", "LA"]:
        # Convert RGBA to RGB for JPEG
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
        image = background
    
    # Resize if needed
    if "max_width" in opts or "max_height" in opts:
        max_width = opts.get("max_width", image.width)
        max_height = opts.get("max_height", image.height)
        
        if image.width > max_width or image.height > max_height:
            image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    
    # Strip metadata if requested
    if opts["strip_metadata"]:
        data = list(image.getdata())
        clean_image = Image.new(image.mode, image.size)
        clean_image.putdata(data)
        image = clean_image
    
    # Save optimized image
    output_buffer = io.BytesIO()
    save_kwargs = {
        "format": opts["format"].upper(),
        "optimize": opts["optimize"],
    }
    
    if opts["format"].lower() in ["jpeg", "jpg", "webp"]:
        save_kwargs["quality"] = opts["quality"]
        if opts["format"].lower() == "jpeg" and opts["progressive"]:
            save_kwargs["progressive"] = True
    
    image.save(output_buffer, **save_kwargs)
    return output_buffer.getvalue()


def auto_optimize_config(image, base_config: Dict[str, Any]) -> Dict[str, Any]:
    """Intelligently choose optimization settings based on image characteristics"""
    config = base_config.copy()
    
    # Choose format based on image characteristics
    if image.mode in ["RGBA", "LA"] or "transparency" in image.info:
        # Images with transparency should use PNG or WebP
        config["format"] = "webp" if base_config.get("format") != "png" else "png"
    elif image.mode == "P" and len(image.getcolors() or []) <= 256:
        # Images with limited colors (like logos) should use PNG
        config["format"] = "png"
    else:
        # Photos should use WebP or JPEG
        config["format"] = "webp"
    
    # Adjust quality based on image size
    pixel_count = image.width * image.height
    if pixel_count > 2000000:  # Large images (>2MP)
        config["quality"] = min(config.get("quality", 85), 75)
    elif pixel_count < 100000:  # Small images (<0.1MP)
        config["quality"] = min(config.get("quality", 85), 95)
    
    return config