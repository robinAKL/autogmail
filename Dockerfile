FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0
COPY . .
EXPOSE 3000
CMD ["sh", "-c", "python -m gmail_auto.workspace_db && gunicorn --bind 0.0.0.0:${PORT:-3000} --workers 1 --threads 4 --timeout 120 server:app"]
