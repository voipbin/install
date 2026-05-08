.PHONY: install lint test clean

install:
	pip install -r requirements.txt

lint:
	python3 -m py_compile scripts/cli.py
	python3 -m py_compile scripts/config.py
	python3 -m py_compile scripts/wizard.py
	python3 -m py_compile scripts/preflight.py
	python3 -m py_compile scripts/gcp.py
	python3 -m py_compile scripts/secretmgr.py
	python3 -m py_compile scripts/display.py
	python3 -m py_compile scripts/utils.py
	python3 -m py_compile scripts/commands/init.py
	python3 -m py_compile scripts/commands/dns.py

test:
	python3 -m pytest tests/ -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
