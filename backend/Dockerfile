FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# GEOS is required by shapely.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgeos-c1v5 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Cloud Run injects $PORT.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
