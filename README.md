# MiningHub

MiningHub é uma plataforma FastAPI + Bootstrap para monitoramento e gerenciamento de mineradores NerdMiner, Bitaxe, Lucky Miner, AxeOS, HTTP e CGMiner.

## Recursos
- API REST, WebSocket, Swagger e métricas Prometheus.
- Autenticação JWT com perfis administrador, operador e somente leitura.
- Banco SQLite por padrão, pronto para PostgreSQL via `MININGHUB_DATABASE_URL`.
- Descoberta por varredura de rede e adaptadores independentes de plugins.
- Dashboard PWA responsivo com tema claro/escuro, Chart.js e atualização periódica.
- Upload de firmware, backup/restauração, eventos, histórico e telemetria.

## Execução com Docker Compose
```bash
cp .env.example .env
docker compose up -d --build
```
Acesse `http://localhost:8080` e a API em `http://localhost:8080/api/docs`.

## Primeiro usuário
```bash
curl -X POST http://localhost:8080/api/auth/bootstrap -H 'Content-Type: application/json' -d '{"username":"admin","password":"altere-esta-senha","role":"admin"}'
```

## CasaOS
Use `casaos/appstore.json` como metadado de AppStore e o `docker-compose.yml` raiz para instalação.

## Backup e restore
Backups são ZIPs gravados em `data/backups` pelo endpoint `/api/backups`; restauração usa `/api/restore`.

## Testes
```bash
pytest
```
