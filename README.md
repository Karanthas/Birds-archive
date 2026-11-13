# Birds Archive

Birds Archive is a self-hosted photo library for bird photography. Drop edited JPEGs into an inbox, review the birds in each photo, and let the background worker file the originals by species and year. Browse the results as a photo gallery or a field guide built around the species you have photographed.

The application interface is in Simplified Chinese. There is no application login. Anyone who can reach an enabled entry point can use the library, subject to the access controls configured for that entry point.

## What you can do

- **Import and review.** Scan an inbox recursively, group duplicate JPEGs by content, and review capture time and location. Enter a local bird name, choose one or more species from an imported catalog, or explicitly mark a photo as unidentified. Batch review lets you confirm each photo's own candidates or apply a shared species set after reviewing the selection.
- **Archive and download.** Human confirmation queues the archive operation. The worker preserves the original JPEG bytes and resolves filename collisions without overwriting an existing file. A photo with several species still has one managed original. Downloads use the actual archived filename.
- **Browse and organize.** The gallery groups archived photos by month and supports filters for species, date, location, favorites, and ratings. Edit notes or correct an archived photo's capture time or species. The trash view supports restoring an original with its species and personal metadata intact.
- **Look up birds.** The guide groups photos by confirmed species. Its information pages can query Wikidata and Wikipedia from the browser for public bird names or identifiers. JPEGs, notes, and locations stay out of those requests.
- **Use optional local recognition.** A separately configured BioCLIP2 CPU worker suggests ranked candidates. The scores express ranking, and a person makes the final identification. Manual review remains available with recognition disabled.

## Quick start with a synthetic JPEG

Run these commands from the repository root on macOS or Linux with CPython 3.12. The base dependency lock contains hashes for the reviewed macOS and Linux distributions. The example uses a fresh temporary directory and loopback HTTP. It exercises manual archiving without a model package or species catalog.

### 1. Install the base dependencies

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip --isolated install --require-hashes -r requirements.lock
.venv/bin/python -m pip check
```

Python supplies the SQLite library through its `sqlite3` module. Check the version and source identifier from the same interpreter that will run Django.

```sh
.venv/bin/python -c 'import sqlite3; print(sqlite3.connect(":memory:").execute("SELECT sqlite_version(), sqlite_source_id()").fetchone())'
```

The validated deployment runtime uses a specific SQLite 3.53.1 build. `checkruntime` verifies its source ID and checks WAL, full synchronization, foreign keys, and storage roots. See the [local runtime notes](docs/local-run.md) for the exact identifier and diagnostics.

### 2. Create the demo and start the web app

```sh
BIRDS_DEMO_ROOT="$(mktemp -d)"
BIRDS_DEMO_ROOT="$(cd "$BIRDS_DEMO_ROOT" && pwd -P)"
mkdir -m 700 "$BIRDS_DEMO_ROOT/inbox" "$BIRDS_DEMO_ROOT/data" \
  "$BIRDS_DEMO_ROOT/archive" "$BIRDS_DEMO_ROOT/claim"

export BIRDS_INBOX_ROOT="$BIRDS_DEMO_ROOT/inbox"
export BIRDS_DATA_ROOT="$BIRDS_DEMO_ROOT/data"
export BIRDS_ARCHIVE_ROOT="$BIRDS_DEMO_ROOT/archive"
export BIRDS_CLAIM_ROOT="$BIRDS_DEMO_ROOT/claim"
export BIRDS_SECRET_KEY="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export BIRDS_LOCAL_HTTP=1
export BIRDS_PROXY_ONLY=0
export BIRDS_RECOGNITION_ENABLED=0
export BIRDS_HANDOFF_MODE=published
export BIRDS_PUBLISHED_CONTRACT=1

.venv/bin/python manage.py migrate --noinput
.venv/bin/python - <<'PY'
import os
from pathlib import Path

from PIL import Image

