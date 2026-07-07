# The single omegahive image: library + CLI tools + tests, all run as one-shot
# compose services (deployment spec §4 — a host needs only an OCI runtime + compose).
# Base pinned by digest for reproducibility; the deployments-record lockfile pins
# the built image digest too.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# uv pinned to the version used to author uv.lock (host uv 0.11.6).
RUN pip install --no-cache-dir uv==0.11.6

WORKDIR /app

# Dependency layer first (cache-friendly): sync deps without the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Project source (includes migrations/ and scenarios/, read at runtime).
COPY . .
RUN uv sync --frozen

ENTRYPOINT ["omegahive"]
