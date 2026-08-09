"""
api_processor.py
用于调用API处理文本的工具模块
"""

import os
import json
import time
import asyncio
import atexit
import random
import threading
from functools import partial
from typing import List, Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
from google import genai
from google.genai import types
from openai import OpenAI
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 读取提示文件（相对于模块自身路径，支持从任何 CWD 调用）
_PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resource")
PROMPT_FILE_PATH = os.path.join(_PROMPT_DIR, "prompt-proofreader-system.xml")
PROMPT_FILE_PATH_JSON = os.path.join(_PROMPT_DIR, "prompt-proofreader-system-outputJSON.xml")
SYSTEM_PROMPT = ""
with open(PROMPT_FILE_PATH, "r", encoding="utf-8") as file:
    SYSTEM_PROMPT = file.read()

API_TIMEOUT_SECONDS = 120.0
API_MAX_ATTEMPTS = 3  # Initial request plus two explicit retries.
# 指数退避重试间隔（秒）。DeepSeek 高峰/劣化时瞬时请求常以空响应失败，
# 5s/8s 太短（实测劣化窗口持续数十秒）无法跨过；放大到 15s/45s 并叠加
# ±30% 随机抖动，分散并发重试。测试用 patch(API_RETRY_DELAYS, (0,0)) 不受影响。
API_RETRY_DELAYS = (15.0, 45.0)
API_RETRY_JITTER = 0.3  # ±30% 抖动

_OPENAI_CLIENTS: dict[str, OpenAI] = {}
_OPENAI_CLIENTS_LOCK = threading.Lock()
_API_EXECUTOR = ThreadPoolExecutor(thread_name_prefix="ai-proofread-api")


def load_system_prompt(mode: str = "rewrite") -> str:
    """按模式加载系统提示词。

    mode="rewrite"  全文重写模式（模型输出整个 target 的重写版）
    mode="json"     JSON 发现模式（模型只输出 [{original_sentence, corrected_sentence}]）
    """
    if mode == "json":
        with open(PROMPT_FILE_PATH_JSON, "r", encoding="utf-8") as file:
            return file.read()
    return SYSTEM_PROMPT

class RateLimiter:
    """
    限速器类，用于控制API调用频率
    """
    def __init__(self, rpm: int):
        self.interval = 60 / rpm
        self.last_call_time = 0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            current_time = time.time()
            elapsed = current_time - self.last_call_time
            if elapsed < self.interval:
                wait_time = self.interval - elapsed
                await asyncio.sleep(wait_time)
            self.last_call_time = time.time()


def _client_config(model: str) -> tuple[str, str | None, str]:
    direct_models = {
        "deepseek-v4-flash", "deepseek-v4-pro",
        "deepseek-chat", "deepseek-reasoner",
    }
    if model in direct_models:
        return "deepseek", os.getenv("DEEPSEEK_API_KEY"), "https://api.deepseek.com"
    if model == "deepseek-v3":
        return (
            "aliyun",
            os.getenv("ALIYPUN_API_KEY"),
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    raise ValueError(f"模型名称错误：{model}")


def _get_openai_client(model: str) -> OpenAI:
    """Return one pooled SDK client per provider for the process lifetime."""
    provider, api_key, base_url = _client_config(model)
    with _OPENAI_CLIENTS_LOCK:
        client = _OPENAI_CLIENTS.get(provider)
        if client is None:
            # 显式 http_client + proxy=None：强制直连，不读环境代理/沙箱透明代理。
            # 实测 WorkBuddy 沙箱子进程虽无代理 env 变量，但宿主 Clash 仍可能在
            # socket 层劫持流量；DeepSeek/Aliyun 走本地代理会引入额外的失败注入点
            # （转隧道半开/间歇劣化）。Google genai 客户端保持系统默认（trust_env），
            # 在需代理的环境（大陆访问 gemini）不受影响。
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                timeout=API_TIMEOUT_SECONDS,
                http_client=httpx.Client(
                    proxy=None,
                    timeout=httpx.Timeout(API_TIMEOUT_SECONDS, connect=10.0),
                ),
            )
            _OPENAI_CLIENTS[provider] = client
    return client