inbox = Path(os.environ["BIRDS_INBOX_ROOT"])
temporary = inbox / "demo.jpg.partial"
Image.new("RGB", (320, 240), (40, 120, 80)).save(temporary, format="JPEG")
temporary.rename(inbox / "demo.jpg")
PY
.venv/bin/python manage.py runworker --once
.venv/bin/python manage.py runserver 127.0.0.1:8000 --noreload
```

The demo writes a temporary file, closes it, and then publishes the final JPEG name on the same filesystem. For everyday importing, use the [JPEG handoff instructions](docs/input-handoff.md) to choose the appropriate mode for your producer.

### 3. Confirm and find the archived photo

1. Open `http://127.0.0.1:8000/review/` and select the demo photo.
2. Click **无法确定并归档** (*Archive as unidentified*). If you have added a local bird name, you can instead select it and click **确认并归档** (*Confirm and archive*).
3. Stop the development server with Ctrl-C. Run the worker once more, then restart the server in the same shell so it keeps the same paths and secret.

   ```sh
   .venv/bin/python manage.py runworker --once
   .venv/bin/python manage.py runserver 127.0.0.1:8000 --noreload
   ```

4. Open `http://127.0.0.1:8000/`. The gallery shows the archived photo when file and database registration are complete. Open its detail page and use **下载原图** (*Download original*). If the photo is still queued, check `/tasks/` and run the worker again.

For ongoing local use, run `manage.py runworker` without `--once` in a separate terminal with the same environment. Keep one file worker per data root.

| Page | What it shows |
| --- | --- |
| `/` | Archived gallery |
| `/review/` | Inbox and pending reviews |
| `/guide/` | Guide to photographed species |
| `/species/` | Species catalog |
| `/tasks/` | Background jobs and retry status |
| `/?view=trash` | Trashed photos and restore actions |

## Storage and file ownership

| Root | Role |
| --- | --- |
| `BIRDS_INBOX_ROOT` | Incoming JPEGs. The import scan reads files here and tracks their content and source versions. |
| `BIRDS_DATA_ROOT` | SQLite database, private previews, deployment records, and worker state. Use a supported local filesystem for SQLite. |
| `BIRDS_ARCHIVE_ROOT` | Managed originals, operation files, and trash. Leave this directory under the file worker's control. |
| `BIRDS_CLAIM_ROOT` | Private staging for files the worker has claimed. The inbox and claim roots must support a rename within the same mount. |

Use separate, non-overlapping roots. The ordinary `copy_compatible` mode waits for a stable observation before importing and leaves files in the inbox. Archiving requires the `published` handoff contract. A producer must close all write handles before publishing the final name and leave the published file unchanged. Keep the database and operation files together after an interruption so the worker can reconcile its recorded progress.

## fnOS deployment

The Compose stack uses a web service and one worker supervisor. The supplied configuration disables model inference and supports manual archiving. NGINX provides separate LAN and remote entry points. The web service runs on internal networks.

1. Prepare four dedicated roots, a non-root deployment identity, and a restricted environment file based on [`deploy/env.example`](deploy/env.example). For Compose, bind the shared host parent at `/roots` with the children `inbox`, `data`, `archive`, and `claim`. Follow the [fnOS guide](docs/fnos.md) for ownership markers, filesystem requirements, and explicit initialization.
2. Provide the reviewed base image and offline Python dependency tree separately. The Docker build uses `materials/python/` and `materials/python-manifest.json`. Follow the documented offline build procedure before starting Compose.
3. Start the initialized web and worker services. Configure each proxy with its own hostname, address, and TLS materials before enabling that entry point.
4. For LAN access, devices use the LAN HTTPS address with normal server certificate verification. For remote access, the deployment guide uses a private Cloudflare Tunnel route and a client certificate per approved device. Keep the application backend inside the Compose networks. See the [remote access guide](docs/remote-access.md), [network policy](deploy/network-policy.md), and [Tunnel configuration example](deploy/cloudflared.yml.example).

fnOS initialization, archive downloads, dual HTTPS, certificate revocation, and isolated restore have been exercised with test deployments. The maintenance sequence has been exercised locally with a real Django migration and controlled Compose responses. The final target-environment checks include Windows delivery, a live Tunnel and four remote clients, a real image upgrade, mixed in-flight recovery, and storage durability. See the [deployment acceptance checklist](docs/fnos-acceptance.md).

## Maintenance, backup, and restore

