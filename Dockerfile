FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose health-check port (Railway / Render / fly.io)
EXPOSE 8080

# Run the trading bot
CMD ["python", "main.py"]
