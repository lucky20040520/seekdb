FROM python:3.12.8-slim
WORKDIR /app
COPY minivectordb /app/minivectordb
ENV PYTHONUNBUFFERED=1 MINIVECTORDB_DATA=/data
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "minivectordb"]
