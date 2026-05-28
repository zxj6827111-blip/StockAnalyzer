FROM node:22-slim AS frontend-builder

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json /frontend/
RUN --mount=type=cache,target=/root/.npm \
    npm config set registry "${NPM_REGISTRY}" \
    && npm ci --prefer-offline --no-audit --fund=false

COPY frontend /frontend
RUN npm run build

FROM python:3.11-slim

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_EXTRA_INDEX_URL=https://pypi.org/simple
ARG PIP_TIMEOUT=600
ARG PIP_RETRIES=10

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL}
ENV PIP_DEFAULT_TIMEOUT=${PIP_TIMEOUT}
ENV PIP_PREFER_BINARY=1
ENV STOCK_ANALYZER_CONTAINERIZED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates cpulimit \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
RUN python -c "import pathlib, tomllib; data = tomllib.loads(pathlib.Path('/app/pyproject.toml').read_text(encoding='utf-8')); pathlib.Path('/app/requirements.docker.txt').write_text('\n'.join(data['project']['dependencies']) + '\n', encoding='utf-8')"
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries ${PIP_RETRIES} --timeout ${PIP_TIMEOUT} -r /app/requirements.docker.txt

COPY README.md /app/README.md
COPY src /app/src
COPY config /app/config
COPY scripts /app/scripts
COPY --from=frontend-builder /frontend/dist /app/frontend_dist
COPY artifacts/model_v1.json /app/bootstrap_seed/model_v1.json

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries ${PIP_RETRIES} --timeout ${PIP_TIMEOUT} --no-deps --no-build-isolation -e .
RUN chmod +x /app/scripts/docker-entrypoint.sh

ARG STOCK_ANALYZER_BUILD_COMMIT=unknown
LABEL org.opencontainers.image.revision=${STOCK_ANALYZER_BUILD_COMMIT}
ENV STOCK_ANALYZER_BUILD_COMMIT=${STOCK_ANALYZER_BUILD_COMMIT}

EXPOSE 8000

CMD ["uvicorn", "stock_analyzer.main:app", "--host", "0.0.0.0", "--port", "8000"]
