# Gunakan Python versi ringan
FROM python:3.10-slim

# Buat folder kerja di dalam server
WORKDIR /app

# Copy file requirements dan install library-nya
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy script bot Anda
COPY arsitek_bot.py .

EXPOSE 7860

# Perintah untuk menjalankan bot secara terus-menerus
CMD ["python", "bot.py"]