def _close_openai_clients() -> None:
    with _OPENAI_CLIENTS_LOCK:
        clients = list(_OPENAI_CLIENTS.values())
        _OPENAI_CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(_close_openai_clients)


def _build_messages(content: str, reference: str,
                    system_prompt: str | None) -> list[dict[str, str]]:
    effective_system = system_prompt if system_prompt is not None else SYSTEM_PROMPT
    messages = [{"role": "system", "content": effective_system}]
    if reference:
        messages.extend([
            {"role": "assistant", "content": ""},
            {"role": "user", "content": reference},
        ])
    messages.extend([
        {"role": "assistant", "content": ""},
        {"role": "user", "content": content},
    ])
    return messages


def _deepseek_request_once(model: str, messages: list[dict[str, str]],
                           max_tokens: int | None = None) -> str | None:
    client = _get_openai_client(model)
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 1.3,
        "stream": False,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def _clean_model_result(result: str) -> str:
    return result.replace("\n</target>", "").replace("<target>\n", "")


def _new_api_stats() -> dict[str, int | float]:
    return {
        "logical_calls": 0,
        "attempts": 0,
        "retries": 0,
        "failures": 0,
        "empty_responses": 0,
        "rate_wait_seconds": 0.0,
        "request_seconds": 0.0,
    }


def deepseek(content: str, reference: str = "",
             model: str = "deepseek-v4-flash",
             system_prompt: str | None = None,
             max_tokens: int | None = None) -> str | None:
    """
    调用各家deepseek校对模型，返回校对后的文本

    max_tokens: 输出上限。None（默认）= 不限制。JSON 发现模式（max 管线 Phase 1）
    会显式传 4096 防止超长 JSON 拖慢审校；全稿重写（proofread p/b）保持不限。

    model: deepseek-v4-flash (默认)
           deepseek-v4-pro
           deepseek-chat (旧名，等价 v4-flash)
           deepseek-reasoner (旧名，等价 v4-flash thinking)
           deepseek-v3 (阿里云百炼)
    system_prompt: 覆盖默认系统提示词（如 JSON 发现模式）
    """

    try:
        _client_config(model)
    except ValueError as exc:
        print(exc)
        return None
    messages = _build_messages(content, reference, system_prompt)
    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            print(f"API {model}: 尝试 {attempt}/{API_MAX_ATTEMPTS}")
            result = _deepseek_request_once(model, messages, max_tokens)
        except Exception as e:
            elapsed = time.perf_counter() - started
            print(f"API {model}: 尝试 {attempt} 异常 ({elapsed:.2f}s): {e}")
        else:
            elapsed = time.perf_counter() - started
            if isinstance(result, str) and result:
                print(f"API {model}: 尝试 {attempt} 成功 ({elapsed:.2f}s)")
                return _clean_model_result(result)
            print(f"API {model}: 尝试 {attempt} 返回空 ({elapsed:.2f}s)")
        if attempt < API_MAX_ATTEMPTS:
            delay = API_RETRY_DELAYS[attempt - 1]
            print(f"API {model}: {delay:.0f}s 后重试")
            time.sleep(delay)
    print(f"API {model}: {API_MAX_ATTEMPTS} 次尝试均失败")
    return None


