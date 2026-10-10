# Railway (or any container host): polls hourly and serves the dashboard.
# Mount a volume and point PAPERTRACKER_DATA_DIR at it so the database
# survives redeploys.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY . .
# Editable install keeps config.yaml next to the package, where it is read from.
RUN pip install -e .

CMD ["python", "-m", "papertracker", "serve"]
