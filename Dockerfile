# Single-stage build (tanpa base image terpisah).
FROM python:3.10-slim

WORKDIR /app

# Install dependencies dulu agar layer-nya ter-cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin sisa kode.
COPY . .

# Port API server RunPod (saat dijalankan dengan --rp_serve_api).
EXPOSE 8000

# Default: handler RunPod. docker-compose meng-override ke mode API server.
CMD ["python", "handler.py"]