async def deepseek_async(
        content: str, reference: str, model: str, rate_limiter: RateLimiter,
        system_prompt: str | None = None, max_tokens: int | None = None,
        stats: dict[str, int | float] | None = None,
        request_label: str = "") -> str | None:
    """
    异步调用deepseek校对模型，返回校对后的文本
    """
    if stats is None:
        stats = _new_api_stats()
    stats["logical_calls"] += 1
    messages = _build_messages(content, reference, system_prompt)
    loop = asyncio.get_running_loop()
    label = f" {request_label}" if request_label else ""

    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        wait_started = time.perf_counter()
        await rate_limiter.wait()
        rate_wait = time.perf_counter() - wait_started
        stats["rate_wait_seconds"] += rate_wait
        stats["attempts"] += 1
        if attempt > 1:
            stats["retries"] += 1
        print(
            f"API {model}{label}: 尝试 {attempt}/{API_MAX_ATTEMPTS} "
            f"(限速等待 {rate_wait:.2f}s)"
        )
        started = time.perf_counter()
        try:
            result = await loop.run_in_executor(
                _API_EXECUTOR,
                partial(_deepseek_request_once, model, messages, max_tokens),
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            stats["request_seconds"] += elapsed
            print(
                f"API {model}{label}: 尝试 {attempt} 异常 "
                f"({elapsed:.2f}s): {exc}"
            )
        else:
            elapsed = time.perf_counter() - started
            stats["request_seconds"] += elapsed
            if isinstance(result, str) and result:
                print(
                    f"API {model}{label}: 尝试 {attempt} 成功 "
                    f"({elapsed:.2f}s)"
                )
                return _clean_model_result(result)
            stats["empty_responses"] += 1
            print(
                f"API {model}{label}: 尝试 {attempt} 返回空 "
                f"({elapsed:.2f}s)"
            )
        if attempt < API_MAX_ATTEMPTS:
            base_delay = API_RETRY_DELAYS[attempt - 1]
            delay = base_delay * random.uniform(
                1.0 - API_RETRY_JITTER, 1.0 + API_RETRY_JITTER)
            print(f"API {model}{label}: {delay:.0f}s 后重试")
            await asyncio.sleep(delay)

    stats["failures"] += 1
    print(f"API {model}{label}: {API_MAX_ATTEMPTS} 次尝试均失败")
    return None

# 配置Google API（懒加载——仅在使用 Google 模型时初始化）
_client = None

def _get_google_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return _client

def chat_google(text: str) -> str|None:
    """
    调用google校对模型，返回校对后的文本
    """
    retry_count = 0
    result = ""
    _client = _get_google_client()
    while retry_count < 3:
        response = _client.models.generate_content(
            model='gemini-2.0-flash-001',
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                # max_output_tokens=3,
                temperature=1.3,
            ),
        )
        result = response.text
        if result:
            break
        retry_count += 1
        time.sleep(3)  # 减少等待时间
    return result


async def chat_google_async(text: str, rate_limiter: RateLimiter) -> str|None:
    """
    异步调用google校对模型，返回校对后的文本
    """
    await rate_limiter.wait()

    # 复用进程级线程池，避免每个段落重复创建/销毁 executor。
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_API_EXECUTOR, chat_google, text)
    return result


def _book_sidecar_dir(json_out: str) -> str:
    """book 路径每段独立的 checkpoint 目录（避免整份 JSON 读改写）。"""
    return f"{json_out}.chunks"


def _book_sidecar_path(json_out: str, index: int) -> str:
    return os.path.join(_book_sidecar_dir(json_out), f"{index:06d}.json")


def _book_error_path(json_out: str, index: int) -> str:
    return os.path.join(_book_sidecar_dir(json_out), f"{index:06d}.error.json")


def _atomic_write_book_chunk(json_out: str, index: int, result: str) -> None:
    """单段结果原子写入侧车文件：O(1)，无需全局锁，中断后可逐段续跑。"""
    path = _book_sidecar_path(json_out, index)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp-{os.getpid()}-{time.time_ns()}"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump({
            "schema": "ai-proofread.book-chunk.v1",
            "index": index,
            "result": result,
        }, f, ensure_ascii=False, indent=2)
        f.flush()
    os.replace(temp_path, path)


def _atomic_write_book_error(json_out: str, index: int,
                             target_text: str, message: str) -> None:
    """失败段的 error 标记，供下次续跑与人工排查。"""
    path = _book_error_path(json_out, index)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp-{os.getpid()}-{time.time_ns()}"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump({
            "schema": "ai-proofread.book-chunk.v1",
            "index": index,
            "status": "failed",
            "target_preview": target_text[:60],
            "error": message,
        }, f, ensure_ascii=False, indent=2)
        f.flush()
    os.replace(temp_path, path)


