# Дефекты части A

Четыре дефекта стартового `hw67-broken`: что было, как проявлялось, как исправлено,
каким числом/проверкой доказан фикс. Все доказательства — результат реального
прогона (`make up → make sweep → make candidate → make check-storage → make check`),
не теоретические.

## 1. Все шесть кандидатов писались в один run

**Что было.** `src/sweep.py::main()` запускал родительский run `sweep`, но вызывал
`run_candidate()` для всех шести конфигураций **внутри одного и того же активного
run**, без вложенного `mlflow.start_run()` на кандидата. Метрики, параметры и
артефакты всех шести прогонов (`log_params`, `log_metrics`, `log_dict`,
`pyfunc.log_model`) писались в один и тот же run id и затирали друг друга —
в MLflow UI оставался только самый последний кандидат.

**Как проявлялось.** После sweep в experiment было фактически 2 run'а
(`sweep` + один слитый "all-candidates"), а не 6 отдельных + родитель.
`promote.py` ищет кандидатов фильтром `tags.mlflow.parentRunId = sweep and
tags.kind = 'candidate'` — при таком слиянии он находил 0 или 1 run вместо 6,
и сравнение шести LoRA-конфигураций было невозможно в принципе.

**Фикс.** В `main()` добавлена обёртка `with mlflow.start_run(run_name=candidate["name"],
nested=True):` вокруг каждого вызова `run_candidate()` — прямой потомок `sweep`.
`run_candidate()` не менялся: он уже работал через `mlflow.active_run()`.

**Доказательство.** `tests/check.sh`, проверка 1 "Минимум шесть отдельных
законченных runs": **PASS**, найдено 6 child runs с уникальными именами
(`qv-r4-low`, `qv-r4-high`, `qv-r8-low`, `qv-r8-high`, `all-r8-low`,
`all-r8-high`), все `status == FINISHED`, варьируются `r`, `lr`, `target_modules`.
`docs/comparison.csv` содержит все 6 строк с разными `final_val_loss`.

## 2. Артефакты MLflow пропадали при пересоздании контейнера

**Что было.** В `compose.yaml` у сервиса `mlflow` не было volume под `/mlartifacts`.
Postgres (метаданные runs) был на volume, а сами артефакты (адаптеры, `source/`,
`samples.json`) лежали в read-write слое контейнера.

**Как проявлялось.** `docker compose up` после пересборки образа или `down/up`
стирал все обученные адаптеры и залогированные модели — run в базе остаётся,
но `download_artifacts` на нём падает: файлов физически нет.

**Фикс.** В `compose.yaml` сервису `mlflow` добавлен именованный volume:
```yaml
volumes:
  - artifacts:/mlartifacts
```
(объявлен в `volumes:` верхнего уровня рядом с `postgres`).

**Доказательство.** `make check-storage` → `tests/check_storage.py`: пишет
`persistence/probe.txt` в новый run, пересоздаёт контейнер `mlflow`
(`docker compose up --build -d --force-recreate mlflow` внутри скрипта) и
скачивает тот же артефакт заново.
```
PASS: новый контейнер вернул тот же артефакт, SHA256 совпал
```
`tests/check.sh`, проверка 8 "Артефакт пережил пересоздание контейнера": **PASS**.

## 3. Нет версии train/val данных в provenance

**Что было.** `src/common.py::provenance()` уже умел считать `code_sha256`,
`git_sha`, `git_dirty`, `config_sha256`, но `src/sweep.py::main()` не прокидывал
`data_train_sha256`/`data_val_sha256` в теги runs — `provenance(p)` вызывался
(функция уже возвращала все нужные хэши), но набор версий не записывался как
теги родительского run и не сверялся при выборе/промоуте кандидата.

**Как проявлялось.** Можно было подменить `data/tokenized/train.pt` между
прогонами sweep и select/promote — ни один run не фиксировал, какими именно
данными он обучен, и `promote.py` не мог обнаружить рассинхронизацию.

