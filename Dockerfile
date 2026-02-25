FROM python:3.11-slim

WORKDIR /app

# Install system deps for python-whois, dnspython
RUN apt-get update && apt-get install -y --no-install-recommends \
    whois \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src"]
