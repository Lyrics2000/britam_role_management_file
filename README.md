# Britam Group Role Library

The role library, previously a single 400 KB HTML file, now runs as a Django
application backed by SQLite. The page looks and behaves exactly as before —
Browse, Compare, My Career Path — but the 290 roles live in a database that HR
can edit, and new roles can be added through the site.

On `docker compose up`, the container migrates the database, loads every role
out of `Britam_Role_Library.html` into SQLite, and serves the site from the
database from that point on.

---

## Quick start

Prerequisites: Docker Engine 24+ with the Compose plugin. On the droplet:

```bash
# 1. The compose file joins an existing shared network. Create it once:
docker network create app-network            # skip if it already exists

# 2. Configure. Never commit the resulting .env file.
cp .env.example .env

# 3. Generate a secret key and paste it into .env as DJANGO_SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# 4. Edit .env:
#      DJANGO_SECRET_KEY=<the value from step 3>
#      DJANGO_ALLOWED_HOSTS=<your hostname or droplet IP>,localhost,127.0.0.1
#      DJANGO_SUPERUSER_USERNAME=hradmin
#      DJANGO_SUPERUSER_PASSWORD=<a passphrase of 12+ characters>
nano .env

# 5. Build and start
docker compose up -d --build

# 6. Watch the first boot seed the database
docker compose logs -f web
```

Expected output on the first boot:

```
entrypoint: applying migrations
entrypoint: seeding from HTML (mode=once)
seed_roles: parsed 290 role(s) from /app/Britam_Role_Library.html
  + business unit: Foundation & IR
  ...
seed_roles: created=290 updated=0 unchanged=0 failed=0 pruned=0 in 1011ms
entrypoint: checking for an editor account
bootstrap_admin: created superuser 'hradmin'.
entrypoint: collecting static files
entrypoint: starting gunicorn on 0.0.0.0:8000
```

The site is on **http://your-host:6519** — the same port as before.

| URL | What it is | Who can reach it |
| --- | --- | --- |
| `/` | The role library | everyone |
| `/accounts/login/` | Editor sign in | everyone |
| `/admin/` | Django admin — bulk edits, CSV export, audit trail | staff only |
| `/api/roles/` | JSON API | read: everyone · write: staff |
| `/api/meta/` | Filter vocabularies and counts | everyone |
| `/healthz` | Liveness probe | everyone |
| `/readyz` | Readiness probe (checks the database) | everyone |

---

## Editing roles

Sign in with the account from `.env`, then either:

**The Manage tab** (in the site header once you are signed in) — a branded form
for day-to-day work: add a role, edit a role, hide a role from the public site,
delete a role. Typing a business unit that does not exist yet creates it.

**`/admin/`** — for bulk work: filter by BU/band/level, search across all
fields, select many rows and export them to CSV, or bulk hide/show.

Every change is recorded in **Role revisions** (`/admin/roles/rolerevision/`):
who changed what, from what value to what value, and when. The trail is
read-only and survives deletion of the role itself.

### Adding more editors

```bash
docker compose exec web python manage.py createsuperuser
```

Or, for an editor who should not have full admin rights, create the user in
`/admin/` and tick **Staff status** only.

---

## How seeding works

`SEED_MODE` in `.env` controls what happens to the HTML file on each boot:

| Mode | Behaviour | Use when |
| --- | --- | --- |
| `once` *(default)* | Loads the HTML only if the database has no roles yet. | HR edits in the app are the source of truth from now on. |
| `sync` | Re-reads the HTML on every boot and updates changed rows. | The HTML file remains the source of truth and is updated by hand. |
| `off` | Never seeds. | The database is restored from a backup. |

Seeding is idempotent — the row's content is fingerprinted (SHA-256), so a
re-run that finds nothing changed writes nothing and leaves no audit noise.

Manual control:

```bash
# See what would change without writing anything
docker compose exec web python manage.py seed_roles --dry-run

# Re-read the HTML and apply changes
docker compose exec web python manage.py seed_roles

# Also hide roles that have been removed from the HTML
docker compose exec web python manage.py seed_roles --prune

# Rewrite every row regardless of the fingerprint
docker compose exec web python manage.py seed_roles --force
```

`--prune` **deactivates** rather than deletes, and never touches roles that were
created by hand in the app.

### Updating the HTML file

If HR hands over a new `Britam_Role_Library.html`:

```bash
cp /path/to/new/Britam_Role_Library.html .
docker compose up -d --build
docker compose exec web python manage.py seed_roles --dry-run   # review
docker compose exec web python manage.py seed_roles             # apply
```

If the new file also changes the *design*, regenerate the template as well:

