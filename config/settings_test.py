import os
import tempfile

os.environ.setdefault("DJANGO_DEBUG", "1")

from .settings import *  # noqa: E402,F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # szybsze testy
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PRIVATE_MEDIA_ROOT = tempfile.mkdtemp(prefix="satok-test-media-")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SITE_URL = "http://testserver"
