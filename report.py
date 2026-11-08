"""Summarize upstream per-process measurements without mixing test metrics."""
import csv
import json
from pathlib import Path
import statistics

ALLOCATORS = ('old', 'new', 'mi3', 'sys')
WORKLOADS = (
    'alloc-test', 'rptest', 'larson', 'mstress', 'glibc-thread',
    'cscratch', 'cthrash', 'cfrac', 'espresso', 'malloc-large',
)
LABELS = {'larson': 'Larson', 'cscratch': 'cache-scratch', 'cthrash': 'cache-thrash'}
NAMES = {'larson': 'larsonN', 'rptest': 'rptestN', 'mstress': 'mstressN'}
PAIRS = (('new', 'old'), ('new', 'mi3'), ('old', 'mi3'), ('new', 'sys'))
MATRICES = (
    ('new', 'old', 'New pmalloc / old pmalloc'),
    ('new', 'mi3', 'New pmalloc / mimalloc 3.6.0'),
    ('old', 'mi3', 'Old pmalloc / mimalloc 3.6.0'),
)
# GNU time %e has 0.01 s resolution; a wall-time ratio is coarse when one
# step is at least 1% of either median.
TIME_RESOLUTION_S = 0.01
COARSE_FRACTION = 0.01
SAMPLE_FIELDS = ('median', 'min', 'max', 'median_peak_rss_mib')


def metric_kind(test):
    if test == 'larsonN':
        return 'normalized_larson'
    if test == 'rptestN':
        return 'normalized_rptest'
    if test == 'glibc-thread':
        return 'normalized_glibc_thread'
    return 'wall_seconds'


def test_name(workload, procs):
    base = {'alloc-test': 'alloc-test', 'cscratch': 'cache-scratch', 'cthrash': 'cache-thrash'}.get(workload)
    if base:
        return base + ('1' if procs == 1 else 'N')
    return NAMES.get(workload, workload)


def summarize(rows):
    times = [row['time_metric_s'] for row in rows]
    return {'median': statistics.median(times), 'min': min(times), 'max': max(times),
            'median_peak_rss_mib': statistics.median(row['max_rss_kib'] for row in rows) / 1024}


def compare(case, numerator, denominator, runs):
    """Ratio of medians plus the two reasons not to trust a small difference."""
    coarse = (case['metric_kind'] == 'wall_seconds' and TIME_RESOLUTION_S
              >= COARSE_FRACTION * min(case[numerator + '_median'], case[denominator + '_median']))
    overlap = None
    if runs > 1:
        overlap = not (case[numerator + '_max'] < case[denominator + '_min']
                       or case[denominator + '_max'] < case[numerator + '_min'])
    return case[numerator + '_median'] / case[denominator + '_median'], coarse, overlap


