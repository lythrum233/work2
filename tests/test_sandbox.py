"""tests/test_sandbox.py —— 沙盒单元测试

来源：作业 1 §9.1 的 T1~T6 用例迁移。

特点：不依赖 LLM，离线确定性运行，可直接验证 tools/sandbox.py 的健壮性。
"""

import unittest

from tools.sandbox import execute_python_code

OK_OUTPUT = 300      # stdout 摘要长度


class TestSandbox(unittest.TestCase):
    # T1 正常执行
    def test_t1_normal_execution(self):
        result = execute_python_code("print(sum(range(101)))")
        self.assertTrue(result["success"])
        self.assertEqual(result["stdout"].strip(), "5050")

    # T2 除零异常
    def test_t2_zero_division(self):
        code = "total = 0\nfor x in [1, 2, 0, 4]:\n    total += 1 / x\nprint(total)"
        result = execute_python_code(code)
        self.assertFalse(result["success"])
        self.assertIn("ZeroDivisionError", result["error_msg"])

    # T3 索引越界
    def test_t3_index_error(self):
        result = execute_python_code("xs = [1]\nprint(xs[5])")
        self.assertFalse(result["success"])
        self.assertIn("IndexError", result["error_msg"])

    # T4 超时保护
    def test_t4_timeout(self):
        result = execute_python_code("import time\ntime.sleep(30)", timeout=3)
        self.assertFalse(result["success"])
        self.assertIn("超时", result["error_msg"])

    # T5 空代码
    def test_t5_empty_code(self):
        result = execute_python_code("   ")
        self.assertFalse(result["success"])
        self.assertIn("代码为空", result["error_msg"])

    # T6 中文输出不乱码
    def test_t6_chinese_output(self):
        result = execute_python_code("print('你好，世界！中文输出测试')")
        self.assertTrue(result["success"])
        self.assertEqual(result["stdout"].strip(), "你好，世界！中文输出测试")


if __name__ == "__main__":
    unittest.main(verbosity=2)
