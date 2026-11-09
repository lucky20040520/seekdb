FROM python:3.12.8-slim-bookworm
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && mkdir -p /data
COPY minivector /app/minivector
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
# Local diagnostic adapter only. Replace framing after official protocol release.
ENTRYPOINT ["python", "-m", "minivector", "--data-dir", "/data"]
