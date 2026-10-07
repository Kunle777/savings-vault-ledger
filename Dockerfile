FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml alembic.ini ./
COPY app app
COPY alembic alembic
RUN pip install --no-cache-dir '.[test]'
CMD ["sh", "-c", "alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
