# iperf3 Web Multi-Interface

Aplicação web em Flask + Socket.IO para executar testes de desempenho com `iperf3` em múltiplas interfaces de rede simultaneamente, dentro de container Docker.

## Pré-requisitos no host (Ubuntu Linux)

- Docker Engine instalado
- Docker Compose plugin (`docker compose`)
- Servidor iperf3 acessível na rede (ex.: `iperf3 -s`)
- Permissão para executar containers com `network_mode: host` e `NET_ADMIN`

## Estrutura do projeto

```text
.
├── app.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── scripts/
│   └── iperf-runner.sh
├── templates/
│   └── index.html
└── static/
    ├── main.js
    └── style.css
```

## Build

```bash
docker compose build
```

## Execução

```bash
docker compose up -d
```

A aplicação ficará disponível em:

- http://localhost:5000

## Como usar

1. Abra a aplicação no navegador.
2. Informe o IP do servidor `iperf3`.
3. Defina o tempo do teste e o modo (`upload`, `download` ou `ambos`).
4. Marque as interfaces desejadas.
5. Clique em **Iniciar teste**.
6. Acompanhe throughput em tempo real, velocímetros e resultados finais por interface.

## Segurança e validações

- O backend valida IP, duração, modo e interfaces.
- A execução usa `subprocess` sem `shell=True`.
- Script auxiliar valida parâmetros antes de chamar `iperf3`.

