from django.test.runner import DiscoverRunner

class NonEmptyDiscoverRunner(DiscoverRunner):
    def build_suite(self, *args, **kwargs):
        suite = super().build_suite(*args, **kwargs)
        if suite.countTestCases() == 0:
            raise RuntimeError("测试集为空，拒绝把零测试作为通过")
        return suite

    def suite_result(self, suite, result, **kwargs):
        if result.skipped:
            self.log(f"有 {len(result.skipped)} 项测试被跳过，拒绝作为通过")
        return super().suite_result(suite, result, **kwargs) + len(result.skipped)
