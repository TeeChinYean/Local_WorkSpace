FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
# Install CPU-only PyTorch (reduces download from 700MB to ~200MB and speeds up significantly)
RUN pip install --no-cache-dir torch --extra-index-url https://download.pytorch.org/whl/cpu
# Install the rest
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 18088

CMD ["python", "main.py"]
