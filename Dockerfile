# syntax=docker/dockerfile:1

# Build the Python virtual environment separately so compilers and development
# headers are not included in the runtime image.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS python-builder

RUN dnf -y upgrade \
    && dnf -y install \
        gcc \
        gcc-c++ \
        make \
        python3.13 \
        python3.13-devel \
        python3.13-pip \
    && dnf clean all \
    && rm -rf /var/cache/dnf

RUN python3.13 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

COPY deep-agents-sdk/requirements.txt /tmp/requirements.txt

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir \
        --requirement /tmp/requirements.txt


# Assemble the production runtime image.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS runtime

RUN dnf -y upgrade \
    && dnf -y install \
        fontconfig \
        libgomp \
        python3.13 \
        shadow-utils \
    && dnf clean all \
    && rm -rf /var/cache/dnf

ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd --system --gid "${APP_GID}" app \
    && useradd \
        --uid "${APP_UID}" \
        --gid app \
        --home-dir /app \
        --shell /sbin/nologin \
        app \
    && mkdir -p \
        /app/deep-agents-sdk \
        /app/projects \
        /app/runtime/databases \
        /app/runtime/generated \
        /tmp/matplotlib \
    && chown -R app:app /app /tmp/matplotlib

COPY --from=python-builder /opt/venv /opt/venv

# Python API.
COPY --chown=app:app \
    deep-agents-sdk/server.py \
    /app/deep-agents-sdk/server.py

COPY --chown=app:app \
    deep-agents-sdk/deep_agents_app/ \
    /app/deep-agents-sdk/deep_agents_app/

# Already-built React resources. Node.js is not needed in this image.
COPY --chown=app:app \
    deep-agents-sdk/static/ \
    /app/deep-agents-sdk/static/

# Initial shared project content.
COPY --chown=app:app \
    projects/ \
    /app/projects/

# Fail clearly when the React production build was not prepared beforehand.
RUN test -f /app/deep-agents-sdk/static/dist/index.html

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PROJECTS_DIR=/app/projects \
    DEEP_AGENTS_DB_DIR=/app/runtime/databases \
    DEEP_AGENTS_GENERATED_DIR=/app/runtime/generated \
    PORT=9010

WORKDIR /app/deep-agents-sdk

USER app

EXPOSE 9010

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '9010') + '/api/auth/config', timeout=3)" || exit 1

STOPSIGNAL SIGTERM

CMD ["sh", "-c", "exec python -m uvicorn server:app --host 0.0.0.0 --port \"${PORT:-9010}\" --workers 1"]
