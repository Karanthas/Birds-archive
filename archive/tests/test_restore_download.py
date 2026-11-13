import os
import shutil
import uuid

from archive.models import ArchiveOperation, PhotoSpecies, RestoreBinding, RestoreSession
from archive.services.management import accept_confirmed_trash, trash_confirmation
from archive.services.paths import fingerprint
from archive.services.restore import _root_fingerprint
from archive.tests.test_download import DownloadFixture


class CompletedRestoreTests(DownloadFixture):
    def test_archived_and_trashed_originals_use_new_inode_at_same_container_path(self):
        archived, _, first = self.completed('bird.jpg', 'red')
        trashed, _, second = self.completed('other.jpg', 'blue')
        trashed.refresh_from_db()
        accept_confirmed_trash(trashed.pk, trash_confirmation(trashed))
        self.run_worker()
        trashed.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(trashed.trashed_at)
        photo_ids = [archived.pk, trashed.pk]
        history = list(ArchiveOperation.objects.filter(photo_id__in=photo_ids).order_by('id').values())
        species = list(PhotoSpecies.objects.filter(photo_id__in=photo_ids).order_by('photo_id', 'species_id').values())
        former = self.archive.with_name('former-archive')
        self.archive.rename(former)
        self.archive.mkdir()
        session = RestoreSession.objects.create(backup_set_id=uuid.uuid4(), backup_manifest_sha256='a' * 64,
            status='activated', target_roots={'archive': str(self.archive)})
        RestoreBinding.objects.create(session=session, kind='root', object_id='archive', role='archive',
            source_relative_path='.', target_relative_path='.', source_fingerprint=_root_fingerprint(former),
            target_fingerprint=_root_fingerprint(self.archive), sha256='', byte_size=0, hardlink_group='archive-root')

        for photo, managed in ((archived, first), (trashed, second)):
            relative = managed.relative_path
            source, target = former / relative, self.archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
            fields = dict(session=session, role='archive', source_relative_path=relative,
                          target_relative_path=relative, source_fingerprint=managed.fingerprint,
                          target_fingerprint=fingerprint(target.stat()), sha256=managed.sha256,
                          byte_size=managed.byte_size, hardlink_group=str(photo.pk))
            RestoreBinding.objects.create(kind='path', object_id='archive:' + relative, **fields)
            RestoreBinding.objects.create(kind='managed_original', object_id=str(photo.pk), **fields)
            self.assert_download(photo, target, managed)
            self.assertEqual(source.read_bytes(), target.read_bytes())

        self.assertEqual(list(ArchiveOperation.objects.filter(photo_id__in=photo_ids).order_by('id').values()), history)
        self.assertEqual(list(PhotoSpecies.objects.filter(photo_id__in=photo_ids).order_by('photo_id', 'species_id').values()), species)
        target = self.archive / first.relative_path
        foreign = target.with_name('same-bytes-other-inode.jpg')
        foreign.write_bytes(target.read_bytes())
        os.replace(foreign, target)
        self.assertEqual(self.download(archived).status_code, 409)
