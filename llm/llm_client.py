"""llm/llm_client.py —— 大模型 API 请求封装

来源：作业 1（D:\\agent\\llm_client.py），原样复用，未作逻辑改动。

使用原生 requests 直连通义千问(OpenAI 兼容模式)的 /chat/completions 接口，
不依赖任何官方 SDK。
职责：
  1. 构造并发送对话请求(含 function calling 工具描述)
  2. 解析响应，统一返回 {content, tool_calls} 结构
  3. 异常捕获与重试：
     - 网络超时/连接失败/限流(429)/服务端错误(5xx) → 指数退避重试
     - Key 无效(401/403) → 抛出 APIKeyError，不重试
"""

import json
import time

import requests

# 这些 HTTP 状态码属于"可重试"错误（限流或服务端临时故障）
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class APIKeyError(Exception):
    """API Key 无效或无权限。重试没有意义，直接提示用户检查 .env。"""


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str,
                 max_retries: int = 3, timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries   # 网络类异常最大重试次数
        self.timeout = timeout           # 单次 HTTP 请求超时(秒)
        # 复用连接的 Session
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def chat(self, messages: list, tools: list = None, temperature: float = 0.2) -> dict:
        """发起一次对话请求。

        参数:
            messages: OpenAI 格式的消息列表(system/user/assistant/tool)
            tools:    工具(function calling)描述列表
        返回:
            {"content": str, "tool_calls": [{"id","name","arguments":dict}]}
        """
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools

        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)

                # ---- 按 HTTP 状态码分类处理 ----
                if resp.status_code in (401, 403):
                    raise APIKeyError(
                        f"API Key 无效或无权限 (HTTP {resp.status_code})，"
                        f"请检查 .env 中的 LLM_API_KEY"
                    )
                if resp.status_code in RETRYABLE_STATUS:
                    # 抛出后由下方 except 捕获进入重试
                    resp.raise_for_status()
                if resp.status_code != 200:
                    # 其他错误(如 400 参数错误)重试无意义，直接失败
                    raise ValueError(
                        f"请求失败 HTTP {resp.status_code}: {resp.text[:300]}"
                    )

                return self._parse_response(resp.json())

            except APIKeyError:
                raise  # Key 错误直接向上抛，不重试
            except (requests.Timeout, requests.ConnectionError,
                    requests.HTTPError) as e:
                last_err = e
                if attempt < self.max_retries:
                    wait = 2 ** attempt  # 指数退避: 2s, 4s, 8s...
                    print(f"  [网络异常] 第{attempt}次请求失败: {e}，{wait}秒后重试...")
                    time.sleep(wait)

        raise ConnectionError(f"请求连续失败 {self.max_retries} 次: {last_err}")

    @staticmethod
    def _parse_response(data: dict) -> dict:
        """解析 OpenAI 兼容格式的响应，统一成简单结构。"""
        message = data["choices"][0]["message"]
        tool_calls = []
        for tc in message.get("tool_calls") or []:
            arguments = tc["function"].get("arguments", "{}")
            # arguments 是 JSON 字符串，解析成 dict；失败则给空 dict
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append({
                "id": tc["id"],
                "name": tc["function"]["name"],
                "arguments": arguments,
            })
        return {
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
        }
