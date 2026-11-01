FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY polyscan ./polyscan
COPY config.example.json ./config.json

EXPOSE 8080
CMD ["python3", "-m", "polyscan", "dashboard", "--host", "0.0.0.0", "--port", "8080", "--interval", "60"]
