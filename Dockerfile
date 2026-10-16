FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 dolce \
    && useradd --system --uid 10001 --gid dolce --home /app --no-create-home dolce

COPY . .
RUN mkdir -p /app/data /app/temp \
    && chown -R dolce:dolce /app
USER dolce

CMD ["python", "main.py"]
