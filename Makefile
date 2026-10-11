.PHONY: test deploy deploy-check

test:
	PYTHONPATH=gateway python3 -m unittest discover -s tests

deploy-check:
	python3 scripts/deploy.py --dry-run

deploy:
	python3 scripts/deploy.py
