# chronolog-observability build helpers.
#
# Targets:
#   workspace        Install JS deps and build the React SPA into
#                    backend/static/workspace/. Flask serves
#                    it at /. Run before building a wheel so the UI ships in it.
#   workspace-dev    Vite dev server on :5173 with /api and /_interceptor proxied
#                    to a locally running `chronolog-observe` (:5000).
#   workspace-clean  Remove the built SPA bundle.
#   demo             Build + run the offline/demo dashboard (docker/), seeding
#                    demo data on first boot. Open http://localhost:5000.
#   demo-down        Stop the demo (keeps the seeded data volume).
#   demo-reseed      Stop + wipe the data volume, then rebuild/run fresh.
#
# The built bundle is picked up by pyproject package-data, so a wheel built after
# `make workspace` includes the UI. The frontend has no chimaera dependency.

FRONTEND_DIR := frontend
BUNDLE_DIR   := backend/static/workspace
COMPOSE      := docker compose -f docker/docker-compose.yml

.PHONY: workspace workspace-dev workspace-clean demo demo-down demo-reseed

workspace:
	cd $(FRONTEND_DIR) && npm install && npm run build

workspace-dev:
	cd $(FRONTEND_DIR) && npm install && npm run dev

workspace-clean:
	rm -rf $(BUNDLE_DIR)

demo:
	$(COMPOSE) up --build

demo-down:
	$(COMPOSE) down

demo-reseed:
	$(COMPOSE) down -v && $(COMPOSE) up --build
