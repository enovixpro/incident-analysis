.PHONY: install run seed test clean help

help:
	@echo "make install  - install dependencies"
	@echo "make seed     - load seed incidents into Chroma"
	@echo "make web      - launch FastAPI dashboard on :8000 (recommended)"
	@echo "make run      - launch legacy Streamlit UI on :8501"
	@echo "make mcp      - launch the MCP server over stdio (for Claude Desktop / claude CLI)"
	@echo "make test     - run pytest (unit + graph smoke; fast)"
	@echo "make test-ui  - run Playwright UI tests (requires `playwright install chromium`)"
	@echo "make clean    - remove caches and chroma data"

install:
	pip install -r requirements.txt

seed:
	python -m src.tools.seed_vectorstore

web:
	uvicorn web.server:app --reload --port 8000

mcp:
	python mcp_server.py

run:
	streamlit run app.py

test:
	pytest tests/test_parser.py tests/test_graph_smoke.py -v

test-ui:
	pytest tests/ui/ -v

clean:
	rm -rf .chroma __pycache__ .pytest_cache **/__pycache__
