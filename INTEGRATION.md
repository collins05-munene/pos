# Backups app: integration steps

1. Copy the `backups/` folder into your project root (next to `tenants/`, `users/`).

2. settings.py
```python
INSTALLED_APPS += ["backups"]

BACKUP_ROOT = BASE_DIR / "private_backups"          # NOT under MEDIA_ROOT / static
BACKUP_ENCRYPTION_KEY = env("BACKUP_ENCRYPTION_KEY", default="")

STORAGES = {
    # ...keep your existing "default" and "staticfiles"...
    "backups": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": BACKUP_ROOT},
    },
}
```
Generate the key once and store it OUTSIDE the server (password manager):
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
(`pip install cryptography`). Lose the key = encrypted backups are unreadable.

3. Root urls.py: `path("backups/", include("backups.urls")),`

4. `python manage.py makemigrations backups && python manage.py migrate`

5. Cron (hourly, `flock` stops overlapping runs):
```
0 * * * * cd /path/to/project && flock -n /tmp/backups.lock /path/to/venv/bin/python manage.py run_scheduled_backups >> /var/log/pos_backups.log 2>&1
```

6. Add a "Backups" link in the admin sidebar -> {% url 'backup-list' %}.

Restore (non-destructive, re-inserts rows missing from the DB):
`python manage.py restore_backup <id>`