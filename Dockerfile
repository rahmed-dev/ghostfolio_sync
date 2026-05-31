FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py ghostfolio.py coingecko.py activity.py ./
COPY exchanges ./exchanges

USER nobody

CMD ["python", "-u", "sync.py"]
