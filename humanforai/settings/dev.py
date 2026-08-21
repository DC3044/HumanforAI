from .base import *

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = "django-insecure-=@11)u+q&q!1q2gwz7e9)-g9379$*uz&x49mz4+ahxiejtl_7^"

# SECURITY WARNING: define the correct hosts in production!
ALLOWED_HOSTS = ["*"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Thread URLs are built from this, and base.py leaves it at example.com — so
# without this override every receipt printed locally points at a domain the IANA
# runs, which is a confusing way to find out the channel works.
WAGTAILADMIN_BASE_URL = "http://localhost:8000"

# Write visits through immediately instead of batching them. Batching exists to
# let a serverless database sleep, which is a production concern and a nuisance
# locally: a request you just made should appear in the admin while you are
# looking at it. A batch of one is the same code path with no waiting.
REGISTER_FLUSH_ROWS = 1


try:
    from .local import *
except ImportError:
    pass
