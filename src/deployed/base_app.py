import asyncio
import cProfile
import gc
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Dict
import deployed.checkpoint_pickle

from deployed.comfy_utils import (
    Input,
    generate_modal_image,
    queue_workflow,
    start_comfy,
    wait_for_completion,
    wait_for_server,
)
import modal


class _ComfyDeployRunner:
    workflow_api_raw = None
    
    # Add this at the beginning of your file, after the imports
    logging.basicConfig(level=logging.INFO)

    # native = True
    native: int = (  # see section on torch.compile below for details
        modal.parameter(default=1)
    )
    
    run_twice: bool = False
    
    skip_workflow_api_validation: bool = False

    logs = []
    
    models_cache = {}

    model_urls = {
        # "checkpoints": [
        #     "https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.ckpt",
        # ],
    }

    loading_time = {}

    start_time = None

    def prompt_worker(self, q, server):
        import execution
        import comfy

        e = execution.PromptExecutor(server)
        last_gc_collect = 0
        need_gc = False
        gc_collect_interval = 10.0

        while True:
            timeout = 1000.0
            if need_gc:
                timeout = max(
                    gc_collect_interval - (current_time - last_gc_collect), 0.0
                )

            queue_item = q.get(timeout=timeout)
            if queue_item is not None:
                item, item_id = queue_item
                execution_start_time = time.perf_counter()
                prompt_id = item[1]
                server.last_prompt_id = prompt_id

                e.execute(item[2], prompt_id, item[3], item[4])
                need_gc = True
                q.task_done(
                    item_id,
                    e.history_result,
                    status=execution.PromptQueue.ExecutionStatus(
                        status_str="success" if e.success else "error",
                        completed=e.success,
                        messages=e.status_messages,
                    ),
                )
                if server.client_id is not None:
                    server.send_sync(
                        "executing",
                        {"node": None, "prompt_id": prompt_id},
                        server.client_id,
                    )

                current_time = time.perf_counter()
                execution_time = current_time - execution_start_time
                logging.info("Prompt executed in {:.2f} seconds".format(execution_time))

            flags = q.get_flags()
            free_memory = flags.get("free_memory", False)

            if flags.get("unload_models", free_memory):
                comfy.model_management.unload_all_models()
                need_gc = True
                last_gc_collect = 0

            if free_memory:
                e.reset()
                need_gc = True
                last_gc_collect = 0

            if need_gc:
                current_time = time.perf_counter()
                if (current_time - last_gc_collect) > gc_collect_interval:
                    comfy.model_management.cleanup_models()
                    gc.collect()
                    comfy.model_management.soft_empty_cache()
                    last_gc_collect = current_time
                    need_gc = False

    async def run_server(
        self, server, address="", port=8188, verbose=True, call_on_start=None
    ):
        addresses = []
        for addr in address.split(","):
            addresses.append((addr, port))
        await asyncio.gather(
            server.start_multi_address(addresses, call_on_start), server.publish_loop()
        )

    async def start_native_comfy_server(self):
        # with cProfile.Profile() as pr:
        import sys
        from pathlib import Path

        # Add the ComfyUI directory to the Python path
        comfy_path = Path("/comfyui")
        sys.path.append(str(comfy_path))

        # Add the ComfyUI/utils directory to the Python path
        utils_path = comfy_path / "utils"
        sys.path.append(str(utils_path))

        t = time.time()
        import nodes

        self.loading_time["import_nodes"] = time.time() - t
        t = time.time()
        import server

        self.loading_time["import_server"] = time.time() - t
        t = time.time()
        import execution

        self.loading_time["import_execution"] = time.time() - t
        t = time.time()
        import comfy

        self.loading_time["import_comfy"] = time.time() - t

        # original_load_checkpoint_guess_config = comfy.sd.load_checkpoint_guess_config

        # # Override the load_checkpoint_guess_config function
        # def custom_load_checkpoint_guess_config(ckpt_path, *args, **kwargs):
        #     if ckpt_path in self.models_cache:
        #         print(f"Loading checkpoint from cache: {ckpt_path}")
        #         return self.models_cache[ckpt_path]
        #     else:
        #         print(f"Loading checkpoint from: {ckpt_path}")
        #     return original_load_checkpoint_guess_config(ckpt_path, *args, **kwargs)

        # # Replace the original function with our custom one
        # comfy.sd.load_checkpoint_guess_config = custom_load_checkpoint_guess_config

        original_load_torch_file = comfy.utils.load_torch_file

        def custom_load_torch_file(ckpt, *args, **kwargs):
            if ckpt in self.models_cache:
                print(f"Loading torch file from cache: {ckpt}")
                return self.models_cache[ckpt]
            else:
                print(f"Loading torch file from: {ckpt}")
                return original_load_torch_file(ckpt, *args, **kwargs)

        comfy.utils.load_torch_file = custom_load_torch_file
        
        original_validate_prompt = execution.validate_prompt
        
        def custom_validate_prompt(prompt, *args, **kwargs):
            if self.skip_workflow_api_validation:
                outputs = set()
                for x in prompt:
                    if 'class_type' not in prompt[x]:
                        error = {
                            "type": "invalid_prompt",
                            "message": f"Cannot execute because a node is missing the class_type property.",
                            "details": f"Node ID '#{x}'",
                            "extra_info": {}
                        }
                        return (False, error, [], [])

                    class_type = prompt[x]['class_type']
                    class_ = nodes.NODE_CLASS_MAPPINGS.get(class_type, None)
                    if class_ is None:
                        error = {
                            "type": "invalid_prompt",
                            "message": f"Cannot execute because node {class_type} does not exist.",
                            "details": f"Node ID '#{x}'",
                            "extra_info": {}
                        }
                        return (False, error, [], [])

                    if hasattr(class_, 'OUTPUT_NODE') and class_.OUTPUT_NODE is True:
                        outputs.add(x)

                if len(outputs) == 0:
                    error = {
                        "type": "prompt_no_outputs",
                        "message": "Prompt has no outputs",
                        "details": "",
                        "extra_info": {}
                    }
                    return (False, error, [], [])

                good_outputs = set()
                errors = []
                node_errors = {}
                validated = {}
                for o in outputs:
                    valid = True
                    
                    if valid is True:
                        good_outputs.add(o)
                  
                return (True, None, list(good_outputs), node_errors)
            return original_validate_prompt(prompt, *args, **kwargs)

        execution.validate_prompt = custom_validate_prompt

        # loop = asyncio.new_event_loop()
        # asyncio.set_event_loop(loop)
        loop = asyncio.get_running_loop()
        server = server.PromptServer(loop)
        q = execution.PromptQueue(server)

        print("Initializing extra nodes")
        t = time.time()
        nodes.init_extra_nodes()
        self.loading_time["init_extra_nodes"] = time.time() - t
        print("Adding routes")
        server.add_routes()

        threading.Thread(
            target=self.prompt_worker,
            daemon=True,
            args=(
                q,
                server,
            ),
        ).start()

        await server.setup()
        # loop.run_until_complete()
        asyncio.create_task(self.run_server(server, verbose=True, port=8188))
        # loop.run_until_complete()

        # pr.print_stats()

    def load_torch_file(self, ckpt, safe_load=False, device=None):
        import torch
        import safetensors

        if device is None:
            device = torch.device("cpu")
        if ckpt.lower().endswith(".safetensors") or ckpt.lower().endswith(".sft"):
            sd = safetensors.torch.load_file(ckpt, device=device.type)
        else:
            if safe_load:
                if not "weights_only" in torch.load.__code__.co_varnames:
                    logging.warning(
                        "Warning torch.load doesn't support weights_only on this pytorch version, loading unsafely."
                    )
                    safe_load = False
            if safe_load:
                pl_sd = torch.load(ckpt, map_location=device, weights_only=True)
            else:
                pl_sd = torch.load(
                    ckpt, map_location=device, pickle_module=deployed.checkpoint_pickle
                )
            if "global_step" in pl_sd:
                logging.debug(f"Global Step: {pl_sd['global_step']}")
            if "state_dict" in pl_sd:
                sd = pl_sd["state_dict"]
            else:
                sd = pl_sd
        return sd

    def download_models(self):
        import os
        import requests

        # Create the base /comfyui/models directory
        os.makedirs("/comfyui/models", exist_ok=True)
        
        print(self.model_urls)
        
        # Get HF token from environment variable
        hf_token = os.getenv('HF_TOKEN')
        headers = {'Authorization': f'Bearer {hf_token}'} if hf_token else {}

        for category, urls in self.model_urls.items():
            # Create category subdirectory
            target_dir = f"/comfyui/models/{category}"
            os.makedirs(target_dir, exist_ok=True)

            for url in urls:
                # Extract the filename from the URL
                filename = url.split("/")[-1]
                filepath = os.path.join(target_dir, filename)

                if not os.path.exists(filepath):
                    print(f"Downloading {filename} to {target_dir}...")
                    # Check if URL is from Hugging Face
                    is_huggingface = "huggingface.co" in url
                    # Only use HF token for Hugging Face URLs
                    current_headers = headers if is_huggingface else {}
                    response = requests.get(url, headers=current_headers)
                    with open(filepath, "wb") as f:
                        f.write(response.content)
                    print(f"Downloaded {filename}")
                else:
                    print(
                        f"{filename} already exists in {target_dir}, skipping download"
                    )

    @modal.enter(snap=False)
    async def launch_comfy_background(self):
        t = time.time()
        import torch

        self.loading_time["import_torch"] = time.time() - t
        # print(f"Time to import torch: {time.time() - t:.2f} seconds")
        print(f"GPUs available: {torch.cuda.is_available()}")

        self.start_time = time.time()
        print("Launching ComfyUI")

        if self.native:
            # t = time.time()
            asyncio.create_task(self.start_native_comfy_server())
            # self.loading_time["native_comfyui"] = time.time() - t
        else:
            self.cleanup_server = await start_comfy(logs=self.logs)

    @modal.exit()
    async def exit(self):
        print("Exiting ComfyUI")
        if not self.native:
            await self.cleanup_server()

    @modal.method()
    async def run(self, input: Input):
        if isinstance(input, dict):
            input = Input(**input)
            
        if self.workflow_api_raw is not None:
            input.workflow_api_raw = json.loads(self.workflow_api_raw)

        # print(input)

        async for event in wait_for_server():
            # print(event)
            pass

        if self.start_time is not None:
            elapsed_time = time.time() - self.start_time
            self.loading_time["total_comfyui_cold_start"] = elapsed_time
            self.start_time = None  # Reset to None so we only log for the first run

        print("Running workflow")
        workflow_start_time = time.perf_counter()
        await queue_workflow(input)
        await wait_for_completion(input.prompt_id)
        workflow_end_time = time.perf_counter()
        workflow_execution_time = workflow_end_time - workflow_start_time
        self.loading_time["workflow_execution_time"] = workflow_execution_time

        if self.run_twice:
            print("Running workflow 2")
            workflow_start_time = time.perf_counter()
            await queue_workflow(input)
            await wait_for_completion(input.prompt_id)
            workflow_end_time = time.perf_counter()
            workflow_execution_time = workflow_end_time - workflow_start_time
            self.loading_time["workflow_execution_time_2"] = workflow_execution_time

        print("\nLoading Times:")
        print("-" * 40)
        print(f"{'Step':<30} | {'Time (seconds)':<15}")
        print("-" * 40)
        for step, t in self.loading_time.items():
            print(f"{step:<30} | {t:<15.2f}")
        print("-" * 40)

        return {"loading_time": self.loading_time}


