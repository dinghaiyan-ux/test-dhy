import unittest

class TestFailure(unittest.TestCase):
    def test_this_will_fail(self):
        """
        这个测试用例故意设置为失败，用于制造 GitHub Actions 的失败状态。
        """
        print("正在运行一个故意制造失败的测试...")
        expected_value = "成功"
        actual_value = "失败"
        
        # 故意导致断言失败
        self.assertEqual(actual_value, expected_value, "这里故意制造了一个错误：实际值与期望值不符")

if __name__ == '__main__':
    unittest.main()
