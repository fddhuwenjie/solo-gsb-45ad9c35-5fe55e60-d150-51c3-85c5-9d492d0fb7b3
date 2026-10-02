FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home appuser \
    && mkdir -p /srv/app/data \
    && chown -R appuser:appuser /srv/app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