def generate(output: Path, records: list, metadata: dict):
    runs = metadata['runs']
    samples = {}
    for row in records:
        if row['measured']:
            key = (row['requested_procs'], row['test'], row['allocator'])
            samples.setdefault(key, []).append(row)
    cases = []
    for procs_key, tests in metadata['expected_tests_by_procs'].items():
        procs = int(procs_key)
        for test in tests:
            case = {'requested_procs': procs, 'test': test, 'metric_kind': metric_kind(test)}
            complete = True
            for allocator in ALLOCATORS:
                rows = samples.get((procs, test, allocator), [])
                case[allocator + '_samples'] = len(rows)
                if len(rows) == runs:
                    values = summarize(rows)
                else:
                    complete = False
                    values = dict.fromkeys(SAMPLE_FIELDS)
                for field in SAMPLE_FIELDS:
                    case[f'{allocator}_{field}'] = values[field]
            case['complete'] = complete
            for numerator, denominator in PAIRS:
                ratio_key = f'{numerator}_over_{denominator}'
                ratio, coarse, overlap = (compare(case, numerator, denominator, runs)
                                          if complete else (None, None, None))
                case[ratio_key] = ratio
                case[ratio_key + '_coarse'] = coarse
                case[ratio_key + '_ranges_overlap'] = overlap
            cases.append(case)
    cases.sort(key=lambda case: (case['requested_procs'], case['test']))
    incomplete = [f"{case['requested_procs']}/{case['test']}" for case in cases if not case['complete']]
    summary = {
        'measured_rows': sum(row['measured'] for row in records),
        'expected_measured_rows': metadata['expected_measured_rows'],
        'case_count': len(cases),
        'incomplete_cases': incomplete,
        'interpretation': (
            'Within-test ratios of medians; below 1 favors numerator. No overall score. '
            '*_coarse: one GNU time step (0.01 s) is at least 1% of a median. '
            '*_ranges_overlap: the min-max ranges overlap, so the difference is within '
            'run-to-run spread (null with one run). Incomplete cases have no ratios. '
            'RSS is process high-water mark, not retained memory.'),
        'cases': cases,
    }
    (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    with (output / 'summary.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(cases[0]))
        writer.writeheader()
        writer.writerows(cases)

    by_case = {(case['requested_procs'], case['test']): case for case in cases}
    threads = [n for n in metadata['threads'] if str(n) in metadata['expected_tests_by_procs']]
    names = [name for name in WORKLOADS if any((n, test_name(name, n)) in by_case for n in threads)]
    order = ('balanced (Williams square)' if metadata['allocator_order_balanced']
             else f'not balanced, {runs} run(s) is not a multiple of 4')
    text = [
        '# Allocator comparison', '',
        'Ratios of median test metrics. Below 1 favors the numerator; above 1 favors the denominator.',
        'Larson, rptest and glibc-thread use normalized inverse-throughput metrics, not whole-program wall time.', '',
        f"Measured rows: {summary['measured_rows']} of {summary['expected_measured_rows']}; "
        f'configurations: {len(cases)}, incomplete: {len(incomplete)}; runs per allocator: {runs}; '
        f'allocator order: {order}.', '',
    ]
    matrices = {}
    for numerator, denominator, title in MATRICES:
        ratio_key = f'{numerator}_over_{denominator}'
        text.extend([f'## {title}', '', '| Test | ' + ' | '.join(str(n) for n in threads) + ' |', '|---|' + '---:|' * len(threads)])
        matrix_rows = []
        for name in names:
            cells = []
            matrix_row = {'test': LABELS.get(name, name)}
            for n in threads:
                case = by_case.get((n, test_name(name, n)))
                if case is None:
                    cells.append('—')
                    matrix_row[str(n)] = None
                elif not case['complete']:
                    cells.append('×')
                    matrix_row[str(n)] = 'incomplete'
                else:
                    ratio = case[ratio_key]
                    cells.append(('≈' if case[ratio_key + '_ranges_overlap'] else '')
                                 + f'{ratio:.2f}' + ('*' if case[ratio_key + '_coarse'] else ''))
                    matrix_row[str(n)] = ratio
            text.append('| ' + LABELS.get(name, name) + ' | ' + ' | '.join(cells) + ' |')
            matrix_rows.append(matrix_row)
        text.append('')
        matrices[ratio_key] = matrix_rows
        with (output / f'{ratio_key}.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['test', *map(str, threads)])
            writer.writeheader()
            writer.writerows(matrix_rows)
    text.extend([
        'Legend:', '',
        '- `≈` before a ratio: the min–max ranges of the two allocators overlap; '
        'the difference is within run-to-run spread.',
        f'- `*` after a ratio: wall time of at most {TIME_RESOLUTION_S / COARSE_FRACTION:g} s; '
        f'one GNU time step ({TIME_RESOLUTION_S:g} s) is at least 1% of a median.',
        '- `—`: not run for this thread count.',
        '- `×`: incomplete; a run failed or a sample is missing, see run_status.json.',
    ])
    if runs == 1:
        text.append('- One run per allocator: spread is unknown, so `≈` is not computed.')
    text.extend([
        '',
        'Serial control rows repeated in groups >1 are included in summary.csv/json, not shown as multithreaded runs.',
        f'Runs per allocator: {runs}. This does not establish statistical significance; '
        'machine load, scheduling and CPU frequency affect results.', '',
    ])
    markdown = '\n'.join(text)
    (output / 'summary.md').write_text(markdown, encoding='utf-8')
    (output / 'matrices.json').write_text(json.dumps(matrices, indent=2, ensure_ascii=False), encoding='utf-8')
    print(markdown, flush=True)
