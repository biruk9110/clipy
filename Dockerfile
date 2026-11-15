FROM python:3.11-slim

# Install system dependencies & ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Hugging Face Spaces runs on port 7860
EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]