**Фикс.** `main()` теперь прокидывает полный `versions = provenance(p)`
(включая `data_train_sha256`, `data_val_sha256`) и в теги родительского run
(`mlflow.start_run(..., tags={"kind": "sweep", **versions})`), и в теги каждого
кандидата через `run_candidate(p, candidate, versions)` →
`mlflow.set_tags({**versions, "kind": "candidate", ...})`.

**Доказательство.** `tests/check.sh`, проверка 2 "Код, данные, конфиг и база
имеют версии": **PASS** — для всех 6 runs теги `code_sha256`, `config_sha256`,
`data_train_sha256`, `data_val_sha256` совпадают с `provenance()`, посчитанным
заново на момент проверки; `git_sha` длиной 40 символов; `base_revision`
(закреплённый HF commit) тоже длиной 40.

## 4. `promote()` не проверял порог loss перед регистрацией

**Что было.** В `src/promote.py` уже была корректно реализована функция
`gate(run, max_ratio)` (сравнивает `final_val_loss / base_val_loss` с порогом),
но `promote(client, run, name, alias, threshold)` её не вызывала — просто
регистрировала модель и проставляла alias независимо от качества кандидата.

**Как проявлялось.** Кандидат с `val_ratio` сильно хуже порога (например,
обучение разошлось / loss вырос) всё равно получал alias `candidate`/`champion`
— порог `max_val_ratio` в `params.yaml` существовал только на бумаге.

**Фикс.** `promote()` теперь первым делом вызывает `ratio = gate(run, threshold)`
(бросает `ValueError`, если `ratio > threshold`), и только после этого
регистрирует модель и проставляет теги/alias. `gate()` не менялась.

**Доказательство.** Два независимых прогона:
- `tests/check.sh`, проверка 6 "Запрет регистрации при непройденном пороге":
  **PASS** — `promote(client, best, name, "test", 0.0)` (нулевой порог) корректно
  бросает `ValueError`, вместо регистрации нулевого порога как PASS.
- `make check-negative` (отдельный негативный тест на уровне Part B gate,
  испорченные ответы кандидата): `PASS: испорченные ответы отклонены, alias не менялся`.

Реальный промоут прошедшего порог кандидата: `make promote` →
`champion -> models:/my-qwen-adapter/1` (run `all-r8-high`, `final_val_loss`
0.2727 против базового 1.0286, т.е. `val_ratio` ≈ 0.265 ≪ порога 1.0).

## Отдельно: два Windows-специфичных фикса (не из списка 4 дефектов)

Эти два бага не входят в перечисленные 4 дефекта — они воспроизводятся только на
Windows и были найдены при реальном прогоне на этой машине, не вычитыванием кода.
Исправлены, т.к. без них `make check` физически не проходит на Windows.

- **`mlflow.pyfunc.log_model(..., artifacts={...})`** на Windows собирает
  относительный путь адаптера через `os.path.join`, получая `artifacts\adapter`
  вместо `artifacts/adapter` в `MLmodel`. `tests/check.py` читает этот путь и
  клеит `f"model/{location}/adapter_config.json"` — смешение `/` и `\` в URL
  ломает HTTP-скачивание артефакта (проверка 4). Исправлено точечной подменой
  `os.path.join` на `posixpath.join` на время вызова `log_model` в
  `src/sweep.py::run_candidate()` (try/finally, восстанавливается сразу после).
- **`src/predict.py`**, запускаемый `tests/check.py` как `python -I ...`
  (изолированный режим), не наследует `PYTHONUTF8`/`PYTHONIOENCODING` — stdout
  падает на кодировку Windows по умолчанию (cp1251), которая не кодирует часть
  символов в вопросах MMLU, и родительский `subprocess.run` падает с
  `UnicodeDecodeError` при чтении вывода. Исправлено явным
  `sys.stdout.reconfigure(encoding="utf-8")` в начале `predict.py` — работает
  независимо от `-I`, т.к. это код, а не переменная окружения.
