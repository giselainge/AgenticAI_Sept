FROM python:3.13-slim-bookworm

WORKDIR /app

# Copy application code
COPY ./llm ./llm
COPY ./invoice_parser ./invoice_parser
COPY ./rag ./rag
COPY ./vector_store ./vector_store
COPY ./scripts ./scripts
COPY requirements.txt ./requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

# Run the FastAPI application by default
CMD ["uvicorn", "llm.main:app", "--host", "0.0.0.0", "--port", "8000"]