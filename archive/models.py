import uuid
import re
from datetime import date, datetime
from django.db import models
from django.utils import timezone


def capture_sort_key(local):
    """Validate local calendar fields; compare up to 32 fractional digits losslessly."""
    if not isinstance(local, str):
        raise ValueError('拍摄时间无效')
    if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', local):
        return date.fromisoformat(local).isoformat()
    match = re.fullmatch(r'([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,32}))?', local)
    if not match:
        raise ValueError('拍摄时间无效')
    # datetime validates calendar/time only: it cannot hold more than six digits.
    seconds = datetime.fromisoformat(match[1]).isoformat(sep=' ', timespec='seconds')
    return seconds+'.'+(match[2] or '').ljust(32, '0')


class Photo(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    byte_size = models.PositiveBigIntegerField(editable=False)
    state = models.CharField(max_length=32, default="review_pending")
    preview_key = models.CharField(max_length=240)
    original_available = models.BooleanField(default=False)
    exif_raw = models.JSONField(default=dict)
    captured_local = models.CharField(max_length=80, null=True, blank=True)
    capture_offset = models.SmallIntegerField(null=True, blank=True)
    capture_precision = models.CharField(max_length=16, default="unknown")
    capture_quality = models.CharField(max_length=16, default="missing")
    manual_capture = models.JSONField(null=True, blank=True)
    current_capture = models.JSONField(null=True, blank=True)
    current_classification = models.CharField(max_length=32, blank=True)
    capture_override_mode = models.CharField(max_length=16, default='inherit')
    management_generation = models.PositiveIntegerField(default=0)
    trash_requested_at = models.DateTimeField(null=True, blank=True)
    trashed_at = models.DateTimeField(null=True, blank=True)
    favorite = models.BooleanField(default=False)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, max_length=10000)
    user_location = models.CharField(max_length=200, blank=True)
    metadata_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sha256", "byte_size"], name="photo_content_unique"),
            models.CheckConstraint(condition=models.Q(rating__isnull=True) | models.Q(rating__gte=1, rating__lte=5), name='photo_rating_range')]
        ordering = ["created_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = Photo.objects.filter(pk=self.pk).values("sha256", "byte_size").first()
            if old and (old["sha256"], old["byte_size"]) != (self.sha256, self.byte_size):
                raise ValueError("照片内容身份不可改写")
        super().save(*args, **kwargs)

    @property
    def capture_display(self):
        return self.effective_capture()['local'] or '未知'

    @property
    def capture_source(self):
        return {'manual': '人工补充', 'exif': '原始 EXIF'}.get(self.effective_capture()['source'], '未知')

    @property
    def capture_quality_display(self):
        return {'reliable':'可靠','missing':'缺失','invalid':'无效'}.get(self.capture_quality,'未知')

    @property
    def capture_precision_display(self):
        precision = self.effective_capture()['precision']
        return {"date": "日期", "second": "秒", "subsecond": "小数秒"}.get(precision, "未知")

    @property
    def capture_offset_display(self):
        offset = self.effective_capture()['offset']
        if offset is None:
            return "时区未知"
        return f"{'+' if offset >= 0 else '-'}{abs(offset)//60:02d}:{abs(offset)%60:02d}"

    @property
    def can_fill_capture(self):
        return self.state == "review_pending" and self.capture_quality in ("missing", "invalid") and self.manual_capture is None

    def effective_capture(self):
        """不为日期补时刻，不为未知偏移虚构 UTC。"""
        unknown = dict.fromkeys(('local', 'precision', 'offset', 'source', 'year', 'month', 'sort_key'))
        if self.capture_override_mode == 'unknown':
            return unknown
        value = self.manual_capture
        source = 'manual'
        if value is None and self.capture_quality == 'reliable':
            value = {'local': self.captured_local, 'precision': self.capture_precision,
                     'offset': self.capture_offset}
            source = 'exif'
        if not isinstance(value, dict):
            return unknown
        local, precision, offset = value.get('local'), value.get('precision'), value.get('offset')
        try:
            if not isinstance(local, str) or precision not in ('date', 'second', 'subsecond'):
                return unknown
            # Earlier manual metadata also accepted the ISO date/time separator.
            if len(local) >= 19 and local[10] == 'T':
                local = local[:10]+' '+local[11:]
            sort_key = capture_sort_key(local)
            if ((precision == 'date' and len(local) != 10)
                    or (precision == 'second' and len(local) != 19)
                    or (precision == 'subsecond' and not 21 <= len(local) <= 52)):
                return unknown
            if offset is not None and (type(offset) is not int or not -840 <= offset <= 840):
                return unknown
        except (ValueError, TypeError):
            return unknown
        return dict(local=local, precision=precision, offset=offset, source=source,
                    year=local[:4], month=local[:7], sort_key=sort_key)

    @property
    def state_display(self):
        return {'review_pending': '待确认', 'confirmed': '等待归档', 'archiving': '归档中',
                'archive_failed': '归档失败，待恢复', 'archived': '已归档'}.get(self.state, '状态未知')


class GalleryCollection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filters = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()


class GalleryMember(models.Model):
    collection = models.ForeignKey(GalleryCollection, on_delete=models.CASCADE, related_name='members')
    photo = models.ForeignKey(Photo, on_delete=models.PROTECT, related_name='gallery_memberships')
    position = models.PositiveBigIntegerField()
    group_key = models.CharField(max_length=7)
    metadata_version = models.PositiveIntegerField()
    management_generation = models.PositiveIntegerField()

    class Meta:
        ordering = ['position']
        constraints = [models.UniqueConstraint(fields=['collection', 'photo'], name='gallery_member_photo_unique'),
            models.UniqueConstraint(fields=['collection', 'position'], name='gallery_member_position_unique')]


class ImportSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    root_key = models.CharField(max_length=64, default="inbox")
    relative_path = models.CharField(max_length=1000)
    observed_version = models.PositiveIntegerField(default=1)
    fingerprint = models.JSONField(default=dict)
    sha256 = models.CharField(max_length=64, blank=True)
    photo = models.ForeignKey(Photo, null=True, blank=True, on_delete=models.PROTECT, related_name="sources")
    state = models.CharField(max_length=32, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    current_observation = models.ForeignKey("SourceObservation", null=True, blank=True,
        on_delete=models.PROTECT, related_name="current_for_sources")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["root_key", "relative_path"], name="source_path_unique")]
        ordering = ["created_at", "id"]


class SourceObservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT, related_name="observations")
    version = models.PositiveIntegerField()
    fingerprint = models.JSONField(default=dict)
    sha256 = models.CharField(max_length=64, blank=True)
    byte_size = models.PositiveBigIntegerField(default=0)
    photo = models.ForeignKey(Photo, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="observations")
    state = models.CharField(max_length=32, default="waiting")
    duplicate = models.BooleanField(default=False)
    available = models.BooleanField(default=False)
    duplicate_first_seen = models.DateTimeField(null=True, blank=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    stable_since = models.DateTimeField(default=timezone.now)
    stable_count = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "version"], name="observation_version_unique")]
        ordering = ["first_seen", "id"]


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT, related_name="jobs")
    source_version = models.PositiveIntegerField()
    expected_sha256 = models.CharField(max_length=64, blank=True)
    kind = models.CharField(max_length=32, default="import")
    state = models.CharField(max_length=32, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    stage = models.CharField(max_length=32, default="queued")
    error_code = models.CharField(max_length=64, blank=True)
    safe_message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "source_version", "kind"], name="job_version_unique")]
        ordering = ["next_attempt_at", "created_at", "id"]


class SourceSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=160)
    version = models.CharField(max_length=160)
    sha256 = models.CharField(max_length=64)
    acquired_at = models.DateField()
    download_page_url = models.URLField(max_length=1000)
    license_url = models.URLField(max_length=1000)
    attribution = models.TextField()
    row_count = models.PositiveIntegerField()
    species_count = models.PositiveIntegerField()
    manifest = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'version'], name='catalog_snapshot_unique')]
        ordering = ['created_at', 'id']


