#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input
python manage.py migrate

python manage.py shell <<EOF
import os
from django.contrib.auth import get_user_model

User = get_user_model()

username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin")
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin@example.com")
password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "ChangeMe123!")

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