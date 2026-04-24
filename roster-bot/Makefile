.PHONY: up down restart logs build

up:
	sudo docker compose up -d

down:
	sudo docker compose down

restart:
	sudo docker compose build && sudo docker compose up -d

logs:
	sudo docker compose logs -f

build:
	sudo docker compose build
