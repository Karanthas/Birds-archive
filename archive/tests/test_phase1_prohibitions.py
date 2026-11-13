"""第一阶段的禁止原则以实际导入、私有页面和来源声明作负向边界。"""
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TransactionTestCase

from archive.models import Photo, Job
from archive.tests.fixtures import command, jpeg


class Phase1ProhibitionTests(TransactionTestCase):
    def setUp(self):
        for item in Path(settings.BIRDS_INBOX_ROOT).iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
        self.owner = User.objects.create_superuser(settings.BIRDS_OWNER_USERNAME, password='synthetic-only')
        self.client.force_login(self.owner)

    def import_photo(self):
        source = Path(settings.BIRDS_INBOX_ROOT) / '阶段禁止原则合成照片.jpg'
        digest = jpeg(source)
        result = command('runworker', '--once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Photo.objects.count(), 1)
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        return Photo.objects.get()

    def test_successful_import_is_review_pending_in_pages(self):
        photo = self.import_photo()
        self.assertEqual(photo.state, 'review_pending')
        self.assertEqual(Job.objects.get().state, 'succeeded')
        for url in ('/review/', f'/photos/{photo.pk}/', '/tasks/'):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, '导入完成' if url == '/tasks/' else '待确认')
                for forbidden in ('已确认', '已归档', '归档成功'):
                    self.assertNotContains(page, forbidden)
                self.assertIn('no-store', page['Cache-Control'])

    def test_source_probes_and_pages_do_not_claim_model_runtime_or_accuracy(self):
        matrix = json.loads((settings.BASE_DIR / 'docs/source-feasibility.json').read_text())
        rows = {row['id']: row for row in matrix['entries']}
        self.assertEqual(rows['model_accuracy']['status'], 'pending')
        for ident in ('model_weights', 'model_cpu', 'model_accuracy', 'cloud_entry', 'cloud_price'):
            self.assertIs(rows[ident]['executed'], False, ident)
        self.assertIn('不是此图库证据', rows['model_accuracy']['evidence_summary'])
        self.assertIn('尚未配置', rows['model_accuracy']['gap'])
        self.assertIn('没有安装工具', rows['model_cpu']['gap'])
        self.assertIn('未下载/执行权重', rows['model_weights']['evidence_summary'])
        document = (settings.BASE_DIR / 'docs/source-feasibility.md').read_text()
        for declaration in ('未下载权重', '不是NAS运行/用户精度', '原始分数不当概率', '没有实际账单/调用次数证据'):
            self.assertIn(declaration, document)
        photo = self.import_photo()
        for url in ('/', '/review/', f'/photos/{photo.pk}/', '/tasks/', '/species/?q=Cardinalis'):
            with self.subTest(url=url):
                page = self.client.get(url)
                for forbidden in ('识别结果', '识别概率', '识别准确率', '识别置信度', '模型已运行', 'NAS实测通过'):
                    self.assertNotContains(page, forbidden)
                self.assertNotRegex(page.content.decode(), r'\b\d+(?:\.\d+)?\s*[%％]')
