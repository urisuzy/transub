# Single-stage build (tanpa base image terpisah).
FROM python:3.10-slim

WORKDIR /app

# Install dependencies dulu agar layer-nya ter-cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin sisa kode.
COPY . .

# Port FastAPI.
EXPOSE 8000

# Jalankan FastAPI via uvicorn.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