class Species(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_kind = models.CharField(max_length=16, choices=[('catalog', '名录'), ('local', '本地')], default='catalog')
    local_name = models.CharField(max_length=300, null=True, blank=True)
    local_key = models.CharField(max_length=1000, null=True, blank=True, unique=True)
    scientific_name = models.CharField(max_length=300, null=True, blank=True)
    scientific_key = models.CharField(max_length=600, db_index=True, null=True, blank=True)
    chinese_name = models.CharField(max_length=300, blank=True)
    search_key = models.CharField(max_length=1000, db_index=True)
    source_snapshot = models.ForeignKey(SourceSnapshot, on_delete=models.PROTECT, related_name='current_species', null=True, blank=True)
    archive_directory = models.CharField(max_length=240, null=True, blank=True, unique=True)
    directory_key = models.CharField(max_length=480, null=True, blank=True, unique=True)

    class Meta:
        ordering = ['scientific_key', 'id']
        constraints = [models.CheckConstraint(name='species_source_identity', condition=(
            models.Q(source_kind='catalog', local_name__isnull=True, local_key__isnull=True,
                scientific_name__isnull=False, scientific_key__isnull=False, source_snapshot__isnull=False)
            & ~models.Q(scientific_name='') & ~models.Q(scientific_key='')
        ) | (
            models.Q(source_kind='local', local_name__isnull=False, local_key__isnull=False,
                scientific_name__isnull=True, scientific_key__isnull=True, source_snapshot__isnull=True)
            & ~models.Q(local_name='') & ~models.Q(local_key='')
        ))]

    @property
    def display_name(self):
        return self.local_name if self.source_kind == 'local' else (self.chinese_name or self.scientific_name)


class SpeciesConcept(models.Model):
    species = models.ForeignKey(Species, on_delete=models.PROTECT, related_name='concepts')
    snapshot = models.ForeignKey(SourceSnapshot, on_delete=models.PROTECT, related_name='concepts')
    external_id = models.CharField(max_length=160)
    evidence = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['snapshot', 'external_id'], name='catalog_concept_unique'),
                       models.UniqueConstraint(fields=['snapshot', 'species'], name='catalog_species_once')]


class Alias(models.Model):
    species = models.ForeignKey(Species, on_delete=models.PROTECT, related_name='aliases')
    name = models.CharField(max_length=300)
    language = models.CharField(max_length=16)
    search_key = models.CharField(max_length=600, db_index=True)
    source_snapshot = models.ForeignKey(SourceSnapshot, on_delete=models.PROTECT, related_name='aliases')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['species', 'name', 'language', 'source_snapshot'], name='catalog_alias_unique')]


class PhotoSpecies(models.Model):
    photo = models.ForeignKey(Photo, on_delete=models.PROTECT, related_name='confirmed_species')
    species = models.ForeignKey(Species, on_delete=models.PROTECT, related_name='confirmed_photos')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['photo', 'species'], name='photo_species_unique')]


class SpeciesGuideBinding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    species = models.OneToOneField(Species, on_delete=models.PROTECT, related_name='guide_binding')
    match_status = models.CharField(max_length=16, default='pending', choices=[
        ('pending', '资料待匹配'), ('ambiguous', '存在歧义'), ('matched', '已匹配')])
    source_concept = models.CharField(max_length=160, blank=True)
    mapping_evidence = models.JSONField(default=dict)
    current_snapshot = models.ForeignKey('BirdGuideSnapshot', null=True, blank=True,
        on_delete=models.PROTECT, related_name='current_for')
    request_generation = models.PositiveIntegerField(default=0)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.current_snapshot_id and self.current_snapshot.binding_id != self.pk:
            raise ValidationError('资料快照必须属于当前绑定。')


class BirdGuideSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    binding = models.ForeignKey(SpeciesGuideBinding, on_delete=models.PROTECT, related_name='snapshots')
    schema_version = models.PositiveIntegerField(default=1)
    payload = models.JSONField()
    content_sha256 = models.CharField(max_length=64)
    source_versions = models.JSONField()
    acquired_at = models.DateTimeField()
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['binding', 'content_sha256'], name='guide_snapshot_content_unique')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            from django.core.exceptions import ValidationError
            raise ValidationError('资料快照不可原地修改；请发布新版本。')
        return super().save(*args, **kwargs)


class GuideReferenceAsset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(BirdGuideSnapshot, on_delete=models.PROTECT, related_name='reference_assets')
    public_file_id = models.CharField(max_length=300)
    author = models.CharField(max_length=2000)
    license = models.CharField(max_length=300)
    license_url = models.URLField(max_length=1000)
    source_url = models.URLField(max_length=1000)
    change_note = models.TextField()
    sha256 = models.CharField(max_length=64)
    relative_key = models.CharField(max_length=300)
    mime = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['snapshot', 'public_file_id'], name='guide_asset_file_unique')]


class ArchiveOperation(models.Model):
    class Stage(models.TextChoices):
        QUEUED = 'queued'
        PROTECTING = 'protecting'
        PROTECTED = 'protected'
        CLAIM_INTENT = 'claim_intent'
        CLAIMED = 'claimed'
        COPYING = 'copying'
        VERIFIED = 'verified'
        PUBLISH_INTENT = 'publish_intent'
        PUBLISHED = 'published'
        REGISTERED = 'registered'
        TEMP_CLEANED = 'temp_cleaned'
        SOURCE_CLEANUP_INTENT = 'source_cleanup_intent'
        SOURCE_CLEANED = 'source_cleaned'
        COMPLETE = 'complete'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schema_version = models.PositiveIntegerField(default=1)
    photo = models.OneToOneField(Photo, on_delete=models.PROTECT, related_name='archive_operation')
    job = models.OneToOneField(Job, on_delete=models.PROTECT, related_name='archive_operation')
    request_id = models.UUIDField(unique=True)
    payload_digest = models.CharField(max_length=64)
    classification = models.CharField(max_length=32)
    species_ids_snapshot = models.JSONField(default=list)
    capture_snapshot = models.JSONField(default=dict)
    original_basename = models.CharField(max_length=1000)
    source_observation = models.ForeignKey(SourceObservation, on_delete=models.PROTECT, related_name='archive_operations')
    source_version = models.PositiveIntegerField()
    source_fingerprint = models.JSONField(default=dict)
    expected_sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    roots_snapshot = models.JSONField(default=dict)
    retry_context = models.JSONField(default=dict)
    claim_relative_path = models.CharField(max_length=1000, blank=True)
    claim_fingerprint = models.JSONField(default=dict)
    temporary_relative_path = models.CharField(max_length=1000, blank=True)
    temporary_fingerprint = models.JSONField(default=dict)
    target_relative_path = models.CharField(max_length=1000, blank=True)
    published_fingerprint = models.JSONField(default=dict)
    protected_fingerprint = models.JSONField(default=dict)
    stage = models.CharField(max_length=32, choices=Stage.choices, default=Stage.QUEUED)
    last_error = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ManagedOriginal(models.Model):
    photo = models.OneToOneField(Photo, on_delete=models.PROTECT, related_name='managed_original')
    operation = models.OneToOneField(ArchiveOperation, on_delete=models.PROTECT, related_name='managed_original')
    root_key = models.CharField(max_length=64, default='archive')
    relative_path = models.CharField(max_length=1000, unique=True)
    actual_basename = models.CharField(max_length=1000)
    fingerprint = models.JSONField(default=dict)
    sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    location_revision = models.PositiveIntegerField(default=0)
    last_management_operation = models.ForeignKey('ManagementOperation', null=True, blank=True,
        on_delete=models.PROTECT, related_name='registered_originals')


