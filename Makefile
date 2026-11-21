.PHONY: run test lint app clean

run:
	bash run.sh

test:
	bash test.sh

lint:
	.venv/bin/ruff check ftpclient tests

app:
	bash scripts/build_app.sh

clean:
	rm -rf build dist *.egg-info .ruff_cache
	find ftpclient tests -name __pycache__ -type d -prune -exec rm -rf {} +
