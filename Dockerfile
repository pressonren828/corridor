FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN mkdir -p /data
VOLUME /data

ENV CORRIDOR_DB=/data/corridor.db
ENV CORRIDOR_TTL=60
ENV CORRIDOR_PORT=8090

EXPOSE 8090

CMD ["python", "server.py"]
