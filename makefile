.PHONY: bootstrap api web backend redis.up

bootstrap:
	@bash tools/bootstrap.sh

api:
	@. .venv/bin/activate && adaos api serve --host 127.0.0.1 --port 8777 --reload

backend:
	@cd src/adaos/integrations/inimatic && npm run start:api-dev

web:
	@cd src/adaos/integrations/inimatic && npm run start

redis.up:
	@docker run --rm -p 6379:6379 --name inimatic-redis redis:7-alpine
