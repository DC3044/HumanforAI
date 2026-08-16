# Build stage: install dependencies with uv into a self-contained venv.
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev


# Runtime stage.
FROM python:3.13-slim-bookworm AS runtime

RUN useradd --create-home app

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DJANGO_SETTINGS_MODULE=humanforai.settings.production \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=app:app . .

# WORKDIR creates /app as root, and --chown above only covers the files copied
# into it, not the directory itself. collectstatic and any media upload need to
# create these two directories, so hand them over explicitly.
RUN mkdir -p /app/static /app/media && chown app:app /app /app/static /app/media

USER app

# A throwaway SECRET_KEY is enough for collectstatic at build time.
RUN SECRET_KEY=build-only python manage.py collectstatic --noinput --clear

EXPOSE 8080

# Cloud Run sets $PORT. Migrating at boot is not best practice for a busy
# multi-instance service, but is the pragmatic choice for this one.
#
# The register's retention runs here too, for the same reason: a Cloud Run Job
# on Cloud Scheduler is the correct home for it, and one more piece of
# infrastructure to create, authorise and remember. Cloud Run recycles instances
# often enough that boot is a serviceable clock, and the command is cheap when
# there is nothing to delete — an indexed count that returns zero.
#
# Unlike the migration it is not allowed to stop the container: expiring old
# telemetry is housekeeping, and the site must come up whether or not it worked.
CMD python manage.py migrate --noinput && \
    { python manage.py prune_visits || echo "prune_visits failed; serving anyway"; } && \
    gunicorn humanforai.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 60
