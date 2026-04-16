FROM python:3.12-alpine

WORKDIR /app

# Build deps for rapidfuzz C extension; removed after install
RUN apk add --no-cache --virtual .build-deps gcc g++ musl-dev

COPY pyproject.toml README.md ./
COPY ebook_enricher/ ./ebook_enricher/

RUN pip install --no-cache-dir . \
 && apk del .build-deps

EXPOSE 8000

CMD ["uvicorn", "ebook_enricher.server:app", "--host", "0.0.0.0", "--port", "8000"]
