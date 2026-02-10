FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependências de sistema para testes de rede e inspeção de interfaces.
RUN apt-get update && apt-get install -y \
    iperf3 \
    iproute2 \
    ethtool \
    net-tools \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x scripts/iperf-runner.sh

EXPOSE 5000

CMD ["python", "app.py"]