```bash
python3 build_template.py      # rewrites roles/templates/roles/role_library.html
docker compose up -d --build
```

---

## Exporting

```bash
# CSV, for a spreadsheet
docker compose exec web python manage.py export_roles --format csv > roles.csv

# JSON
docker compose exec web python manage.py export_roles --format json > roles.json

# A legacy-shaped `const ROLES = [...]` array, for a static fallback page
docker compose exec web python manage.py export_roles --format js > roles.js
```

---

## Backups

The database is a single file on the named volume `britam_role_data`. Back it
up with SQLite's own `.backup`, which is safe while the app is running (a plain
`cp` of a WAL-mode database can capture a torn state):

```bash
# Take a backup
docker compose exec web sqlite3 /data/db.sqlite3 ".backup '/data/backup.sqlite3'"
docker compose cp web:/data/backup.sqlite3 ./britam-roles-$(date +%F).sqlite3

# Restore
docker compose down
docker compose cp ./britam-roles-2026-08-17.sqlite3 web:/data/db.sqlite3
docker compose up -d
```

A nightly cron on the host:

```cron
0 2 * * * cd /path/to/britam_role_management_file && docker compose exec -T web sqlite3 /data/db.sqlite3 ".backup '/data/nightly.sqlite3'" && docker compose cp web:/data/nightly.sqlite3 /backups/britam-roles-$(date +\%F).sqlite3
```

---

## The AI Assistant

The old page had `const AI_KEY = ''` waiting for an Anthropic key to be pasted
into the HTML. **That was not safe to use**: the key would be visible to every
visitor in view-source, could be copied and spent by anyone, and could not be
rate limited.

The key now lives only in the container's environment. The browser posts a
question to `/api/ai/`; the server builds the role context from the database,
calls Anthropic, and returns only the answer text.

To enable it, set `ANTHROPIC_API_KEY` in `.env` and restart:

```bash
docker compose up -d
```

Guard rails, all configurable in `.env`:

* per-IP burst limit (`THROTTLE_AI`, default 10/min) plus an nginx limit
* a global daily ceiling (`AI_DAILY_BUDGET_REQUESTS`, default 500 successful
  calls per rolling 24 h), counted in the database so it holds across workers
* every call is logged in `/admin/roles/airequestlog/` with latency and outcome

Leave `ANTHROPIC_API_KEY` blank and the tab shows a "not configured" message;
everything else works.

---

## Operations

```bash
# Status and logs
docker compose ps
docker compose logs -f web
docker compose logs -f nginx

# Restart / stop
docker compose restart web
docker compose down                 # keeps the data volume
docker compose down -v              # DELETES the database — be sure

# Shell access
docker compose exec web sh
docker compose exec web python manage.py shell

# Direct database access
docker compose exec web sqlite3 /data/db.sqlite3 "SELECT COUNT(*) FROM roles_role;"

# Health
curl http://localhost:6519/healthz     # {"status":"ok",...}
curl http://localhost:6519/readyz      # also proves the DB answers and is seeded
```

Logs are JSON, one object per line, each carrying a `request_id` that nginx
sets and the response returns in the `X-Request-ID` header. To trace a user's
report of an error:

```bash
docker compose logs web | grep '"request_id": "3f2a91c0d4e15b78"'
```

### Running the tests

```bash
docker compose exec web python -m pytest              # full suite with coverage
docker compose exec web python -m pytest -m integration
docker compose exec web python -m pytest roles/tests/test_legacy_parser.py -v
```

---

## Enabling HTTPS

Until TLS terminates in front of this stack, `docker compose exec web python
manage.py check --deploy` reports five warnings (HSTS, SSL redirect, secure
cookies). They are expected and clear once you put a certificate in front and
set:

```ini
DJANGO_BEHIND_TLS_PROXY=1
DJANGO_SECURE_SSL_REDIRECT=1
DJANGO_CSRF_TRUSTED_ORIGINS=https://roles.example.com
```

Then `docker compose up -d`. Turning `DJANGO_BEHIND_TLS_PROXY=1` on *without*
real HTTPS makes cookies secure-only and sign-in will appear to fail silently.

---

## Project layout

