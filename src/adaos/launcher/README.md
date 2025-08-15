# Sentinel use

```bash
# 1) Запустить лончер
ADAOS_CMD="adaos start --lang en --http 0.0.0.0:8788" \
ADAOS_TOKEN="dev-local-token" \
python -m adaos.launcher.sentinel

# 2) Клиентский вызов (ядро поднимется “лениво”)
curl -H "X-AdaOS-Token: dev-local-token" \
     -X POST http://127.0.0.1:8777/api/say \
     -d '{"text":"Hello from lazy boot!"}'

```
