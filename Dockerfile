FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY garmin_service.py .

# /data is a mounted volume for persisted state (tokens, last activity ID)
VOLUME ["/data"]

CMD ["python", "-u", "garmin_service.py"]