class _ComfyDeployRunnerOptimizedModels(_ComfyDeployRunner):
    @modal.enter(snap=True)
    async def load(self):
        # import comfy.utils
        import torch

        import sys
        from pathlib import Path

        # Add the ComfyUI directory to the Python path
        comfy_path = Path("/comfyui")
        sys.path.append(str(comfy_path))

        # Add the ComfyUI/utils directory to the Python path
        utils_path = comfy_path / "utils"
        sys.path.append(str(utils_path))

        import nodes
        import server
        import execution
        import comfy
        
        import xformers

        print(f"GPUs available: {torch.cuda.is_available()}")

        for model_path in self.models_to_cache:
            t = time.time()
            print(f"Loading model: {model_path}")
            # self.models_cache[model_path] = comfy.sd.load_checkpoint_guess_config(
            #     model_path,
            #     output_vae=True,
            #     output_clip=True,
            #     embedding_directory=folder_paths.get_folder_paths("embeddings")
            # )
            self.models_cache[model_path] = self.load_torch_file(model_path)
            print(f"Time to load model {model_path}: {time.time() - t:.2f} seconds")


class _ComfyDeployRunnerOptimizedImports(_ComfyDeployRunner):
    @modal.enter(snap=True)
    async def load(self):
        # import comfy.utils
        import torch

        import sys
        from pathlib import Path

        # Add the ComfyUI directory to the Python path
        comfy_path = Path("/comfyui")
        sys.path.append(str(comfy_path))

        # Add the ComfyUI/utils directory to the Python path
        utils_path = comfy_path / "utils"
        sys.path.append(str(utils_path))

        import nodes
        import server
        import execution
        import comfy
        
        import xformers

        print(f"GPUs available: {torch.cuda.is_available()}")

class _ComfyDeployRunnerModelsDownloadOptimzedImports(_ComfyDeployRunner):
    @modal.enter(snap=True)
    async def load(self):
        # import comfy.utils
        import torch

        import sys
        from pathlib import Path

        # Add the ComfyUI directory to the Python path
        comfy_path = Path("/comfyui")
        sys.path.append(str(comfy_path))

        # Add the ComfyUI/utils directory to the Python path
        utils_path = comfy_path / "utils"
        sys.path.append(str(utils_path))

        import nodes
        import server
        import execution
        import comfy
        
        import xformers

        print(f"GPUs available: {torch.cuda.is_available()}")

        for model_path in self.models_to_cache:
            t = time.time()
            print(f"Loading model: {model_path}")
            
            # model_path_with_prefix = f"/comfyui/models/{model_path.split('/', 2)[-1]}"
            # self.models_cache[model_path] = comfy.sd.load_checkpoint_guess_config(
            #     model_path,
            #     output_vae=True,
            #     output_clip=True,
            #     embedding_directory=folder_paths.get_folder_paths("embeddings")
            # )
            self.models_cache[model_path] = self.load_torch_file(model_path)
            print(model_path)
            print(f"Time to load model {model_path}: {time.time() - t:.2f} seconds")
