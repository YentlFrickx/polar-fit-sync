FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
RUN useradd -r -u 1001 app \
    && mkdir -p /data \
    && chown -R app /app /data
    USER app
# EXPOSE is informational only (does not control runtime binding); keep this
# literal in sync with the PFS_PORT default in config.py / docker-compose.yml.
EXPOSE 8080
ENTRYPOINT ["python", "-m", "polar_fit_sync"]
CMD ["web"]
