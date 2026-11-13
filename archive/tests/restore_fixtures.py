"""Run the actual backup and restore services with an isolated Django test database."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.db import connection
from django.test import override_settings

from archive.services import backup, maintenance, restore


@contextmanager
def restored_backup(case, *, activate=True):
    roles = {'inbox': 'inbox', 'data': 'data', 'archive': 'archive', 'claim': 'claims'}
    originals = {role: getattr(case, attr) for role, attr in roles.items()}
    original_database = connection.settings_dict['NAME']
    with tempfile.TemporaryDirectory(prefix='birds-resume-') as directory:
        work = Path(directory).resolve()
        destination = work / 'backup'
        destination.mkdir()
        registry = originals['data'] / 'deployment-v1.json'
        if not registry.exists():
            registry.write_text(json.dumps({'deployment_id': 'resume-test-source'}))
        requested = maintenance.request('resume-fixture')
        session_id = requested['session_id']
        maintenance._mutate_state(session_id, lambda state: {
            **state, 'supervisor_ack': {'session_id': session_id, 'status': 'drained'},
        })
        try:
            with patch.object(backup, 'validate_deployment'):
                set_path = backup.create(session_id, destination)['set']
        finally:
            maintenance.resume(session_id)
        targets = {role: work / ('target-' + role) for role in roles}
        for role, root in targets.items():
            root.mkdir()
            (root / restore.OWNER).write_text(json.dumps({'deployment_id': 'resume-test-target', 'role': role}))
        prepared = restore.prepare_restore(set_path, targets)
        connection.close()
        connection.settings_dict['NAME'] = targets['data'] / 'archive.sqlite3'
        with override_settings(**{'BIRDS_' + role.upper() + '_ROOT': str(path)
                                  for role, path in targets.items()}):
            try:
                for role, attr in roles.items():
                    setattr(case, attr, targets[role])
                session = restore.register_prepared_restore(prepared)
                if activate:
                    restore.verify_restore(session.pk)
                    session = restore.activate_restore(session.pk)
                yield session
            finally:
                for role, attr in roles.items():
                    setattr(case, attr, originals[role])
                connection.close()
                connection.settings_dict['NAME'] = original_database
