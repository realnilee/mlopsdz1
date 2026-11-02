PYTHON ?= python3
VENV_PYTHON ?= .venv/Scripts/python.exe
export PYTHONUTF8 = 1
.PHONY: check check-fast check-target ci-break real-init real-flow real-api stub-experiments

check:
	$(PYTHON) -m compileall -q release.py tests
	$(PYTHON) -m unittest discover -s tests -v

# Без весов и без пакетов: публичные тесты и интеграционные на прототипе.
check-fast: check
	$(PYTHON) -m compileall -q lab.py service.py api_contract.py tests_integration
	$(PYTHON) -m unittest discover -s tests_integration -t . -v

# Те же интеграционные тесты на Prefect и FastAPI; веса не загружаются.
check-target:
	HW_LAB_CMD="-m flows.release_flow" HW_SERVICE_CMD="-m app.api" $(VENV_PYTHON) -m unittest discover -s tests_integration -t . -v

ci-break:
	$(PYTHON) tools/ci_break_demo.py

real-init:
	$(VENV_PYTHON) -m flows.release_flow --workspace .release-run init --real

real-flow:
	$(VENV_PYTHON) -m flows.release_flow --workspace .release-run run --real

real-api:
	$(VENV_PYTHON) tools/real_api_run.py

stub-experiments:
	$(VENV_PYTHON) tools/stub_experiments.py
