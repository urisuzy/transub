# Gunakan image dasar Python
FROM python:3.9-slim

# Set work directory di dalam container
WORKDIR /app

# Salin semua file ke dalam container
COPY . .

# Instal dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Ekspos port jika aplikasi memerlukan akses tertentu (misalnya Flask di port 5000)
EXPOSE 5000

# Tentukan perintah untuk menjalankan aplikasi di container
CMD ["python", "handler.py"]