class RestoreSession(models.Model):
    """Append-only provenance for one isolated restore of a published backup set."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup_set_id = models.UUIDField()
    backup_manifest_sha256 = models.CharField(max_length=64)
    source_deployment_id = models.CharField(max_length=160, blank=True)
    source_roots = models.JSONField(default=dict)
    target_roots = models.JSONField(default=dict)
    manifest = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=[
        ('prepared', '已准备'), ('verified', '已核验'), ('activated', '已激活'),
        ('failed', '失败')], default='prepared')
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['backup_set_id', 'backup_manifest_sha256', 'target_roots'],
                                    name='restore_target_once'),
            models.UniqueConstraint(fields=['status'], condition=models.Q(status='activated'),
                                    name='one_activated_restore'),
        ]


class RestoreBinding(models.Model):
    """Frozen source fact and its verified target counterpart; never rewrites old journals."""
    session = models.ForeignKey(RestoreSession, on_delete=models.PROTECT, related_name='bindings')
    kind = models.CharField(max_length=32)
    object_id = models.CharField(max_length=1000)
    role = models.CharField(max_length=16)
    source_relative_path = models.CharField(max_length=1000)
    target_relative_path = models.CharField(max_length=1000)
    source_fingerprint = models.JSONField(default=dict)
    target_fingerprint = models.JSONField(default=dict)
    sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    hardlink_group = models.CharField(max_length=100)
    verification_reason = models.CharField(max_length=64, default='backup_manifest_verified')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['session', 'kind', 'object_id', 'role'],
                                                name='restore_binding_object_role_unique')]


class ManagementOperation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(unique=True)
    payload_digest = models.CharField(max_length=64)
    photo = models.ForeignKey(Photo, on_delete=models.PROTECT, related_name='management_operations')
    kind = models.CharField(max_length=16)
    generation = models.PositiveIntegerField()
    source_location_revision = models.PositiveIntegerField()
    source_snapshot = models.JSONField(default=dict)
    target_snapshot = models.JSONField(default=dict)
    root_identity = models.JSONField(default=dict)
    target_relative_path = models.CharField(max_length=1000, blank=True)
    target_fingerprint = models.JSONField(default=dict)
    collision_counter = models.PositiveIntegerField(default=0)
    stage = models.CharField(max_length=16, default='accepted')
    status = models.CharField(max_length=16, default='pending')
    error_code = models.CharField(max_length=64, blank=True)
    retry_context = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['photo'],
            condition=models.Q(status__in=['pending', 'running', 'failed', 'blocked']),
            name='one_unresolved_management')]
        ordering = ['created_at', 'id']


class MetadataReceipt(models.Model):
    request_id = models.UUIDField(primary_key=True)
    photo = models.ForeignKey(Photo, on_delete=models.PROTECT, related_name='metadata_receipts')
    payload_digest = models.CharField(max_length=64)
    result = models.JSONField(default=dict)


class ManagementPreview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=32)
    selection_ids = models.JSONField()
    frozen_rows = models.JSONField()
    revision = models.PositiveIntegerField(default=1)
    selection_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class ManagementResult(models.Model):
    request_id = models.UUIDField(primary_key=True)
    preview = models.ForeignKey(ManagementPreview, on_delete=models.PROTECT, related_name='results')
    photo = models.ForeignKey(Photo, on_delete=models.PROTECT, related_name='management_results')
    payload_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16)
    result = models.JSONField(default=dict)


class ArchiveCompletion(models.Model):
    photo = models.OneToOneField(Photo, on_delete=models.PROTECT, related_name='archive_completion')
    operation = models.OneToOneField(ArchiveOperation, on_delete=models.PROTECT, related_name='completion')
    completed_at = models.DateTimeField(auto_now_add=True)


class ModelArtifact(models.Model):
    """Immutable package identity; selecting a package does not requeue old photos."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manifest_digest = models.CharField(max_length=64, unique=True)
    provider = models.CharField(max_length=160)
    version = models.CharField(max_length=160)
    revision = models.CharField(max_length=160)
    manifest = models.JSONField(default=dict)
    selection_state = models.CharField(max_length=16, choices=[
        ('candidate', '评测候选'), ('selected', '已选择'), ('retired', '已停用')], default='candidate')
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['is_current'], condition=models.Q(is_current=True),
                name='one_current_recognition_model'),
            models.CheckConstraint(condition=models.Q(is_current=False) | models.Q(selection_state='selected'),
                name='current_model_is_selected')]

    def save(self, *args, **kwargs):
        fields = ('manifest_digest', 'provider', 'version', 'revision', 'manifest')
        old = type(self).objects.filter(pk=self.pk).values(*fields).first() if not self._state.adding else None
        if old and any(old[field] != getattr(self, field) for field in fields):
            raise ValueError('模型包身份不可改写')
        super().save(*args, **kwargs)


