import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('archive', '0011_bird_guide')]
    operations = [
        migrations.CreateModel(name='RestoreSession', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('backup_set_id', models.UUIDField()),
            ('backup_manifest_sha256', models.CharField(max_length=64)),
            ('source_deployment_id', models.CharField(blank=True, max_length=160)),
            ('source_roots', models.JSONField(default=dict)), ('target_roots', models.JSONField(default=dict)),
            ('manifest', models.JSONField(default=dict)),
            ('status', models.CharField(choices=[('prepared', '已准备'), ('verified', '已核验'), ('activated', '已激活'), ('failed', '失败')], default='prepared', max_length=16)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('verified_at', models.DateTimeField(blank=True, null=True)),
            ('activated_at', models.DateTimeField(blank=True, null=True)),
        ]),
        migrations.CreateModel(name='RestoreBinding', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('kind', models.CharField(max_length=32)), ('object_id', models.CharField(max_length=1000)),
            ('role', models.CharField(max_length=16)), ('source_relative_path', models.CharField(max_length=1000)),
            ('target_relative_path', models.CharField(max_length=1000)),
            ('source_fingerprint', models.JSONField(default=dict)), ('target_fingerprint', models.JSONField(default=dict)),
            ('sha256', models.CharField(max_length=64)), ('byte_size', models.PositiveBigIntegerField()),
            ('hardlink_group', models.CharField(max_length=100)),
            ('verification_reason', models.CharField(default='backup_manifest_verified', max_length=64)),
            ('session', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bindings', to='archive.restoresession')),
        ]),
        migrations.AddConstraint(model_name='restoresession', constraint=models.UniqueConstraint(fields=('backup_set_id', 'backup_manifest_sha256', 'target_roots'), name='restore_target_once')),
        migrations.AddConstraint(model_name='restoresession', constraint=models.UniqueConstraint(condition=models.Q(status='activated'), fields=('status',), name='one_activated_restore')),
        migrations.AddConstraint(model_name='restorebinding', constraint=models.UniqueConstraint(fields=('session', 'kind', 'object_id', 'role'), name='restore_binding_object_role_unique')),
    ]
