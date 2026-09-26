#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

echo "Collecting static files..."
python manage.py collectstatic --no-input

echo "Applying database migrations..."
python manage.py migrate

echo "Creating/checking admin user..."
python manage.py shell <<EOF
import os
from django.contrib.auth import get_user_model

User = get_user_model()

username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin")
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin@example.com")
password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "untamed2005")

if not password:
    raise RuntimeError(
        "DJANGO_SUPERUSER_PASSWORD environment variable is not set."
    )

if not User.objects.filter(username=username).exists():
    admin = User.objects.create_superuser(
        username=username,
        email=email,
        password=password,
    )

    admin.role = User.Roles.ADMIN
    admin.save()

    print(f"Admin user '{username}' created successfully.")
else:
    print(f"Admin user '{username}' already exists.")
EOF

echo "Build completed successfully."