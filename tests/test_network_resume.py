import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import proofreader
from src.max_pipeline import phase1_json_proofread, run_max


class CountingRateLimiter:
    def __init__(self, *_args, **_kwargs):
        self.wait_count = 0

    async def wait(self):
        self.wait_count += 1


class NetworkRetryTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        proofreader._close_openai_clients()

    def test_openai_client_is_reused_and_sdk_retries_are_disabled(self):
        fake_client = MagicMock()
        with patch("src.proofreader.OpenAI", return_value=fake_client) as factory:
            first = proofreader._get_openai_client("deepseek-v4-flash")
            second = proofreader._get_openai_client("deepseek-chat")

        self.assertIs(first, second)
        factory.assert_called_once_with(
            api_key=proofreader.os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
            max_retries=0,
            timeout=proofreader.API_TIMEOUT_SECONDS,
        )

    async def test_empty_responses_stop_after_two_observable_retries(self):
        limiter = CountingRateLimiter()
        stats = proofreader._new_api_stats()
        with patch("src.proofreader.API_RETRY_DELAYS", (0.0, 0.0)), \
                patch("src.proofreader._deepseek_request_once",
                      return_value=None) as request:
            result = await proofreader.deepseek_async(
                "target", "", "deepseek-v4-flash", limiter,
                stats=stats, request_label="test-empty",
            )

        self.assertIsNone(result)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(limiter.wait_count, 3)
        self.assertEqual(stats["attempts"], 3)
        self.assertEqual(stats["retries"], 2)
        self.assertEqual(stats["empty_responses"], 3)
        self.assertEqual(stats["failures"], 1)

    async def test_exception_retry_is_rate_limited_and_then_succeeds(self):
        limiter = CountingRateLimiter()
        stats = proofreader._new_api_stats()
        with patch("src.proofreader.API_RETRY_DELAYS", (0.0, 0.0)), \
                patch("src.proofreader._deepseek_request_once",
                      side_effect=[RuntimeError("temporary"), "[]"]) as request:
            result = await proofreader.deepseek_async(
                "target", "", "deepseek-v4-flash", limiter,
                stats=stats, request_label="test-error",
            )

        self.assertEqual(result, "[]")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(limiter.wait_count, 2)
        self.assertEqual(stats["attempts"], 2)
        self.assertEqual(stats["retries"], 1)
        self.assertEqual(stats["failures"], 0)

    async def test_book_path_does_not_wait_twice(self):
        limiters = []

        def make_limiter(*args, **kwargs):
            limiter = CountingRateLimiter(*args, **kwargs)
            limiters.append(limiter)
            return limiter

        async def fake_deepseek_async(
                _content, _reference, _model, rate_limiter, **_kwargs):
            await rate_limiter.wait()
            return "校后文本"

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "chunks.json"
            output = Path(temp) / "result.json"
            source.write_text(
                '[{"target":"原文","context":"","reference":""}]',
                encoding="utf-8",
            )
            with patch("src.proofreader.RateLimiter", side_effect=make_limiter), \
                    patch("src.proofreader.deepseek_async",
                          new=fake_deepseek_async):
                await proofreader.process_paragraphs_async(
                    str(source), str(output), rpm=15, max_concurrent=1,
                )

        self.assertEqual(len(limiters), 1)
        self.assertEqual(limiters[0].wait_count, 1)


class MaxCliTests(unittest.TestCase):
    def test_max_chunk_size_reaches_pipeline(self):
        from src import cli

        with patch.object(
                sys, "argv",
                ["proofread", "max", "sample.md", "--chunk-size", "640",
                 "--no-view"]), \
                patch("src.max_pipeline.run_max", return_value={}) as run_max:
            cli.main()

        self.assertEqual(run_max.call_args.kwargs["chunk_size"], 640)


class MaxCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_is_failed_not_empty_success(self):
        chunks = [{"target": "第一段。", "context": ""}]
        identity = {
            "source_sha256": "a" * 64,
            "model": "deepseek-v4-flash",
            "prompt_sha256": "b" * 64,
            "chunk_size": 200,
        }

        async def invalid_json(*_args, **_kwargs):
            return "不是 JSON"

        with tempfile.TemporaryDirectory() as temp, patch(
                "src.proofreader.deepseek_async", new=invalid_json):
            result = await phase1_json_proofread(
                chunks, concurrent=1, rpm=100000,
                checkpoint_root=Path(temp) / "checkpoints",
                checkpoint_identity=identity,
                system_prompt="test prompt",
            )

        self.assertEqual(result["findings"], [])
        self.assertEqual(result["stats"]["failed_chunks"], 1)
        self.assertEqual(result["stats"]["invalid_json"], 1)

    async def test_wall_clock_timeout_marks_chunk_failed_and_resumes(self):
        """墙钟看门狗：单块永不返回（模拟 DeepSeek 静默 trickle）时，
        request_timeout 后记 failed checkpoint，而不是永久挂起；续跑只补失败块。"""
        chunks = [{"target": "第一段。", "context": ""}]
        identity = {
            "source_sha256": "a" * 64,
            "model": "deepseek-v4-flash",
            "prompt_sha256": "b" * 64,
            "chunk_size": 200,
        }

        async def never_returns(*_args, **_kwargs):
            await asyncio.Event().wait()  # 永不完成

        with tempfile.TemporaryDirectory() as temp:
            checkpoint_root = Path(temp) / "checkpoints"
            with patch("src.proofreader.deepseek_async", new=never_returns):
                result = await phase1_json_proofread(
                    chunks, concurrent=1, rpm=100000,
                    checkpoint_root=checkpoint_root,
                    checkpoint_identity=identity,
                    system_prompt="test prompt",
                    request_timeout=0.5,
                )

            self.assertEqual(result["findings"], [])
            self.assertEqual(result["stats"]["failed_chunks"], 1)
            self.assertEqual(result["stats"]["failures"], 1)
            run_dir = next(path for path in checkpoint_root.iterdir()
                           if path.is_dir())
            record = json.loads(
                (run_dir / "chunk-000000.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "failed")
            self.assertIn("墙钟超时", record["error"])

            # 续跑：同一 checkpoint 根，只补失败块，其余不重复调用 API
            resumed_calls = 0

            async def complete(*_args, **_kwargs):
                nonlocal resumed_calls
                resumed_calls += 1
                return "[]"

            with patch("src.proofreader.deepseek_async", new=complete):
                resumed = await phase1_json_proofread(
                    chunks, concurrent=1, rpm=100000,
                    checkpoint_root=checkpoint_root,
                    checkpoint_identity=identity,
                    system_prompt="test prompt",
                    request_timeout=5.0,
                )

            self.assertEqual(resumed_calls, 1)
            self.assertEqual(resumed["stats"]["failed_chunks"], 0)
            self.assertEqual(resumed["stats"]["attempted_chunks"], 1)
            self.assertEqual(resumed["stats"]["checkpoint_hits"], 0)

    async def test_interruption_resumes_missing_chunk_and_then_uses_zero_calls(self):
        chunks = [
            {"target": "第一段。", "context": ""},
            {"target": "第二段。", "context": ""},
        ]
        identity = {
            "source_sha256": "a" * 64,
            "model": "deepseek-v4-flash",
            "prompt_sha256": "b" * 64,
            "chunk_size": 200,
        }
        first_calls = 0

        async def interrupt_on_second(*_args, **_kwargs):
            nonlocal first_calls
            first_calls += 1
            if first_calls == 2:
                raise asyncio.CancelledError()
            return "[]"

        with tempfile.TemporaryDirectory() as temp:
            checkpoint_root = Path(temp) / "checkpoints"
            with patch("src.proofreader.deepseek_async",
                       new=interrupt_on_second):
                with self.assertRaises(asyncio.CancelledError):
                    await phase1_json_proofread(
                        chunks, concurrent=1, rpm=100000,
                        checkpoint_root=checkpoint_root,
                        checkpoint_identity=identity,
                        system_prompt="test prompt",
                    )
            self.assertEqual(first_calls, 2)

            resumed_calls = 0

            async def complete_missing(*_args, **_kwargs):
                nonlocal resumed_calls
                resumed_calls += 1
                return "[]"

            with patch("src.proofreader.deepseek_async", new=complete_missing):
                resumed = await phase1_json_proofread(
                    chunks, concurrent=1, rpm=100000,
                    checkpoint_root=checkpoint_root,
                    checkpoint_identity=identity,
                    system_prompt="test prompt",
                )

            self.assertEqual(resumed_calls, 1)
            self.assertEqual(resumed["stats"]["checkpoint_hits"], 1)
            self.assertEqual(resumed["stats"]["attempted_chunks"], 1)
            self.assertEqual(resumed["stats"]["failed_chunks"], 0)
            self.assertEqual(resumed["findings"], [])

            final_calls = 0

            async def should_not_run(*_args, **_kwargs):
                nonlocal final_calls
                final_calls += 1
                raise AssertionError("completed checkpoints must skip the API")

            with patch("src.proofreader.deepseek_async", new=should_not_run):
                final = await phase1_json_proofread(
                    chunks, concurrent=1, rpm=100000,
                    checkpoint_root=checkpoint_root,
                    checkpoint_identity=identity,
                    system_prompt="test prompt",
                )

            self.assertEqual(final_calls, 0)
            self.assertEqual(final["stats"]["checkpoint_hits"], 2)
            self.assertEqual(final["stats"]["attempted_chunks"], 0)
            self.assertEqual(final["stats"]["logical_calls"], 0)
            self.assertEqual(final["findings"], [])
            self.assertFalse(list(checkpoint_root.rglob("*.tmp-*")))
            run_dir = next(path for path in checkpoint_root.iterdir()
                           if path.is_dir())
            records = sorted(run_dir.glob("chunk-*.json"))
            self.assertEqual(len(records), 2)
            for index, record in enumerate(records):
                payload = json.loads(record.read_text(encoding="utf-8"))
                self.assertEqual(payload["identity"], identity)
                self.assertEqual(payload["index"], index)
                self.assertEqual(payload["status"], "complete")
                self.assertEqual(payload["findings"], [])


class RunMaxCheckpointIntegrationTests(unittest.TestCase):
    def test_second_run_uses_checkpoint_without_network_calls(self):
        calls = 0

        async def empty_findings(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return "[]"

        alignment = {
            "stats": {"match": 1, "delete": 0, "insert": 0},
            "alignment": [],
            "html": "alignment.html",
        }
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.md"
            source.write_text("待审正文。", encoding="utf-8")
            report = Path(temp) / "report.html"
            with patch("src.proofreader.deepseek_async", new=empty_findings), \
                    patch("src.max_pipeline.phase0_tgscc", return_value=[]), \
                    patch("src.max_pipeline.phase0_variants", return_value=[]), \
                    patch("src.max_pipeline.phase0_structure", return_value=[]), \
                    patch("src.max_pipeline.phase3_align",
                          return_value=alignment), \
                    patch("src.max_pipeline.phase4_report",
                          return_value=str(report)):
                first = run_max(
                    str(source), concurrent=1, rpm=100000,
                    chunk_size=200,
                )
                first_call_count = calls
                second = run_max(
                    str(source), concurrent=1, rpm=100000,
                    chunk_size=200,
                )

            self.assertEqual(first_call_count, 1)
            self.assertEqual(calls, 1)
            self.assertEqual(first["stats"]["phase1"]["attempted_chunks"], 1)
            self.assertEqual(second["stats"]["phase1"]["attempted_chunks"], 0)
            self.assertEqual(second["stats"]["phase1"]["checkpoint_hits"], 1)
            saved = json.loads(
                (Path(temp) / "source_max_results.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(saved["stats"]["phase1"]["logical_calls"], 0)


if __name__ == "__main__":
    unittest.main()