On the Docker host, copy the [environment example](docs/deployment/06-environment.example.json) to an ignored file such as `docs/deployment/06-environment.json` and fill in the actual configuration paths. For an upgrade, provide both environment files. The new one changes only the application image reference.

```sh
python3 deploy/maintain.py check --config docs/deployment/06-environment.json
python3 deploy/maintain.py status --config docs/deployment/06-environment.json
python3 deploy/maintain.py restart --config docs/deployment/06-environment.json
python3 deploy/maintain.py upgrade --config docs/deployment/06-environment.json
```

Run `restart` or `upgrade` as appropriate for the maintenance window. An upgrade drains the worker, creates and independently verifies a backup, confirms the old services have stopped, and runs one migration through the maintenance entry point. It resumes service after the new processes start and the database baseline matches. If a step fails, retain the reported stage, session ID, and any backup set ID for diagnosis.

A backup set contains a consistent SQLite snapshot, stored files and hard-link relationships, and any required offline model materials. The final `COMPLETE.json` marks a published set. Restore a verified set into **new isolated roots** using the `prepare`, `verify`, and `activate` stages. After activation, check photo counts, original SHA-256 values, species associations, trash, and pending tasks. Required dependencies and known task stages determine which operations can resume. Restoring a later backup made from an already restored instance still needs additional support.

Keep deployment secrets and Tunnel credentials in restricted configuration and supply them separately during recovery. The [backup and restore guide](docs/backup-restore.md) provides the exact Compose commands, maintenance-session handling, and readback steps.

## Catalog and optional recognition

A fresh database supports manual review. Import a verified local CSV and its manifest to populate the searchable catalog. The dry run checks the package and reports changes before importing.

```sh
.venv/bin/python manage.py importcatalog \
  --file /path/to/catalog.csv --manifest /path/to/catalog-manifest.json --dry-run
```

After reviewing the result, repeat the command without `--dry-run` in the local demo. On a deployed instance, use the documented `offline` maintenance entry point. The [source feasibility notes](docs/source-feasibility.md) describe catalog and guide sources.

Recognition requires a separate CPU inference environment, a verified offline BioCLIP2 package, and explicit memory and timeout settings. Model weights and the original evaluation materials live outside the source checkout. Use the [recognition runtime guide](docs/recognition/runtime.md) for setup and the [model selection notes](docs/recognition/selection.md) for the scope of the evaluation.

## Development and tests

The application uses Django, SQLite, Pillow, server-rendered templates, and HTMX. Gunicorn and WhiteNoise serve the deployed application and its static assets. Background jobs are recorded in SQLite, and one file worker executes the archive and management operations.

```text
archive/
  management/commands/   Django commands for workers, data, and maintenance
  migrations/            SQLite schema history
  services/              Import, archive, recognition, backup, and restore logic
  static/ and templates/ Web assets and server-rendered pages
  tests/                 Isolated application and operational checks
config/                  Django settings, URL configuration, and WSGI entry point
deploy/                  Container entry point, proxy templates, and maintenance tools
docs/                    Deployment guides and data-source notes
```

Use `config.settings_test` for automated tests. It creates temporary storage and an isolated database. Run tests in a separate shell if you have set a shared `BIRDS_TEST_ROOT` for a demo.

```sh
.venv/bin/python manage.py check --settings=config.settings_test
.venv/bin/python manage.py test \
  archive.tests.test_walkthrough \
  archive.tests.test_deploy_lifecycle.DeployLifecycleTests \
  archive.tests.test_restore_archive.ArchiveResumeTests \
  archive.tests.test_restore_management.ManagementResumeTests \
  archive.tests.test_restore_recognition.SourceResumeTests \
  --settings=config.settings_test --noinput
```

The tests above use synthetic inputs and a controlled recognition provider. The full collection also contains live TLS checks that require a configured target and historical recognition checks that require the original local evidence. See the [local test notes](docs/local-run.md) for the repository's test settings and commands.

Keep private photos, credentials, databases, model weights, backup sets, and raw deployment evidence outside Git. The [ignore rules](.gitignore) preserve source files, templates, and the bundled field-guide assets.
