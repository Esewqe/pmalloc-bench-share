"""Run the pinned, unmodified mimalloc-bench workloads serially."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


SUITE = Path('/suite')
WORK = SUITE / 'out/bench'
LIBRARIES = {
    'sys': None,
    'mi3': SUITE / 'extern/mi3/out/release/libmimalloc.so',
    'old': SUITE / 'extern/pmalloc/libpmalloc_old.so',
    'new': SUITE / 'extern/pmalloc/libpmalloc_new.so',
}
PROVIDERS = {
    'sys': 'libc.so.6',
    'mi3': 'libmimalloc.so',
    'old': 'libpmalloc_old.so',
    'new': 'libpmalloc_new.so',
}
SERIAL = ['cfrac', 'espresso', 'malloc-large']
THREADED = ['larson', 'mstress', 'rptest', 'alloc-test', 'cscratch',
            'cthrash', 'glibc-thread']
# Williams square: within every 4 consecutive runs each allocator takes each
# position once and directly follows each other allocator exactly once.
ORDERS = (
    ('old', 'new', 'sys', 'mi3'),
    ('new', 'mi3', 'old', 'sys'),
    ('mi3', 'sys', 'new', 'old'),
    ('sys', 'old', 'mi3', 'new'),
)
PMALLOC_FLAGS = (
    '-std=c++17 -O3 -DNDEBUG -fPIC -ftls-model=initial-exec '
    '-fvisibility=hidden -fno-builtin-malloc -fno-builtin-calloc '
    '-fno-builtin-realloc -fno-builtin-free -fno-builtin-strdup '
    '-fno-builtin-strndup -shared -pthread'
)


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n',
                    encoding='utf-8')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def clean_environment():
    environment = dict(os.environ)
    environment.pop('LD_PRELOAD', None)
    return environment


def kill_session(session):
    """SIGKILL every live process of a session.

    coreutils timeout moves each test into its own process group, so killing
    only the runner's process group would leave the test itself running.
    """
    for _ in range(50):
        victims = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / 'stat').read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            state, _, _, sid = stat[stat.rindex(')') + 2:].split()[:4]
            if int(sid) == session and state != 'Z':
                victims.append(int(entry.name))
        if not victims:
            return
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.02)


def run_process(command, *, cwd=None, environment=None, timeout):
    """Keep output, including on timeout, and leave no process of the run alive."""
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors='replace',
            start_new_session=True,
        )
    except OSError as error:
        return {'returncode': None, 'error': str(error), 'timed_out': False,
                'interrupted': False, 'stdout': '', 'stderr': ''}
    timed_out = False
    interrupted = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        timed_out = isinstance(error, subprocess.TimeoutExpired)
        interrupted = isinstance(error, KeyboardInterrupt)
        kill_session(process.pid)
        stdout, stderr = process.communicate()
    kill_session(process.pid)
    return {'returncode': process.returncode, 'timed_out': timed_out,
            'interrupted': interrupted, 'stdout': stdout, 'stderr': stderr}


def save_process(directory, stem, result):
    (directory / f'{stem}.stdout.log').write_text(result['stdout'], encoding='utf-8')
    (directory / f'{stem}.stderr.log').write_text(result['stderr'], encoding='utf-8')
    (directory / f'{stem}.log').write_text(
        result['stdout'] + result['stderr'], encoding='utf-8')
    return {key: value for key, value in result.items()
            if key not in ('stdout', 'stderr')}


def command_output(command):
    return subprocess.check_output(
        command, text=True, stderr=subprocess.STDOUT,
        env=clean_environment()).strip()


def commit(directory, marker):
    recorded = marker.read_text(encoding='utf-8').strip()
    actual = (command_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'])
              if (directory / '.git').exists() else recorded)
    if actual != recorded:
        raise RuntimeError(f'commit marker disagrees with checkout: {directory}')
    return actual


def selectors_for(procs, selected):
    return [selector for selector in selected
            if procs == 1 or selector not in SERIAL]


def expected_tests(procs, selectors):
    tests = []
    for selector in selectors:
        if selector in SERIAL or selector == 'glibc-thread':
            tests.append(selector)
        elif selector in ('larson', 'mstress', 'rptest'):
            tests.append(selector + 'N')
        else:
            name = {'alloc-test': 'alloc-test', 'cscratch': 'cache-scratch',
                    'cthrash': 'cache-thrash'}[selector]
            tests.append(name + '1')
            if procs > 1:
                tests.append(name + 'N')
    return tests


def actual_threads(test, procs):
    if test in SERIAL or test in ('alloc-test1', 'cache-scratch1', 'cache-thrash1'):
        return 1
    return min(procs, 16) if test == 'alloc-testN' else procs


def read_optional(path):
    try:
        return Path(path).read_text(encoding='utf-8').strip()
    except OSError:
        return None


def host_metadata(threads):
    """Record what governs allocator timings; warn about unstable CPU setups."""
    allowed = sorted(os.sched_getaffinity(0))
    cores = {}
    for cpu in allowed:
        topology = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
        key = (read_optional(topology / 'physical_package_id'),
               read_optional(topology / 'core_id'))
        cores.setdefault(key, []).append(cpu)
    topology_known = all(None not in key for key in cores)
    cpu_max = read_optional('/sys/fs/cgroup/cpu.max')
    quota = None
    if cpu_max and not cpu_max.startswith('max'):
        limit, period = cpu_max.split()
        quota = int(limit) / int(period)
    usable = len(allowed) if quota is None else min(len(allowed), quota)
    model = next((line.split(':', 1)[1].strip()
                  for line in (read_optional('/proc/cpuinfo') or '').splitlines()
                  if line.startswith('model name')), None)
    warnings = []
    if max(threads) > usable:
        warnings.append(f'{max(threads)} threads requested, but only {usable:g} CPUs are usable; '
                        'multithreaded rows measure oversubscription')
    siblings = [cpus for cpus in cores.values() if len(cpus) > 1]
    if topology_known and siblings and max(threads) > 1:
        warnings.append(f'allowed CPUs include SMT siblings (e.g. {",".join(map(str, siblings[0]))}); '
                        'for stable multithreaded results pin one CPU per physical core '
                        'with docker run --cpuset-cpus')
    return {
        'cpu_model': model,
        'logical_cpus': os.cpu_count(),
        'allowed_cpus': allowed,
        'allowed_cpus_by_core': sorted(cores.values()) if topology_known else None,
        'cgroup_cpu_max': cpu_max,
        'cgroup_memory_max': read_optional('/sys/fs/cgroup/memory.max'),
        'transparent_hugepage': read_optional('/sys/kernel/mm/transparent_hugepage/enabled'),
        'transparent_hugepage_defrag': read_optional('/sys/kernel/mm/transparent_hugepage/defrag'),
        'scaling_governor': read_optional('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'),
        'warnings': warnings,
    }


def collect_metadata(opts, output, groups):
    metadata = {
        'suite_commit': commit(SUITE, SUITE / 'suite-commit.txt'),
        'mimalloc_commit': commit(SUITE / 'extern/mi3', SUITE / 'mimalloc-commit.txt'),
        'mimalloc_version': '3.6.0',
        'pmalloc_headers': {
            alias: {'path': str(Path('/opt/allocators') / alias / 'pmalloc_posix.hpp'),
                    'sha256': sha256(Path('/opt/allocators') / alias / 'pmalloc_posix.hpp')}
            for alias in ('old', 'new')
        },
        'libraries': {
            alias: {'path': str(library), 'sha256': sha256(library)}
            for alias, library in LIBRARIES.items() if library is not None
        },
        'kernel': command_output(['uname', '-a']),
        'compiler': command_output(['g++', '--version']),
        'libc': command_output(['ldd', '--version']),
        'threads': opts.threads,
        'runs': opts.runs,
        'warmup': opts.warmup,
        'timeout_s': opts.timeout,
        'tests': opts.tests,
        'smoke_only': opts.smoke_only,
        'options': vars(opts),
        'output': str(output),
        'serial_selectors': SERIAL,
        'threaded_selectors': THREADED,
        'selectors_by_procs': {str(procs): selectors for procs, selectors in groups},
        'expected_tests_by_procs': {
            str(procs): expected_tests(procs, selectors) for procs, selectors in groups
        },
        'skipped_procs': [procs for procs in opts.threads
                          if not selectors_for(procs, opts.tests)],
        'expected_measured_rows': sum(len(expected_tests(procs, selectors))
                                      for procs, selectors in groups) * len(LIBRARIES) * opts.runs,
        'pmalloc_flags': PMALLOC_FLAGS,
        'pmalloc_defines': ['NDEBUG'],
        'pmalloc_stats': False,
        'smoke_flags': '-std=c++17 -O2 -pthread -fno-builtin -ldl',
        'runner': 'unmodified upstream bench.sh; fresh child process per test; serial execution',
        'metric': ('official time column: wall seconds for most tests; normalized '
                   'relative seconds for larson, rptest and glibc-thread'),
        'allocator_orders': [list(order) for order in ORDERS],
        'allocator_order_design': ('Williams square: run i of each thread group uses order '
                                   'i mod 4; warmups and measured runs are counted separately'),
        'allocator_order_balanced': opts.runs % len(ORDERS) == 0,
        'started_utc': datetime.now(timezone.utc).isoformat(),
    }
    metadata.update(host_metadata(opts.threads))
    if not metadata['allocator_order_balanced']:
        metadata['warnings'].append(f'--runs {opts.runs} is not a multiple of {len(ORDERS)}: '
                                    'allocator positions are not balanced')
    for source, target in (
        (SUITE / 'extern/mi3/out/release/CMakeCache.txt', 'mimalloc-CMakeCache.txt'),
        (WORK / 'CMakeCache.txt', 'suite-CMakeCache.txt'),
    ):
        shutil.copy2(source, output / target)
    return metadata


def external_arguments(output):
    source = SUITE / 'pmalloc-external.txt'
    shutil.copy2(source, output / source.name)
    entries = {}
    for line in source.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in ('old', 'new') or parts[0] in entries:
            raise RuntimeError(f'invalid external registration: {line!r}')
        entries[parts[0]] = parts[1]
    if entries != {alias: str(LIBRARIES[alias]) for alias in ('old', 'new')}:
        raise RuntimeError('external registrations do not match expected pmalloc libraries')
    arguments = {'sys': 'sys', 'mi3': 'mi3'}
    for alias in ('old', 'new'):
        path = output / f'external-{alias}.txt'
        path.write_text(f'{alias} {entries[alias]}\n', encoding='utf-8')
        arguments[alias] = f'--external={path}'
    return arguments


def smoke(output, metadata, statuses):
    succeeded = True
    for alias in ('sys', 'mi3', 'old', 'new'):
        environment = clean_environment()
        if LIBRARIES[alias] is not None:
            environment['LD_PRELOAD'] = str(LIBRARIES[alias])
        command = ['/opt/allocator-smoke']
        result = run_process(command, environment=environment, timeout=30)
        status = save_process(output, f'smoke-{alias}', result)
        providers = [line.removeprefix('malloc provider: ').strip()
                     for line in result['stdout'].splitlines()
                     if line.startswith('malloc provider: ')]
        provider_ok = len(providers) == 1 and Path(providers[0]).name == PROVIDERS[alias]
        status.update({'kind': 'smoke', 'allocator': alias, 'command': command,
                       'malloc_providers': providers, 'provider_ok': provider_ok})
        status['success'] = (result['returncode'] == 0 and provider_ok
                             and not result['timed_out'] and not result['interrupted'])
        statuses.append(status)
        save_json(output / 'run_status.json', statuses)
        print(f'SMOKE {alias}: {"PASS" if status["success"] else "FAIL"}', flush=True)
        if len(providers) == 1 and Path(providers[0]).is_file():
            metadata['libraries'].setdefault(alias, {}).update({
                'malloc_provider': providers[0],
                'provider_sha256': sha256(Path(providers[0])),
            })
        save_json(output / 'environment.json', metadata)
        succeeded = succeeded and status['success']
        if result['interrupted']:
            raise KeyboardInterrupt
    return succeeded


def parse_rows(path):
    rows, errors = [], []
    if not path.is_file():
        return rows, ['official benchres.csv is missing']
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        parts = line.split()
        try:
            if len(parts) != 8:
                raise ValueError('expected eight columns')
            row = {
                'test': parts[0], 'allocator': parts[1],
                'time_metric_s': float(parts[2]), 'max_rss_kib': int(parts[3]),
                'user_s': float(parts[4]), 'system_s': float(parts[5]),
                'major_faults': int(parts[6]), 'minor_faults': int(parts[7]),
            }
            if not all(math.isfinite(row[key])
                       for key in ('time_metric_s', 'user_s', 'system_s')):
                raise ValueError('nonfinite metric')
            if row['time_metric_s'] <= 0 or row['max_rss_kib'] <= 0:
                raise ValueError('nonpositive time or RSS metric')
            if any(row[key] < 0 for key in ('user_s', 'system_s', 'major_faults', 'minor_faults')):
                raise ValueError('negative resource metric')
            rows.append(row)
        except ValueError as error:
            errors.append(f'line {number}: {error}: {line}')
    return rows, errors


def official_run(output, procs, selectors, phase, measured, order, arguments, timeout):
    directory = output / f'procs{procs}' / phase
    directory.mkdir(parents=True)
    for pattern in ('benchres*', '*-out.txt*'):
        for path in WORK.glob(pattern):
            if path.is_file():
                path.unlink()
    command = ['bash', '../../bench.sh', f'--procs={procs}', f'-T={timeout}',
               '-r=1', '-n=1', *(arguments[alias] for alias in order), *selectors]
    tests = expected_tests(procs, selectors)
    # Upstream enforces the per-test limit. This guard also bounds shell/helper hangs.
    invocation_limit = len(tests) * len(LIBRARIES) * (timeout + 10) + 60
    print('RUN', ' '.join(command), f'phase={phase}', flush=True)
    result = run_process(command, cwd=WORK, environment=clean_environment(),
                         timeout=invocation_limit)
    status = save_process(directory, 'runner', result)
    for pattern in ('benchres*', '*-out.txt*'):
        for path in WORK.glob(pattern):
            if path.is_file():
                shutil.copy2(path, directory / path.name)
    rows, errors = parse_rows(directory / 'benchres.csv')
    for row in rows:
        row.update({'requested_procs': procs, 'phase': phase, 'measured': measured,
                    'actual_threads': actual_threads(row['test'], procs)})
    counts = Counter((row['test'], row['allocator']) for row in rows)
    expected = {(test, alias) for test in tests for alias in LIBRARIES}
    actual = set(counts)
    status.update({
        'kind': 'benchmark', 'procs': procs, 'requested_procs': procs,
        'phase': phase, 'measured': measured, 'command': command,
        'allocator_order': order, 'selectors': selectors, 'rows': len(rows),
        'missing_results': sorted(expected - actual),
        'unexpected_results': sorted(actual - expected),
        'duplicate_results': [[test, alias, count]
                              for (test, alias), count in sorted(counts.items()) if count != 1],
        'invalid_results': errors, 'invocation_timeout_s': invocation_limit,
    })
    status['success'] = (
        result['returncode'] == 0 and not result['timed_out'] and not result['interrupted']
        and not any(status[key] for key in ('missing_results', 'unexpected_results',
                                           'duplicate_results', 'invalid_results'))
    )
    return rows, status


def parse_options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='new directory; default: /results/run-<UTC timestamp>')
    parser.add_argument('--threads', nargs='+', type=int, default=[1, 2, 4, 8])
    parser.add_argument('--runs', type=int, default=4,
                        help='measured runs per configuration; multiples of 4 balance allocator order')
    parser.add_argument('--warmup', type=int, default=0)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--smoke-only', action='store_true')
    parser.add_argument('--tests', nargs='+', choices=SERIAL + THREADED,
                        default=SERIAL + THREADED)
    opts = parser.parse_args()
    if opts.runs <= 0 or opts.timeout <= 0 or any(procs <= 0 for procs in opts.threads):
        parser.error('runs, timeout and every thread count must be positive')
    if opts.warmup < 0:
        parser.error('warmup must be nonnegative')
    if len(set(opts.threads)) != len(opts.threads) or len(set(opts.tests)) != len(opts.tests):
        parser.error('thread counts and tests must not contain duplicates')
    groups = [(procs, selectors_for(procs, opts.tests)) for procs in opts.threads]
    groups = [(procs, selectors) for procs, selectors in groups if selectors]
    if not groups:
        parser.error('no workloads selected: cfrac, espresso and malloc-large require --threads including 1')
    return parser, opts, groups


def main():
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    parser, opts, groups = parse_options()
    output = (Path(opts.output) if opts.output else
              Path('/results') / datetime.now(timezone.utc).strftime('run-%Y%m%dT%H%M%S.%fZ'))
    output = output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        parser.error(f'cannot create fresh output directory {output}: {error}')
    records, statuses = [], []
    metadata = {'options': vars(opts), 'output': str(output), 'status': 'initializing'}
    save_json(output / 'environment.json', metadata)
    save_json(output / 'raw_results.json', records)
    save_json(output / 'run_status.json', statuses)
    print(f'OUTPUT {output}', flush=True)
    try:
        metadata = collect_metadata(opts, output, groups)
        metadata['status'] = 'running'
        save_json(output / 'environment.json', metadata)
        if not opts.smoke_only:
            for warning in metadata['warnings']:
                print(f'WARNING: {warning}', flush=True)
        arguments = external_arguments(output)
        if not smoke(output, metadata, statuses):
            raise RuntimeError('allocator API smoke failed; see smoke logs and run_status.json')
        failed_runs = []
        if not opts.smoke_only:
            for procs, selectors in groups:
                for index in range(-opts.warmup, opts.runs):
                    phase = f'warmup{-index:02}' if index < 0 else f'run{index + 1:02}'
                    order = ORDERS[index % len(ORDERS)]
                    rows, status = official_run(output, procs, selectors, phase,
                                                index >= 0, order, arguments, opts.timeout)
                    records.extend(rows)
                    statuses.append(status)
                    save_json(output / 'raw_results.json', records)
                    save_json(output / 'run_status.json', statuses)
                    print(f'DONE procs={procs} phase={phase} rows={len(rows)} '
                          f'success={status["success"]}', flush=True)
                    if status['interrupted']:
                        raise KeyboardInterrupt
                    if not status['success']:
                        failed_runs.append(f'procs{procs}/{phase}')
            measured = sum(row['measured'] for row in records)
            if not failed_runs and measured != metadata['expected_measured_rows']:
                raise RuntimeError(f'incorrect measured row count: {measured}')
            import report
            report.generate(output, records, metadata)
        metadata['finished_utc'] = datetime.now(timezone.utc).isoformat()
        if failed_runs:
            metadata['status'] = 'incomplete'
            metadata['error'] = (f'{len(failed_runs)} benchmark invocation(s) failed: '
                                 f'{", ".join(failed_runs)}; see run_status.json')
            statuses.append({'kind': 'runner', 'success': False, 'error': metadata['error']})
            save_json(output / 'run_status.json', statuses)
            save_json(output / 'environment.json', metadata)
            print(f'ERROR: {metadata["error"]}; partial report: {output}',
                  file=sys.stderr, flush=True)
            return 1
        metadata['status'] = 'complete'
        save_json(output / 'environment.json', metadata)
        print(f'Saved {len(records)} official result rows to {output}', flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        metadata['status'] = 'failed'
        metadata['error'] = ('interrupted by SIGINT/SIGTERM' if isinstance(error, KeyboardInterrupt)
                             else str(error) or type(error).__name__)
        metadata['finished_utc'] = datetime.now(timezone.utc).isoformat()
        statuses.append({'kind': 'runner', 'success': False, 'error': metadata['error']})
        save_json(output / 'environment.json', metadata)
        save_json(output / 'raw_results.json', records)
        save_json(output / 'run_status.json', statuses)
        print(f'ERROR: {metadata["error"]}; evidence: {output}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