```
britam_role_management_file/
├── docker-compose.yml         web (gunicorn) + nginx, published on :6519
├── Dockerfile                 multi-stage python:3.12-slim, non-root
├── entrypoint.sh              migrate -> seed -> superuser -> static -> serve
├── nginx.conf                 reverse proxy, rate limits, security headers
├── .env.example               every setting, documented
├── requirements.txt           pinned to exact versions
├── build_template.py          regenerates the template from the legacy HTML
├── manage.py
├── Britam_Role_Library.html   the original file — the seed source
│
├── config/
│   ├── settings.py            env-driven; refuses unsafe production values
│   ├── urls.py                page, api, auth, admin, probes
│   ├── wsgi.py / asgi.py
│
└── roles/
    ├── models.py              BusinessUnit, Role, RoleRevision, AIRequestLog
    ├── legacy_html.py         parser for the `const ROLES = [...]` literal
    ├── serializers.py         validation and the legacy field aliases
    ├── views.py               page, CRUD API, meta, AI proxy, probes
    ├── filters.py             ?q= &bu= &band= &level=
    ├── permissions.py         public read, staff write
    ├── pagination.py
    ├── exceptions.py          the {"error":{code,message,details}} envelope
    ├── middleware.py          request ids and access logging
    ├── logging_utils.py       JSON formatter
    ├── signals.py             the audit trail
    ├── admin.py               back office + CSV export
    ├── apps.py                SQLite WAL / busy_timeout pragmas
    ├── management/commands/
    │   ├── seed_roles.py      HTML -> SQLite (idempotent, retrying)
    │   ├── bootstrap_admin.py first superuser from env vars
    │   └── export_roles.py    SQLite -> CSV / JSON / JS
    ├── migrations/0001_initial.py
    ├── templates/roles/       role_library.html (generated), login.html
    ├── static/roles/app.js    the whole front end
    └── tests/                 146 tests, 90% coverage
```

---

## Architecture decisions

The reasoning behind each non-obvious choice is recorded as an ADR comment at
the point in the code where it applies:

| ADR | Where | Decision |
| --- | --- | --- |
| 001 | `requirements.txt` | Exact version pins, not ranges |
| 002 | `config/settings.py` | One env-driven settings module |
| 003 | `config/settings.py` | SQLite, made safe with WAL + busy_timeout |
| 004 | `config/settings.py` | WhiteNoise instead of a shared static volume |
| 005 | `config/settings.py` | JSON logs to stdout only |
| 006 | `roles/models.py` | `icontains` search, not SQLite FTS5 |
| 007 | `roles/models.py` | Hand-rolled revisions, not django-reversion |
| 008 | `roles/views.py` | Server-side AI proxy; the key never reaches the browser |
| 009 | `roles/legacy_html.py` | A real parser, not regexes or a node `eval` |
| 010 | `roles/pagination.py` | Large default page so filtering stays client-side |
| 011 | `entrypoint.sh` | gunicorn sync workers with threads |
| 012 | `Dockerfile` | Multi-stage slim image, non-root user |
| 013 | `roles/serializers.py` | Suppress DRF's auto `UniqueTogetherValidator` |

---

## What changed from the previous deployment

| Before | Now |
| --- | --- |
| nginx serving one static HTML file | nginx → gunicorn → Django |
| 290 roles hard-coded in a `const ROLES = [...]` array | 290 rows in SQLite on a persistent volume |
| Editing meant hand-editing a 400 KB HTML file | Manage tab and `/admin/`, with an audit trail |
| Anthropic API key would be exposed in page source | Key server-side only, rate limited and budgeted |
| Role text interpolated into `innerHTML` unescaped | All values escaped — HR-entered content cannot inject script |
| Roles referenced by array index | Roles referenced by database id (stable across inserts) |
| `alert()` for validation messages | Inline banners |
| 64 MB memory limit | 512 MB limit, 192 MB reservation |
| No health checks beyond "nginx is up" | `/healthz` (liveness) and `/readyz` (database + seed) |

The visual design, the CSS, the Browse/Compare/Career Path behaviour and the
`:6519` port are unchanged.

---

## Troubleshooting

**`DJANGO_SECRET_KEY is not set`** — `.env` is missing or in the wrong
directory. It must sit next to `docker-compose.yml`.

**`data directory /data is not writable`** — the volume was created root-owned
by an older image. `docker compose down && docker volume rm britam_role_data &&
docker compose up -d`, then restore from a backup.

**The site loads but shows "The role library could not be loaded"** — the API
call failed. Check `docker compose logs web` and `curl localhost:6519/readyz`.
If `readyz` reports `"seed": "empty"`, run `docker compose exec web python
manage.py seed_roles`.

**`network app-network declared as external, but could not be found`** — run
`docker network create app-network`.

**Sign-in returns to the login page with no error** — `DJANGO_BEHIND_TLS_PROXY=1`
without real HTTPS in front. Set it back to `0`.

**429 responses under normal use** — the rate limits assume a handful of
concurrent users. Raise `THROTTLE_ROLES_READ` in `.env` and the `general` zone
rate in `nginx.conf`.
