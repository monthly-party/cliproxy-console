.PHONY: test deploy deploy-check

test:
	# Port 9 (discard) is never the live proxy: a test that forgets to fake it fails instead.
	PROXY_PORT=9 PYTHONPATH=gateway python3 -m unittest discover -s tests

deploy-check:
	python3 scripts/deploy.py --dry-run

deploy:
	python3 scripts/deploy.py
