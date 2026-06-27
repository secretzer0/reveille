CLAUDE ?= $(HOME)/.claude

.PHONY: help build test install uninstall lint clean

help:
	@echo "make test       run the test suite"
	@echo "make build      verify (test) — there is nothing to compile; a single zero-dep script"
	@echo "make install    deploy fswatch.py + commands into $(CLAUDE)"
	@echo "make uninstall  remove them from $(CLAUDE)"
	@echo "make lint       ruff check (if installed)"

# build == verify it works. No compile step: fswatch.py is a single stdlib+ctypes file.
build: test

test:
	python3 tests/test_fswatch.py
	python3 tests/test_bus.py

install:
	install -d "$(CLAUDE)/scripts" "$(CLAUDE)/commands"
	install -m 0755 src/fswatch.py "$(CLAUDE)/scripts/fswatch.py"
	install -m 0755 src/bus.py "$(CLAUDE)/scripts/bus.py"
	install -m 0644 commands/watch-loop.md "$(CLAUDE)/commands/watch-loop.md"
	install -m 0644 commands/watch-send.md "$(CLAUDE)/commands/watch-send.md"
	install -m 0644 commands/watch-list.md "$(CLAUDE)/commands/watch-list.md"
	install -m 0644 commands/watch-stop.md "$(CLAUDE)/commands/watch-stop.md"
	@echo "installed to $(CLAUDE) — restart running sessions to load the commands."

uninstall:
	rm -f "$(CLAUDE)/scripts/fswatch.py" "$(CLAUDE)/scripts/bus.py" \
	      "$(CLAUDE)/commands/watch-loop.md" "$(CLAUDE)/commands/watch-send.md" \
	      "$(CLAUDE)/commands/watch-list.md" "$(CLAUDE)/commands/watch-stop.md"
	@echo "removed from $(CLAUDE)."

lint:
	@command -v ruff >/dev/null 2>&1 && ruff check src tests || echo "ruff not installed — skipping"

clean:
	rm -rf src/__pycache__ tests/__pycache__ .ruff_cache .mypy_cache
