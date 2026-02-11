FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependências de sistema para testes de rede de alta performance.
RUN apt-get update && apt-get upgrade -y && apt-get install -y \
    iperf3 \
    iproute2 \
    ethtool \
    net-tools \
    iputils-ping \
    procps \
    gcc \
    libffi-dev \
    libssl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Atualiza PIP para a versão mais recente
RUN python -m pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x scripts/iperf-runner.sh

EXPOSE 5000

CMD ["python", "app.py"]
