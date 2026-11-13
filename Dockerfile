FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --no-create-home app
COPY portal ./portal
USER app
EXPOSE 3001
CMD ["gunicorn", "--bind", "0.0.0.0:3001", "--workers", "2", "--threads", "12", "--timeout", "60", "portal:create_app()"]
