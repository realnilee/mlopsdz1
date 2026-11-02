"""Заполняет defects.csv: семь причин, воспроизведение, исправление и регрессионный тест."""
import csv
from pathlib import Path

FIX = '2c87fe3'
PROCESS = 'tests_integration.test_release_process.ProcessTests.'
HTTP = 'tests_integration.test_http_service.ServiceTests.'
RED, GREEN = 'evidence/red/integration.log', 'evidence/green/integration_prototype.log; evidence/green/integration_prefect_fastapi.log'
ROWS = [
    ('После изменения данных используется старый результат подготовки',
     'lab.py run; изменить байты input.txt по тому же пути; lab.py run',
     'PREPARED после смены байтов или config; CACHE_HIT при тех же входах',
     'CACHE_HIT: ключ не изменился',
     'release.cache_key хешировал только строку пути, без содержимого файла и config',
     PROCESS + 'test_recovery_uses_cache_and_any_input_change_invalidates'),
    ('Неполный или чужой отчёт получает зелёный выпуск',
     'lab.py run --report report-incomplete.json',
     'Ненулевой exit, accepted.json побайтно прежний',
     "exit 0 и публикация B: строка 'false' истинна, остальные поля не проверялись",
     'release.validate_release проверял только истинность report.passed',
     PROCESS + 'test_incomplete_rejected_foreign_reports_keep_pointer'),
    ('Ошибка записи портит или теряет указатель',
     'mock os.fsync с OSError во время release.publish',
     'OSError наружу, старые байты на месте, временного файла нет',
     'OSError не возникает: fsync не вызывается, запись идёт прямо в целевой файл',
     'release.publish писал через target.write_text без временного файла, fsync и os.replace',
     'tests_integration.test_release_process.WriteFailureTests.test_failed_fsync_keeps_old_bytes_and_no_temp_file'),
    ('Повтор задачи размножает публикации',
     'lab.py run три раза подряд; также сбой между указателем и журналом и повтор',
     'Одно событие на выпуск; новое решение допуска — новое событие',
     '3 записи вместо 1; после сбоя с восстановлением 2 вместо 1',
     'lab.run безусловно дописывал publications.jsonl и не имел идентификатора выпуска',
     PROCESS + 'test_repeat_publish_makes_one_event_new_evaluation_makes_another; '
     + PROCESS + 'test_crash_between_pointer_and_journal_recovers_without_double_event'),
    ('Сервис объявляет готовность и отвечает до конца прогрева',
     'service.py --warmup-seconds 6; сразу GET /ready и POST /generate',
     '/health 200, /ready 503, /generate 503, calls 0',
     '/ready 200 сразу после старта',
     'ServiceState.ready всегда True и не связан с warmup_finished; generate не проверял прогрев',
     HTTP + 'test_ready_and_generate_blocked_during_warmup'),
    ('Метаданные сообщают новую версию, а отвечает старая модель',
     'Запустить сервис на A; опубликовать B в другом процессе; POST /generate',
     'До перезапуска ответ и model_id равны A; после перезапуска оба B',
     "Ответ от A, но model_id 'fixture-B'",
     'ServiceState.generate перечитывал accepted.json на каждый запрос вместо данных загруженного backend',
     HTTP + 'test_reported_version_is_the_loaded_one_through_a_b_a'),
    ('Неверный запрос запускает вычисление',
     'POST /generate с пустым prompt, max_new_tokens 0, 100000 или строкой; сравнить /debug.calls',
     '4xx и неизменное число вызовов',
     '200, /debug.calls растёт на каждый запрос',
     'В service.py не было проверки запроса перед backend.generate, лимит протокола не применялся',
     HTTP + 'test_bad_requests_get_4xx_without_backend_call'),
]
with Path('defects.csv').open('w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f, lineterminator='\n')
    writer.writerow(['id', 'symptom', 'reproduce', 'expected', 'observed', 'root_cause', 'fix_commit', 'regression_test', 'before_log', 'after_log'])
    for number, (symptom, reproduce, expected, observed, cause, test) in enumerate(ROWS, 1):
        writer.writerow([number, symptom, reproduce, expected, observed, cause, FIX, test, RED, GREEN])
