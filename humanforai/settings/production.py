"""
Production settings for Cloud Run.

Everything sensitive or deployment-specific comes from environment variables:

  SECRET_KEY            required
  DATABASE_URL          e.g. postgres://user:pass@host/db (falls back to SQLite,
                        which on Cloud Run is EPHEMERAL — fine for a first smoke
                        deploy, useless as a record)
  ALLOWED_HOSTS         comma-separated, e.g. "humanforai-xyz.a.run.app"
  CSRF_TRUSTED_ORIGINS  comma-separated with scheme, e.g. "https://humanforai-xyz.a.run.app"
  WAGTAILADMIN_BASE_URL e.g. "https://humanforai-xyz.a.run.app"
  MCP_REGISTRY_AUTH     the "v=MCPv1; k=ed25519; p=..." line served at
                        /.well-known/mcp-registry-auth, for domain-based
                        publishing to the MCP Registry (optional)
"""

import os

import dj_database_url

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, STORAGES

DEBUG = False

SECRET_KEY = os.environ["SECRET_KEY"]

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
]

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Cloud Run terminates TLS at the load balancer.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# Hashed + compressed static files served by whitenoise.
STORAGES["staticfiles"]["BACKEND"] = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# User-uploaded media (Wagtail images and documents) goes to Google Cloud
# Storage. The container filesystem is ephemeral, so a file written there
# survives only until the instance is recycled — and the failure is silent:
# the upload succeeds, the page renders, and the image 404s days later with
# the database row still pointing at a path that no longer exists.
#
# Guarded on the env var so a deploy without a bucket still boots on local
# disk rather than failing at import.
GS_BUCKET_NAME = os.environ.get("GS_BUCKET_NAME", "")

# Set at module level as well as in OPTIONS below. The backend honours either,
# but Wagtail's wagtailadmin.W004 check inspects this setting and cannot see
# inside STORAGES["default"]["OPTIONS"] — so the two are kept in agreement.
GS_FILE_OVERWRITE = False

if GS_BUCKET_NAME:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": GS_BUCKET_NAME,
            # The bucket enforces uniform bucket-level access, which rejects
            # per-object ACLs outright. Read access comes from the bucket IAM
            # policy (allUsers:objectViewer); writes from the runtime service
            # account's objectAdmin binding, picked up as ADC on Cloud Run.
            "default_acl": None,
            # Objects are publicly readable, so hand out plain URLs. Signed
            # URLs would expire, which breaks browser caching and quietly
            # rots any media URL that has been shared or indexed.
            "querystring_auth": False,
            # django-storages defaults this to True, which silently replaces
            # an existing object when a new upload collides on name. Django
            # would otherwise suffix the name to keep both. Overwriting is
            # unrecoverable, so make collisions rename instead.
            "file_overwrite": False,
            "max_memory_size": 10 * 1024 * 1024,
        },
    }

WAGTAILADMIN_BASE_URL = os.environ.get(
    "WAGTAILADMIN_BASE_URL", "https://example.com"
)

MCP_REGISTRY_AUTH = os.environ.get("MCP_REGISTRY_AUTH", "")


# Inbox arrival notification.
#
# With no INBOX_NOTIFY_EMAILS the feature is off. With addresses but no
# EMAIL_HOST, the console backend inherited from base.py still applies, so the
# notification is written to stdout and lands in Cloud Logging — visible, just
# not delivered. That is a deliberate fallback rather than a silent no-op.
INBOX_NOTIFY_EMAILS = [
    e.strip() for e in os.environ.get("INBOX_NOTIFY_EMAILS", "").split(",") if e.strip()
]

DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", "Human for AI <noreply@yourhuman.ai>"
)

EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") != "0"
    # Bounded so a hanging SMTP server cannot pin a Cloud Run instance.
    EMAIL_TIMEOUT = 10

# Log to stdout so Cloud Logging picks everything up.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

try:
    from .local import *  # noqa: F401,F403
except ImportError:
    pass
