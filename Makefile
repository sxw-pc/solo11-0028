PYTHON ?= python
AS_OF ?= 2026-11-01T00:00:00Z
LAKE ?= data/lake
USAGE ?= data/usage/access.jsonl
POLICY ?= config/policy.yaml
DB ?= data/ledger/tier_triage.sqlite
ARTIFACTS ?= data/artifacts

.PHONY: demo test serve scan profile evaluate plan clean

demo:
	$(PYTHON) scripts/run_demo.py

test:
	$(PYTHON) -m pytest -q

serve:
	$(PYTHON) -m tier_triage serve --lake $(LAKE) --db $(DB) --port 5000

scan:
	$(PYTHON) -m tier_triage scan --lake $(LAKE) --db $(DB) \
		--out $(ARTIFACTS)/inventory.json

profile:
	$(PYTHON) -m tier_triage profile --lake $(LAKE) --as-of $(AS_OF) \
		--out $(ARTIFACTS)/volume_profile.json

evaluate:
	$(PYTHON) -m tier_triage evaluate --lake $(LAKE) --usage $(USAGE) \
		--policy $(POLICY) --db $(DB) --as-of $(AS_OF) \
		--out $(ARTIFACTS)/evaluation.json

plan:
	$(PYTHON) -m tier_triage plan --lake $(LAKE) --usage $(USAGE) \
		--policy $(POLICY) --db $(DB) --as-of $(AS_OF) \
		--out $(ARTIFACTS)/plan.json

clean:
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(p) for p in (pathlib.Path('data/lake'), pathlib.Path('data/usage'), pathlib.Path('data/ledger'), pathlib.Path('data/artifacts')) if p.exists()]"
