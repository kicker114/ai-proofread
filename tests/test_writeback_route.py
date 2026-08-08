"""A3 回归测试：writeback ROUTING 尊重显式 fix_class。

max pipeline findings 可能自带 fix_class（显式），writeback 应优先尊重它，
而非只凭 phase+severity 推断（旧行为会覆盖 LLM/上游的显式意图）。
"""
import unittest

from src.writeback import _route, findings_to_adeu_changes


def _finding(phase: str, severity: str, fix_class: str = "", **kw) -> dict:
    f = {
        "phase": phase,
        "severity": severity,
        "original": "测试原文句子足够长用于定位",
        "suggestion": "测试修改后的句子内容",
        "type": "test",
    }
    if fix_class:
        f["fix_class"] = fix_class
    f.update(kw)
    return f


class WritebackRouteTests(unittest.TestCase):
    def test_explicit_fix_class_respected(self):
        # 1_llm warn 默认路由是 must_fix，但显式 polish 应被尊重
        changes = findings_to_adeu_changes(
            [_finding("1_llm", "warn", "polish")]
        )
        self.assertEqual(len(changes), 1)
        # polish → 不改文（new_text == text）
        self.assertEqual(changes[0]["new_text"], "测试原文句子足够长用于定位")
        self.assertIn("润色", changes[0]["comment"])

    def test_explicit_must_fix_edits_text(self):
        changes = findings_to_adeu_changes(
            [_finding("1_llm", "info", "must_fix")]
        )
        # 1_llm info 默认是 polish，但显式 must_fix 应改文
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["new_text"], "测试修改后的句子内容")

    def test_explicit_verify_no_edit(self):
        changes = findings_to_adeu_changes(
            [_finding("2_names", "info", "verify")]
        )
        self.assertEqual(changes[0]["new_text"], "测试原文句子足够长用于定位")
        self.assertIn("待核", changes[0]["comment"])

    def test_no_explicit_falls_back_to_route(self):
        # 无显式 fix_class → 仍用 phase+severity 路由
        fc, prefix = _route("1_llm", "warn")
        self.assertEqual(fc, "must_fix")

    def test_invalid_explicit_ignored(self):
        # 非法 fix_class → 忽略，回退路由
        changes = findings_to_adeu_changes(
            [_finding("1_llm", "warn", "bogus")]
        )
        self.assertEqual(changes[0]["new_text"], "测试修改后的句子内容")  # must_fix 改文


if __name__ == "__main__":
    unittest.main()
