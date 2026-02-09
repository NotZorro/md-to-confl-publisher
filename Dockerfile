FROM python:3.12-slim

WORKDIR /app

# Install runtime deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy publisher source
COPY . /app

# Default command (can be overridden)
ENTRYPOINT ["python", "/app/publish_docs.py"]
