import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402
import pytest_asyncio  # noqa: E402
from uuid import uuid4  # noqa: E402
from upstash_redis import Redis
from datetime import datetime
from contextlib import asynccontextmanager  # noqa: E402
from api.models import User  # noqa: E402
import os
import json
from api.routes.utils import generate_persistent_token  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: E402
from sqlalchemy.pool import AsyncAdaptedQueuePool  # noqa: E402
from dotenv import load_dotenv

load_dotenv()

redis_url = os.getenv("UPSTASH_REDIS_META_REST_URL")
redis_token = os.getenv("UPSTASH_REDIS_META_REST_TOKEN")
# Use environment variables for database connection
DATABASE_URL = os.getenv("DATABASE_URL")

# Ensure the URL uses the asyncpg dialect
if DATABASE_URL and not DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")



# Configure engine with larger pool size and longer timeout
engine = create_async_engine(
    DATABASE_URL,
    poolclass=AsyncAdaptedQueuePool,
    pool_size=20,  # Increased from default 5
    max_overflow=30,  # Increased from default 10
    pool_timeout=60,  # Increased from default 30
    pool_pre_ping=True,  # Enable connection health checks
    pool_recycle=3600,  # Recycle connections after 1 hour
)

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


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


@asynccontextmanager
async def get_test_client(app, user):
    """Helper function to create a new client instance with async context manager support"""
    api_key = generate_persistent_token(user.id, None)
    client = AsyncClient(
        base_url=app + "/api",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120.0,  # 30 seconds timeout for all operations
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def test_client(app, user):
    """Fixture to provide an authenticated test client"""
    api_key = generate_persistent_token(user.id, None)
    client = AsyncClient(
        base_url=app + "/api",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120.0,
    )
    yield client
    await client.aclose()


basic_workflow_json = """
{
  "last_node_id": 20,
  "last_link_id": 13,
  "nodes": [
    {
      "id": 17,
      "type": "SaveImage",
      "pos": {
        "0": 999.4330444335938,
        "1": 372.1318664550781
      },
      "size": {
        "0": 315,
        "1": 58
      },
      "flags": {},
      "order": 1,
      "mode": 0,
      "inputs": [
        {
          "link": 13,
          "name": "images",
          "type": "IMAGE"
        }
      ],
      "outputs": [],
      "properties": {},
      "widgets_values": [
        "ComfyUI"
      ]
    },
    {
      "id": 16,
      "type": "ComfyUIDeployExternalImage",
      "pos": {
        "0": 387,
        "1": 424
      },
      "size": {
        "0": 390.5999755859375,
        "1": 366
      },
      "flags": {},
      "order": 0,
      "mode": 0,
      "inputs": [
        {
          "link": null,
          "name": "default_value",
          "type": "IMAGE",
          "shape": 7
        }
      ],
      "outputs": [
        {
          "name": "image",
          "type": "IMAGE",
          "links": [
            13
          ],
          "slot_index": 0
        }
      ],
      "properties": {
        "Node name for S&R": "ComfyUIDeployExternalImage"
      },
      "widgets_values": [
        "https://comfy-deploy-output-dev.s3.us-east-2.amazonaws.com/assets/img_bRFqDVG5VG87N29W.png",
        "",
        "",
        "https://comfy-deploy-output-dev.s3.us-east-2.amazonaws.com/assets/img_bRFqDVG5VG87N29W.png",
        ""
      ]
    }
  ],
  "links": [
    [
      13,
      16,
      0,
      17,
      0,
      "IMAGE"
    ]
  ],
  "groups": [],
  "config": {},
  "extra": {
    "ds": {
      "scale": 1.3109994191499965,
      "offset": {
        "0": -242.70708454566912,
        "1": -115.02119499557512
      }
    },
    "node_versions": {
      "comfy-core": "v0.2.4",
      "comfyui-deploy": "4073a43d3d04f6659acc7954f79a4fa7d83a3867"
    }
  },
  "version": 0.4
}
"""
basic_workflow_api_json = """
{
  "16": {
    "inputs": {
      "input_id": "https://comfy-deploy-output-dev.s3.us-east-2.amazonaws.com/assets/img_bRFqDVG5VG87N29W.png",
      "display_name": "",
      "description": "",
      "default_value_url": "https://comfy-deploy-output-dev.s3.us-east-2.amazonaws.com/assets/img_bRFqDVG5VG87N29W.png"
    },
    "class_type": "ComfyUIDeployExternalImage"
  },
  "17": {
    "inputs": {
      "filename_prefix": "ComfyUI",
      "images": [
        "16",
        0
      ]
    },
    "class_type": "SaveImage"
  }
}
"""

heavy_machine_1_data = {
    "name": "Heavy Machine 1",
    "type": "comfy-deploy-serverless",
    "gpu": "CPU",
    "wait_for_build": True,
    "comfyui_version": "75c1c757d90ca891eff823893248ef8b51d31d01",
    "machine_builder_version": "4",
    "docker_command_steps": {
        "steps": [
            {
                "id": "0a944b0c-3",
                "data": {
                    "url": "https://github.com/kijai/ComfyUI-KJNodes",
                    "hash": "c3dc82108a2a86c17094107ead61d63f8c76200e",
                    "meta": {
                        "message": "Fix ConditioningMultiCombine",
                        "committer": {
                            "date": "2025-04-25T08:19:49.000Z",
                            "name": "kijai",
                            "email": "40791699+kijai@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/kijai/ComfyUI-KJNodes/commit/c3dc82108a2a86c17094107ead61d63f8c76200e",
                        "latest_hash": "c3dc82108a2a86c17094107ead61d63f8c76200e",
                        "stargazers_count": 1286
                    },
                    "name": "KJNodes for ComfyUI",
                    "files": [
                        "https://github.com/kijai/ComfyUI-KJNodes"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "02c4ef4f-8",
                "data": {
                    "url": "https://github.com/lldacing/ComfyUI_BiRefNet_ll",
                    "hash": "617827cee90a137a5c770394a47e162b6fbde041",
                    "meta": {
                        "message": "Merge pull request #21 from ComfyNodePRs/update-publish-yaml\n\nUpdate Github Action for Publishing to Comfy Registry",
                        "committer": {
                            "date": "2025-03-28T06:38:43.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/lldacing/ComfyUI_BiRefNet_ll/commit/617827cee90a137a5c770394a47e162b6fbde041",
                        "latest_hash": "617827cee90a137a5c770394a47e162b6fbde041",
                        "stargazers_count": 195
                    },
                    "name": "ComfyUI_BiRefNet_ll",
                    "files": [
                        "https://github.com/lldacing/ComfyUI_BiRefNet_ll"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "f27f14d4-2",
                "data": {
                    "url": "https://github.com/cubiq/ComfyUI_essentials",
                    "hash": "9d9f4bedfc9f0321c19faf71855e228c93bd0dc9",
                    "meta": {
                        "message": "maintenance mode",
                        "committer": {
                            "date": "2025-04-14T07:33:21.000Z",
                            "name": "cubiq",
                            "email": "matteo@elf.io"
                        },
                        "commit_url": "https://github.com/cubiq/ComfyUI_essentials/commit/9d9f4bedfc9f0321c19faf71855e228c93bd0dc9",
                        "latest_hash": "9d9f4bedfc9f0321c19faf71855e228c93bd0dc9",
                        "stargazers_count": 810
                    },
                    "name": "ComfyUI Essentials",
                    "files": [
                        "https://github.com/cubiq/ComfyUI_essentials"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "8d4a6112-d",
                "data": {
                    "url": "https://github.com/kijai/ComfyUI-IC-Light",
                    "hash": "0208191a9bd2a214167c8a52237ecadd1fa0220c",
                    "meta": {
                        "message": "typo",
                        "committer": {
                            "date": "2025-01-23T16:59:45.000Z",
                            "name": "kijai",
                            "email": "40791699+kijai@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/kijai/ComfyUI-IC-Light/commit/0208191a9bd2a214167c8a52237ecadd1fa0220c",
                        "latest_hash": "0208191a9bd2a214167c8a52237ecadd1fa0220c",
                        "stargazers_count": 997
                    },
                    "name": "ComfyUI-IC-Light",
                    "files": [
                        "https://github.com/kijai/ComfyUI-IC-Light"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "d2894b86-d",
                "data": {
                    "url": "https://github.com/rgthree/rgthree-comfy",
                    "hash": "5dc53323e07a021038af9f2a4a06ebc071f7218c",
                    "meta": {
                        "message": "Hacks around a bug in ComfyUI Frontend with cyclical configuration data. Addresses #471",
                        "committer": {
                            "date": "2025-04-19T01:01:25.000Z",
                            "name": "rgthree",
                            "email": "regis.gaughan@gmail.com"
                        },
                        "commit_url": "https://github.com/rgthree/rgthree-comfy/commit/5dc53323e07a021038af9f2a4a06ebc071f7218c",
                        "latest_hash": "5dc53323e07a021038af9f2a4a06ebc071f7218c",
                        "stargazers_count": 1804
                    },
                    "name": "rgthree's ComfyUI Nodes",
                    "files": [
                        "https://github.com/rgthree/rgthree-comfy"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "a8d9657e-e",
                "data": {
                    "url": "https://github.com/yolain/ComfyUI-Easy-Use",
                    "hash": "1c4cb43f7b219048251d52251237a42f22790a62",
                    "meta": {
                        "message": "Fix line are removed at the end of a connection on easy related nodes #748",
                        "committer": {
                            "date": "2025-04-28T11:11:42.000Z",
                            "name": "yolain",
                            "email": "yolain@163.com"
                        },
                        "commit_url": "https://github.com/yolain/ComfyUI-Easy-Use/commit/1c4cb43f7b219048251d52251237a42f22790a62",
                        "latest_hash": "1c4cb43f7b219048251d52251237a42f22790a62",
                        "stargazers_count": 1523
                    },
                    "name": "ComfyUI Easy Use",
                    "files": [
                        "https://github.com/yolain/ComfyUI-Easy-Use"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "aeec996b-2",
                "data": {
                    "url": "https://github.com/huchenlei/ComfyUI-IC-Light-Native",
                    "hash": "40883a839d2a719ad3d3a454700609b94b1a9c65",
                    "meta": {
                        "message": "chore(publish): update GitHub Actions workflow for node publishing (#58)\n\n- Add permissions to allow issue writing\n- Update action version from `main` to `v1`\n- Add condition to run job only for specific repository owner",
                        "committer": {
                            "date": "2025-02-25T16:35:36.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/huchenlei/ComfyUI-IC-Light-Native/commit/40883a839d2a719ad3d3a454700609b94b1a9c65",
                        "latest_hash": "40883a839d2a719ad3d3a454700609b94b1a9c65",
                        "stargazers_count": 609
                    },
                    "name": "ComfyUI-IC-Light-Native",
                    "files": [
                        "https://github.com/huchenlei/ComfyUI-IC-Light-Native"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "45806450-f",
                "data": {
                    "pip": [],
                    "url": "https://github.com/cubiq/ComfyUI_IPAdapter_plus",
                    "hash": "a0f451a5113cf9becb0847b92884cb10cbdec0ef",
                    "meta": {
                        "message": "wording",
                        "committer": {
                            "date": "2025-04-14T07:29:11.000Z",
                            "name": "cubiq",
                            "email": "matteo@elf.io"
                        },
                        "latest_hash": "a0f451a5113cf9becb0847b92884cb10cbdec0ef"
                    },
                    "name": "ComfyUI_IPAdapter_plus",
                    "files": [
                        "https://github.com/cubiq/ComfyUI_IPAdapter_plus"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "2342fbf7-b",
                "data": {
                    "pip": [
                        "timm==0.6.13",
                        "transformers",
                        "fairscale",
                        "pycocoevalcap",
                        "opencv-python",
                        "qrcode[pil]",
                        "pytorch_lightning",
                        "kornia",
                        "pydantic",
                        "segment_anything",
                        "boto3>=1.34.101"
                    ],
                    "url": "https://github.com/sipherxyz/comfyui-art-venture",
                    "hash": "fc00f4a094be1ba41d6c7bfcc157fb075d289573",
                    "meta": {
                        "message": "fix(web): error when redefine value property",
                        "committer": {
                            "date": "2025-04-15T08:23:05.000Z",
                            "name": "Tung Nguyen",
                            "email": "tung.nguyen@atherlabs.com"
                        },
                        "latest_hash": "fc00f4a094be1ba41d6c7bfcc157fb075d289573"
                    },
                    "name": "ComfyUI ArtVenture",
                    "files": [
                        "https://github.com/sipherxyz/comfyui-art-venture"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            }
        ]
    },
    "allow_concurrent_inputs": 1,
}

heavy_machine_2_data = {
    "name": "Heavy Machine 2",
    "type": "comfy-deploy-serverless",
    "gpu": "CPU",
    "wait_for_build": True,
    "machine_builder_version": "4",
    "allow_concurrent_inputs": 1,
    "comfyui_version": "75c1c757d90ca891eff823893248ef8b51d31d01",
    "docker_command_steps": {
        "steps": [
            {
                "id": "5d9f45e5-c",
                "data": {
                    "url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                    "hash": "0b1ac0f1c5a395e17065821e4fd47aba3bf23900",
                    "meta": {
                        "message": "bump version to v8.10",
                        "committer": {
                            "date": "2025-03-23T15:06:42.000Z",
                            "name": "Dr.Lt.Data",
                            "email": "dr.lt.data@gmail.com"
                        },
                        "commit_url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack/commit/0b1ac0f1c5a395e17065821e4fd47aba3bf23900",
                        "latest_hash": "0b1ac0f1c5a395e17065821e4fd47aba3bf23900",
                        "stargazers_count": 2248
                    },
                    "name": "ComfyUI Impact Pack",
                    "files": [
                        "https://github.com/ltdrdata/ComfyUI-Impact-Pack"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "425ab5ad-e",
                "data": {
                    "url": "https://github.com/WASasquatch/was-node-suite-comfyui",
                    "hash": "9ae952b1b435d2bd846bfe6516919b5a8b9201aa",
                    "meta": {
                        "message": "Merge pull request #558 from zzhengyang/png-save-with-dpi\n\nsave png image with dpi",
                        "committer": {
                            "date": "2025-03-07T19:22:03.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/WASasquatch/was-node-suite-comfyui/commit/9ae952b1b435d2bd846bfe6516919b5a8b9201aa",
                        "latest_hash": "9ae952b1b435d2bd846bfe6516919b5a8b9201aa",
                        "stargazers_count": 1423
                    },
                    "name": "WAS Node Suite",
                    "files": [
                        "https://github.com/WASasquatch/was-node-suite-comfyui"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "4a90d185-7",
                "data": {
                    "url": "https://github.com/cubiq/ComfyUI_IPAdapter_plus",
                    "hash": "9d076a3df0d2763cef5510ec5ab807f6632c39f5",
                    "meta": {
                        "message": "Merge pull request #793 from svenrog/main\n\nFix for issue with pipeline in load_models",
                        "committer": {
                            "date": "2025-02-26T06:31:16.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/cubiq/ComfyUI_IPAdapter_plus/commit/9d076a3df0d2763cef5510ec5ab807f6632c39f5",
                        "latest_hash": "9d076a3df0d2763cef5510ec5ab807f6632c39f5",
                        "stargazers_count": 4851
                    },
                    "name": "ComfyUI_IPAdapter_plus",
                    "files": [
                        "https://github.com/cubiq/ComfyUI_IPAdapter_plus"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "a58cd26f-6",
                "data": {
                    "url": "https://github.com/cubiq/ComfyUI_essentials",
                    "hash": "33ff89fd354d8ec3ab6affb605a79a931b445d99",
                    "meta": {
                        "message": "interpolate_pos_encoding is not True by default",
                        "committer": {
                            "date": "2024-12-07T09:40:22.000Z",
                            "name": "cubiq",
                            "email": "matteo@elf.io"
                        },
                        "commit_url": "https://github.com/cubiq/ComfyUI_essentials/commit/33ff89fd354d8ec3ab6affb605a79a931b445d99",
                        "latest_hash": "33ff89fd354d8ec3ab6affb605a79a931b445d99",
                        "stargazers_count": 756
                    },
                    "name": "ComfyUI Essentials",
                    "files": [
                        "https://github.com/cubiq/ComfyUI_essentials"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "b1694378-a",
                "data": {
                    "url": "https://github.com/kijai/ComfyUI-KJNodes",
                    "hash": "a5bd3c86c8ed6b83c55c2d0e7a59515b15a0137f",
                    "meta": {
                        "message": "Make TeaCache node error out instead of silently fail if ComfyUI isn't new enough to pass transformer_options",
                        "committer": {
                            "date": "2025-03-19T08:13:08.000Z",
                            "name": "kijai",
                            "email": "40791699+kijai@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/kijai/ComfyUI-KJNodes/commit/a5bd3c86c8ed6b83c55c2d0e7a59515b15a0137f",
                        "latest_hash": "a5bd3c86c8ed6b83c55c2d0e7a59515b15a0137f",
                        "stargazers_count": 1070
                    },
                    "name": "KJNodes for ComfyUI",
                    "files": [
                        "https://github.com/kijai/ComfyUI-KJNodes"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "9a27d30b-5",
                "data": {
                    "url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
                    "hash": "c9dcc3a229437df232d61da4f9697c87c1f22428",
                    "meta": {
                        "message": "Merge 1.5.13",
                        "committer": {
                            "date": "2025-03-24T03:38:31.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite/commit/c9dcc3a229437df232d61da4f9697c87c1f22428",
                        "latest_hash": "c9dcc3a229437df232d61da4f9697c87c1f22428",
                        "stargazers_count": 889
                    },
                    "name": "ComfyUI-VideoHelperSuite",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "b3231297-b",
                "data": {
                    "url": "https://github.com/chrisgoringe/cg-use-everywhere",
                    "hash": "ccf8d95cb0678b611e5c082ca5d76a703a301539",
                    "meta": {
                        "message": "Update pyproject.toml",
                        "committer": {
                            "date": "2025-03-24T22:10:31.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/chrisgoringe/cg-use-everywhere/commit/ccf8d95cb0678b611e5c082ca5d76a703a301539",
                        "latest_hash": "ccf8d95cb0678b611e5c082ca5d76a703a301539",
                        "stargazers_count": 633
                    },
                    "name": "Use Everywhere (UE Nodes)",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "8c6f93c6-0",
                "data": {
                    "url": "https://github.com/Derfuu/Derfuu_ComfyUI_ModdedNodes",
                    "hash": "d0905bed31249f2bd0814c67585cf4fe3c77c015",
                    "meta": {
                        "message": "update version to 1.0.1",
                        "committer": {
                            "date": "2024-06-22T02:09:21.000Z",
                            "name": "Derfuu",
                            "email": "qwesterseven@yandex.ru"
                        },
                        "commit_url": "https://github.com/Derfuu/Derfuu_ComfyUI_ModdedNodes/commit/d0905bed31249f2bd0814c67585cf4fe3c77c015",
                        "latest_hash": "d0905bed31249f2bd0814c67585cf4fe3c77c015",
                        "stargazers_count": 397
                    },
                    "name": "Derfuu_ComfyUI_ModdedNodes",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "27447775-e",
                "data": {
                    "url": "https://github.com/Fannovel16/ComfyUI-Frame-Interpolation",
                    "hash": "18b7cce5b08290741929e39d4955b88382db4e1d",
                    "meta": {
                        "message": "Merge pull request #81 from cavivie/main\n\nfix: device mps does not support border padding mode",
                        "committer": {
                            "date": "2025-03-14T00:08:06.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/Fannovel16/ComfyUI-Frame-Interpolation/commit/18b7cce5b08290741929e39d4955b88382db4e1d",
                        "latest_hash": "18b7cce5b08290741929e39d4955b88382db4e1d",
                        "stargazers_count": 636
                    },
                    "name": "ComfyUI Frame Interpolation",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "6c23db55-1",
                "data": {
                    "url": "https://github.com/ltdrdata/ComfyUI-Impact-Subpack",
                    "hash": "74db20c95eca152a6d686c914edc0ef4e4762cb8",
                    "meta": {
                        "message": "update README.md",
                        "committer": {
                            "date": "2025-01-26T09:54:15.000Z",
                            "name": "Dr.Lt.Data",
                            "email": "dr.lt.data@gmail.com"
                        },
                        "commit_url": "https://github.com/ltdrdata/ComfyUI-Impact-Subpack/commit/74db20c95eca152a6d686c914edc0ef4e4762cb8",
                        "latest_hash": "74db20c95eca152a6d686c914edc0ef4e4762cb8",
                        "stargazers_count": 151
                    },
                    "name": "ComfyUI Impact Subpack",
                    "files": [
                        "https://github.com/ltdrdata/ComfyUI-Impact-Subpack"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "ce7a404a-c",
                "data": {
                    "url": "https://github.com/ltdrdata/ComfyUI-Inspire-Pack",
                    "hash": "b35b22d5638ca23cc414af4a9b4425c80b52029e",
                    "meta": {
                        "message": "improve: ForEachList - display progress of iteration\n\nhttps://github.com/ltdrdata/ComfyUI-Inspire-Pack/issues/216",
                        "committer": {
                            "date": "2025-03-23T14:29:58.000Z",
                            "name": "Dr.Lt.Data",
                            "email": "dr.lt.data@gmail.com"
                        },
                        "commit_url": "https://github.com/ltdrdata/ComfyUI-Inspire-Pack/commit/b35b22d5638ca23cc414af4a9b4425c80b52029e",
                        "latest_hash": "b35b22d5638ca23cc414af4a9b4425c80b52029e",
                        "stargazers_count": 537
                    },
                    "name": "ComfyUI Inspire Pack",
                    "files": [
                        "https://github.com/ltdrdata/ComfyUI-Inspire-Pack"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "b790df6d-2",
                "data": {
                    "url": "https://github.com/ltdrdata/comfyui-connection-helper",
                    "hash": "e022993d814349cb7955f3cfa70bb61b1cf4aff7",
                    "meta": {
                        "message": "update pyproject.toml",
                        "committer": {
                            "date": "2025-01-28T17:20:39.000Z",
                            "name": "Dr.Lt.Data",
                            "email": "dr.lt.data@gmail.com"
                        },
                        "commit_url": "https://github.com/ltdrdata/comfyui-connection-helper/commit/e022993d814349cb7955f3cfa70bb61b1cf4aff7",
                        "latest_hash": "e022993d814349cb7955f3cfa70bb61b1cf4aff7",
                        "stargazers_count": 15
                    },
                    "name": "ComfyUI Connection Helper",
                    "files": [
                        "https://github.com/ltdrdata/comfyui-connection-helper"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "5498f5f0-2",
                "data": {
                    "url": "https://github.com/comfyanonymous/ComfyUI_experiments",
                    "hash": "934dba9d206e4738e0dac26a09b51f1dffcb4e44",
                    "meta": {
                        "message": "Merge pull request #7 from bvhari/master\n\nAdd Tonemap Noise + Rescale CFG",
                        "committer": {
                            "date": "2023-09-13T06:28:20.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/comfyanonymous/ComfyUI_experiments/commit/934dba9d206e4738e0dac26a09b51f1dffcb4e44",
                        "latest_hash": "934dba9d206e4738e0dac26a09b51f1dffcb4e44",
                        "stargazers_count": 174
                    },
                    "name": "ComfyUI_experiments",
                    "files": [
                        "https://github.com/comfyanonymous/ComfyUI_experiments"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "80fd1446-2",
                "data": {
                    "url": "https://github.com/Fannovel16/comfyui_controlnet_aux",
                    "hash": "83463c2e4b04e729268e57f638b4212e0da4badc",
                    "meta": {
                        "message": "Merge some PR, bump version",
                        "committer": {
                            "date": "2025-03-11T20:05:02.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/Fannovel16/comfyui_controlnet_aux/commit/83463c2e4b04e729268e57f638b4212e0da4badc",
                        "latest_hash": "83463c2e4b04e729268e57f638b4212e0da4badc",
                        "stargazers_count": 2796
                    },
                    "name": "ComfyUI's ControlNet Auxiliary Preprocessors",
                    "files": [
                        "https://github.com/Fannovel16/comfyui_controlnet_aux"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "0bdb9e2f-d",
                "data": {
                    "url": "https://github.com/Fannovel16/ComfyUI-Video-Matting",
                    "hash": "dd5ff373c327ed9caa321bca54e4cab8104f3735",
                    "meta": {
                        "message": "Merge pull request #2 from haohaocreates/publish\n\nAdd Github Action for Publishing to Comfy Registry",
                        "committer": {
                            "date": "2024-06-20T16:14:38.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/Fannovel16/ComfyUI-Video-Matting/commit/dd5ff373c327ed9caa321bca54e4cab8104f3735",
                        "latest_hash": "dd5ff373c327ed9caa321bca54e4cab8104f3735",
                        "stargazers_count": 200
                    },
                    "name": "ComfyUI-Video-Matting",
                    "files": [
                        "https://github.com/Fannovel16/ComfyUI-Video-Matting"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "9d650cf5-c",
                "data": {
                    "url": "https://github.com/WASasquatch/FreeU_Advanced",
                    "hash": "a78e2979a33df1790544e7d414bb7bd133f3854c",
                    "meta": {
                        "message": "Merge pull request #13 from ComfyNodePRs/licence-update\n\nUpdate PyProject Toml - License",
                        "committer": {
                            "date": "2024-10-27T01:49:14.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/WASasquatch/FreeU_Advanced/commit/a78e2979a33df1790544e7d414bb7bd133f3854c",
                        "latest_hash": "a78e2979a33df1790544e7d414bb7bd133f3854c",
                        "stargazers_count": 119
                    },
                    "name": "FreeU_Advanced",
                    "files": [
                        "https://github.com/WASasquatch/FreeU_Advanced"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "88261f0c-9",
                "data": {
                    "url": "https://github.com/WASasquatch/WAS_Extras",
                    "hash": "1aa294579a52d380c4c4a446defb189d73ff903b",
                    "meta": {
                        "message": "Bump version (no change)",
                        "committer": {
                            "date": "2024-06-17T04:08:31.000Z",
                            "name": "Jordan Thompson",
                            "email": "jordan.thompson@plailabs.com"
                        },
                        "commit_url": "https://github.com/WASasquatch/WAS_Extras/commit/1aa294579a52d380c4c4a446defb189d73ff903b",
                        "latest_hash": "1aa294579a52d380c4c4a446defb189d73ff903b",
                        "stargazers_count": 33
                    },
                    "name": "WAS_Extras",
                    "files": [
                        "https://github.com/WASasquatch/WAS_Extras"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "5327f19e-b",
                "data": {
                    "url": "https://github.com/city96/SD-Latent-Upscaler",
                    "hash": "89543b07f2c5eccb80ea08102d139323be04e6aa",
                    "meta": {
                        "message": "Merge pull request #3 from yoinked-h/patch-1\n\nadd noise mask support (inpainting)",
                        "committer": {
                            "date": "2023-11-27T00:26:14.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/city96/SD-Latent-Upscaler/commit/89543b07f2c5eccb80ea08102d139323be04e6aa",
                        "latest_hash": "89543b07f2c5eccb80ea08102d139323be04e6aa",
                        "stargazers_count": 151
                    },
                    "name": "SD-Latent-Upscaler",
                    "files": [
                        "https://github.com/city96/SD-Latent-Upscaler"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "f5e7a333-f",
                "data": {
                    "url": "https://github.com/city96/ComfyUI-GGUF",
                    "hash": "bc5223b0e37e053dbec2ea5e5f52c2fd4b8f712a",
                    "meta": {
                        "message": "Fix qtype logging on newer gguf-py",
                        "committer": {
                            "date": "2025-03-16T19:14:33.000Z",
                            "name": "City",
                            "email": "125218114+city96@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/city96/ComfyUI-GGUF/commit/bc5223b0e37e053dbec2ea5e5f52c2fd4b8f712a",
                        "latest_hash": "bc5223b0e37e053dbec2ea5e5f52c2fd4b8f712a",
                        "stargazers_count": 1725
                    },
                    "name": "ComfyUI-GGUF",
                    "files": [
                        "https://github.com/city96/ComfyUI-GGUF"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "f5de4f24-4",
                "data": {
                    "url": "https://github.com/pythongosssss/ComfyUI-WD14-Tagger",
                    "hash": "d33501765c5bf3dca6e90e0ebaa962890999fbc5",
                    "meta": {
                        "message": "Merge branch 'main' of https://github.com/pythongosssss/ComfyUI-WD14-Tagger",
                        "committer": {
                            "date": "2024-10-23T19:52:57.000Z",
                            "name": "pythongosssss",
                            "email": "125205205+pythongosssss@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/pythongosssss/ComfyUI-WD14-Tagger/commit/d33501765c5bf3dca6e90e0ebaa962890999fbc5",
                        "latest_hash": "d33501765c5bf3dca6e90e0ebaa962890999fbc5",
                        "stargazers_count": 810
                    },
                    "name": "ComfyUI WD 1.4 Tagger",
                    "files": [
                        "https://github.com/pythongosssss/ComfyUI-WD14-Tagger"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "54eaa18d-7",
                "data": {
                    "url": "https://github.com/TinyTerra/ComfyUI_tinyterraNodes",
                    "hash": "c669e6cff81ba44cce91c9eee3236c1a64725394",
                    "meta": {
                        "message": "Merge pull request #186 from ComfyNodePRs/update-publish-yaml\n\nUpdate Github Action for Publishing to Comfy Registry",
                        "committer": {
                            "date": "2025-03-14T08:21:19.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/TinyTerra/ComfyUI_tinyterraNodes/commit/c669e6cff81ba44cce91c9eee3236c1a64725394",
                        "latest_hash": "c669e6cff81ba44cce91c9eee3236c1a64725394",
                        "stargazers_count": 487
                    },
                    "name": "ComfyUI_tinyterraNodes",
                    "files": [
                        "https://github.com/TinyTerra/ComfyUI_tinyterraNodes"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "326a1771-3",
                "data": {
                    "url": "https://github.com/chflame163/ComfyUI_LayerStyle",
                    "hash": "4b273d4f08ea28b0743ababab70ae2e6d93be194",
                    "meta": {
                        "message": "update readme",
                        "committer": {
                            "date": "2025-03-12T06:43:27.000Z",
                            "name": "chflame163",
                            "email": "chflame@163.com"
                        },
                        "commit_url": "https://github.com/chflame163/ComfyUI_LayerStyle/commit/4b273d4f08ea28b0743ababab70ae2e6d93be194",
                        "latest_hash": "4b273d4f08ea28b0743ababab70ae2e6d93be194",
                        "stargazers_count": 2070
                    },
                    "name": "ComfyUI Layer Style",
                    "files": [
                        "https://github.com/chflame163/ComfyUI_LayerStyle"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "068203b3-5",
                "data": {
                    "url": "https://github.com/chflame163/ComfyUI_LayerStyle_Advance",
                    "hash": "89aadaa6c0b8c9adfab86f5d0196f1d13383d47c",
                    "meta": {
                        "message": "update readme",
                        "committer": {
                            "date": "2025-03-12T06:44:08.000Z",
                            "name": "chflame163",
                            "email": "chflame@163.com"
                        },
                        "commit_url": "https://github.com/chflame163/ComfyUI_LayerStyle_Advance/commit/89aadaa6c0b8c9adfab86f5d0196f1d13383d47c",
                        "latest_hash": "89aadaa6c0b8c9adfab86f5d0196f1d13383d47c",
                        "stargazers_count": 214
                    },
                    "name": "ComfyUI_LayerStyle_Advance",
                    "files": [
                        "https://github.com/chflame163/ComfyUI_LayerStyle_Advance"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "3e3b1b8d-c",
                "data": {
                    "url": "https://github.com/hayden-fr/ComfyUI-Model-Manager",
                    "hash": "811f1bc3521c02e655392e5185ac09745e160452",
                    "meta": {
                        "message": "Support optional in py3.9 (#165)\n\n* fix: support optional in py3.9\n\n* prepare release 2.5.4",
                        "committer": {
                            "date": "2025-03-14T09:02:54.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/hayden-fr/ComfyUI-Model-Manager/commit/811f1bc3521c02e655392e5185ac09745e160452",
                        "latest_hash": "811f1bc3521c02e655392e5185ac09745e160452",
                        "stargazers_count": 112
                    },
                    "name": "ComfyUI-Model-Manager",
                    "files": [
                        "https://github.com/hayden-fr/ComfyUI-Model-Manager"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "94010adf-b",
                "data": {
                    "url": "https://github.com/kijai/ComfyUI-WanVideoWrapper.git",
                    "hash": "12eef5bb83b43d09c59b08592e37a489ff1cb86d",
                    "meta": {
                        "message": "Disable enhance-a-video when sampling single image",
                        "committer": {
                            "date": "2025-03-25T09:08:47.000Z",
                            "name": "kijai",
                            "email": "40791699+kijai@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/kijai/ComfyUI-WanVideoWrapper/commit/12eef5bb83b43d09c59b08592e37a489ff1cb86d",
                        "latest_hash": "12eef5bb83b43d09c59b08592e37a489ff1cb86d",
                        "stargazers_count": 1614
                    },
                    "name": "ComfyUI-WanVideoWrapper.git",
                    "files": [
                        "https://github.com/kijai/ComfyUI-WanVideoWrapper.git"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "4f2eda81-f",
                "data": {
                    "url": "https://github.com/kijai/ComfyUI-SUPIR",
                    "hash": "53fc4f82f139e0875e1f4f3716fbeafa073e4242",
                    "meta": {
                        "message": "clean some unnecessary dependencies",
                        "committer": {
                            "date": "2024-07-06T23:44:51.000Z",
                            "name": "kijai",
                            "email": "40791699+kijai@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/kijai/ComfyUI-SUPIR/commit/53fc4f82f139e0875e1f4f3716fbeafa073e4242",
                        "latest_hash": "53fc4f82f139e0875e1f4f3716fbeafa073e4242",
                        "stargazers_count": 1823
                    },
                    "name": "ComfyUI-SUPIR",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "1b3c3190-c",
                "data": {
                    "url": "https://github.com/shadowcz007/comfyui-mixlab-nodes",
                    "hash": "b2bb1876def6330fccf1e03cc69d2166cae7bedb",
                    "meta": {
                        "message": "Merge pull request #393 from wengxiaoxiong/main\n\nfix: Pillow 10.0.0+ compatibility",
                        "committer": {
                            "date": "2025-02-05T10:24:45.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/shadowcz007/comfyui-mixlab-nodes/commit/b2bb1876def6330fccf1e03cc69d2166cae7bedb",
                        "latest_hash": "b2bb1876def6330fccf1e03cc69d2166cae7bedb",
                        "stargazers_count": 1539
                    },
                    "name": "comfyui-mixlab-nodes",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "e5e078a0-e",
                "data": {
                    "url": "https://github.com/Suzie1/ComfyUI_Comfyroll_CustomNodes",
                    "hash": "d78b780ae43fcf8c6b7c6505e6ffb4584281ceca",
                    "meta": {
                        "message": "version 1.76",
                        "committer": {
                            "date": "2024-01-24T22:44:09.000Z",
                            "name": "Suzie1",
                            "email": "7.lyssa@gmail.com"
                        },
                        "commit_url": "https://github.com/Suzie1/ComfyUI_Comfyroll_CustomNodes/commit/d78b780ae43fcf8c6b7c6505e6ffb4584281ceca",
                        "latest_hash": "d78b780ae43fcf8c6b7c6505e6ffb4584281ceca",
                        "stargazers_count": 821
                    },
                    "name": "Comfyroll Studio",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "d6496cb4-b",
                "data": {
                    "url": "https://github.com/Jonseed/ComfyUI-Detail-Daemon",
                    "hash": "f391accbda2d309cdcbec65cb9fcc80a41197b20",
                    "meta": {
                        "message": "fixing node publish error to comfy registry",
                        "committer": {
                            "date": "2025-03-14T16:47:32.000Z",
                            "name": "Jonseed",
                            "email": "40897189+Jonseed@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/Jonseed/ComfyUI-Detail-Daemon/commit/f391accbda2d309cdcbec65cb9fcc80a41197b20",
                        "latest_hash": "f391accbda2d309cdcbec65cb9fcc80a41197b20",
                        "stargazers_count": 629
                    },
                    "name": "ComfyUI-Detail-Daemon",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "c3faefb1-3",
                "data": {
                    "url": "https://github.com/storyicon/comfyui_segment_anything",
                    "hash": "ab6395596399d5048639cdab7e44ec9fae857a93",
                    "meta": {
                        "message": "Merge pull request #54 from frantic/patch-1\n\nAvoid installing dependencies on every run",
                        "committer": {
                            "date": "2024-03-21T11:41:45.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/storyicon/comfyui_segment_anything/commit/ab6395596399d5048639cdab7e44ec9fae857a93",
                        "latest_hash": "ab6395596399d5048639cdab7e44ec9fae857a93",
                        "stargazers_count": 892
                    },
                    "name": "segment anything",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "745a894a-c",
                "data": {
                    "url": "https://github.com/pythongosssss/ComfyUI-Custom-Scripts",
                    "hash": "9f7b3215e6af317603056a9a1666bf6e83e28835",
                    "meta": {
                        "message": "Merge pull request #452 from ComfyNodePRs/update-publish-yaml\n\nUpdate Github Action for Publishing to Comfy Registry",
                        "committer": {
                            "date": "2025-03-16T13:01:51.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/pythongosssss/ComfyUI-Custom-Scripts/commit/9f7b3215e6af317603056a9a1666bf6e83e28835",
                        "latest_hash": "9f7b3215e6af317603056a9a1666bf6e83e28835",
                        "stargazers_count": 2269
                    },
                    "name": "pythongosssss/ComfyUI-Custom-Scripts",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "0320767c-0",
                "data": {
                    "url": "https://github.com/crystian/ComfyUI-Crystools",
                    "hash": "576b44b9b79e3bf4b5d50457a28924d89a42e7e1",
                    "meta": {
                        "message": "Merge branch 'main' of https://github.com/crystian/ComfyUI-Crystools",
                        "committer": {
                            "date": "2025-03-07T21:21:26.000Z",
                            "name": "Crystian",
                            "email": "crystian@gmail.com"
                        },
                        "commit_url": "https://github.com/crystian/ComfyUI-Crystools/commit/576b44b9b79e3bf4b5d50457a28924d89a42e7e1",
                        "latest_hash": "576b44b9b79e3bf4b5d50457a28924d89a42e7e1",
                        "stargazers_count": 1083
                    },
                    "name": "Crystools",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "d7778d93-0",
                "data": {
                    "url": "https://github.com/ssitu/ComfyUI_UltimateSDUpscale",
                    "hash": "778a475dde8116a2066fe07f6c9ca15554e0b5be",
                    "meta": {
                        "message": "Merge pull request #141 from Visionatrix/fix/submodule\n\ncorrect submodule download",
                        "committer": {
                            "date": "2025-03-06T14:58:45.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/ssitu/ComfyUI_UltimateSDUpscale/commit/778a475dde8116a2066fe07f6c9ca15554e0b5be",
                        "latest_hash": "778a475dde8116a2066fe07f6c9ca15554e0b5be",
                        "stargazers_count": 1074
                    },
                    "name": "UltimateSDUpscale",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "12777960-e",
                "data": {
                    "url": "https://github.com/BoyuanJiang/FitDiT-ComfyUI",
                    "hash": "17215e36598123b792b4de050511e76c939793b8",
                    "meta": {
                        "message": "Merge remote-tracking branch 'upstream/FitDiT-ComfyUI'",
                        "committer": {
                            "date": "2025-01-21T12:09:05.000Z",
                            "name": "GitHub Actions",
                            "email": "actions@github.com"
                        },
                        "commit_url": "https://github.com/BoyuanJiang/FitDiT-ComfyUI/commit/17215e36598123b792b4de050511e76c939793b8",
                        "latest_hash": "17215e36598123b792b4de050511e76c939793b8",
                        "stargazers_count": 91
                    },
                    "name": "FitDiT[official] - High-fidelity Virtual Try-on",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "a28bc642-0",
                "data": {
                    "url": "https://github.com/BadCafeCode/masquerade-nodes-comfyui",
                    "hash": "432cb4d146a391b387a0cd25ace824328b5b61cf",
                    "meta": {
                        "message": "Recommend alternative node packs in the README",
                        "committer": {
                            "date": "2024-06-19T04:16:26.000Z",
                            "name": "Jacob Segal",
                            "email": "jacob.e.segal@gmail.com"
                        },
                        "commit_url": "https://github.com/BadCafeCode/masquerade-nodes-comfyui/commit/432cb4d146a391b387a0cd25ace824328b5b61cf",
                        "latest_hash": "432cb4d146a391b387a0cd25ace824328b5b61cf",
                        "stargazers_count": 399
                    },
                    "name": "Masquerade Nodes",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "783f9a54-a",
                "data": {
                    "url": "https://github.com/yolain/ComfyUI-Easy-Use",
                    "hash": "4f694195a28c20ace34cd0ed5e747bebaf5cb8a7",
                    "meta": {
                        "message": "Fix samplers can't display output image",
                        "committer": {
                            "date": "2025-03-27T07:22:02.000Z",
                            "name": "yolain",
                            "email": "yolain@163.com"
                        },
                        "commit_url": "https://github.com/yolain/ComfyUI-Easy-Use/commit/4f694195a28c20ace34cd0ed5e747bebaf5cb8a7",
                        "latest_hash": "4f694195a28c20ace34cd0ed5e747bebaf5cb8a7",
                        "stargazers_count": 1437
                    },
                    "name": "ComfyUI Easy Use",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "207bb6b4-6",
                "data": {
                    "url": "https://github.com/807502278/ComfyUI-WJNodes",
                    "hash": "859de78d3dd6cbd11b3573a337f94c4cb565b66f",
                    "meta": {
                        "message": "Add mask clipping parameter settings",
                        "committer": {
                            "date": "2025-03-25T08:37:16.000Z",
                            "name": "807502278",
                            "email": "807502278@qq.com"
                        },
                        "commit_url": "https://github.com/807502278/ComfyUI-WJNodes/commit/859de78d3dd6cbd11b3573a337f94c4cb565b66f",
                        "latest_hash": "859de78d3dd6cbd11b3573a337f94c4cb565b66f",
                        "stargazers_count": 11
                    },
                    "name": "ComfyUI-WJNodes",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "8acf83e9-9",
                "data": {
                    "url": "https://github.com/rgthree/rgthree-comfy",
                    "hash": "cb3612624037ad50d76a8caf44ed58694648e245",
                    "meta": {
                        "message": "Fix case where the link type is not defined (which shouldn't happen but was in #447)",
                        "committer": {
                            "date": "2025-03-26T03:00:35.000Z",
                            "name": "rgthree",
                            "email": "regis.gaughan@gmail.com"
                        },
                        "commit_url": "https://github.com/rgthree/rgthree-comfy/commit/cb3612624037ad50d76a8caf44ed58694648e245",
                        "latest_hash": "cb3612624037ad50d76a8caf44ed58694648e245",
                        "stargazers_count": 1671
                    },
                    "name": "rgthree's ComfyUI Nodes",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "df85b674-b",
                "data": {
                    "url": "https://github.com/Gourieff/ComfyUI-ReActor",
                    "hash": "d901609a1d5d1942a6b069b2f8f3778fee3a7134",
                    "meta": {
                        "message": "UPD: Comfy Registry",
                        "committer": {
                            "date": "2025-03-09T17:02:29.000Z",
                            "name": "Gourieff | 古仁",
                            "email": "gourieff@gmail.com"
                        },
                        "commit_url": "https://github.com/Gourieff/ComfyUI-ReActor/commit/d901609a1d5d1942a6b069b2f8f3778fee3a7134",
                        "latest_hash": "d901609a1d5d1942a6b069b2f8f3778fee3a7134",
                        "stargazers_count": 398
                    },
                    "name": "comfyui-reactor-node",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "d740c060-4",
                "data": {
                    "url": "https://github.com/omar92/ComfyUI-QualityOfLifeSuit_Omar92",
                    "hash": "f09d10dea0afbd3984a284acf8f0913a634e36ec",
                    "meta": {
                        "message": "Update README.md",
                        "committer": {
                            "date": "2024-09-10T14:16:30.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/omar92/ComfyUI-QualityOfLifeSuit_Omar92/commit/f09d10dea0afbd3984a284acf8f0913a634e36ec",
                        "latest_hash": "f09d10dea0afbd3984a284acf8f0913a634e36ec",
                        "stargazers_count": 147
                    },
                    "name": "Quality of life Suit:V2",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "9db6de15-9",
                "data": {
                    "url": "https://github.com/Trung0246/ComfyUI-0246",
                    "hash": "d09cb3fe8266e4731316955a99c28dd721ee26cd",
                    "meta": {
                        "message": "Bump version",
                        "committer": {
                            "date": "2025-03-15T03:39:33.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/Trung0246/ComfyUI-0246/commit/d09cb3fe8266e4731316955a99c28dd721ee26cd",
                        "latest_hash": "d09cb3fe8266e4731316955a99c28dd721ee26cd",
                        "stargazers_count": 120
                    },
                    "name": "ComfyUI-0246",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "8f7d6fd8-c",
                "data": {
                    "url": "https://github.com/welltop-cn/ComfyUI-TeaCache",
                    "hash": "a6140dea8c139e628082c4fb6b56f53449296384",
                    "meta": {
                        "message": "Support retention mode for Wan2.1 models",
                        "committer": {
                            "date": "2025-03-26T10:07:58.000Z",
                            "name": "YunjieYu",
                            "email": "yjyu007@163.com"
                        },
                        "commit_url": "https://github.com/welltop-cn/ComfyUI-TeaCache/commit/a6140dea8c139e628082c4fb6b56f53449296384",
                        "latest_hash": "a6140dea8c139e628082c4fb6b56f53449296384",
                        "stargazers_count": 627
                    },
                    "name": "ComfyUI-TeaCache",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "facd7ec1-f",
                "data": {
                    "url": "https://github.com/lquesada/ComfyUI-Inpaint-CropAndStitch",
                    "hash": "1d93a54f8d65db4268d6af32e3d58c3d45dd505e",
                    "meta": {
                        "message": "Merge pull request #67 from ComfyNodePRs/update-publish-yaml\n\nUpdate Github Action for Publishing to Comfy Registry",
                        "committer": {
                            "date": "2025-03-27T15:01:08.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/lquesada/ComfyUI-Inpaint-CropAndStitch/commit/1d93a54f8d65db4268d6af32e3d58c3d45dd505e",
                        "latest_hash": "1d93a54f8d65db4268d6af32e3d58c3d45dd505e",
                        "stargazers_count": 571
                    },
                    "name": "ComfyUI-Inpaint-CropAndStitch",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "f59fb76c-5",
                "data": {
                    "url": "https://github.com/cubiq/PuLID_ComfyUI",
                    "hash": "4e1fd4024cae77a0c53edb8ecc3c8ee04027ebef",
                    "meta": {
                        "message": "Merge pull request #84 from balazik/fix-face-selection\n\nFixed face selection by removing reverse=True in apply_pulid function",
                        "committer": {
                            "date": "2024-10-05T16:21:01.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/cubiq/PuLID_ComfyUI/commit/4e1fd4024cae77a0c53edb8ecc3c8ee04027ebef",
                        "latest_hash": "4e1fd4024cae77a0c53edb8ecc3c8ee04027ebef",
                        "stargazers_count": 825
                    },
                    "name": "PuLID_ComfyUI",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "c8e65362-d",
                "data": {
                    "url": "https://github.com/JPS-GER/ComfyUI_JPS-Nodes",
                    "hash": "0e2a9aca02b17dde91577bfe4b65861df622dcaf",
                    "meta": {
                        "message": "Add files via upload",
                        "committer": {
                            "date": "2024-04-21T09:44:11.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/JPS-GER/ComfyUI_JPS-Nodes/commit/0e2a9aca02b17dde91577bfe4b65861df622dcaf",
                        "latest_hash": "0e2a9aca02b17dde91577bfe4b65861df622dcaf",
                        "stargazers_count": 67
                    },
                    "name": "JPS Custom Nodes for ComfyUI",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "7e749330-c",
                "data": {
                    "url": "https://github.com/cubiq/ComfyUI_InstantID",
                    "hash": "1ef34ef573581bd9727c1e0ac05aa956b356a510",
                    "meta": {
                        "message": "Merge pull request #224 from m-Jawa-d/main\n\nAdded Chinese (Simplified) translation for README",
                        "committer": {
                            "date": "2024-09-30T08:54:04.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/cubiq/ComfyUI_InstantID/commit/1ef34ef573581bd9727c1e0ac05aa956b356a510",
                        "latest_hash": "1ef34ef573581bd9727c1e0ac05aa956b356a510",
                        "stargazers_count": 1550
                    },
                    "name": "ComfyUI InstantID (Native Support)",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "4bdc3486-1",
                "data": {
                    "url": "https://github.com/jamesWalker55/comfyui-various",
                    "hash": "5bd85aaf7616878471469c4ec7e11bbd0cef3bf2",
                    "meta": {
                        "message": "New experimental sound module",
                        "committer": {
                            "date": "2025-02-27T11:01:47.000Z",
                            "name": "James Walker",
                            "email": "james.chunho@gmail.com"
                        },
                        "commit_url": "https://github.com/jamesWalker55/comfyui-various/commit/5bd85aaf7616878471469c4ec7e11bbd0cef3bf2",
                        "latest_hash": "5bd85aaf7616878471469c4ec7e11bbd0cef3bf2",
                        "stargazers_count": 87
                    },
                    "name": "Various ComfyUI Nodes by Type",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "3593b39b-9",
                "data": {
                    "url": "https://github.com/Nourepide/ComfyUI-Allor",
                    "hash": "af9caecc2a4e3d432be6aa8b7826da0bc1bb420c",
                    "meta": {
                        "message": "Update logo.",
                        "committer": {
                            "date": "2023-12-15T18:50:01.000Z",
                            "name": "Nourepide",
                            "email": "nourepide@gmail.com"
                        },
                        "commit_url": "https://github.com/Nourepide/ComfyUI-Allor/commit/af9caecc2a4e3d432be6aa8b7826da0bc1bb420c",
                        "latest_hash": "af9caecc2a4e3d432be6aa8b7826da0bc1bb420c",
                        "stargazers_count": 252
                    },
                    "name": "Allor Plugin",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "4ac24e59-1",
                "data": {
                    "url": "https://github.com/ZHO-ZHO-ZHO/ComfyUI-Text_Image-Composite",
                    "hash": "47c1531abd59a5315aa0092536867745711ff897",
                    "meta": {
                        "message": "20240418 新增个人详情",
                        "committer": {
                            "date": "2024-04-17T20:02:42.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/ZHO-ZHO-ZHO/ComfyUI-Text_Image-Composite/commit/47c1531abd59a5315aa0092536867745711ff897",
                        "latest_hash": "47c1531abd59a5315aa0092536867745711ff897",
                        "stargazers_count": 107
                    },
                    "name": "ComfyUI-Text_Image-Composite [WIP]",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "c7c24c8a-c",
                "data": {
                    "url": "https://github.com/palant/image-resize-comfyui",
                    "hash": "ae5888637742ff1668b6cd32954ba48d81dbd39d",
                    "meta": {
                        "message": "Fixes #2 - Some input values might be missing during validation",
                        "committer": {
                            "date": "2024-01-18T20:59:51.000Z",
                            "name": "Wladimir Palant",
                            "email": "374261+palant@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/palant/image-resize-comfyui/commit/ae5888637742ff1668b6cd32954ba48d81dbd39d",
                        "latest_hash": "ae5888637742ff1668b6cd32954ba48d81dbd39d",
                        "stargazers_count": 89
                    },
                    "name": "Image Resize for ComfyUI",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "fb57dcfe-6",
                "data": {
                    "url": "https://github.com/vuongminh1907/ComfyUI_ZenID",
                    "hash": "21b90a97c530df6e218cdbe5117a0b331708eab6",
                    "meta": {
                        "message": "Update README.md",
                        "committer": {
                            "date": "2025-03-27T00:11:22.000Z",
                            "name": "GitHub",
                            "email": "noreply@github.com"
                        },
                        "commit_url": "https://github.com/vuongminh1907/ComfyUI_ZenID/commit/21b90a97c530df6e218cdbe5117a0b331708eab6",
                        "latest_hash": "21b90a97c530df6e218cdbe5117a0b331708eab6",
                        "stargazers_count": 154
                    },
                    "name": "ComfyUI_ZenID",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "d7ba2e45-b",
                "data": {
                    "url": "https://github.com/sergekatzmann/ComfyUI_Nimbus-Pack",
                    "hash": "abf2252a9327764f89e6c498dcab0db6c2e5c194",
                    "meta": {
                        "message": "Resolution presets and calculations",
                        "committer": {
                            "date": "2024-04-06T15:37:21.000Z",
                            "name": "Serge Katzmann",
                            "email": "serge.katzmann@gmail.com"
                        },
                        "commit_url": "https://github.com/sergekatzmann/ComfyUI_Nimbus-Pack/commit/abf2252a9327764f89e6c498dcab0db6c2e5c194",
                        "latest_hash": "abf2252a9327764f89e6c498dcab0db6c2e5c194",
                        "stargazers_count": 4
                    },
                    "name": "ComfyUI_Nimbus-Pack",
                    "files": [
                        "https://github.com/sergekatzmann/ComfyUI_Nimbus-Pack"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "91ae1e7d-c",
                "data": {
                    "url": "https://github.com/kijai/ComfyUI-CogVideoXWrapper",
                    "hash": "dbc63f622dd095391335612d0c7d7bbff8745cc8",
                    "meta": {
                        "message": "some tweaks to test I2V with context windows, add context window preview",
                        "committer": {
                            "date": "2025-01-28T20:40:58.000Z",
                            "name": "kijai",
                            "email": "40791699+kijai@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/kijai/ComfyUI-CogVideoXWrapper/commit/dbc63f622dd095391335612d0c7d7bbff8745cc8",
                        "latest_hash": "dbc63f622dd095391335612d0c7d7bbff8745cc8",
                        "stargazers_count": 1458
                    },
                    "name": "ComfyUI CogVideoX Wrapper",
                    "files": [],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "e79e5fd7-1",
                "data": {
                    "url": "https://github.com/hay86/ComfyUI_AceNodes",
                    "hash": "25c95b8cde96cda112a4075d1f5ef740a0063bc4",
                    "meta": {
                        "message": "typo",
                        "committer": {
                            "date": "2025-03-06T22:19:09.000Z",
                            "name": "xukf",
                            "email": "xukaifeng1986@gmail.com"
                        },
                        "commit_url": "https://github.com/hay86/ComfyUI_AceNodes/commit/25c95b8cde96cda112a4075d1f5ef740a0063bc4",
                        "latest_hash": "25c95b8cde96cda112a4075d1f5ef740a0063bc4",
                        "stargazers_count": 61
                    },
                    "name": "ComfyUI_AceNodes",
                    "files": [
                        "https://github.com/hay86/ComfyUI_AceNodes"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "50622a02-f",
                "data": "RUN wget --no-check-certificate 'https://drive.google.com/uc?export=download&id=1-i5IyCbaoPb4Qn5nZCXIRCO5LP4bQ7I0' -O /comfyui/custom_nodes/comfyui-mixlab-nodes/assets/fonts/neue-plak-extrablack.ttf\nRUN wget --no-check-certificate 'https://drive.google.com/uc?export=download&id=1AdFUc7mLM2dHVZ0C1_dFN8h3VHldxd6p' -O /comfyui/custom_nodes/comfyui-mixlab-nodes/assets/fonts/neue-plak-comp-bold.otf",
                "type": "commands"
            },
            {
                "id": "28a06d48-9",
                "data": {
                    "url": "https://github.com/kijai/ComfyUI-Florence2",
                    "hash": "8d7115944536f32c72475c63980c2d26d9c3dfca",
                    "meta": {
                        "message": "Update requirements.txt",
                        "committer": {
                            "date": "2025-04-05T23:22:20.000Z",
                            "name": "kijai",
                            "email": "40791699+kijai@users.noreply.github.com"
                        },
                        "commit_url": "https://github.com/kijai/ComfyUI-Florence2/commit/8d7115944536f32c72475c63980c2d26d9c3dfca",
                        "latest_hash": "8d7115944536f32c72475c63980c2d26d9c3dfca",
                        "stargazers_count": 1125
                    },
                    "name": "ComfyUI-Florence2",
                    "files": [
                        "https://github.com/kijai/ComfyUI-Florence2"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            },
            {
                "id": "comfyui-deploy",
                "data": {
                    "url": "https://github.com/BennyKok/comfyui-deploy",
                    "hash": "266e9d1024ee7d63c7b471e7724ea2e9876d8b93",
                    "name": "ComfyUI Deploy",
                    "files": [
                        "https://github.com/BennyKok/comfyui-deploy"
                    ],
                    "install_type": "git-clone"
                },
                "type": "custom-node"
            }
        ]
    }
}


@pytest_asyncio.fixture(scope="session")
async def paid_user():
    user_id = str(uuid4())

    redis = Redis(url=redis_url, token=redis_token)
    data = {
        "data": {
            "plans": ["business_monthly"],
            "names": [],
            "prices": [],
            "amount": [],
            "charges": [],
            "cancel_at_period_end": False,
            "canceled_at": None,
            "payment_issue": False,
            "payment_issue_reason": "",
        },
        "version": "1.0",
        "timestamp": int(datetime.now().timestamp()),
    }
    redis.set(f"plan:{user_id}", json.dumps(data))
    print("redis set", redis.get(f"plan:{user_id}"))

    async with get_db_context() as db:
        user = User(
            id=user_id,
            username="business_user",
            name="Business User",
        )
        db.add(user)
    yield user

@pytest_asyncio.fixture(scope="session")
async def paid_user_2():
    user_id = str(uuid4())

    redis = Redis(url=redis_url, token=redis_token)
    data = {
        "data": {
            "plans": ["business_monthly"],
            "names": [],
            "prices": [],
            "amount": [],
            "charges": [],
            "cancel_at_period_end": False,
            "canceled_at": None,
            "payment_issue": False,
            "payment_issue_reason": "",
        },
        "version": "1.0",
        "timestamp": int(datetime.now().timestamp()),
    }
    redis.set(f"plan:{user_id}", json.dumps(data))
    print("redis set", redis.get(f"plan:{user_id}"))

    async with get_db_context() as db:
        user = User(
            id=user_id,
            username="business_user",
            name="Business User",
        )
        db.add(user)
    yield user



@pytest_asyncio.fixture(scope="session")
async def free_user():
    user_id = str(uuid4())

    redis = Redis(url=redis_url, token=redis_token)
    data = {
        "plans": [],
        "names": [],
        "prices": [],
        "amount": [],
        "charges": [],
        "cancel_at_period_end": False,
        "canceled_at": None,
        "payment_issue": False,
        "payment_issue_reason": "",
    }
    redis.set(f"plan:{user_id}", json.dumps(data))
    print("redis set", redis.get(f"plan:{user_id}"))

    async with get_db_context() as db:
        user = User(
            id=user_id,
            username="free_user",
            name="Free User",
        )
        db.add(user)
    yield user


@pytest_asyncio.fixture(scope="session")
async def app():
    import uvicorn
    import requests
    from time import sleep

    # Start the server in a separate thread/process
    import multiprocessing

    def run_server():
        uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)

    # Start server process
    server_process = multiprocessing.Process(target=run_server)
    server_process.start()

    # Wait for server to be ready
    max_retries = 30  # Maximum number of retries
    retry_delay = 0.5  # Delay between retries in seconds

    print("Waiting for server to be ready")

    for attempt in range(max_retries):
        try:
            print(f"Attempt {attempt + 1} of {max_retries}")
            response = requests.get("http://localhost:8000/")
            if response.status_code == 200:
                break
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise RuntimeError("Failed to start server after maximum retries")
            sleep(retry_delay)

    yield "http://localhost:8000"

    print("Stopping server")
    # Cleanup: stop the server
    server_process.terminate()
    server_process.join()


@pytest_asyncio.fixture(scope="session")
async def test_serverless_machine(app, paid_user):
    machine_data = {
        "name": "test-serverless-machine-paid",
        "gpu": "CPU",
        "wait_for_build": True,
    }

    async with get_test_client(app, paid_user) as client:
        response = await client.post("/machine/serverless", json=machine_data)
        assert response.status_code == 200
        machine_id = response.json()["id"]

        yield machine_id

        delete_response = await client.delete(f"/machine/{machine_id}?force=true")
        assert delete_response.status_code == 200


@pytest_asyncio.fixture(scope="session")
async def test_heavy_serverless_machine_1(app, paid_user):

    async with get_test_client(app, paid_user) as client:
        response = await client.post("/machine/serverless", json=heavy_machine_1_data)
        assert response.status_code == 200
        machine_id = response.json()["id"]

        yield machine_id

        delete_response = await client.delete(f"/machine/{machine_id}?force=true")
        assert delete_response.status_code == 200


@pytest_asyncio.fixture(scope="session")
async def test_heavy_serverless_machine_2(app, paid_user):

    async with get_test_client(app, paid_user) as client:
        response = await client.post("/machine/serverless", json=heavy_machine_2_data)
        assert response.status_code == 200
        machine_id = response.json()["id"]

        yield machine_id

        delete_response = await client.delete(f"/machine/{machine_id}?force=true")
        assert delete_response.status_code == 200


@pytest_asyncio.fixture(scope="session")
async def test_create_workflow_deployment(app, paid_user, test_serverless_machine):
    """Test creating a workflow and deployment"""
    async with get_test_client(app, paid_user) as client:
        workflow_data = {
            "name": "test-workflow",
            "workflow_json": basic_workflow_json,
            "workflow_api": basic_workflow_api_json,
            "machine_id": test_serverless_machine,
        }
        response = await client.post("/workflow", json=workflow_data)
        assert response.status_code == 200, (
            f"Workflow creation failed with response: {response.text}"
        )
        workflow_id = response.json()["workflow_id"]

        response = await client.get(f"/workflow/{workflow_id}/versions")
        assert response.status_code == 200, (
            f"Getting workflow versions failed with response: {response.text}"
        )
        workflow_version_id = response.json()[0]["id"]

        deployment_data = {
            "workflow_id": workflow_id,
            "workflow_version_id": workflow_version_id,
            "machine_id": test_serverless_machine,
            "environment": "production",
        }
        print(f"Deployment data: {deployment_data}")
        response = await client.post("/deployment", json=deployment_data)
        if response.status_code != 200:
            print(f"Deployment creation failed with status {response.status_code}")
            print(f"Response body: {response.text}")
            raise AssertionError(f"Deployment creation failed: {response.text}")
        deployment_id = response.json()["id"]

        yield deployment_id

@pytest_asyncio.fixture(scope="session")
async def test_create_workflow_deployment_public(app, paid_user, test_serverless_machine):
    """Test creating a workflow and deployment"""
    async with get_test_client(app, paid_user) as client:
        workflow_data = {
            "name": "test-workflow-public",
            "workflow_json": basic_workflow_json,
            "workflow_api": basic_workflow_api_json,
            "machine_id": test_serverless_machine,
        }
        response = await client.post("/workflow", json=workflow_data)
        assert response.status_code == 200, (
            f"Workflow creation failed with response: {response.text}"
        )
        workflow_id = response.json()["workflow_id"]

        response = await client.get(f"/workflow/{workflow_id}/versions")
        assert response.status_code == 200, (
            f"Getting workflow versions failed with response: {response.text}"
        )
        workflow_version_id = response.json()[0]["id"]

        deployment_data = {
            "workflow_id": workflow_id,
            "workflow_version_id": workflow_version_id,
            "machine_id": test_serverless_machine,
            "environment": "public-share",
        }
        print(f"Deployment data: {deployment_data}")
        response = await client.post("/deployment", json=deployment_data)
        if response.status_code != 200:
            print(f"Deployment creation failed with status {response.status_code}")
            print(f"Response body: {response.text}")
            raise AssertionError(f"Deployment creation failed: {response.text}")
        deployment_id = response.json()["id"]

        yield deployment_id


basic_workflow_json_output_id = """
{"extra":{"ds":{"scale":1,"offset":[-134.83461235393543,-31.966476026948783]}},"links":[[14,16,0,18,0,"IMAGE"]],"nodes":[{"id":16,"pos":[337.5181884765625,284.7711486816406],"mode":0,"size":[390.5999755859375,366],"type":"ComfyUIDeployExternalImage","flags":{},"order":0,"inputs":[{"link":null,"name":"default_value","type":"IMAGE","shape":7}],"outputs":[{"name":"image","type":"IMAGE","links":[14],"slot_index":0}],"properties":{"Node name for S&R":"ComfyUIDeployExternalImage"},"widgets_values":["input_image","","","https://comfy-deploy-output-dev.s3.us-east-2.amazonaws.com/assets/img_bRFqDVG5VG87N29W.png",""]},{"id":18,"pos":[893.0611572265625,382.5367736816406],"mode":0,"size":[327.5999755859375,130],"type":"ComfyDeployOutputImage","flags":{},"order":1,"inputs":[{"link":14,"name":"images","type":"IMAGE"}],"outputs":[],"properties":{"Node name for S&R":"ComfyDeployOutputImage"},"widgets_values":["ComfyUI","webp",80,"my_image"]}],"config":{},"groups":[],"version":0.4,"last_link_id":14,"last_node_id":18}
"""
basic_workflow_api_json_output_id = """
{"16":{"_meta":{"title":"External Image (ComfyUI Deploy)"},"inputs":{"input_id":"input_image","description":"","display_name":"","default_value_url":"https://comfy-deploy-output-dev.s3.us-east-2.amazonaws.com/assets/img_bRFqDVG5VG87N29W.png"},"class_type":"ComfyUIDeployExternalImage"},"18":{"_meta":{"title":"Image Output (ComfyDeploy)"},"inputs":{"images":["16",0],"quality":80,"file_type":"webp","output_id":"my_image","filename_prefix":"ComfyUI"},"class_type":"ComfyDeployOutputImage"}}
"""

@pytest_asyncio.fixture(scope="session")
async def test_create_workflow_deployment_output_id(app, paid_user, test_serverless_machine):
    """Test creating a workflow and deployment"""
    async with get_test_client(app, paid_user) as client:
        workflow_data = {
            "name": "test-workflow-output-id",
            "workflow_json": basic_workflow_json_output_id,
            "workflow_api": basic_workflow_api_json_output_id,
            "machine_id": test_serverless_machine,
        }
        response = await client.post("/workflow", json=workflow_data)
        assert response.status_code == 200, (
            f"Workflow creation failed with response: {response.text}"
        )
        workflow_id = response.json()["workflow_id"]

        response = await client.get(f"/workflow/{workflow_id}/versions")
        assert response.status_code == 200, (
            f"Getting workflow versions failed with response: {response.text}"
        )
        workflow_version_id = response.json()[0]["id"]

        deployment_data = {
            "workflow_id": workflow_id,
            "workflow_version_id": workflow_version_id,
            "machine_id": test_serverless_machine,
            "environment": "production",
        }
        print(f"Deployment data: {deployment_data}")
        response = await client.post("/deployment", json=deployment_data)
        if response.status_code != 200:
            print(f"Deployment creation failed with status {response.status_code}")
            print(f"Response body: {response.text}")
            raise AssertionError(f"Deployment creation failed: {response.text}")
        deployment_id = response.json()["id"]

        yield deployment_id


@pytest_asyncio.fixture(scope="session")
async def test_run_deployment_sync_public(app, test_free_user, test_create_workflow_deployment_public):
    """Test running a deployment"""
    async with get_test_client(app, test_free_user) as client:
        deployment_id = test_create_workflow_deployment_public
        response = await client.post(
            "/run/deployment/sync", json={"deployment_id": deployment_id}
        )
        assert response.status_code == 200
        run_id = response.json()[0]["run_id"]
        assert run_id is not None
        yield run_id


@pytest_asyncio.fixture(scope="session")
async def test_free_user():
    """Fixture for a test user with free plan"""
    async with get_db_context() as db:
        user = User(
            id=str(uuid4()),
            username="test_free_user",
            name="Test Free User",
            # The plan will be handled by the backend based on user's subscription
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    yield user


@pytest_asyncio.fixture(scope="function")
async def test_custom_machine(app, paid_user):
    """Fixture that creates and tears down a test machine"""
    machine_data = {
        "name": "classic-custom-machine",
        "type": "classic",
        "endpoint": "http://localhost:8188",
        "auth_token": "test_auth_token",
    }

    async with get_test_client(app, paid_user) as client:
        response = await client.post("/machine/custom", json=machine_data)
        assert response.status_code == 200
        machine_id = response.json()["id"]
        print(f"Machine ID: {machine_id}")

        yield machine_id

        # Cleanup after test is done
        delete_response = await client.delete(f"/machine/{machine_id}")
        assert delete_response.status_code == 200
