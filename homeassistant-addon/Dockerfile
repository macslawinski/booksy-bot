FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY booksy_bot ./booksy_bot
RUN pip install --no-cache-dir . && rm -rf /app/booksy_bot
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