class RecognitionState(models.Model):
    photo = models.OneToOneField(Photo, on_delete=models.PROTECT, related_name='recognition_state')
    requested_generation = models.PositiveIntegerField(default=0)
    active_run = models.ForeignKey('RecognitionRun', null=True, blank=True,
        on_delete=models.PROTECT, related_name='active_for_states')


class RecognitionRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(unique=True)
    payload_digest = models.CharField(max_length=64)
    photo = models.ForeignKey(Photo, on_delete=models.PROTECT, related_name='recognition_runs')
    source_observation = models.ForeignKey(SourceObservation, on_delete=models.PROTECT,
        related_name='recognition_runs')
    source_version = models.PositiveIntegerField()
    source_fingerprint = models.JSONField(default=dict)
    photo_sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    metadata_version = models.PositiveIntegerField()
    artifact = models.ForeignKey(ModelArtifact, null=True, blank=True, on_delete=models.PROTECT,
        related_name='recognition_runs')
    generation = models.PositiveIntegerField()
    reason = models.CharField(max_length=32)
    state = models.CharField(max_length=32, default='queued')
    attempts = models.PositiveIntegerField(default=0)
    cycle_attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    queue_age = models.DateTimeField(default=timezone.now)
    safe_error = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['photo', 'generation'], name='recognition_generation_unique')]
        ordering = ['queue_age', 'created_at', 'id']


class RecognitionCandidate(models.Model):
    run = models.ForeignKey(RecognitionRun, on_delete=models.PROTECT, related_name='candidates')
    rank = models.PositiveSmallIntegerField()
    species = models.ForeignKey(Species, null=True, blank=True, on_delete=models.PROTECT,
        related_name='recognition_candidates')
    raw_label = models.CharField(max_length=300)
    score = models.FloatField()
    mapping_status = models.CharField(max_length=32, choices=[
        ('mapped', '已映射'), ('unmapped', '未映射')])
    mapping_evidence = models.CharField(max_length=1000, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'rank'], name='recognition_candidate_rank_unique'),
            models.CheckConstraint(condition=models.Q(rank__gte=1, rank__lte=3), name='recognition_rank_top_three')]
        ordering = ['rank']


class BatchReviewPreview(models.Model):
    """One immutable revision; a newer row in the series invalidates old tokens."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    series_id = models.UUIDField(default=uuid.uuid4, editable=False)
    mode = models.CharField(max_length=16)
    revision = models.PositiveIntegerField(default=1)
    selection_ids = models.JSONField()
    selection_digest = models.CharField(max_length=64)
    frozen_rows = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['series_id', 'revision'], name='batch_revision_unique')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('预览修订不可改写')
        super().save(*args, **kwargs)


class BatchReviewResult(models.Model):
    preview = models.ForeignKey(BatchReviewPreview, on_delete=models.PROTECT, related_name='results')
    revision = models.PositiveIntegerField()
    photo_id = models.UUIDField()
    request_id = models.UUIDField(unique=True)
    payload_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=[('accepted', '已受理'), ('skipped', '已跳过'), ('failed', '失败')])
    reason_code = models.CharField(max_length=64)
    operation = models.ForeignKey(ArchiveOperation, null=True, blank=True,
        on_delete=models.PROTECT, related_name='batch_results')
    completed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['preview', 'revision', 'photo_id'], name='batch_result_unique')]
