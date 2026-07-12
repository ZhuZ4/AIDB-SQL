import os
from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner

DEFAULT_LLM="gemini-2.0-flash"#"gemini-2.5-flash-preview-05-20"
DEFAULT_REASONING_LLM="gemini-2.5-flash-preview-05-20"

def load_environment_variables():
    """
    Load environment variables from a .env file located in the project root directory.
    Assumes utils.py is in src/your_package_name/
    """
    # Get the directory of the current script (utils.py)
    # e.g., /path/to/project/src/building_intelligent_agents
    current_script_dir = os.path.dirname(os.path.abspath(__file__))

    # Go up one level to the 'src' directory
    # e.g., /path/to/project/src
    src_dir = os.path.dirname(current_script_dir)

    # Go up one more level to the project root directory
    # e.g., /path/to/project
    project_root = os.path.dirname(src_dir)

    # Construct the path to the .env file in the project root
    dotenv_path = os.path.join(project_root, ".env")

    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path=dotenv_path)
        print(f"Loaded environment variables from: {dotenv_path}")
    else:
        print(f"Warning: .env file not found at {dotenv_path}. Ensure it's in the project root.")


import os
from typing import Optional
import logging

from google.adk.models.lite_llm import LiteLlm


logger = logging.getLogger(__name__)


def create_model(
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None
) -> LiteLlm:
    """
    创建 LiteLlm 模型实例。

    优先使用环境变量配置，如果未设置则使用默认值。
    注意：生产环境应通过环境变量配置敏感信息。

    Args:
        base_url: API 基础 URL，默认从环境变量 LITE_LLM_BASE_URL 读取
        api_key: API 密钥，默认从环境变量 LITE_LLM_API_KEY 读取
        model_name: 模型名称，默认从环境变量 LITE_LLM_MODEL_NAME 读取

    Returns:
        LiteLlm: 配置好的模型实例

    Raises:
        ValueError: 如果必需的配置项缺失
    """
    # 从环境变量读取配置，提供默认值（仅用于开发环境）
    llm_config = {
        "base_url": base_url or os.environ.get("LITE_LLM_BASE_URL"),
        "api_key": api_key or os.environ.get("LITE_LLM_API_KEY"),
        "model_name": model_name or os.environ.get(
            "LITE_LLM_MODEL_NAME",
            # "qwen3.5-plus"
            # "gpt-4-omni"
            "Qwen/Qwen3-235B-A22B"
            # "qwen3-max"
            # "kimi-k2.6"
        ),
        "headers": {}
    }

    # 验证必需的配置项
    if not llm_config["api_key"]:
        raise ValueError(
            "LITE_LLM_API_KEY 环境变量未设置。"
            "请设置环境变量或在 .env 文件中配置。"
        )

    logger.info(
        "LiteLLM config loaded: model=%s, api_base=%s, api_key_set=%s",
        llm_config["model_name"],
        llm_config["base_url"],
        bool(llm_config["api_key"]),
    )

    model = LiteLlm(
        model=f"openai/{llm_config['model_name']}",
        api_base=llm_config["base_url"],
        api_key=llm_config["api_key"],
        extra_body={
            "chat_template_kwargs": {"enable_thinking": False}
        },
    )
    return model

def create_session(runner: InMemoryRunner, session_id: str, user_id: str, state=None):
    """
    Create a new session using the provided runner.
    
    :param runner: The InMemoryRunner instance to use for session creation.
    :param session_id: The ID of the session to create.
    :param user_id: The ID of the user for whom the session is created.
    """
    import asyncio
    print(f"Creating session: {session_id} for user: {user_id} on app: {runner.app_name}")
    
    try:
        coro = runner.session_service.create_session(
            app_name=runner.app_name,
            user_id=user_id,
            session_id=session_id,
            state=state or {}
        )
        try:
            loop = asyncio.get_running_loop()
            # Already inside an event loop (e.g. adk run), schedule as a task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(asyncio.run, coro).result()
        except RuntimeError:
            # No running event loop, safe to use asyncio.run
            asyncio.run(coro)
        print("Session created successfully.")
    except Exception as e:
        print(f"Error creating session: {e}")
        exit()