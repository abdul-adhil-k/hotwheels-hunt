FROM python:3.11-slim

WORKDIR /app

# Install Playwright system dependencies
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libgbm1 libasound2 libxshmfence1 libgtk-3-0 \
    libxss1 libglib2.0-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY . .

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
