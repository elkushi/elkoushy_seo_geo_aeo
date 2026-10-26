"""Thin LLM client for NVIDIA NIM (free dev tier, OpenAI-compatible API).

Set NVIDIA_API_KEY in .env (build.nvidia.com > any model > Get API Key).
NVIDIA_MODEL overrides the default model. Thinking is disabled to save the
rate limit; only the final answer is returned.

Usage:
  python tools/llm_client.py "What is Ahrefs?"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import env  # noqa: E402

BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"


def available() -> bool:
    return bool(env("NVIDIA_API_KEY"))


def model_name() -> str:
    return env("NVIDIA_MODEL") or DEFAULT_MODEL


def ask(prompt: str, system: str | None = None, temperature: float = 0.4, max_tokens: int = 1500) -> str:
    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key=env("NVIDIA_API_KEY"), timeout=120, max_retries=2)
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    resp = client.chat.completions.create(
        model=model_name(), messages=messages, temperature=temperature, top_p=0.95, max_tokens=max_tokens,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return (resp.choices[0].message.content or "").strip()


if __name__ == "__main__":
    if not available():
        sys.exit("NVIDIA_API_KEY is not set in .env")
    print(ask(" ".join(sys.argv[1:]) or "Say hello in five words."))
