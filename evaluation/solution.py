"""Реализация двух функций контракта оценки. Контракт и примеры: EVALUATION.md и tests/test_eval.py."""
import json
import math

STATS_KEYS = ('n', 'correct', 'valid', 'errors', 'accuracy', 'format_rate')
REPORT_KEYS = {'overall', 'slices', 'per_item', 'model', 'protocol_sha256',
               'golden_sha256', 'baseline_spec_sha256', 'status', 'behavioral'}
HASH_KEYS = ('protocol_sha256', 'golden_sha256', 'baseline_spec_sha256')


def _parse_answer(raw, choices):
    """Строгий JSON с единственным строковым полем answer из choices.

    Markdown-ограждения, лишние поля, неверный тип значения и значения
    не из choices — всё невалидно. Возвращает (parsed, valid).
    """
    try:
        obj = json.loads(raw) if raw else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, False
    if not isinstance(obj, dict) or set(obj.keys()) != {'answer'}:
        return None, False
    answer = obj['answer']
    if not isinstance(answer, str) or answer not in choices:
        return None, False
    return answer, True


def _aggregate(items):
    """n/correct/valid/errors/accuracy/format_rate по уже размеченным per_item.

    Ни одна строка не выбрасывается: ошибки и невалидные ответы остаются
    в знаменателе (n), просто не попадают в correct/valid.
    """
    n = len(items)
    correct = sum(1 for it in items if it['correct'])
    valid = sum(1 for it in items if it['valid'])
    errors = sum(1 for it in items if it['error'])
    return {
        'n': n,
        'correct': correct,
        'valid': valid,
        'errors': errors,
        'accuracy': correct / n if n else 0.0,
        'format_rate': valid / n if n else 0.0,
    }


def score(rows, outputs):
    """Верните overall, slices и per_item. Не удаляйте плохие ответы из знаменателя.

    Строгий ответ: JSON с единственным строковым полем answer из choices.
    outputs: [{id, raw, error, latency_seconds}, ...].
    В overall и каждом срезе: n, correct, valid, errors, accuracy, format_rate.
    В per_item: id, raw, parsed, valid, correct, error, slices.
    Несовпадающие/дублирующиеся id и пустой набор должны вызвать ValueError.
    """
    if not rows or not outputs:
        raise ValueError('Пустой набор вопросов или ответов')
    row_ids = [r['id'] for r in rows]
    out_ids = [o['id'] for o in outputs]
    if len(set(row_ids)) != len(row_ids):
        raise ValueError('Дублирующиеся id в rows')
    if len(set(out_ids)) != len(out_ids):
        raise ValueError('Дублирующиеся id в outputs')
    if set(row_ids) != set(out_ids):
        raise ValueError('id outputs не совпадают с id rows: ошибка контракта')

    by_id = {o['id']: o for o in outputs}
    per_item = []
    for row in rows:
        out = by_id[row['id']]
        parsed, valid = _parse_answer(out.get('raw'), row['choices'])
        correct = bool(valid and parsed == row['expected'])
        per_item.append({
            'id': row['id'],
            'raw': out.get('raw'),
            'parsed': parsed,
            'valid': valid,
            'correct': correct,
            'error': out.get('error'),
            'slices': list(row['slices']),
        })

    overall = _aggregate(per_item)
    slice_names = sorted({s for row in rows for s in row['slices']})
    slices = {name: _aggregate([it for it in per_item if name in it['slices']]) for name in slice_names}
    return {'overall': overall, 'slices': slices, 'per_item': per_item}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _check_stats(stats, label):
    """Структура, конечность значений, отсутствие ошибок generation и
    соответствие accuracy/format_rate полям correct/valid/n (defense against
    отчётов, в которых числа поправили руками отдельно от n)."""
    _require(isinstance(stats, dict) and set(STATS_KEYS) <= set(stats), f'{label}: неполные метрики')
    n = stats['n']
    _require(isinstance(n, int) and n > 0, f'{label}: пустой набор')
    for key in ('accuracy', 'format_rate'):
        _require(isinstance(stats[key], (int, float)) and math.isfinite(stats[key]), f'{label}: {key} не конечно')
    _require(abs(stats['accuracy'] - stats['correct'] / n) < 1e-9, f'{label}: accuracy не соответствует correct/n')
    _require(abs(stats['format_rate'] - stats['valid'] / n) < 1e-9, f'{label}: format_rate не соответствует valid/n')
    _require(stats['errors'] == 0, f'{label}: в generation есть ошибки')


def quality_gate(baseline, candidate, critical_slices):
    """Верните {passed: True, ...} или вызовите ValueError с причиной отказа.

    Проверьте совместимость протокола/набора/baseline, метрики и counts,
    все id и срезы, общую accuracy/format_rate, критические срезы и behavior.
    Missing, NaN, пустой набор и ошибки генерации означают отказ.
    Пороги задаются до измерения; учебный допуск регрессии равен нулю.
    """
    for label, report in (('baseline', baseline), ('candidate', candidate)):
        _require(isinstance(report, dict) and REPORT_KEYS <= set(report), f'{label}: отчёт неполон')
        _require(report.get('status') == 'complete', f'{label}: статус не complete')

    for key in HASH_KEYS:
        _require(baseline[key] == candidate[key], f'Несовместимые отчёты: {key} различается')

    _check_stats(baseline['overall'], 'baseline.overall')
    _check_stats(candidate['overall'], 'candidate.overall')
    _require(baseline['overall']['n'] >= 50, 'golden меньше 50 примеров')
    _require(len(baseline['slices']) >= 3, 'Меньше трёх срезов golden')

    base_ids = {it['id'] for it in baseline['per_item']}
    cand_ids = {it['id'] for it in candidate['per_item']}
    _require(base_ids == cand_ids, 'id baseline и candidate не совпадают')

    _require(set(baseline['slices']) == set(candidate['slices']), 'Имена срезов не совпадают')
    for name, b_stats in baseline['slices'].items():
        c_stats = candidate['slices'][name]
        _check_stats(b_stats, f'baseline.slices.{name}')
        _check_stats(c_stats, f'candidate.slices.{name}')
        _require(b_stats['n'] == c_stats['n'], f'Срез {name}: размер группы не совпадает')

    _require(candidate['overall']['accuracy'] >= baseline['overall']['accuracy'], 'Регрессия общей accuracy')
    _require(candidate['overall']['format_rate'] >= baseline['overall']['format_rate'], 'Регрессия format_rate')

    missing_critical = [s for s in critical_slices if s not in candidate['slices']]
    _require(not missing_critical, f'Критический срез отсутствует: {missing_critical}')
    for name in critical_slices:
        _require(candidate['slices'][name]['accuracy'] >= baseline['slices'][name]['accuracy'],
                  f'Регрессия в критическом срезе {name}')

    behavioral = candidate.get('behavioral')
    _require(isinstance(behavioral, dict) and 'overall' in behavioral, 'Нет behavioral-отчёта кандидата')
    b_overall = behavioral['overall']
    _check_stats(b_overall, 'candidate.behavioral.overall')
    _require(b_overall['n'] >= 3, 'Меньше трёх behavioral-кейсов')
    _require(b_overall['correct'] == b_overall['n'], 'Не все behavioral-кейсы прошли')

    return {
        'passed': True,
        'overall_accuracy': {'baseline': baseline['overall']['accuracy'], 'candidate': candidate['overall']['accuracy']},
        'overall_format_rate': {'baseline': baseline['overall']['format_rate'], 'candidate': candidate['overall']['format_rate']},
        'critical_slices': list(critical_slices),
        'behavioral_n': b_overall['n'],
    }
