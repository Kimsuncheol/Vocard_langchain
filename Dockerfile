FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.in pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY vocard ./vocard
RUN pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home vocard \
    && mkdir /app/state && chown vocard:vocard /app/state
USER vocard
CMD ["uvicorn", "vocard.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
