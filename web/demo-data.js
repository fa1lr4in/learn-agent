// 此处所有内容都是前端示例；没有读取 Python 文件或调用模型。
export const DEMO = {
  prompt: "请核对 main.py 和 test_main.py，运行现有验收，再运行入口，简要报告结果。不要修改文件。",
  files: [
    { path: "main.py", content: "def sum_to(n):\n    return sum(range(1, n + 1))\n\nif __name__ == '__main__':\n    print(sum_to(100))" },
    { path: "test_main.py", content: "import unittest\nfrom main import sum_to\n\nclass SumTests(unittest.TestCase):\n    def test_zero(self): self.assertEqual(sum_to(0), 0)\n    def test_one(self): self.assertEqual(sum_to(1), 1)\n    def test_ten(self): self.assertEqual(sum_to(10), 55)\n    def test_hundred(self): self.assertEqual(sum_to(100), 5050)" },
  ],
  executions: [
    {
      id: "tests", name: "run_command", title: "运行现有验收测试",
      command: "python -m unittest -v test_main.py",
      result: { exit_code: 0, stdout: "", stderr: "test_hundred ... ok\ntest_one ... ok\ntest_ten ... ok\ntest_zero ... ok\n\nRan 4 tests in 0.000s\n\nOK" },
    },
    {
      id: "entry", name: "run_python", title: "运行项目入口",
      command: "python main.py",
      result: { exit_code: 0, stdout: "5050", stderr: "" },
    },
  ],
  conclusion: "示例验收完成：4 项测试通过，报告位于 stderr；入口 stdout 为 5050，退出码为 0。两次运行分别取得确认，没有修改文件。以上内容来自前端示例数据。",
};
