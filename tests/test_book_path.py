"""Regression tests for the book path (proofread b) reliability fix.

The old book path rewrote the whole output JSON per paragraph (O(N²), and
serialized by a global lock) and silently skipped retry-exhausted paragraphs
with exit code 0. The new path writes one atomic sidecar file per paragraph,
raises RuntimeError when any paragraph fails, and resumes by only processing
missing paragraphs.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.proofreader import process_paragraphs_async


class BookPathReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def _write_source(self, path: Path, n: int = 3) -> None:
        chunks = [
            {"target": f"第{i + 1}段待校正文。", "context": "", "reference": ""}
            for i in range(n)
        ]
        path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")

    async def test_success_writes_sidecar_and_merges(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "chunks.json"
            output = root / "out.json"
            self._write_source(source, n=3)

            async def fake_async(_content, _reference, _model, rate_limiter,
                                 **_kwargs):
                await rate_limiter.wait()
                return "修正后文本"

            with patch("src.proofreader.deepseek_async", new=fake_async):
                result = await process_paragraphs_async(
                    str(source), str(output), rpm=100000, max_concurrent=2)

            self.assertEqual(result, ["修正后文本"] * 3)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data, ["修正后文本"] * 3)

            sidecar = root / "out.json.chunks"
            self.assertTrue(sidecar.is_dir())
            self.assertEqual(len(list(sidecar.glob("*.json"))), 3)
            self.assertEqual(len(list(sidecar.glob("*.error.json"))), 0)

    async def test_failure_raises_and_marks_error_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "chunks.json"
            output = root / "out.json"
            self._write_source(source, n=3)

            async def flaky(_content, _reference, _model, rate_limiter,
                            **_kwargs):
                await rate_limiter.wait()
                if "第2段" in _content:
                    return None  # 重试耗尽
                return "修正后文本"

            with patch("src.proofreader.deepseek_async", new=flaky):
                with self.assertRaisesRegex(RuntimeError, "第2段"):
                    await process_paragraphs_async(
                        str(source), str(output), rpm=100000, max_concurrent=2)

            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data, ["修正后文本", None, "修正后文本"])
            self.assertTrue(
                (root / "out.json.chunks" / "000001.error.json").exists())

    async def test_resume_only_processes_failed_paragraph(self):
        calls = []

        async def flaky(_content, _reference, _model, rate_limiter, **_kwargs):
            await rate_limiter.wait()
            calls.append(_content)
            if "第2段" in _content:
                return None
            return "修正后文本"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "chunks.json"
            output = root / "out.json"
            self._write_source(source, n=3)

            with patch("src.proofreader.deepseek_async", new=flaky):
                with self.assertRaises(RuntimeError):
                    await process_paragraphs_async(
                        str(source), str(output), rpm=100000, max_concurrent=2)
            first_calls = list(calls)
            self.assertEqual(len(first_calls), 3)

            calls.clear()

            async def now_ok(_content, _reference, _model, rate_limiter,
                             **_kwargs):
                await rate_limiter.wait()
                calls.append(_content)
                return "修正后文本"

            with patch("src.proofreader.deepseek_async", new=now_ok):
                result = await process_paragraphs_async(
                    str(source), str(output), rpm=100000, max_concurrent=2)

            self.assertEqual(len(calls), 1)  # 已完成段零模型调用
            self.assertIn("第2段", calls[0])
            self.assertEqual(result, ["修正后文本"] * 3)


if __name__ == "__main__":
    unittest.main()
