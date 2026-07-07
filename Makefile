# chronolog-observability build helpers.
#
# Targets:
#   workspace        Install JS deps and build the React SPA into
#                    backend/static/workspace/. Flask serves
#                    it at /. Run before building a wheel so the UI ships in it.
#   workspace-dev    Vite dev server on :5173 with /api and /_interceptor proxied
#                    to a locally running `chronolog-observe` (:5000).
#   workspace-clean  Remove the built SPA bundle.
#
# The built bundle is picked up by pyproject package-data, so a wheel built after
# `make workspace` includes the UI. The frontend has no chimaera dependency.

FRONTEND_DIR := frontend
BUNDLE_DIR   := backend/static/workspace

.PHONY: workspace workspace-dev workspace-clean

workspace:
	cd $(FRONTEND_DIR) && npm install && npm run build

workspace-dev:
	cd $(FRONTEND_DIR) && npm install && npm run dev

workspace-clean:
	rm -rf $(BUNDLE_DIR)
