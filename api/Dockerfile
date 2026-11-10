FROM python:3.13-slim
# WeasyPrint draws the PDF receipts with Pango
RUN apt-get update && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY api/pyproject.toml ./
RUN mkdir app && pip install --no-cache-dir . && rm -rf app
COPY api/ ./
COPY corpus/ /corpus/
RUN pip install --no-cache-dir --no-deps .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
