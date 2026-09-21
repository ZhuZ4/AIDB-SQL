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
from typing import Optional, Callable
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from google.adk.models.lite_llm import LiteLlm
from experiments.model_contract import SUPPORTED_MODELS, nonthinking_body, validate_model_contract


logger = logging.getLogger(__name__)


class LlmCallBudgetExceeded(RuntimeError):
    """The question exhausted its fixed model call budget before an API call."""


@dataclass
class ModelUsageTracker:
    """Count actual model invocations, including failed calls, without prompts/keys."""

    max_calls: int = 40
    calls: list = field(default_factory=list)
    on_update: Optional[Callable[[dict], None]] = None

    def checkpoint(self) -> None:
        if self.on_update is not None:
            self.on_update(self.snapshot())

    def snapshot(self) -> dict:
        reported = [item for item in self.calls if item.get("usage") is not None]
        missing = len(self.calls) - len(reported)
        def total(name):
            values = [item["usage"].get(name) for item in reported]
            return sum(value for value in values if value is not None) if values else None
        return {
            "llm_calls": len(self.calls),
            "prompt_tokens": total("prompt_token_count"),
            "completion_tokens": total("candidates_token_count"),
            "total_tokens": total("total_token_count"),
            "cached_tokens": total("cached_content_token_count"),
            "reasoning_tokens": total("thoughts_token_count"),
            "usage_complete": missing == 0,
            "calls_without_usage": missing,
            "tokens_estimated": False,
            "api_cost": None,
            "api_cost_available": False,
            "calls": [dict(item) for item in self.calls],
        }


model_usage_tracker: ContextVar[Optional[ModelUsageTracker]] = ContextVar(
    "model_usage_tracker", default=None
)


class TrackedLiteLlm(LiteLlm):
    async def generate_content_async(self, llm_request, stream=False):
        tracker = model_usage_tracker.get()
        if tracker is None:
            async for response in super().generate_content_async(llm_request, stream=stream):
                yield response
            return
        if len(tracker.calls) >= tracker.max_calls:
            raise LlmCallBudgetExceeded(f"Maximum model calls reached ({tracker.max_calls})")
        started = time.monotonic()
        record = {"call": len(tracker.calls) + 1, "model": self.model,
                  "usage": None, "status": "running"}
        tracker.calls.append(record)
        # Persist intent before invoking the provider. A process kill in this
        # narrow interval may overcount by one; it cannot silently undercount.
        tracker.checkpoint()
        try:
            async for response in super().generate_content_async(llm_request, stream=stream):
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    # Stream metadata is cumulative for one invocation, not additive.
                    record["usage"] = usage.model_dump(exclude_none=True)
                if getattr(response, "error_code", None):
                    record["response_error_code"] = str(response.error_code)
                yield response
            record["status"] = "completed"
        except BaseException as exc:
            record["status"] = "failed"
            record["exception_type"] = type(exc).__name__
            raise
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            tracker.checkpoint()


def create_model(
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None
) -> LiteLlm:
    """
    创建 LiteLlm 模型实例。

    显式参数优先，否则使用环境变量；已知实验别名遵循提供方契约。
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
    # The experiment worker validates its model before entering this general app factory.
    llm_config = {
        "base_url": base_url or os.environ.get("LITE_LLM_BASE_URL"),
        "api_key": api_key or os.environ.get("LITE_LLM_API_KEY"),
        "model_name": model_name or os.environ.get("LITE_LLM_MODEL_NAME", "Qwen/Qwen3-235B-A22B"),
        "headers": {}
    }

    if llm_config["model_name"] in SUPPORTED_MODELS:
        validate_model_contract(llm_config["model_name"], llm_config["base_url"])
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

    model = TrackedLiteLlm(
        model=f"openai/{llm_config['model_name']}",
        api_base=llm_config["base_url"],
        api_key=llm_config["api_key"],
        temperature=float(os.environ.get("LITE_LLM_TEMPERATURE", "0")),
        # Retry at the experiment scheduler boundary, where attempts are persisted.
        num_retries=0,
        max_retries=0,
        timeout=float(os.environ.get("LITE_LLM_REQUEST_TIMEOUT", "120")),
        extra_body=(nonthinking_body(llm_config["model_name"])
                    if llm_config["model_name"] in SUPPORTED_MODELS
                    else {"chat_template_kwargs": {"enable_thinking": False}}),
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
