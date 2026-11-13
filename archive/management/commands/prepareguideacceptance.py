"""Disposable synthetic guide acceptance; never accepts production settings."""
import hashlib
import json
import os
import re
import tempfile
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qsl, urlencode
from wsgiref.simple_server import make_server

from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command


class Command(BaseCommand):
    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--check', action='store_true')
        mode.add_argument('--serve', action='store_true')
        parser.add_argument('--port', type=int, default=0)
        parser.add_argument('--remaining-scenarios', action='store_true',
            help='仅临时验收：ordinary/text200/nojs 场景，禁止 live 公开透传')
        parser.add_argument('--guide-nav-fail-once', action='store_true',
            help='仅remaining验收：第一次增强GET /guide/?q=北美返回503')

    def handle(self, *args, **options):
        root = Path(getattr(settings, 'BIRDS_TEST_ROOT', '/invalid'))
        if (settings.SETTINGS_MODULE != 'config.settings_test' or root.is_symlink()
                or root.resolve().parent != Path(tempfile.gettempdir()).resolve()
                or not root.name.startswith('birds-tests-') or root.stat().st_uid != os.getuid()
                or (root/'.birds-test-root').is_symlink()
                or (root/'.birds-test-root').read_text() != 'birds isolated v1'):
            raise CommandError('只允许已验证临时根的 config.settings_test')
        for value in [settings.BIRDS_INBOX_ROOT, settings.BIRDS_DATA_ROOT, settings.BIRDS_ARCHIVE_ROOT,
                settings.BIRDS_CLAIM_ROOT, settings.STATIC_ROOT, settings.DATABASES['default']['NAME']]:
            if not Path(value).resolve().is_relative_to(root.resolve()):
                raise CommandError('验收路径超出临时根')
        if options['guide_nav_fail_once'] and not (options['serve'] and options['remaining_scenarios']):
            raise CommandError('--guide-nav-fail-once仅允许--serve --remaining-scenarios')
        from archive.services.guide_online import load_metadata
        assets = [asset for row in load_metadata()['entries'].values() for asset in row.get('reference_assets', [])]
        from archive.services.guide_sources import BUNDLE_PATH
        for asset in assets:
            if hashlib.sha256((BUNDLE_PATH.parent/asset['relative_key']).read_bytes()).hexdigest() != asset['sha256']:
                raise CommandError('参考图缺失或哈希不符')
        if len(assets) != 2:
            raise CommandError('必须复用两张许可图')
        if options['check']:
            self.stdout.write(json.dumps({'check':'pass','root':str(root),'assets':len(assets),
                'fixtures':'--serve creates two synthetic JPEGs, mapped/unmapped candidates and catalog concepts',
                'fixture_script':Path('archive/tests/guide_online_acceptance_fixture.js').is_file()}))
            return
        from archive.models import Photo
        call_command('migrate', interactive=False, verbosity=0)
        if Photo.objects.exists():
            raise CommandError('仅允许全新验收库，拒绝覆盖已有照片')
        from archive.models import SourceSnapshot, Species, SpeciesConcept, Job, RecognitionRun, RecognitionState, ModelArtifact, RecognitionCandidate
        from archive.services.guide_sources import load_evidence
        from archive.services.intake import scan_once
        from archive.services.jobs import process_source
        from archive.services.paths import open_root
        from archive.services.confirmation import confirm_photo
        from archive.tests.fixtures import jpeg
        evidence = load_evidence()
        snapshot = SourceSnapshot.objects.create(source='AviList',version='v2025b',
            sha256='c03d5fbcfe18d75ce08f08d3bd4c03f056ced508cbc1b27a6c6d25a5220e2250',
            acquired_at='2026-10-26',download_page_url='https://www.avilist.org/',
            license_url='https://creativecommons.org/licenses/by/4.0/',attribution='Synthetic catalog rows',row_count=4,species_count=4,manifest={})
        species = []
        for row, name in zip(evidence['mappings'], ['大白鹭','红耳鹎','北美红雀','灰额主红雀']):
            item = Species.objects.create(scientific_name=row['scientific_name'],scientific_key=row['scientific_name'].lower(),
                chinese_name=name,search_key=name,source_snapshot=snapshot)
            SpeciesConcept.objects.create(species=item,snapshot=snapshot,external_id=row['external_id'],evidence='Synthetic row for verified public concept')
            species.append(item)
        for filename,color in [('候选合成.jpg','red'),('已确认合成.jpg','blue')]:
            jpeg(Path(settings.BIRDS_INBOX_ROOT)/filename,color=color)
        scan_once()
        a,b = open_root(settings.BIRDS_INBOX_ROOT),open_root(settings.BIRDS_DATA_ROOT)
        try:
            for job in Job.objects.filter(kind='import',state='pending'):
                if not process_source(job,a,b): raise CommandError('合成导入未完成')
        finally:
            os.close(a);os.close(b)
        pending = Photo.objects.get(observations__source__relative_path='候选合成.jpg')
        confirmed = Photo.objects.get(observations__source__relative_path='已确认合成.jpg')
        confirm_photo(confirmed.pk,confirmed.metadata_version,uuid.uuid4(),'species',[species[2].pk],[])
        call_command('runworker','--once',verbosity=0)
        artifact = ModelArtifact.objects.create(manifest_digest='e'*64,provider='合成候选（未运行模型）',version='fixture',revision='acceptance',manifest={})
        run = RecognitionRun.objects.get(photo=pending)
        RecognitionRun.objects.filter(pk=run.pk).update(artifact=artifact,state='succeeded',safe_error='')
        RecognitionState.objects.filter(photo=pending).update(active_run=run)
        RecognitionCandidate.objects.create(run=run,rank=1,species=species[2],raw_label='Cardinalis cardinalis',score=.8,mapping_status='mapped',mapping_evidence='Synthetic exact concept')
        RecognitionCandidate.objects.create(run=run,rank=2,raw_label='未核实同名候选',score=.7,mapping_status='unmapped')
        # Only this disposable template loads the control fixture, before production JS.
        from django.test import override_settings
        from copy import deepcopy
        override = root/'templates'/'archive'; override.mkdir(parents=True)
        base = Path('archive/templates/archive/base.html').read_text()
        base = base.replace('<script defer src="{% static \'archive/app.js\' %}">',
            '<script src="/acceptance-fixture.js"></script><script defer src="{% static \'archive/app.js\' %}">')
        if options['remaining_scenarios']:
            base = base.replace("{% static 'archive/app.css' %}",
                "{% if request.COOKIES.guide_acceptance_mode == 'text200' %}"
                "{% static 'archive/acceptance-text200.css' %}{% else %}"
                "{% static 'archive/app.css' %}{% endif %}")
            base = base.replace('</head>',
                '<meta name="guide-acceptance-controlled-only" content="true"></head>')
            base = base.replace('<body>', '<body><aside data-acceptance-marker>'
                '<p>本地合成验收；公开API透传关闭。</p>'
                "{% if request.COOKIES.guide_acceptance_mode == 'text200' %}"
                '<p data-acceptance-text-scale="2">受控作者样式200%文字：根字号与固定px字号翻倍；间距和布局值不变；非原生浏览器缩放。</p>'
                "{% elif request.COOKIES.guide_acceptance_mode == 'nojs' %}"
                '<p data-acceptance-nojs>受控无脚本：响应CSP sandbox不含allow-scripts；保留原HTML、普通链接、表单和noscript。</p>'
                '{% else %}<p>普通字号；固定受控响应。</p>{% endif %}'
                '<p data-acceptance-script-state>脚本尚未执行</p>'
                '<p data-acceptance-request-count>受控API请求：0；公开透传：关闭</p></aside>')
        (override/'base.html').write_text(base)
        templates = deepcopy(settings.TEMPLATES)
        templates[0]['DIRS'] = [str(root/'templates')]
        template_override = override_settings(TEMPLATES=templates)
        template_override.enable()
        call_command('collectstatic',interactive=False,verbosity=0)
        text_scale = None
        if options['remaining_scenarios']:
            css_path = Path(settings.STATIC_ROOT)/'archive'/'app.css'
            css = css_path.read_text()
            mapping = []
            def double_font(match):
                before = match.group('size')
                after = format(float(before)*2, 'g')
                mapping.append({'line':css.count('\n',0,match.start())+1,
                    'before_px':before, 'after_px':after})
                return match.group('prefix')+after+'px'
            scaled_css = re.sub(r'(?P<prefix>\bfont-size\s*:\s*)(?P<size>\d+(?:\.\d+)?)px\b',
                double_font,css)
            if not mapping or not re.search(r':root\s*\{[^}]*font-size\s*:\s*32px',scaled_css):
                raise CommandError('受控文字放大缺少根字号映射')
            scaled_path = css_path.with_name('acceptance-text200.css')
            scaled_path.write_text(scaled_css)
            text_scale = {'factor':2, 'method':'controlled author font-size only; not native browser zoom',
                'source_sha256':hashlib.sha256(css.encode()).hexdigest(),
                'scaled_sha256':hashlib.sha256(scaled_css.encode()).hexdigest(),
                'fixed_px_mapping':mapping, 'rem_and_inherited':'double via root font-size',
                'other_css_values':'unchanged'}
            (root/'text-scale.json').write_text(json.dumps(text_scale,ensure_ascii=False,indent=2))
        from django.core.wsgi import get_wsgi_application
        from django.http import FileResponse
        from django.views.static import serve
        app = get_wsgi_application()
        guide_nav_fault = {'enabled':options['guide_nav_fail_once'], 'consumed':0,
            'trigger':'GET /guide/; q=北美; Sec-Fetch-Dest=empty (enhanced fetch, not document navigation)'}
        def application(environ,start_response):
            mode = 'ordinary'
            selected_mode = None
            if options['remaining_scenarios']:
                cookies = SimpleCookie(environ.get('HTTP_COOKIE',''))
                if 'guide_acceptance_mode' in cookies:
                    mode = cookies['guide_acceptance_mode'].value
                query = parse_qsl(environ.get('QUERY_STRING',''),keep_blank_values=True)
                selected_mode = next((value for key,value in query if key == 'acceptance_mode'),None)
                # The fixture selector is never passed to production query validation.
                environ['QUERY_STRING'] = urlencode([(key,value) for key,value in query if key != 'acceptance_mode'])
                if selected_mode in ('ordinary','text200','nojs'):
                    mode = selected_mode
                    cookies['guide_acceptance_mode'] = mode
                    environ['HTTP_COOKIE'] = '; '.join(cookie.OutputString() for cookie in cookies.values())
                if mode not in ('ordinary','text200','nojs'):
                    mode = 'ordinary'
            def acceptance_start(status,headers,exc_info=None):
                if options['remaining_scenarios']:
                    headers = list(headers)
                    headers.append(('X-Acceptance-Mode',mode))
                    if selected_mode in ('ordinary','text200','nojs'):
                        headers.append(('Set-Cookie','guide_acceptance_mode='+mode+'; Path=/; SameSite=Strict; HttpOnly'))
                    if mode == 'nojs':
                        # Additional policy preserves every original CSP directive.
                        headers.append(('Content-Security-Policy',
                            'sandbox allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation'))
                return start_response(status,headers,exc_info)
            path = environ.get('PATH_INFO','')
            if (guide_nav_fault['enabled'] and not guide_nav_fault['consumed']
                    and environ.get('REQUEST_METHOD') == 'GET' and path == '/guide/'
                    and dict(parse_qsl(environ.get('QUERY_STRING',''))).get('q') == '北美'
                    and environ.get('HTTP_SEC_FETCH_DEST') == 'empty'):
                guide_nav_fault['consumed'] = 1
                (root/'guide-nav-fault.json').write_text(json.dumps(guide_nav_fault,ensure_ascii=False))
                self.stdout.write(json.dumps({'acceptance_fault':'guide-nav-503-once','consumed':1}))
                self.stdout.flush()
                body = '受控图鉴导航失败（仅一次）'.encode()
                acceptance_start('503 Service Unavailable',[
                    ('Content-Type','text/plain; charset=utf-8'),('Cache-Control','no-store'),
                    ('X-Acceptance-Fault','guide-nav-503-once'),('Content-Length',str(len(body)))])
                return [body]
            if path == '/acceptance-fixture.js':
                response = FileResponse(open('archive/tests/guide_online_acceptance_fixture.js','rb'),content_type='text/javascript')
                acceptance_start('200 OK',list(response.items())); return response
            if path.startswith('/static/'):
                from django.core.handlers.wsgi import WSGIRequest
                response = serve(WSGIRequest(environ),path[8:],document_root=settings.STATIC_ROOT)
                acceptance_start(str(response.status_code)+' OK',list(response.items())); return response
            return app(environ,acceptance_start)
        server = make_server('127.0.0.1',options['port'],application)
        record = {'root':str(root),'pid':os.getpid(),'port':server.server_port,'single_thread':True,
            'candidate':'/photos/'+str(pending.pk)+'/', 'confirmed':'/photos/'+str(confirmed.pk)+'/',
            'information':'/bird-info/'+str(species[2].pk)+'/?candidate=1',
            'guide':'/guide/'+str(species[2].pk)+'/', 'unphotographed':'/guide/'+str(species[3].pk)+'/',
            'sha256':[pending.sha256,confirmed.sha256],
            'remaining_scenarios':options['remaining_scenarios'], 'text_scale':text_scale,
            'guide_nav_fault':guide_nav_fault,
            'mode_selection':'?acceptance_mode=ordinary|text200|nojs; cookie persists for local navigation'}
        (root/'acceptance.json').write_text(json.dumps(record))
        self.stdout.write(json.dumps(record));self.stdout.flush()
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally:
            server.server_close()
            template_override.disable()