def _load_book_sidecar(json_out: str, length: int) -> dict[int, str]:
    """从侧车目录 + legacy 整份 JSON 恢复已完成的段（resume 依据）。"""
    done: dict[int, str] = {}
    sidecar_dir = _book_sidecar_dir(json_out)
    if os.path.isdir(sidecar_dir):
        for name in os.listdir(sidecar_dir):
            if not name.endswith(".json"):
                continue
            try:
                i = int(name[:-5])  # 去掉 ".json"
            except ValueError:
                continue
            try:
                with open(os.path.join(sidecar_dir, name), "r",
                          encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if (isinstance(payload, dict)
                    and isinstance(payload.get("result"), str)):
                done[i] = payload["result"]
    if os.path.exists(json_out):
        try:
            with open(json_out, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except (OSError, json.JSONDecodeError):
            legacy = None
        if isinstance(legacy, list):
            for i, val in enumerate(legacy):
                if isinstance(val, str):
                    done.setdefault(i, val)
    return done


async def process_paragraphs_async(json_in: str, json_out: str, start_count: int|list[int]=1, stop_count: int|None=None, model: str="deepseek-chat", rpm: int=15, max_concurrent: int=3):
    """
    异步处理文本段落，将结果存为整份 JSON 文件。

    Args:
        json_in (str): 输入 JSON 文件路径
        json_out (str): 输出 JSON 文件路径
        start_count (int|list[int]): 开始处理的段落索引（从1开始），默认为1
        stop_count (int|None): 结束处理的段落索引，默认为None（处理到最后）
        model (str): 使用的模型，默认为"deepseek-chat"
        rpm (int): 每分钟请求数，默认为30
        max_concurrent (int): 最大并发数，默认为3

    可靠性改进（相对旧实现）：
      - 每段结果独立侧车原子写（`{json_out}.chunks/`），不再整份 JSON
        读改写（原 O(N²) 且被全局锁串行化）。
      - 中断后重跑同一命令只补侧车缺失的段。
      - 重试耗尽的段记 `*.error.json`，结束时若有失败统一抛出
        RuntimeError（非静默跳过、CLI 非零退出）；输出 JSON 保留空段为
        null，可直接续跑。
      - 生产环境建议改用 `proofread max`（每块原子 checkpoint + 失败即抛错）。
    """
    # 读取输入 JSON 文件
    with open(json_in, "r", encoding="utf-8") as f:
        input_paragraphs: List[dict] = json.load(f)

    input_paragraphs_length = len(input_paragraphs)
    done = _load_book_sidecar(json_out, input_paragraphs_length)

    # 确定要处理的段落索引
    indices_to_process = []

    if isinstance(start_count, int):
        # 处理从 start_count 到 stop_count 的段落
        start_index = start_count - 1
        stop_index = input_paragraphs_length - 1 if stop_count is None else stop_count - 1

        for i in range(start_index, stop_index + 1):
            if i < input_paragraphs_length and i not in done:
                indices_to_process.append(i)
    elif isinstance(start_count, list):
        # 处理指定索引的段落
        for idx in start_count:
            i = idx - 1  # 转换为 0-indexed
            if 0 <= i < input_paragraphs_length and i not in done:
                indices_to_process.append(i)

    # 创建日志文件
    log_file_path = f"{json_out}.log"
    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
    with open(log_file_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n{'='*50}\n")
        log_file.write(f"异步处理开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log_file.write(f"待处理段落数: {len(indices_to_process)}/{input_paragraphs_length}\n")
        log_file.write(f"最大并发数: {max_concurrent}\n")
        log_file.write(f"{'='*50}\n\n")

    # 创建限速器和信号量（每段只 await 一次限速）
    rate_limiter = RateLimiter(rpm)
    semaphore = asyncio.Semaphore(max_concurrent)
    failed: List[int] = []

    # 定义异步处理任务
    async def process_one(i):
        async with semaphore:
            target_text = input_paragraphs[i]["target"]
            reference_text = input_paragraphs[i]["reference"] if "reference" in input_paragraphs[i] else ""
            context_text = input_paragraphs[i]["context"] if "context" in input_paragraphs[i] else ""

            # 判断是否需要添加上下文
            is_with_context = context_text and context_text.strip() != target_text.strip()
            print(f"处理 {i+1}/{input_paragraphs_length}{' with context' if is_with_context else ''}{' with reference' if reference_text else ''}:\n{target_text[:30]} ...\n")

            # 加标签，合并
            pre_text = f"<reference>\n{reference_text}\n</reference>" if reference_text else ""
            if is_with_context:
                pre_text += f"\n<context>\n{context_text}\n</context>"
            post_text = f"<target>\n{target_text}\n</target>"

            start_time = time.time()

            # 调用相应的 API
            processed_text = None
            if model.startswith("deepseek"):
                processed_text = await deepseek_async(
                    post_text, pre_text, model, rate_limiter,
                    request_label=f"book {i + 1}/{input_paragraphs_length}",
                )
            elif model == "google":
                processed_text = await chat_google_async(pre_text+'\n'+post_text, rate_limiter)
            else:
                print(f"不支持的模型: {model}")
                failed.append(i)
                _atomic_write_book_error(
                    json_out, i, target_text, f"不支持的模型: {model}")
                return

            elapsed = time.time() - start_time

            if processed_text:
                _atomic_write_book_chunk(json_out, i, processed_text)
                print(f"完成 {i+1}/{input_paragraphs_length} 长度 {len(target_text)} 用时 {elapsed:.2f}s\n{'-'*40}\n")
                with open(log_file_path, "a", encoding="utf-8") as log_file:
                    log_file.write(f"完成 {i+1}/{input_paragraphs_length} 长度 {len(target_text)} 用时 {elapsed:.2f}s\n")
            else:
                failed.append(i)
                _atomic_write_book_error(
                    json_out, i, target_text, "API 重试耗尽（空响应或异常）")
                print(f"段落 {i+1}/{input_paragraphs_length}: 重试耗尽，记入 error checkpoint\n{'-'*40}\n")
                with open(log_file_path, "a", encoding="utf-8") as log_file:
                    log_file.write(f"段落 {i+1}/{input_paragraphs_length}: 处理失败\n")
                    log_file.write(f"原文: {target_text.strip().splitlines()[0][:20]}...\n{'-'*40}\n")

    if indices_to_process:
        await asyncio.gather(*(process_one(i) for i in indices_to_process))

    # 合并侧车 → 整份输出 JSON（只写一次）
    final_output: List[str | None] = [None] * input_paragraphs_length
    for i in range(input_paragraphs_length):
        path = _book_sidecar_path(json_out, i)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload.get("result"), str):
                    final_output[i] = payload["result"]
            except (OSError, json.JSONDecodeError):
                pass
    if os.path.dirname(json_out):
        os.makedirs(os.path.dirname(json_out), exist_ok=True)
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(final_output, f, ensure_ascii=False, indent=2)

    # 记录处理完成信息
    with open(log_file_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n{'='*50}\n")
        log_file.write(f"处理结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        processed_count = sum(1 for p in final_output if p is not None)
        processed_length = sum(len(p) for p in final_output if p is not None)
        log_file.write(f"已处理段落数、字数: {processed_count}/{input_paragraphs_length}, {processed_length}/{sum(len(p) for p in input_paragraphs)}\n")
        log_file.write(f"未处理段落数: {input_paragraphs_length - processed_count}/{input_paragraphs_length}\n")
        log_file.write(f"{'='*50}\n\n")

    # 生成 Markdown 文件
    md_file_path = f"{json_out}.md"
    processed_paragraphs = [p for p in final_output if p is not None]
    if processed_paragraphs:
        with open(md_file_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(processed_paragraphs))

    if failed:
        failed_list = ", ".join(f"第{i+1}段" for i in sorted(failed)[:20])
        raise RuntimeError(
            f"book 审校有 {len(failed)}/{len(indices_to_process)} 段重试耗尽"
            f"（{failed_list}{'…' if len(failed) > 20 else ''}）；"
            f"输出已保留在 {json_out}（失败段为 null），"
            f"请检查 API 配额/网络后重跑同一命令续跑（已完成的段零模型调用）。"
            f"生产环境建议改用 `proofread max`（每块原子 checkpoint、失败即抛错）。"
        )

    return final_output


def process_by_once(file_in: str, file_out: str, chat_func: Callable=deepseek, model: str="deepseek-chat"):
    """
    一次性处理整个文件
    """
    with open(file_in, encoding="utf8",mode="r") as f:
        with open(file_out,encoding="utf8", mode="w") as f_out:
            text = f.read()
            text = chat_func(text, model=model)
            if text:
                f_out.write(text)
