#!/usr/bin/env python3
"""Synthetic-only regression tests; never access a real device or user repository."""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(os.environ.get('BENCHMARK_TEST_SCRIPT', str(Path(__file__).parent / 'utils/run_siq_benchmark.py'))).resolve()
spec = importlib.util.spec_from_file_location('benchmark', SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

HEADERS = ('time,dma_cur,rss_cur,dma_max_by_sampling,rss_max_by_sampling,'
           'rss_max_by_getrusage,max_of_dma_add_rss_gpumap_by_sampling,'
           'cur_of_dma_add_rss_gpumap_by_sampling,gpu_mmap_cur,gpu_mmap_max_by_sampling')
NATIVE = '''Memory Usage Summary
DMA Max (sampling): 10.00 MB
RSS Max (sampling): 20.00 MB
GPU mmap Max (sampling): 2.00 MB
RSS Max (getrusage): 22.00 MB
DMA+RSS+GPU mmap Max (sampling): 22.00 MB
Sampling Period: 5 ms
'''


class MeasurementTest(unittest.TestCase):
    def test_duplicate_adb_serial_requires_transport_and_pins_commands(self):
        devices = ('List of devices attached\n'
                   'same device product:first model:first transport_id:1\n'
                   'same device product:second model:second transport_id:2\n')
        replies = [devices, devices, 'same', 'chip', 'second', '17',
                   devices, 'same', 'chip', 'second', '17']
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, replies.pop(0), '')

        with mock.patch.object(b.subprocess, 'run', side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, '--transport-id'):
                b.collect_device('adb', {'ANDROID_SERIAL': 'same'})
            self.assertEqual(2, b.collect_device('adb', {'ANDROID_SERIAL': 'same'}, 2)['transport_id'])
            self.assertEqual(2, b.collect_device('adb', {'ANDROID_SERIAL': 'product:second'})['transport_id'])
        self.assertTrue(all(command[1:3] == ['-t', '2'] for command in calls[2:6] + calls[7:]))

        with mock.patch.dict(os.environ, {'_BENCH_REAL_ADB': '/real/adb',
                                          '_BENCH_TRANSPORT_ID': '2'}), \
             mock.patch.object(b.os, 'execv') as execv:
            b.tool_wrapper_main('adb', ['shell', 'true'])
        execv.assert_called_once_with('/real/adb', ['/real/adb', '-t', '2', 'shell', 'true'])

    def csv_file(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'samples.csv'
        path.write_text(text)
        return path

    def full_csv(self):
        return self.csv_file(HEADERS + '\n' +
            '100,10,1,10,1,22,11,11,0,0\n'
            '116,1,20,10,20,22,22,22,1,1\n'
            '132,1,1,10,20,22,22,4,2,2\n')

    def test_joint_peak_and_current_mean_not_sum_or_cumulative_mean(self):
        memory, sampling = b.memory_measurement(self.full_csv(), NATIVE)
        self.assertEqual(memory['joint']['sampled_peak']['value'], 22)
        self.assertEqual(memory['dma']['sample_mean']['value'], 4)
        self.assertEqual(memory['rss']['sample_mean']['value'], 22 / 3)
        self.assertEqual(sampling['configured_period_ms'], 5)
        self.assertEqual(sampling['actual_interval_ms']['median'], 16)
        self.assertEqual(sampling['coverage']['status'], 'unverified')
        self.assertEqual(memory['joint']['sampled_peak']['unit'], 'MB')

    def test_missing_is_not_zero(self):
        path = self.csv_file('time,rss_cur\n1,0\n2,0\n')
        memory, sampling = b.memory_measurement(path, '')
        self.assertIsNone(memory['dma']['sampled_peak']['value'])
        self.assertEqual(memory['rss']['sampled_peak']['value'], 0)
        self.assertIn('dma_cur', sampling['missing_columns'])

    def test_rounding_and_mismatch(self):
        b.memory_measurement(self.full_csv(), NATIVE.replace('10.00', '10.004'))
        with self.assertRaises(RuntimeError):
            b.memory_measurement(self.full_csv(), NATIVE.replace('10.00', '19.00'))

    def test_bad_samples(self):
        for rows in ('2,1\n1,1\n', '1,nan\n2,1\n', '1,inf\n2,1\n', '1,1\n'):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                b.memory_measurement(self.csv_file('time,dma_cur\n' + rows), '')

    def test_duplicates_and_unknown_columns(self):
        path = self.csv_file('time,dma_cur,new_metric\n1,1,3\n1,1,3\n17,2,4\n')
        _, sampling = b.memory_measurement(path, '')
        self.assertEqual(sampling['duplicate_timestamps'], 1)
        self.assertEqual(sampling['unknown_columns'], ['new_metric'])

    def test_leave_coverage_and_marker_fallback(self):
        text = 'leave process_image 0.123ms\n' + '@@PROCESS_IMAGE_PERFORMANCE@@: 1\n' * 2
        result = b.structured_performance(text, 'process_image,missing', 2)
        self.assertEqual(result['process_image']['source'], 'native_outer_marker_ms')
        self.assertEqual(result['process_image']['statistics']['count'], 2)
        self.assertEqual(result['missing']['status'], 'missing')
        text += 'leave process_image 0.456ms\n'
        result = b.structured_performance(text, 'process_image', 2)
        self.assertEqual(result['process_image']['source'], 'native_leave_ms')
        self.assertAlmostEqual(result['process_image']['statistics']['all_mean_ms'], .2895)

    def test_lifecycle_order_and_scope_are_verified(self):
        text = ('@@SIQ_BENCH_BEGIN@@ mode=1 pid=42 iteration=1\n'
                '@@PROCESS_IMAGE_PERFORMANCE@@: 1\n'
                '@@SIQ_BENCH_END@@ mode=1 pid=42 iteration=1 status=0\n')
        self.assertEqual(b.validate_run(text, 1, 1)['pid'], 42)
        outside = text.replace('@@PROCESS_IMAGE_PERFORMANCE@@: 1\n', '') + '@@PROCESS_IMAGE_PERFORMANCE@@: 1\n'
        with self.assertRaises(RuntimeError):
            b.validate_run(outside, 1, 1)

    def test_native_invalid_values_and_bad_cumulative_peak(self):
        for value in ('nan', 'inf', '-1'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                b.memory_measurement(self.full_csv(), NATIVE.replace('10.00', value))
        path = self.csv_file('time,dma_cur,dma_max_by_sampling\n1,2,1\n2,3,1\n')
        with self.assertRaises(RuntimeError):
            b.memory_measurement(path, '')

    def test_invalid_json_cli_does_not_echo_arguments(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.main(['--json', '--unsupported', 'ARGUMENT_SENTINEL'])
        self.assertEqual(code, 1)
        self.assertNotIn('ARGUMENT_SENTINEL', stdout.getvalue() + stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'failed')

    def test_json_failure_is_one_object_and_no_raw_exception(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.main(['--json'])
        self.assertNotEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['measurement_valid'])
        self.assertIn('schema_version', result)


def sampling_fixture(interval=5, count=21, coverage='unverified'):
    return {'configured_period_ms': 5, 'sample_count': count, 'rows': count,
            'duration_ms': interval * (count - 1), 'start_ms': 100,
            'end_ms': 100 + interval * (count - 1),
            'actual_interval_ms': dict(min=interval, median=interval, mean=interval, max=interval),
            'coverage': {'status': coverage}}


def valid_result(value=10):
    result = b.new_result()
    result.update({'status': 'ok', 'exit_code': 0, 'measurement_valid': True})
    result['validity']['deployment'] = 'verified'
    result['experiment'] = dict(project='project/adapter', adapter='adapter', platform='mtk',
        debug_mode=0, mode=2, actual_repeat=2, input_frames=1,
        input_digest='synthetic', instrumentation='memory_sampling')
    result['device'] = {'serial': 'fixture-device'}
    result['sampling'] = sampling_fixture()
    result['source'] = dict(head='fixture-head', fingerprint=dict(index='i', staged='s', unstaged='u'), submodules={})
    result['deployment'] = {'sha256': {'siq_test': 'fixture-hash'}}
    result['experiment']['full_build_invocation_observed'] = True
    result['performance'] = {'process_image': {'source': 'native_leave_ms', 'status': 'ok',
        'coverage': 'complete', 'samples': [value, value], 'iteration_counts': [1, 1],
        'iteration_composition': [['process_image'], ['process_image']], 'calls_per_iteration': 1,
        'statistics': b.sample_statistics([value, value])}}
    result['memory'] = {'dma': {'sampled_peak': b.memory_value(value, 'csv:dma_max_by_sampling')}}
    return result


class ComparisonTest(unittest.TestCase):
    def test_direction_zero_baseline_and_missing(self):
        result = b.compare_results(valid_result(0), valid_result(4))
        metric = result['metrics'][0]
        self.assertEqual(metric['delta'], 4)
        self.assertIsNone(metric['delta_percent'])
        self.assertTrue(any(m['status'] == 'missing' for m in result['metrics']))

    def test_detected_identical_deployment_without_build_is_not_comparable(self):
        left, right = valid_result(), valid_result()
        left['source'], right['source'] = {'head': 'left'}, {'head': 'right'}
        left['deployment'] = right['deployment'] = {'sha256': {'siq_test': 'same fixture hash'}}
        right['experiment']['full_build_invocation_observed'] = False
        result = b.compare_results(left, right)
        self.assertFalse(result['checks']['no_detected_unbuilt_reuse'])
        self.assertEqual(result['metrics'][0]['status'], 'not_comparable')

    def test_same_head_dirty_source_reuse_is_rejected(self):
        left, right = valid_result(), valid_result(1)
        right['source']['fingerprint']['unstaged'] = 'dirty-B'
        right['experiment']['full_build_invocation_observed'] = False
        result = b.compare_results(left, right)
        self.assertFalse(result['checks']['no_detected_unbuilt_reuse'])
        self.assertTrue(all(row['delta'] is None for row in result['metrics']))

    def test_actual_submodule_head_and_fingerprint_reuse_is_rejected(self):
        for field, value in (('head', 'actual-other'), ('fingerprint', dict(index='i', staged='s', unstaged='dirty'))):
            left, right = valid_result(), valid_result()
            for side in (left, right):
                side['source']['submodules'] = {'module/nested': dict(head='actual', branch=None,
                    gitlink='declared', fingerprint=dict(index='i', staged='s', unstaged='u'))}
            right['source']['submodules']['module/nested'][field] = value
            right['experiment']['full_build_invocation_observed'] = False
            with self.subTest(field=field):
                result = b.compare_results(left, right)
                self.assertFalse(result['checks']['no_detected_unbuilt_reuse'])
                self.assertIsNone(result['metrics'][0]['delta_percent'])

    def test_branch_and_detached_state_are_not_source_changes(self):
        left, right = valid_result(), valid_result(1)
        left['source']['branch'], right['source']['branch'] = None, 'refs/heads/current'
        for side in (left, right):
            side['source']['submodules'] = {'module': dict(head='actual', branch=None,
                fingerprint=dict(index='i', staged='s', unstaged='u'))}
        right['source']['submodules']['module']['branch'] = 'refs/heads/module'
        right['experiment']['full_build_invocation_observed'] = False
        result = b.compare_results(left, right)
        self.assertTrue(result['checks']['no_detected_unbuilt_reuse'])
        self.assertEqual(result['metrics'][0]['delta_percent'], -90)

    def test_missing_source_or_build_metadata_is_unverified(self):
        for section, field in (('source', 'fingerprint'), ('source', 'submodules'),
                               ('deployment', 'sha256'), ('experiment', 'full_build_invocation_observed')):
            left, right = valid_result(), valid_result()
            del right[section][field]
            with self.subTest(section=section, field=field):
                result = b.compare_results(left, right)
                self.assertIsNone(result['checks']['no_detected_unbuilt_reuse'])
                self.assertEqual(result['build_provenance']['unbuilt_reuse'], 'unverified')
                self.assertIsNone(result['metrics'][0]['delta'])

    def test_missing_actual_module_identity_is_unverified(self):
        for module in ({'head': 'actual'}, {'initialization': 'uninitialized', 'head': None}):
            left, right = valid_result(), valid_result()
            left['source']['submodules'] = {'module': dict(head='actual', fingerprint=dict(index='i', staged='s', unstaged='u'))}
            right['source']['submodules'] = {'module': module}
            with self.subTest(module=module):
                result = b.compare_results(left, right)
                self.assertIsNone(result['checks']['no_detected_unbuilt_reuse'])
                self.assertEqual(result['build_provenance']['source_identity'], 'unverified')
                self.assertIsNone(result['metrics'][0]['delta_percent'])

    def test_sources_config_device_input_are_hard_checks(self):
        for field in ('project', 'adapter', 'platform', 'debug_mode', 'input_digest', 'actual_repeat'):
            left, right = valid_result(), valid_result()
            right['experiment'][field] = 'different'
            self.assertFalse(b.compare_results(left, right)['comparable'])
        left, right = valid_result(), valid_result()
        right['performance']['process_image']['source'] = 'native_outer_marker_ms'
        self.assertEqual(b.compare_results(left, right)['metrics'][0]['status'], 'not_comparable')
        right['device'] = {'serial': 'other'}
        self.assertFalse(b.compare_results(left, right)['comparable'])
        left, right = valid_result(), valid_result()
        right['sampling']['configured_period_ms'] = 16
        self.assertFalse(b.compare_results(left, right)['comparable'])


class SamplingQualityTest(unittest.TestCase):
    def compare(self, sampling, tolerance=None):
        left, right = valid_result(10), valid_result(1)
        right['sampling'] = sampling
        for side in (left, right):
            side['memory'] = {component: {metric: b.memory_value(10, 'csv:fixture')
                for metric in ('sampled_peak', 'sample_mean', 'getrusage_high_water')}
                for component in b.MEMORY_COLUMNS}
        result = (b.compare_results(left, right) if tolerance is None else
                  b.compare_results(left, right, sampling_quality_tolerance=tolerance))
        self.assertTrue(result['comparable'])  # Common checks, not every metric's status.
        self.assertEqual(result['metrics'][0]['status'], 'ok')
        self.assertEqual(result['metrics'][0]['delta_percent'], -90)
        self.assertTrue(left['measurement_valid'] and right['measurement_valid'])
        return result

    def assert_memory_rejected(self, sampling, tolerance=None):
        result = self.compare(sampling, tolerance)
        rows = [r for r in result['metrics'] if r['metric'].startswith('memory.')]
        self.assertTrue(all(r['status'] == 'not_comparable' for r in rows))
        self.assertTrue(all(r['delta'] is None and r['delta_percent'] is None for r in rows))
        self.assertTrue(all('sampling quality' in r['reason'] for r in rows))
        self.assertFalse(result['sampling_quality']['comparable'])
        self.assertEqual(result['sampling_quality']['status'], 'not_comparable')
        return result

    def test_same_config_actual_five_vs_hundred_ms_rejects_only_memory(self):
        self.assert_memory_rejected(sampling_fixture(100))

    def test_median_match_but_max_differs(self):
        sampling = sampling_fixture()
        sampling['actual_interval_ms'].update(min=0, max=25)
        result = self.assert_memory_rejected(sampling)
        self.assertTrue(result['sampling_quality']['checks']['median_interval_ratio'])
        self.assertFalse(result['sampling_quality']['checks']['max_interval_ratio'])

    def test_density_differs_despite_matching_median_and_max(self):
        left, right = valid_result(), valid_result()
        for side, mean in ((left, 7.5), (right, 45.5)):
            side['sampling'] = sampling_fixture(mean)
            side['sampling']['actual_interval_ms'].update(min=0, median=5, max=100)
        result = b.compare_results(left, right)
        self.assertFalse(result['sampling_quality']['checks']['effective_interval_ratio'])
        self.assertTrue(result['sampling_quality']['checks']['median_interval_ratio'])
        self.assertEqual(next(r for r in result['metrics'] if r['metric'] ==
                             'memory.dma.sampled_peak')['status'], 'not_comparable')

    def test_same_density_shorter_span_and_count_are_legal(self):
        result = self.compare(sampling_fixture(count=5))
        quality = result['sampling_quality']
        self.assertTrue(quality['comparable'])
        self.assertEqual(quality['evidence']['right']['effective_interval_ms'], 5)
        self.assertEqual(quality['evidence']['right']['sample_count'], 5)
        self.assertEqual(quality['tolerance'], 2.0)
        self.assertTrue(any(r['status'] == 'ok' for r in result['metrics'] if r['metric'].startswith('memory.')))

    def test_default_and_override_ratio_boundary(self):
        self.assertTrue(self.compare(sampling_fixture(10))['sampling_quality']['comparable'])
        self.assert_memory_rejected(sampling_fixture(10.001))
        self.assertTrue(self.compare(sampling_fixture(15), 3)['sampling_quality']['comparable'])
        self.assert_memory_rejected(sampling_fixture(15.001), 3)
        self.assertTrue(self.compare(sampling_fixture(), 1)['sampling_quality']['comparable'])

    def test_coverage_missing_invalid_incomplete_or_mismatched(self):
        for status in (None, 'invalid', 'incomplete', 'failed', 'complete', 'unknown'):
            sampling = sampling_fixture(coverage=status)
            with self.subTest(status=status):
                self.assert_memory_rejected(sampling, 1000)
        for coverage in (None, [], {}):
            sampling = sampling_fixture()
            sampling['coverage'] = coverage
            with self.subTest(coverage=coverage):
                self.assert_memory_rejected(sampling)

    def test_both_unverified_warn_and_both_complete_do_not_claim_peak_proof(self):
        result = self.compare(sampling_fixture())
        self.assertEqual(result['sampling_quality']['status'], 'warning')
        self.assertTrue(any('coverage' in w and 'unverified' in w for w in result['sampling_quality']['warnings']))
        left, right = valid_result(), valid_result()
        for side in (left, right):
            side['sampling']['coverage']['status'] = 'complete'
        quality = b.compare_results(left, right)['sampling_quality']
        self.assertTrue(quality['comparable'])
        self.assertEqual(quality['status'], 'ok')
        self.assertTrue(any('continuous' in w for w in quality['warnings']))


    def test_missing_or_bad_quality_fields_cannot_be_overridden(self):
        for field in ('sample_count', 'duration_ms', 'actual_interval_ms', 'coverage'):
            sampling = sampling_fixture()
            del sampling[field]
            with self.subTest(missing=field):
                self.assert_memory_rejected(sampling, 1000)
        for field, values in (('sample_count', (None, 1, 0, 2.5, True, '21')),
                              ('duration_ms', (None, 0, -1, float('nan'), float('inf'), '100')),
                              ('rows', (0, 20, True)), ('end_ms', (0, float('nan')))):
            for value in values:
                sampling = sampling_fixture()
                sampling[field] = value
                with self.subTest(field=field, value=value):
                    result = self.assert_memory_rejected(sampling, 1000)
                    json.dumps(result['sampling_quality'], allow_nan=False)
        for field in ('min', 'median', 'mean', 'max'):
            for value in (None, -1, float('nan'), float('inf'), '5', True):
                sampling = sampling_fixture()
                sampling['actual_interval_ms'][field] = value
                with self.subTest(interval_field=field, value=value):
                    result = self.assert_memory_rejected(sampling, 1000)
                    json.dumps(result['sampling_quality'], allow_nan=False)

    def test_inconsistent_stats_and_missing_actual_statistics(self):
        for stats in (None, [], {}, dict(min=6, median=5, mean=5, max=5),
                      dict(min=0, median=0, mean=5, max=10),
                      dict(min=0, median=5, mean=10, max=10),
                      dict(min=0, median=5, mean=5, max=101)):
            sampling = sampling_fixture()
            sampling['actual_interval_ms'] = stats
            with self.subTest(stats=stats):
                self.assert_memory_rejected(sampling, 1000)

    def test_interval_statistics_must_be_possible_for_sample_count(self):
        for count in (2, 3):
            sampling = sampling_fixture(count=count)
            sampling['actual_interval_ms']['min'] = 0
            for tolerance in (2, 1000):
                with self.subTest(count=count, tolerance=tolerance):
                    result = self.assert_memory_rejected(sampling, tolerance)
                    self.assertFalse(result['sampling_quality']['evidence']['right']['checks']['consistent_intervals'])

    def test_one_interval_and_two_interval_statistics_can_be_valid(self):
        self.assertTrue(self.compare(sampling_fixture(count=2))['sampling_quality']['comparable'])
        sampling = sampling_fixture(interval=2.5, count=3)
        sampling['actual_interval_ms'].update(min=0, max=5)
        self.assertTrue(self.compare(sampling)['sampling_quality']['comparable'])

    def test_cli_default_valid_boundary_and_invalid_tolerance_json(self):
        self.assertEqual(b.build_parser().parse_args([]).sampling_quality_tolerance, 2.0)
        for value in ('1', '3.5'):
            args = b.build_parser().parse_args(['--sampling-quality-tolerance', value])
            self.assertEqual(args.sampling_quality_tolerance, float(value))
        for value in ('nan', 'inf', '-inf', '0.9', '0', 'bad'):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(value=value), contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(b.main(['--json', '--sampling-quality-tolerance=' + value]), 1)
                self.assertEqual(json.loads(stdout.getvalue())['status'], 'failed')
        for value in (float('nan'), float('inf'), 0.9, True, None):
            with self.subTest(direct=value), self.assertRaises(b.BenchmarkError):
                b.compare_results(valid_result(), valid_result(), value)


class DeploymentSafetyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.install = self.repo / 'siq3_install/release/C_SDK'
        self.receipt = self.repo / 'deployment.json'
        self.command = 'cd /fixture; ./siq_test -n 1 -p input'
        env = {'_BENCH_REPO_ROOT': str(self.repo), '_BENCH_DEPLOYMENT': str(self.receipt)}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.input = mock.patch.object(b, 'remote_input_manifest', return_value={})
        self.input.start()
        self.addCleanup(self.input.stop)

    def payload(self):
        files = [self.install / 'bin/siq_test', self.install / 'libs/arm64-v8a/libfixture.so']
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic ' + path.name.encode())
        return files

    def hashes(self, files):
        return '\n'.join(b.file_sha256(p) + '  /fixture/' + p.name for p in files) + '\n'

    def test_local_collision_rejected_before_adb_even_for_identical_bytes(self):
        files = self.payload()
        duplicate = self.install / 'common/libs/arm64-v8a/libfixture.so'
        duplicate.parent.mkdir(parents=True)
        for content in (b'different fixture', files[1].read_bytes()):
            duplicate.write_bytes(content)
            with self.subTest(identical=content == files[1].read_bytes()):
                with self.assertRaises(b.BenchmarkError):
                    b.build_artifact_snapshot(self.repo)
                with mock.patch.object(b.subprocess, 'run') as adb:
                    with self.assertRaises(b.BenchmarkError):
                        b.verify_deployment('fixture-adb', [], self.command)
                    adb.assert_not_called()
                self.assertFalse(self.receipt.exists())

    def test_empty_snapshot_and_missing_category(self):
        self.assertEqual(b.build_artifact_snapshot(self.repo), {})
        with self.assertRaises(b.MissingDeploymentArtifacts):
            b.deployment_artifacts(self.repo)

    def test_snapshot_does_not_swallow_other_benchmark_errors(self):
        with mock.patch.object(b, 'deployment_artifacts', side_effect=b.BenchmarkError('fixture failure')):
            with self.assertRaises(b.BenchmarkError):
                b.build_artifact_snapshot(self.repo)

    def test_unique_payload_shape_and_star_hash_marker(self):
        files = self.payload()
        responses = [subprocess.CompletedProcess([], 0, self.hashes(files).replace('  /', ' */'), ''),
                     subprocess.CompletedProcess([], 0, '1\n', '')]
        with mock.patch.dict(os.environ, {'_BENCH_INPUT_DIR': ''}), \
                mock.patch.object(b.subprocess, 'run', side_effect=responses):
            b.verify_deployment('fixture-adb', [], self.command)
        payload = json.loads(self.receipt.read_text())
        self.assertEqual(payload['sha256'], {p.name: b.file_sha256(p) for p in files})
        self.assertEqual(set(payload['local_build_artifacts']), {p.name for p in files})
        self.assertEqual(payload['invocations'], 1)

    def test_remote_duplicate_and_malformed_hashes_cannot_verify(self):
        files = self.payload()
        normal = self.hashes(files)
        bad = (normal + normal.splitlines()[0] + '\n',
               normal + b.file_sha256(files[1]) + '  /other/libfixture.so\n',
               normal.replace('/fixture/libfixture.so', '/other/libfixture.so'),
               normal.replace('/fixture/libfixture.so', '/fixture/nested/../libfixture.so'),
               normal.replace('/fixture/libfixture.so', 'libfixture.so'),
               normal.replace(b.file_sha256(files[0]), 'malformed'),
               normal + 'malformed line\n', normal + '\n',
               normal.splitlines()[0] + '\n',
               normal + '0' * 64 + '  /fixture/extra.so\n')
        for index, output in enumerate(bad):
            receipt = self.repo / ('bad-deployment-%d.json' % index)
            responses = [subprocess.CompletedProcess([], 0, output, ''),
                         subprocess.CompletedProcess([], 0, '1\n', '')]
            with self.subTest(index=index), mock.patch.dict(os.environ,
                    {'_BENCH_INPUT_DIR': '', '_BENCH_DEPLOYMENT': str(receipt)}), \
                    mock.patch.object(b.subprocess, 'run', side_effect=responses) as adb:
                with self.assertRaises(b.BenchmarkError):
                    b.verify_deployment('fixture-adb', [], self.command)
                self.assertEqual(adb.call_count, 1)
                self.assertFalse(receipt.exists())


def lifecycle_log(rounds, mode=1, function='ref', tail=''):
    """Each round is one process window; repeat_full keeps release in its lifecycle."""
    text = ''
    for index, values in enumerate(rounds, 1):
        if mode == 1 or index == 1:
            text += f'@@SIQ_BENCH_BEGIN@@ mode={mode} pid=42 iteration={index}\n'
        text += ''.join(f'leave {function} {value}ms\n' for value in values)
        text += 'leave process_image 1ms\n@@PROCESS_IMAGE_PERFORMANCE@@: 2\n'
        if mode == 1 or index == len(rounds):
            text += tail
            text += f'@@SIQ_BENCH_END@@ mode={mode} pid=42 iteration={index if mode == 1 else 1} status=0\n'
    return text


class PerformanceCoverageTest(unittest.TestCase):
    def compare(self, left_log, right_log, mode=1, requested='ref'):
        left, right = valid_result(), valid_result()
        for side, text in ((left, left_log), (right, right_log)):
            b.validate_run(text, mode, 2)
            side['experiment']['mode'] = mode
            side['performance'] = b.structured_performance(text, requested, 2)
        return left, right, b.compare_results(left, right)

    def test_missing_round_does_not_create_false_ninety_percent_gain(self):
        left, right, comparison = self.compare(lifecycle_log([[10], [10]]), lifecycle_log([[1, 1], []]))
        metric = right['performance']['ref']
        self.assertEqual(metric['iteration_counts'], [2, 0])
        self.assertEqual(metric['samples'], [1, 1])
        self.assertEqual(metric['statistics']['all_mean_ms'], 1)
        self.assertEqual(metric['status'], 'invalid')
        self.assertNotIn('per_call_ms', metric['statistics'])
        self.assertEqual(comparison['metrics'][0]['status'], 'not_comparable')
        self.assertIsNone(comparison['metrics'][0]['delta_percent'])

    def test_changed_call_count_between_sides_is_not_comparable(self):
        left, right, comparison = self.compare(lifecycle_log([[10], [10]]), lifecycle_log([[1, 1], [1, 1]]))
        self.assertEqual(right['performance']['ref']['status'], 'ok')
        self.assertEqual(right['performance']['ref']['iteration_counts'], [2, 2])
        self.assertEqual(right['performance']['ref']['calls_per_iteration'], 2)
        self.assertTrue(all(row['delta'] is None for row in comparison['metrics']
                            if row['metric'].startswith('performance.')))
        self.assertEqual(next(row for row in comparison['metrics']
                              if row['metric'] == 'memory.dma.sampled_peak')['status'], 'ok')

    def test_stable_multi_calls_have_verified_slots_and_comparison(self):
        for mode in (1, 2):
            with self.subTest(mode=mode):
                left, right, comparison = self.compare(lifecycle_log([[10, 20], [12, 22]], mode),
                    lifecycle_log([[5, 10], [6, 11]], mode), mode)
                metric = right['performance']['ref']
                self.assertEqual(metric['coverage'], 'complete')
                self.assertEqual(metric['iteration_counts'], [2, 2])
                self.assertEqual(metric['statistics']['calls_per_iteration'], 2)
                self.assertEqual(metric['statistics']['per_call_ms'][0]['all_mean_ms'], 5.5)
                self.assertEqual(comparison['metrics'][0]['delta_percent'], -50)
                self.assertIn('not proven call-sites', metric['call_slots'])

    def test_missing_lifecycle_is_unverified_not_even_count_complete(self):
        text = 'leave ref 1ms\nleave ref 1ms\n@@PROCESS_IMAGE_PERFORMANCE@@: 2\n' * 2
        metrics = b.structured_performance(text, 'ref,process_image', 2)
        for metric in metrics.values():
            self.assertEqual(metric['status'], 'unverified')
            self.assertEqual(metric['coverage'], 'unverified')
            self.assertIsNone(metric['iteration_counts'])
            self.assertNotIn('per_call_ms', metric['statistics'])

    def test_repeat_full_release_after_marker_belongs_to_same_iteration(self):
        text = lifecycle_log([[], []], tail='leave release 10ms\n')
        metric = b.structured_performance(text, 'release', 2)['release']
        self.assertEqual(metric['iteration_counts'], [1, 1])
        self.assertEqual(metric['status'], 'ok')

    def test_repeat_process_trailing_or_outside_samples_are_unverified(self):
        for text in (lifecycle_log([[1], [1]], 2, tail='leave ref 3ms\n'),
                     'leave ref 3ms\n' + lifecycle_log([[1], [1]], 2)):
            with self.subTest(text=text):
                metric = b.structured_performance(text, 'ref', 2)['ref']
                self.assertEqual(metric['status'], 'unverified')
                self.assertEqual(metric['iteration_counts'], [1, 1])
                self.assertEqual(len(metric['samples']), 3)

    def test_qualified_selection_and_source_names_are_preserved(self):
        target = 'cv::Mat hdr_df::HDRSticherDfFP16::compute_weight_map(cv::Mat&, float)'
        requested = 'compute_weight_map,hdr_df::HDRSticherDfFP16::compute_weight_map'
        text = lifecycle_log([[110, 41], [108, 40]], function=target,
                             tail='leave hdr_df::HDRSticherDfFP16::compute_weight_map_evn(cv::Mat&) 7ms\n')
        metrics = b.structured_performance(text, requested, 2)
        for name, metric in metrics.items():
            self.assertEqual(metric['samples'], [110, 41, 108, 40])
            self.assertEqual(metric['samples'], b.extract_performance_samples(text, name)[name])
            self.assertEqual(metric['iteration_composition'], [[target, target], [target, target]])
            self.assertEqual(metric['status'], 'ok')

    def test_equal_counts_different_invocation_names_between_sides_are_rejected(self):
        left, right, comparison = self.compare(lifecycle_log([[10], [10]], function='A::ref'),
                                               lifecycle_log([[1], [1]], function='B::ref'))
        self.assertEqual(right['performance']['ref']['status'], 'ok')
        self.assertEqual(left['performance']['ref']['iteration_counts'], [1, 1])
        self.assertEqual(comparison['metrics'][0]['status'], 'not_comparable')
        self.assertIsNone(comparison['metrics'][0]['delta_percent'])

    def test_equal_counts_changed_invocation_order_within_run_is_invalid(self):
        text = lifecycle_log([[10, 20], [12, 22]], function='A::ref')
        text = text.replace('leave A::ref 20ms', 'leave B::ref 20ms').replace('leave A::ref 12ms', 'leave B::ref 12ms')
        metric = b.structured_performance(text, 'ref', 2)['ref']
        self.assertEqual(metric['iteration_counts'], [2, 2])
        self.assertEqual(metric['status'], 'invalid')
        self.assertIsNone(metric['calls_per_iteration'])
        self.assertNotIn('per_call_ms', metric['statistics'])

    def test_repeat_process_missing_middle_marker_window_is_invalid(self):
        metric = b.structured_performance(lifecycle_log([[10], [], [10]], 2), 'ref', 3)['ref']
        self.assertEqual(metric['iteration_counts'], [1, 0, 1])
        self.assertEqual(metric['status'], 'invalid')
        self.assertIn('previous_outer_marker', metric['iteration_scope'])

    def test_process_image_complete_leaves_and_fallback_sources_reject_comparison(self):
        for mode in (1, 2):
            complete = lifecycle_log([[], []], mode)
            fallback = complete.replace('leave process_image 1ms\n', '', 1)
            with self.subTest(mode=mode):
                left, right, comparison = self.compare(complete, fallback, mode, 'process_image')
                self.assertEqual(left['performance']['process_image']['source'], 'native_leave_ms')
                self.assertEqual(right['performance']['process_image']['source'], 'native_outer_marker_ms')
                self.assertEqual(right['performance']['process_image']['status'], 'ok')
                self.assertEqual(right['performance']['process_image']['iteration_counts'], [1, 1])
                self.assertEqual(comparison['metrics'][0]['status'], 'not_comparable')

    def test_unscoped_process_image_leaves_use_verified_marker_fallback(self):
        text = lifecycle_log([[], []]).replace('leave process_image 1ms\n', '', 1)
        text = text.replace('status=0\n', 'status=0\nleave process_image 9ms\n', 1)
        metric = b.structured_performance(text, 'process_image', 2)['process_image']
        self.assertEqual(metric['source'], 'native_outer_marker_ms')
        self.assertEqual(metric['samples'], [2, 2])
        self.assertEqual(metric['coverage'], 'complete')

    def test_unverified_coverage_is_not_comparable(self):
        left, right = valid_result(), valid_result()
        right['performance'] = b.structured_performance('leave process_image 1ms\n' * 2, 'process_image', 2)
        comparison = b.compare_results(left, right)
        self.assertEqual(comparison['metrics'][0]['status'], 'not_comparable')
        self.assertIsNone(comparison['metrics'][0]['delta_percent'])

    def test_comparison_statistics_use_structured_samples_not_legacy_summary(self):
        left, right = valid_result(10), valid_result(1)
        right['performance_statistics'] = {'process_image': {'all_mean_ms': 999}}
        right['performance']['process_image']['statistics']['all_mean_ms'] = 999
        comparison = b.compare_results(left, right)
        self.assertEqual(comparison['metrics'][0]['right'], 1)
        self.assertEqual(comparison['metrics'][0]['delta_percent'], -90)

    def test_unpaired_lifecycle_does_not_prove_coverage(self):
        text = lifecycle_log([[1], [1]]).replace('iteration=2 status=0', 'iteration=2 status=1')
        metric = b.structured_performance(text, 'ref', 2)['ref']
        self.assertEqual(metric['status'], 'unverified')
        self.assertIsNone(metric['iteration_counts'])


class TransactionPointerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.receipt = self.repo / 'active.json'
        with mock.patch.object(b, 'git_text', return_value=''):
            self.tx = b.ABTransaction(self.repo, self.receipt, {'head': 'fixture-head'}, 'target')

    def execution_pointers(self, label='left', **extras):
        disk = json.loads(self.receipt.read_text())
        disk.update(active_artifacts=str(self.repo / label),
                    patch_receipt=str(self.repo / label / 'source_patch.json'), **extras)
        b.atomic_json(self.receipt, disk)
        return disk

    def test_save_after_a_retains_execution_owned_pointers(self):
        self.tx.stage('left_running')
        disk = self.execution_pointers()
        self.tx.data['left_artifacts'] = disk['active_artifacts']
        self.tx.save()
        saved = json.loads(self.receipt.read_text())
        for field in ('active_artifacts', 'patch_receipt'):
            self.assertEqual(saved[field], disk[field])
            self.assertEqual(self.tx.data[field], disk[field])
        self.assertEqual(saved['state'], 'left_running')

    def test_merge_whitelist_does_not_adopt_disk_state_stash_or_original(self):
        self.tx.data['stash_oid'] = 'owned-stash'
        disk = self.execution_pointers(state='complete', stash_oid='other-stash', original={'head': 'other'},
                                       foreign_field='must not copy')
        self.tx.stage('restoring_source')
        saved = json.loads(self.receipt.read_text())
        self.assertEqual(saved['state'], 'restoring_source')
        self.assertEqual(saved['stash_oid'], 'owned-stash')
        self.assertEqual(saved['original']['head'], 'fixture-head')
        self.assertNotIn('foreign_field', saved)
        self.assertEqual(saved.get('patch_receipt'), disk['patch_receipt'])

    def test_new_transaction_never_inherits_previous_id_pointers(self):
        old = self.execution_pointers(state='complete')
        with mock.patch.object(b, 'git_text', return_value=''):
            tx = b.ABTransaction(self.repo, self.receipt, {'head': 'new'}, 'new-target')
        saved = json.loads(self.receipt.read_text())
        self.assertNotEqual(saved['transaction_id'], old['transaction_id'])
        self.assertEqual(saved['transaction_id'], tx.data['transaction_id'])
        self.assertNotIn('active_artifacts', saved)
        self.assertNotIn('patch_receipt', saved)

    def test_wrong_id_fails_closed_and_preserves_original_receipt(self):
        disk = self.execution_pointers(transaction_id='different-transaction')
        with self.assertRaises(b.BenchmarkError):
            self.tx.stage('restoring_source')
        self.assertEqual(self.tx.data['state'], 'prepared')
        self.assertEqual(json.loads(self.receipt.read_text()), disk)

    def test_non_string_controlled_pointer_fails_without_overwrite(self):
        for value in (None, '', 42, [], {}):
            disk = self.execution_pointers()
            disk['patch_receipt'] = value
            b.atomic_json(self.receipt, disk)
            with self.subTest(value=value), self.assertRaises(b.BenchmarkError):
                self.tx.save()
            self.assertEqual(json.loads(self.receipt.read_text()), disk)

    def test_atomic_write_failure_reverts_memory_stage_and_keeps_disk_pointers(self):
        self.tx.stage('left_running')
        disk = self.execution_pointers()
        with mock.patch.object(b.os, 'replace', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                self.tx.stage('restoring_source')
        self.assertEqual(self.tx.data['state'], 'left_running')
        self.assertEqual(json.loads(self.receipt.read_text()), disk)
        self.tx.stage('restoring_source')
        self.assertEqual(json.loads(self.receipt.read_text()).get('patch_receipt'), disk['patch_receipt'])


class GitFixtureTest(unittest.TestCase):
    def git(self, directory, *arguments):
        return subprocess.run(['git', '-C', str(directory), *arguments], env=self.env,
            check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, directory, message):
        self.git(directory, 'add', '.')
        self.git(directory, 'commit', '-m', message)
        return self.git(directory, 'rev-parse', 'HEAD')

    def init(self, directory):
        directory.mkdir()
        self.git(directory, 'init', '-b', 'main')
        self.git(directory, 'config', 'user.email', 'fixture@example.invalid')
        self.git(directory, 'config', 'user.name', 'Fixture')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = dict(os.environ, GIT_ALLOW_PROTOCOL='file')
        self.patchenv = mock.patch.dict(os.environ, self.env)
        self.patchenv.start()
        self.addCleanup(self.patchenv.stop)
        upstream = self.base / 'local-submodule'
        self.init(upstream)
        (upstream / 'code').write_text('old\n')
        self.sub_old = self.commit(upstream, 'old submodule')
        (upstream / 'code').write_text('new\n')
        (upstream / 'collision').write_text('tracked later\n')
        self.sub_new = self.commit(upstream, 'new submodule')
        self.repo = self.base / 'root'
        self.init(self.repo)
        (self.repo / 'source').write_text('original\n')
        (self.repo / '.gitignore').write_text('ignored\n')
        self.git(self.repo, 'submodule', 'add', str(upstream), 'module')
        self.module = self.repo / 'module'
        self.git(self.module, 'checkout', '--detach', self.sub_old)
        self.old = self.commit(self.repo, 'left root')
        self.git(self.module, 'checkout', '--detach', self.sub_new)
        (self.repo / 'source').write_text('current\n')
        self.current = self.commit(self.repo, 'right root')
        self.git(self.module, 'checkout', '-b', 'sub-live')
        self.active = self.repo / '.git/siq-benchmark-transaction.json'
        self.input = self.base / 'input'
        self.input.mkdir()
        (self.input / 'input.raw').write_bytes(b'fixture')

    def nested_fixture(self, *, initialized=True, target_change=False, actual_old=False, deep=False):
        tools = self.repo / 'tools'
        tools.mkdir()
        self.git(self.repo, 'mv', 'module', 'tools/cmake_one')
        self.module = tools / 'cmake_one'
        self.git(self.repo, 'submodule', 'add', str(self.base / 'local-submodule'), 'dev_toolkit')
        upstream = self.base / 'nested-upstream'
        self.init(upstream)
        (upstream / 'code').write_text('leaf old\n')
        if deep:
            grand = self.base / 'grand-upstream'
            self.init(grand)
            (grand / 'code').write_text('grand\n')
            self.commit(grand, 'grand dependency')
            self.git(upstream, 'submodule', 'add', str(grand), 'grand')
        self.leaf_old = self.commit(upstream, 'leaf old')
        (upstream / 'code').write_text('leaf new\n')
        self.leaf_new = self.commit(upstream, 'leaf new')
        self.git(self.module, 'submodule', 'add', str(upstream), 'submodule/toolchains-from-source')
        self.leaf = self.module / 'submodule/toolchains-from-source'
        self.leaf_path = 'tools/cmake_one/submodule/toolchains-from-source'
        self.git(self.leaf, 'checkout', '--detach', self.leaf_old if target_change else self.leaf_new)
        (self.module / 'code').write_text('parent left\n')
        self.parent_old = self.commit(self.module, 'parent left')
        (self.repo / 'source').write_text('left\n')
        self.old = self.commit(self.repo, 'nested root left')
        self.git(self.leaf, 'checkout', '--detach', self.leaf_new)
        (self.module / 'code').write_text('parent right\n')
        self.parent_new = self.commit(self.module, 'parent right')
        (self.repo / 'source').write_text('right\n')
        self.current = self.commit(self.repo, 'nested root right')
        if initialized:
            self.git(self.leaf, 'checkout', '-b', 'nested-live', self.leaf_old if actual_old else self.leaf_new)
            if deep:
                self.git(self.leaf, 'submodule', 'update', '--init')
                self.grand = self.leaf / 'grand'
                self.git(self.grand, 'checkout', '-b', 'grand-live')
        else:
            self.leaf.rename(self.base / 'saved-nested-worktree')
            self.leaf.mkdir()

    def transaction(self, target=None):
        return b.ABTransaction(self.repo, self.active, b.source_snapshot(self.repo), target or self.old)

    def args(self):
        args = b.build_parser().parse_args(['--json', '--ab-test-commit', self.old])
        args.repo_root = self.repo
        args.run_script = self.repo / 'run.sh'
        args.input_dir = self.input
        args.work_dir = self.base / 'artifacts'
        return args

    def test_recovered_single_transaction_is_unblocked_only_after_restore_verifies(self):
        receipt = self.base / 'source_patch.json'
        receipt.write_text(json.dumps({'restored': True, 'exact_restore': True}))
        record = {'kind': 'single', 'state': 'running',
                  'source': b.source_snapshot(self.repo), 'patch_receipt': str(receipt)}
        self.active.write_text(json.dumps(record))
        (self.repo / 'source').write_text('concurrent edit\n')
        self.assertFalse(b.finish_recovered_single(self.repo, self.active))
        self.assertEqual(json.loads(self.active.read_text())['state'], 'running')
        (self.repo / 'source').write_text('current\n')
        receipt.write_text(json.dumps({'restored': False, 'exact_restore': False}))
        self.assertFalse(b.finish_recovered_single(self.repo, self.active))
        receipt.write_text(json.dumps({'restored': True, 'exact_restore': True}))
        self.assertTrue(b.finish_recovered_single(self.repo, self.active))
        self.assertEqual(json.loads(self.active.read_text())['state'], 'complete')
        self.assertTrue(json.loads(self.active.read_text())['recovery_verified'])

    def test_real_stash_index_untracked_branch_and_existing_user_stash(self):
        (self.repo / 'source').write_text('user stash\n')
        self.git(self.repo, 'stash', 'push', '-m', 'user entry')
        stash = self.git(self.repo, 'rev-parse', 'refs/stash')
        (self.repo / 'source').write_text('staged\n')
        self.git(self.repo, 'add', 'source')
        (self.repo / 'source').write_text('unstaged\n')
        (self.repo / 'untracked').write_text('do not delete')
        (self.module / 'scratch').write_text('allowed')
        original = b.source_snapshot(self.repo)
        tx = self.transaction()
        tx.switch()
        self.assertEqual(b.git_head(self.repo), self.old)
        self.assertEqual(b.git_head(self.module), self.sub_old)
        self.assertIsNone(b.git_branch(self.repo))
        owned = tx.data['stash_oid']
        entries = self.git(self.repo, 'stash', 'list', '--format=%H')
        reflog = self.git(self.repo, 'reflog', 'show', 'refs/stash', '--format=%H %gs')
        tx.restore()
        b.verify_original(self.repo, original)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'refs/stash'), owned)
        self.assertEqual(self.git(self.repo, 'stash', 'list', '--format=%H'), entries)
        self.assertEqual(self.git(self.repo, 'reflog', 'show', 'refs/stash', '--format=%H %gs'), reflog)
        self.assertIn(stash, entries)
        self.assertIn(owned, entries)
        self.assertEqual((self.repo / 'source').read_text(), 'unstaged\n')
        self.assertEqual((self.repo / 'untracked').read_text(), 'do not delete')
        self.assertFalse(tx.data['stash_dropped'])
        self.assertTrue(tx.data['restoration_verified'])
        self.assertIn('atomic ref/reflog drop safety', tx.data['stash_retained_reason'])

    def test_detached_and_actual_module_head_not_gitlink(self):
        self.git(self.repo, 'checkout', '--detach', self.current)
        self.git(self.module, 'checkout', '--detach', self.sub_old)
        original = b.source_snapshot(self.repo)
        tx = self.transaction()
        tx.switch()
        tx.restore()
        b.verify_original(self.repo, original)
        self.assertEqual(b.git_head(self.module), self.sub_old)
        self.assertIsNone(b.git_branch(self.repo))

    def test_nonrecursive_update_allows_real_style_uninitialized_nested_dependency(self):
        self.nested_fixture(initialized=False)
        original = b.source_snapshot(self.repo)
        self.assertEqual(set(original['submodules']), {'dev_toolkit', 'tools/cmake_one', self.leaf_path})
        self.assertEqual(original['submodules'][self.leaf_path]['initialization'], 'uninitialized')
        self.assertEqual(set(b.submodule_snapshot(self.repo, include_nested=False)), {'dev_toolkit', 'tools/cmake_one'})
        b.check_modules_clean(self.repo)
        self.git(self.repo, 'config', 'submodule.recurse', 'true')
        tx = self.transaction()
        tx.switch()
        self.assertEqual(b.git_head(self.module), self.parent_old)
        self.assertEqual(tx.data['left_source']['submodules'][self.leaf_path]['initialization'], 'uninitialized')
        self.assertFalse((self.leaf / '.git').exists())
        tx.restore()
        b.verify_original(self.repo, original)
        self.assertTrue(tx.data['restoration_verified'])
        self.assertFalse((self.leaf / '.git').exists())

    def test_initialized_nested_identity_and_untracked_repository_boundary(self):
        self.nested_fixture(deep=True)
        scratch = self.leaf / 'scratch-repository'
        self.init(scratch)
        (scratch / 'code').write_text('untracked repository\n')
        self.commit(scratch, 'scratch')
        (scratch / 'code').write_text('untracked dirty repository must not be visited')
        original = b.source_snapshot(self.repo)
        self.assertEqual(set(original['submodules']),
            {'dev_toolkit', 'tools/cmake_one', self.leaf_path, self.leaf_path + '/grand'})
        tx = self.transaction()
        tx.switch()
        for path in (self.leaf_path, self.leaf_path + '/grand'):
            for key in ('head', 'branch', 'fingerprint'):
                self.assertEqual(tx.data['left_source']['submodules'][path][key], original['submodules'][path][key])
        tx.data['original']['submodules'] = dict(reversed(list(original['submodules'].items())))
        calls = []
        checkout = b.checkout
        def recording(repo, *args, **kwargs):
            calls.append(repo)
            return checkout(repo, *args, **kwargs)
        with mock.patch.object(b, 'checkout', recording):
            tx.restore()
        self.assertLess(calls.index(self.repo), calls.index(self.module))
        self.assertLess(calls.index(self.module), calls.index(self.leaf))
        self.assertLess(calls.index(self.leaf), calls.index(self.grand))
        b.verify_original(self.repo, original)
        self.assertEqual((scratch / 'code').read_text(), 'untracked dirty repository must not be visited')

    def test_nested_actual_not_target_gitlink_fails_before_a_and_recovers(self):
        self.nested_fixture(target_change=True)
        original = b.source_snapshot(self.repo)
        with mock.patch.object(b, 'execute_single') as run:
            result = b.run_ab(self.args(), self.active)
        run.assert_not_called()
        self.assertIn('nested actual HEAD differs from target gitlink', result['primary_error']['message'])
        self.assertIsNone(result['recovery_error'])
        self.assertFalse(result['measurement_valid'])
        self.assertIsNone(result['left'])
        b.verify_original(self.repo, original)

    def test_nested_original_actual_not_gitlink_is_restored_after_valid_a(self):
        self.nested_fixture(target_change=True, actual_old=True)
        original = b.source_snapshot(self.repo)
        self.assertFalse(original['submodules'][self.leaf_path]['actual_matches_gitlink'])
        tx = self.transaction()
        tx.switch()
        self.assertTrue(tx.data['left_source']['submodules'][self.leaf_path]['actual_matches_gitlink'])
        tx.restore()
        b.verify_original(self.repo, original)
        self.assertEqual(b.git_head(self.leaf), self.leaf_old)
        self.assertEqual(b.git_branch(self.leaf), 'refs/heads/nested-live')

    def test_changed_uninitialized_nested_target_is_not_claimed_complete(self):
        self.nested_fixture(initialized=False, target_change=True)
        original = b.source_snapshot(self.repo)
        tx = self.transaction()
        with self.assertRaisesRegex(b.BenchmarkError, 'uninitialized nested target gitlink changed'):
            tx.switch()
        tx.restore()
        b.verify_original(self.repo, original)
        self.assertFalse((self.leaf / '.git').exists())

    def test_nested_dirty_conflicts_and_in_progress_operations_refused(self):
        self.nested_fixture()
        (self.leaf / 'code').write_text('nested dirty\n')
        with self.assertRaisesRegex(b.BenchmarkError, 'tracked dirty submodule'):
            b.run_ab(self.args(), self.active)
        self.assertFalse(self.active.exists())
        self.git(self.leaf, 'checkout', '--', 'code')
        blob = self.git(self.leaf, 'rev-parse', 'HEAD:code')
        entries = '0 ' + '0' * len(blob) + '\tcode\n'
        entries += ''.join(f'100644 {blob} {stage}\tcode\n' for stage in (1, 2, 3))
        subprocess.run(['git', '-C', str(self.leaf), 'update-index', '--index-info'],
            input=entries, env=self.env, capture_output=True, text=True, check=True)
        with self.assertRaisesRegex(b.BenchmarkError, 'unresolved Git conflicts'):
            b.run_ab(self.args(), self.active)
        self.assertFalse(self.active.exists())
        self.git(self.leaf, 'restore', '--source=HEAD', '--staged', '--worktree', '--', 'code')
        marker = Path(self.git(self.leaf, 'rev-parse', '--absolute-git-dir')) / 'MERGE_HEAD'
        marker.write_text(self.leaf_old + '\n')
        with self.assertRaisesRegex(b.BenchmarkError, 'in-progress Git operation'):
            b.run_ab(self.args(), self.active)
        self.assertFalse(self.active.exists())
        self.assertEqual(b.git_head(self.repo), self.current)

    def test_nested_staged_gitlink_edit_is_dirty_not_just_actual_difference(self):
        self.nested_fixture()
        self.git(self.leaf, 'checkout', '--detach', self.leaf_old)
        b.check_modules_clean(self.repo)
        self.git(self.module, 'add', 'submodule/toolchains-from-source')
        self.assertTrue(b.tracked_dirty(self.module))
        with self.assertRaisesRegex(b.BenchmarkError, 'tracked dirty submodule'):
            b.run_ab(self.args(), self.active)
        self.assertFalse(self.active.exists())
        self.assertEqual(b.git_head(self.repo), self.current)

    def test_new_nested_target_gitlink_cannot_adopt_untracked_repository(self):
        self.nested_fixture()
        self.git(self.module, 'submodule', 'add', str(self.base / 'nested-upstream'), 'new-nested')
        self.commit(self.module, 'target adds nested repository')
        target = self.commit(self.repo, 'target nested gitlink')
        self.git(self.repo, 'checkout', '--detach', self.current)
        self.git(self.module, 'checkout', '--detach', self.parent_new)
        extra = self.module / 'new-nested'
        identity = (b.git_head(extra), b.git_branch(extra), b.tracked_fingerprint(extra))
        original = b.source_snapshot(self.repo)
        self.assertNotIn('tools/cmake_one/new-nested', original['submodules'])
        tx = self.transaction(target)
        with self.assertRaisesRegex(b.BenchmarkError, 'new target nested gitlink collides'):
            tx.switch()
        self.assertEqual((b.git_head(extra), b.git_branch(extra), b.tracked_fingerprint(extra)), identity)
        tx.restore()
        b.verify_original(self.repo, original)

    def test_nonrecursive_update_detects_clean_nested_identity_change(self):
        self.nested_fixture()
        original = b.source_snapshot(self.repo)
        real = b.git_bytes
        def changing(repo, *args, **kwargs):
            value = real(repo, *args, **kwargs)
            if repo == self.repo and args == ('submodule', 'update', '--init'):
                self.git(self.leaf, 'checkout', '--detach', self.leaf_new)
            return value
        self.git(self.repo, 'checkout', '--detach', self.old)
        with mock.patch.object(b, 'git_bytes', changing), \
                self.assertRaisesRegex(b.BenchmarkError, 'changed existing nested submodule identity'):
            b.update_modules(self.repo)
        self.assertEqual(b.git_head(self.leaf), self.leaf_new)
        self.assertIsNone(b.git_branch(self.leaf))
        self.git(self.repo, 'checkout', 'main')
        self.git(self.module, 'checkout', 'sub-live')
        self.git(self.leaf, 'checkout', 'nested-live')
        b.verify_original(self.repo, original)

    def removed_nested_target(self):
        self.git(self.module, 'rm', '--cached', '--', 'submodule/toolchains-from-source')
        self.git(self.module, 'config', '--file', '.gitmodules', '--remove-section',
                 'submodule.submodule/toolchains-from-source')
        self.git(self.module, 'add', '.gitmodules')
        self.git(self.module, 'commit', '-m', 'remove nested target gitlink without removing checkout')
        target = self.commit(self.repo, 'root target removes nested gitlink')
        self.git(self.repo, 'checkout', '--detach', self.current)
        self.git(self.module, 'checkout', '--detach', self.parent_new)
        return target

    def test_removed_nested_gitlink_identity_is_preserved_and_restored(self):
        self.nested_fixture()
        target = self.removed_nested_target()
        original = b.source_snapshot(self.repo)
        tx = self.transaction(target)
        tx.switch()
        self.assertNotIn(self.leaf_path, tx.data['left_source']['submodules'])
        self.assertEqual(tx.data['left_preserved_nested'][self.leaf_path]['head'], self.leaf_new)
        tx.restore()
        b.verify_original(self.repo, original)

    def test_removed_nested_boundary_clean_concurrent_change_stops_recovery(self):
        self.nested_fixture()
        tx = self.transaction(self.removed_nested_target())
        tx.switch()
        self.git(self.leaf, 'checkout', '--detach', self.leaf_old)
        with self.assertRaisesRegex(b.BenchmarkError, 'preserved nested submodule changed during A'):
            tx.restore()
        self.assertEqual(b.git_head(self.leaf), self.leaf_old)
        self.assertFalse(tx.data.get('restoration_verified'))

    def test_dirty_submodule_and_gitlink_index_refused_without_stash(self):
        (self.module / 'code').write_text('dirty')
        with self.assertRaises(b.BenchmarkError):
            b.run_ab(self.args(), self.active)
        self.assertFalse(self.active.exists())
        (self.module / 'code').write_text('new\n')
        self.git(self.module, 'checkout', '--detach', self.sub_old)
        self.git(self.repo, 'add', 'module')
        with self.assertRaises(b.BenchmarkError):
            b.run_ab(self.args(), self.active)
        self.assertEqual(b.git_head(self.repo), self.current)

    def test_submodule_untracked_and_ignored_collision_refused(self):
        self.git(self.module, 'checkout', '--detach', self.sub_old)
        for ignored in (False, True):
            with self.subTest(ignored=ignored):
                (self.module / 'collision').write_text('keep me')
                if ignored:
                    (self.module / '.git/info/exclude').exists() if (self.module / '.git').is_dir() else None
                    exclude = Path(self.git(self.module, 'rev-parse', '--absolute-git-dir')) / 'info/exclude'
                    exclude.write_text('collision\n')
                with self.assertRaises(b.BenchmarkError):
                    b.update_modules(self.repo)
                self.assertEqual((self.module / 'collision').read_text(), 'keep me')

    def test_root_ignored_collision_is_protected(self):
        (self.repo / 'ignored').write_text('tracked target')
        self.git(self.repo, 'add', '-f', 'ignored')
        target = self.commit(self.repo, 'tracked ignored path')
        self.git(self.repo, 'checkout', '--detach', self.current)
        (self.repo / 'ignored').write_text('private ignored data')
        with self.assertRaises(b.BenchmarkError):
            b.protected_collisions(self.repo, target)
        self.assertEqual((self.repo / 'ignored').read_text(), 'private ignored data')

    def test_update_strategy_that_moves_branches_is_refused(self):
        for strategy in ('merge', 'rebase', 'none', '!custom'):
            self.git(self.repo, 'config', 'submodule.module.update', strategy)
            with self.assertRaises(b.BenchmarkError):
                b.run_ab(self.args(), self.active)
            self.assertFalse(self.active.exists())

    def test_checkout_failure_restores_stash_exactly(self):
        (self.repo / 'source').write_text('dirty\n')
        original = b.source_snapshot(self.repo)
        tx = self.transaction()
        real = b.checkout
        def failing(repo, oid=None, branch=None):
            if oid == self.old:
                raise b.BenchmarkError('injected checkout failure')
            return real(repo, oid, branch)
        with mock.patch.object(b, 'checkout', failing), self.assertRaises(b.BenchmarkError):
            tx.switch()
        tx.restore()
        b.verify_original(self.repo, original)

    def test_stash_pending_and_apply_conflict_are_not_retried(self):
        (self.repo / 'source').write_text('dirty\n')
        tx = self.transaction()
        tx.stage('stash_pending')
        with self.assertRaises(b.BenchmarkError):
            tx.restore()
        tx = self.transaction()
        tx.switch()
        real = b.git_bytes
        calls = []
        def fail(repo, *arguments, **kwargs):
            if arguments[:2] == ('stash', 'apply'):
                calls.append(arguments)
                (self.repo / 'source').write_text('partial apply evidence')
                raise b.BenchmarkError('injected conflict')
            return real(repo, *arguments, **kwargs)
        with mock.patch.object(b, 'git_bytes', fail), self.assertRaises(b.BenchmarkError):
            tx.restore()
        with self.assertRaises(b.BenchmarkError):
            tx.restore()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'refs/stash'), tx.data['stash_oid'])
        self.assertEqual((self.repo / 'source').read_text(), 'partial apply evidence')

    def test_concurrent_clean_head_switch_is_not_silently_overwritten(self):
        tx = self.transaction()
        tx.switch()
        self.git(self.repo, 'checkout', '--detach', self.current)
        with self.assertRaises(b.BenchmarkError):
            tx.restore()
        self.assertEqual(b.git_head(self.repo), self.current)
        self.assertIsNone(b.git_branch(self.repo))
        self.assertFalse(tx.data.get('restoration_verified'))

    def test_original_branch_moved_stops_without_force(self):
        tx = self.transaction()
        tx.switch()
        self.git(self.repo, 'update-ref', 'refs/heads/main', self.old)
        with self.assertRaises(b.BenchmarkError):
            tx.restore()
        self.assertEqual(b.git_head(self.repo), self.old)
        self.assertFalse(tx.data.get('restoration_verified'))

    def test_invalid_commit_json_does_not_switch_or_create_receipt(self):
        original = b.source_snapshot(self.repo)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(b.shutil, 'which', return_value='/fixture/tool'), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.main(['--json', '--repo-root', str(self.repo), '--run-script', str(self.repo / 'run.sh'),
                '--input-dir', str(self.input), '--adapter', 'adapter', '--ab_test_commit', 'missing-commit'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())['kind'], 'ab')
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'failed')
        self.assertFalse(self.active.exists())
        b.verify_original(self.repo, original)

    def test_ab_device_timeout_fails_before_stash_or_checkout(self):
        (self.repo / 'source').write_text('current dirty worktree\n')
        before = b.source_snapshot(self.repo)
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ['--json', '--ab_test_commit', self.old, '--repo-root', str(self.repo),
                '--run-script', str(self.repo / 'run.sh'), '--input-dir', str(self.input),
                '--adapter', 'fixture']
        with mock.patch.object(b, 'collect_device', side_effect=subprocess.TimeoutExpired('adb', 15)), \
                mock.patch.object(b, 'run_ab') as run, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.main(argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())['primary_error']['type'], 'Timeout')
        run.assert_not_called()
        self.assertFalse(self.active.exists())
        self.assertEqual(self.git(self.repo, 'stash', 'list'), '')
        b.verify_original(self.repo, before)

    def test_ab_device_serial_is_pinned_before_switch(self):
        args_seen = []
        def run(args, active):
            args_seen.append(args.serial)
            result = b.new_result('ab')
            result.update(status='ok', exit_code=0)
            return result
        argv = ['--json', '--ab_test_commit', self.old, '--repo-root', str(self.repo),
                '--run-script', str(self.repo / 'run.sh'), '--input-dir', str(self.input),
                '--adapter', 'fixture']
        with mock.patch.object(b, 'collect_device', return_value={'serial': 'fixed-fixture', 'transport_id': 1}), \
                mock.patch.object(b, 'run_ab', side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b.main(argv), 0)
        self.assertEqual(args_seen, ['fixed-fixture'])
        self.assertFalse(self.active.exists())

    def test_entry_lock_and_unfinished_receipt(self):
        with b.benchmark_lock(self.repo):
            with self.assertRaises(b.BenchmarkError):
                with b.benchmark_lock(self.repo):
                    pass
        self.active.write_text('{"state":"stash_pending"}')
        stdout = io.StringIO()
        with mock.patch.object(b.shutil, 'which', return_value='/fixture/tool'), contextlib.redirect_stdout(stdout):
            code = b.main(['--json', '--repo-root', str(self.repo), '--run-script', str(self.repo / 'run.sh')])
        self.assertEqual(code, 1)
        self.assertIn('unfinished', json.loads(stdout.getvalue())['primary_error']['message'])
        self.assertEqual(json.loads(self.active.read_text())['state'], 'stash_pending')

    def test_orchestrator_left_invalid_stops_right_and_restores(self):
        calls = []
        def run(args):
            calls.append(b.git_head(args.repo_root))
            result = b.new_result()
            result['primary_error'] = {'type': 'fixture', 'message': 'build failed'}
            return result
        original = b.source_snapshot(self.repo)
        with mock.patch.object(b, 'execute_single', run):
            result = b.run_ab(self.args(), self.active)
        self.assertEqual(calls, [self.old])
        self.assertIsNone(result['right'])
        b.verify_original(self.repo, original)
        self.assertEqual(json.loads(self.active.read_text())['state'], 'complete')

    def test_stash_retention_warning_and_receipt_match_verified_restore(self):
        (self.repo / 'source').write_text('dirty\n')
        commands = []
        real = b.git_bytes
        def recording(repo, *arguments, **kwargs):
            commands.append(arguments)
            return real(repo, *arguments, **kwargs)
        with mock.patch.object(b, 'git_bytes', recording), \
                mock.patch.object(b, 'execute_single', side_effect=lambda args: valid_result()):
            result = b.run_ab(self.args(), self.active)
        receipt = json.loads(self.active.read_text())
        self.assertEqual(receipt['state'], 'complete')
        self.assertTrue(receipt['restoration_verified'])
        self.assertFalse(receipt['stash_dropped'])
        reason = receipt['stash_retained_reason']
        self.assertIn('external Git concurrency', reason)
        self.assertIn('atomic ref/reflog drop safety', reason)
        self.assertEqual(result['validity']['source_restore'], 'verified')
        self.assertTrue(result['measurement_valid'])
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['warnings'], result['comparison']['sampling_quality']['warnings'] +
                         ['transaction stash retained: ' + reason + '; inspect receipt for the exact OID'])
        self.assertIn(receipt['stash_oid'], self.git(self.repo, 'stash', 'list', '--format=%H'))
        self.assertFalse(any(args[0] in ('update-ref', 'reflog') or args[:2] == ('stash', 'drop') for args in commands))

    def test_packed_stash_ref_is_conservatively_retained(self):
        (self.repo / 'source').write_text('dirty\n')
        tx = self.transaction()
        tx.switch()
        self.git(self.repo, 'pack-refs', '--all')
        self.assertIn(b' refs/stash', (self.repo / '.git/packed-refs').read_bytes())
        tx.restore()
        self.assertFalse(tx.data['stash_dropped'])
        self.assertEqual(self.git(self.repo, 'rev-parse', 'refs/stash'), tx.data['stash_oid'])
        b.verify_original(self.repo, tx.data['original'])

    def test_existing_stash_lock_is_preserved_and_own_stash_retained(self):
        (self.repo / 'source').write_text('dirty\n')
        tx = self.transaction()
        tx.switch()
        lock = self.repo / '.git/refs/stash.lock'
        lock.write_text('fixture-owned concurrent lock')
        tx.restore()
        self.assertEqual(lock.read_text(), 'fixture-owned concurrent lock')
        self.assertFalse(tx.data['stash_dropped'])
        self.assertEqual(self.git(self.repo, 'rev-parse', 'refs/stash'), tx.data['stash_oid'])
        b.verify_original(self.repo, tx.data['original'])

    def test_newer_user_stash_is_never_dropped(self):
        (self.repo / 'source').write_text('original dirty\n')
        tx = self.transaction()
        tx.switch()
        (self.repo / 'source').write_text('concurrent user stash\n')
        self.git(self.repo, 'stash', 'push', '-m', 'new user entry')
        user = self.git(self.repo, 'rev-parse', 'refs/stash')
        tx.restore()
        self.assertEqual(self.git(self.repo, 'rev-parse', 'refs/stash'), user)
        self.assertFalse(tx.data['stash_dropped'])
        self.assertIn(tx.data['stash_oid'], self.git(self.repo, 'stash', 'list', '--format=%H'))

    def test_new_target_submodule_cannot_modify_existing_untracked_repository(self):
        self.git(self.repo, 'submodule', 'add', str(self.base / 'local-submodule'), 'second')
        target = self.commit(self.repo, 'target declares second repository')
        self.git(self.repo, 'checkout', '--detach', self.current)
        second = self.repo / 'second'
        head, branch = b.git_head(second), b.git_branch(second)
        args = self.args()
        args.ab_test_commit = target
        with self.assertRaises(b.BenchmarkError):
            b.run_ab(args, self.active)
        self.assertFalse(self.active.exists())
        self.assertEqual((b.git_head(second), b.git_branch(second)), (head, branch))

    def test_real_partial_update_then_locked_second_dependency_restores(self):
        self.git(self.repo, 'submodule', 'add', str(self.base / 'local-submodule'), 'second')
        original_oid = self.commit(self.repo, 'original with second dependency')
        second = self.repo / 'second'
        self.git(self.module, 'checkout', '--detach', self.sub_old)
        self.git(second, 'checkout', '--detach', self.sub_old)
        target = self.commit(self.repo, 'target with old dependency gitlinks')
        self.git(self.repo, 'checkout', '--detach', original_oid)
        self.git(self.module, 'checkout', 'sub-live')
        self.git(second, 'checkout', 'main')
        original = b.source_snapshot(self.repo)
        lock = Path(self.git(second, 'rev-parse', '--absolute-git-dir')) / 'index.lock'
        lock.write_text('fixture lock')
        tx = self.transaction(target)
        with self.assertRaises(b.BenchmarkError):
            tx.switch()
        self.assertEqual(b.git_head(self.module), self.sub_old)
        self.assertEqual(b.git_head(second), self.sub_new)
        lock.unlink()  # Only our temporary fixture lock, never a user's lock.
        tx.restore()
        b.verify_original(self.repo, original)

    def test_partial_submodule_update_failure_recovers_existing_actual_branch(self):
        tx = self.transaction()
        real = b.update_modules
        count = []
        def fail(repo, **kwargs):
            count.append(1)
            if len(count) == 1:
                b.checkout(self.module, self.sub_old)
                raise b.BenchmarkError('second dependency failed after first updated')
            return real(repo, **kwargs)
        with mock.patch.object(b, 'update_modules', fail):
            with self.assertRaises(b.BenchmarkError):
                tx.switch()
            tx.restore()
        b.verify_original(self.repo, tx.data['original'])
        self.assertEqual(b.git_branch(self.module), 'refs/heads/sub-live')

    def test_orchestrator_interrupt_and_timeout_restore_and_stop(self):
        original = b.source_snapshot(self.repo)
        for status, code in (('interrupted', 130), ('failed', 124)):
            with self.subTest(status=status):
                args = self.args()
                args.work_dir = self.base / status
                failed = b.new_result()
                failed.update(status=status, exit_code=code,
                              primary_error={'type': 'Interrupted' if code == 130 else 'Timeout', 'message': 'fixture'})
                with mock.patch.object(b, 'execute_single', return_value=failed) as run:
                    result = b.run_ab(args, self.active)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(result['exit_code'], code)
                b.verify_original(self.repo, original)

    def test_a_tracked_edits_prevent_checkout_and_right_run(self):
        def run(args):
            (self.repo / 'source').write_text('unexpected tracked cache rewrite')
            return valid_result()
        with mock.patch.object(b, 'execute_single', run) as mocked:
            result = b.run_ab(self.args(), self.active)
        self.assertIsNotNone(result['recovery_error'])
        self.assertEqual(b.git_head(self.repo), self.old)
        self.assertEqual((self.repo / 'source').read_text(), 'unexpected tracked cache rewrite')
        self.assertIsNone(result['right'])
        self.assertNotEqual(json.loads(self.active.read_text())['state'], 'complete')

    def test_implicit_submodule_recursion_is_disabled(self):
        self.git(self.repo, 'config', 'submodule.recurse', 'true')
        original = b.source_snapshot(self.repo)
        tx = self.transaction()
        tx.switch()
        tx.restore()
        b.verify_original(self.repo, original)
        self.assertEqual(self.git(self.module, 'rev-parse', 'sub-live'), self.sub_new)

    def test_new_initialization_is_retained_and_recorded(self):
        # Simulate an uninitialized worktree while keeping the fixture's gitdir.
        self.module.rename(self.base / 'saved-module-worktree')
        self.module.mkdir()
        original = b.source_snapshot(self.repo)
        self.assertEqual(original['submodules']['module']['initialization'], 'uninitialized')
        tx = self.transaction()
        tx.switch()
        tx.restore()
        b.verify_original(self.repo, original)
        self.assertEqual(tx.data['new_initializations_retained'], ['module'])
        self.assertEqual(b.git_head(self.module), self.sub_new)

    def test_no_forbidden_git_switch_commands(self):
        real = b.git_bytes
        commands = []
        def recording(repo, *arguments, **kwargs):
            commands.append(arguments)
            return real(repo, *arguments, **kwargs)
        with mock.patch.object(b, 'git_bytes', recording):
            tx = self.transaction()
            tx.switch()
            tx.restore()
        for arguments in commands:
            self.assertFalse({'--force', 'reset', 'clean', 'clone', 'worktree'} & set(arguments))
            if arguments[:2] == ('submodule', 'update'):
                self.assertEqual(arguments, ('submodule', 'update', '--init'))

    def test_deployment_hashes_and_input_manifest_are_independently_verified(self):
        install = self.repo / 'siq3_install/release/C_SDK'
        binary = install / 'bin/siq_test'
        library = install / 'libs/arm64-v8a/libfixture.so'
        binary.parent.mkdir(parents=True)
        library.parent.mkdir(parents=True)
        binary.write_bytes(b'synthetic executable')
        library.write_bytes(b'synthetic shared library')
        receipt = self.base / 'deployment.json'
        manifest = b.build_input_manifest(self.input)
        env = {'_BENCH_REPO_ROOT': str(self.repo), '_BENCH_DEPLOYMENT': str(receipt), '_BENCH_INPUT_DIR': str(self.input)}
        command = 'cd /fixture; ./siq_test -n 1 -p input'
        def response(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0,
                '\n'.join(b.file_sha256(p) + ' /fixture/' + p.name for p in (binary, library)), '')
        with mock.patch.dict(os.environ, env), mock.patch.object(b.subprocess, 'run', response), \
                mock.patch.object(b, 'remote_input_manifest', return_value=manifest):
            b.verify_deployment('fixture-adb', [], command)
        self.assertEqual(json.loads(receipt.read_text())['input_manifest'], manifest)
        bad_receipt = self.base / 'bad-deployment.json'
        env['_BENCH_DEPLOYMENT'] = str(bad_receipt)
        with mock.patch.dict(os.environ, env), mock.patch.object(b.subprocess, 'run', response), \
                mock.patch.object(b, 'remote_input_manifest', return_value={}), self.assertRaises(RuntimeError):
            b.verify_deployment('fixture-adb', [], command)
        self.assertFalse(bad_receipt.exists())

    def test_stash_success_pending_receipt_window_is_durable(self):
        (self.repo / 'source').write_text('dirty\n')
        tx = self.transaction()
        real = tx.save
        def fail_saved():
            if tx.data['state'] == 'stash_saved':
                raise OSError('disk failure after stash before oid receipt write')
            real()
        with mock.patch.object(tx, 'save', fail_saved), self.assertRaises(OSError):
            tx.switch()
        receipt = json.loads(self.active.read_text())
        self.assertEqual(receipt['state'], 'stash_pending')
        self.assertIsNone(receipt['stash_oid'])
        self.assertNotEqual(self.git(self.repo, 'rev-parse', 'refs/stash'), receipt['stash_previous'])
        self.assertEqual(receipt['original']['head'], self.current)

    def fake_single(self, host_exit=0, merge_exit=1, pull_relocated=False):
        source = self.repo / 'src/projects/project/native_test/src'
        source.mkdir(parents=True)
        (source / 'main.cpp').write_text('int main() {\n    test.GeneralTestOneImage(optParam);\n}\n')
        (source / 'post_process_test.cpp').write_text(
            'int PostProcessTest::GeneralTestOneImage() {\n'
            ' int count = optParam.loop_cnt;\n for(int i=0;i<count;i++) {}\n}\n')
        script = self.repo / 'scripts/run_fixture.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/bash\n')
        self.commit(self.repo, 'synthetic native sources')
        csv_text = HEADERS + '\n100,10,1,10,1,22,11,11,0,0\n116,1,20,10,20,22,22,22,1,1\n132,1,1,10,20,22,22,4,2,2\n'
        def logged(command, cwd, env, log, *args, **kwargs):
            log.write_text('PRIVATE_HELPER_TRANSCRIPT\n')
            if log.name == 'run.log':
                log.write_text('RAW_TRANSCRIPT_SENTINEL\n@@SIQ_BENCH_BEGIN@@ mode=2 pid=42 iteration=1\n'
                    'leave process_image 0.5ms\n@@PROCESS_IMAGE_PERFORMANCE@@: 1\n'
                    'leave process_image 1.5ms\n@@PROCESS_IMAGE_PERFORMANCE@@: 2\n'
                    '@@SIQ_BENCH_END@@ mode=2 pid=42 iteration=1 status=0\n' + NATIVE +
                    'close csv file: /data/local/tmp/mem_use_info_fixture_42.csv\n')
                manifest = b.build_input_manifest(self.input)
                Path(env['_BENCH_DEPLOYMENT']).write_text(json.dumps({'sha256': {'siq_test': '0' * 64},
                    'input_frames': 1, 'invocations': 1, 'input_remote_dir': '/fixture/input',
                    'input_manifest': manifest, 'remote_input_manifest': manifest}))
                Path(command[5]).joinpath('perfetto_adapter.perfetto').write_bytes(b'fixture trace')
                return host_exit
            if 'pull' in command:
                if pull_relocated and not log.name.endswith('_relocated.log'):
                    return 1
                Path(command[-1]).write_text(csv_text)
            if log.name.startswith('merge_perfetto'):
                return merge_exit
            return 0
        argv = ['--json', '--no-adbsync', '--repo-root', str(self.repo), '--run-script', str(script),
                '--input-dir', str(self.input), '--adapter', 'adapter', '--siq-project', 'project/adapter',
                '--loops', '2', '--work-dir', str(self.base / 'single-artifacts')]
        return argv, logged

    def test_original_positional_command_is_not_parsed_as_script_arguments_twice(self):
        named, logged = self.fake_single(host_exit=1)
        script = named[named.index('--run-script') + 1]
        argv = [script, 'fixture-user', 'fixture-host', str(self.input), str(self.base / 'output'),
                '1', 'adapter', '0', 'mtk', '--mode', 'repeat_process', '--loops', '2',
                '--json', '--no-adbsync', '--siq-project', 'project/adapter',
                '--work-dir', str(self.base / 'positional-artifacts')]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(b, 'run_logged', logged), \
                mock.patch.object(b, 'collect_device', return_value={'serial': 'fixture', 'transport_id': 1}), \
                mock.patch.object(b.shutil, 'which', return_value='/fixture/tool'), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.main(argv)
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)  # Only the injected trace merge failure.
        self.assertTrue(result['measurement_valid'])
        self.assertEqual(result['experiment']['actual_repeat'], 2)
        self.assertEqual(result['experiment']['adapter'], 'adapter')
        self.assertIsNone(result['primary_error'])

    def test_original_positional_ab_command_is_normalized_before_sides(self):
        script = self.repo / 'scripts/run_fixture.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/bash\n')
        self.commit(self.repo, 'fixture script')
        argv = [str(script), 'fixture-user', 'fixture-host', str(self.input), str(self.base / 'output'),
                '1', 'adapter', '0', 'mtk', '--mode', 'repeat_full', '--json',
                '--ab_test_commit', self.old]
        seen = []
        def run(args, active):
            path, tokens, _ = b.parse_run_command(args)
            seen.append((path, tokens, args.mode, args.input_dir, args.adapter))
            result = b.new_result('ab')
            result.update(status='ok', exit_code=0)
            return result
        with mock.patch.object(b, 'collect_device', return_value={'serial': 'fixture', 'transport_id': 1}), \
                mock.patch.object(b, 'run_ab', side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b.main(argv), 0)
        self.assertEqual(seen[0][0], script)
        self.assertEqual(seen[0][1][0], 'fixture-user')
        self.assertEqual(seen[0][2], 1)
        self.assertEqual(seen[0][3], self.input)
        self.assertEqual(seen[0][4], 'adapter')

    def test_json_native_host_warning_and_merge_failure_keep_measurement(self):
        argv, logged = self.fake_single(host_exit=1)
        before = b.source_snapshot(self.repo)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(b, 'run_logged', logged), mock.patch.object(b, 'collect_device', return_value={'serial': 'fixture', 'transport_id': 1}), \
                mock.patch.object(b.shutil, 'which', return_value='/fixture/tool'), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.main(argv)
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)  # merge warning retains the original exit semantics
        self.assertEqual(result['status'], 'warning')
        self.assertTrue(result['measurement_valid'])
        self.assertEqual(result['run_exit'], 1)
        self.assertEqual(result['validity']['native'], 'valid')
        self.assertEqual(result['validity']['host'], 'warning')
        self.assertEqual(result['validity']['trace'], 'warning')
        self.assertNotIn('RAW_TRANSCRIPT_SENTINEL', stdout.getvalue() + stderr.getvalue())
        self.assertTrue(result['source']['patch']['exact_restore'])
        b.verify_original(self.repo, before)
        persisted = json.loads(Path(result['artifacts']['summary']['path']).read_text())
        self.assertEqual(persisted['exit_code'], code)
        self.assertEqual(result['artifacts']['summary']['sha256'],
                         b.file_sha256(Path(result['artifacts']['summary']['path'])))
        for artifact in ('run_log', 'deployment', 'patch_receipt', 'trace'):
            value = result['artifacts'][artifact]
            self.assertEqual(value['sha256'], b.file_sha256(Path(value['path'])))

    def test_relocated_csv_keeps_both_pull_attempt_logs(self):
        argv, logged = self.fake_single(pull_relocated=True)
        stdout = io.StringIO()
        with mock.patch.object(b, 'run_logged', logged), mock.patch.object(b, 'collect_device', return_value={'serial': 'fixture', 'transport_id': 1}), \
                mock.patch.object(b.shutil, 'which', return_value='/fixture/tool'), contextlib.redirect_stdout(stdout):
            b.main(argv)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result['measurement_valid'])
        directory = Path(result['artifacts']['summary']['path']).parent
        self.assertTrue((directory / 'adb_pull_01.log').exists())
        self.assertTrue((directory / 'adb_pull_01_relocated.log').exists())

    def test_local_process_timeout_and_no_transcript_replay(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.run_logged([os.sys.executable, '-c',
                'import time; print("RAW_TRANSCRIPT_SENTINEL", flush=True); time.sleep(20)'],
                self.base, self.env, self.base / 'timeout.log', timeout_seconds=1, cat_log_on_failure=True)
        self.assertEqual(code, 124)
        self.assertNotIn('RAW_TRANSCRIPT_SENTINEL', stdout.getvalue() + stderr.getvalue())

    def child_program(self, kill=False):
        return (
            'import importlib.util,json,time,sys\n'
            'from pathlib import Path\n'
            'spec=importlib.util.spec_from_file_location("bench",sys.argv[1]); b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)\n'
            'repo=Path(sys.argv[2]); active=repo/".git/siq-benchmark-transaction.json"\n'
            'args=b.build_parser().parse_args(["--json","--ab-test-commit",sys.argv[3]])\n'
            'args.repo_root=repo; args.input_dir=Path(sys.argv[4]); args.work_dir=Path(sys.argv[5]); args.run_script=repo/"run.sh"\n'
            'def wait(args):\n'
            ' print("READY",flush=True)\n'
            ' time.sleep(60)\n'
            ' return b.new_result()\n'
            'b.execute_single=wait\n'
            'with b.interruption_cleanup():\n'
            ' with b.benchmark_lock(repo):\n'
            '  result=b.run_ab(args,active)\n'
            ' print(json.dumps(b.public_result(result)),flush=True)\n')

    def start_child(self):
        process = subprocess.Popen([os.sys.executable, '-c', self.child_program(), str(SCRIPT),
            str(self.repo), self.old, str(self.input), str(self.base / 'child-artifacts')],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(lambda: b.terminate_process_group(process) if process.poll() is None else None)
        # Wait boundedly without leaking a child if initialization fails.
        import select
        ready, _, _ = select.select([process.stdout], [], [], 15)
        self.assertTrue(ready)
        self.assertEqual(process.stdout.readline().strip(), 'READY')
        return process

    def test_real_sigterm_restores_transaction(self):
        (self.repo / 'source').write_text('staged\n')
        self.git(self.repo, 'add', 'source')
        (self.repo / 'source').write_text('unstaged\n')
        original = b.source_snapshot(self.repo)
        process = self.start_child()
        process.terminate()
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result['exit_code'], 130)
        self.assertIsNone(result['recovery_error'])
        b.verify_original(self.repo, original)
        self.assertEqual(json.loads(self.active.read_text())['state'], 'complete')

    def test_real_sigkill_retains_receipt_and_blocks_next_entry(self):
        (self.repo / 'source').write_text('dirty\n')
        process = self.start_child()
        process.kill()
        process.communicate(timeout=15)
        receipt = json.loads(self.active.read_text())
        self.assertEqual(receipt['state'], 'left_running')
        self.assertIsNotNone(receipt['stash_oid'])
        self.assertEqual(receipt['original']['head'], self.current)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(b.shutil, 'which', return_value='/fixture/tool'), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = b.main(['--json', '--repo-root', str(self.repo), '--run-script', str(self.repo / 'run.sh')])
        self.assertEqual(code, 1)
        self.assertIn('unfinished', json.loads(stdout.getvalue())['primary_error']['message'])
        self.assertEqual(b.git_head(self.repo), self.old)
        self.assertEqual(json.loads(self.active.read_text()), receipt)

    def pointer_single(self, args):
        args.work_dir.mkdir(parents=True, exist_ok=True)
        patch = args.work_dir / 'source_patch.json'
        b.atomic_json(patch, {'synthetic': True})
        disk = json.loads(args._active_path.read_text())
        disk.update(active_artifacts=str(args.work_dir), patch_receipt=str(patch))
        b.atomic_json(args._active_path, disk)
        result = valid_result()
        result['_work_dir'] = str(args.work_dir)
        return result

    def test_restoring_source_interruption_keeps_left_pointer_on_disk_and_archive(self):
        real = b.checkout
        def interrupt(repo, oid, branch=None):
            if oid == self.current:
                raise KeyboardInterrupt('fixture recovery interruption')
            return real(repo, oid, branch)
        with mock.patch.object(b, 'execute_single', self.pointer_single), \
                mock.patch.object(b, 'checkout', interrupt):
            result = b.run_ab(self.args(), self.active)
        disk = json.loads(self.active.read_text())
        self.assertEqual(disk['state'], 'restoring_source')
        self.assertEqual(result['recovery_error']['type'], 'Interrupted')
        self.assertEqual(disk.get('active_artifacts'), disk['left_artifacts'])
        self.assertTrue(Path(disk['patch_receipt']).is_file())
        archived = json.loads(Path(result['artifacts']['transaction']['path']).read_text())
        self.assertEqual(archived['patch_receipt'], disk['patch_receipt'])

    def test_right_complete_and_archive_retain_current_execution_pointer(self):
        with mock.patch.object(b, 'execute_single', self.pointer_single):
            result = b.run_ab(self.args(), self.active)
        disk = json.loads(self.active.read_text())
        archived = json.loads(Path(result['artifacts']['transaction']['path']).read_text())
        self.assertTrue(result['measurement_valid'])
        self.assertEqual(disk['state'], 'complete')
        self.assertEqual(disk.get('active_artifacts'), disk['right_artifacts'])
        self.assertNotEqual(disk['left_artifacts'], disk['right_artifacts'])
        self.assertEqual(disk['patch_receipt'], str(Path(disk['right_artifacts']) / 'source_patch.json'))
        self.assertEqual(archived, disk)
        self.assertTrue(Path(disk['patch_receipt']).is_file())

    def test_right_recovery_failure_archive_retains_right_pointer(self):
        failure = {'type': 'FixtureRecovery', 'message': 'right restoration failed'}
        def run(args):
            result = self.pointer_single(args)
            if args.work_dir.name == 'right':
                result['recovery_error'] = failure
                result['measurement_valid'] = False
            return result
        with mock.patch.object(b, 'execute_single', run):
            result = b.run_ab(self.args(), self.active)
        disk = json.loads(self.active.read_text())
        archived = json.loads(Path(result['artifacts']['transaction']['path']).read_text())
        self.assertFalse(result['measurement_valid'])
        self.assertEqual(result['recovery_error'], failure)
        self.assertEqual(disk['state'], 'right_running')
        self.assertEqual(disk['active_artifacts'], disk['right_artifacts'])
        self.assertEqual(disk['patch_receipt'], str(Path(disk['right_artifacts']) / 'source_patch.json'))
        self.assertEqual(archived, disk)

    def test_archive_refresh_failure_does_not_mask_errors_or_reuse_left_pointer(self):
        real_save = b.ABTransaction.save
        failure = {'type': 'FixtureRecovery', 'message': 'right restoration failed'}
        primary = {'type': 'FixturePrimary', 'message': 'right execution failed'}
        for mode in ('write_error', 'wrong_id', 'interrupt'):
            def run(args):
                result = self.pointer_single(args)
                if args.work_dir.name == 'right':
                    result.update(recovery_error=failure, primary_error=primary, measurement_valid=False)
                    if mode == 'wrong_id':
                        disk = json.loads(self.active.read_text())
                        disk['transaction_id'] = 'foreign-transaction'
                        b.atomic_json(self.active, disk)
                return result
            def save(tx, initial=False):
                if mode in ('write_error', 'interrupt') and tx.data['state'] == 'right_running' and tx.data['recovery_error']:
                    if mode == 'interrupt':
                        raise KeyboardInterrupt('fixture final receipt interruption')
                    raise OSError('fixture final receipt write failure')
                return real_save(tx, initial=initial)
            args = self.args()
            args.work_dir = self.base / ('archive-' + mode)
            with self.subTest(mode=mode), mock.patch.object(b, 'execute_single', run), \
                    mock.patch.object(b.ABTransaction, 'save', save):
                result = b.run_ab(args, self.active)
            archived = json.loads(Path(result['artifacts']['transaction']['path']).read_text())
            disk = json.loads(self.active.read_text())
            self.assertEqual(result['primary_error'], primary)
            self.assertEqual(result['recovery_error'], failure)
            self.assertEqual(result['exit_code'], 130 if mode == 'interrupt' else 1)
            self.assertEqual(result['status'], 'interrupted' if mode == 'interrupt' else 'failed')
            self.assertFalse(archived['execution_pointers_verified'])
            self.assertNotIn('active_artifacts', archived)
            self.assertNotIn('patch_receipt', archived)
            self.assertIn('receipt_write_error', archived)
            self.assertEqual(disk['active_artifacts'], archived['right_artifacts'])
            self.assertEqual(disk['state'], 'right_running')
            if mode == 'wrong_id':
                self.assertEqual(disk['transaction_id'], 'foreign-transaction')

    def test_orchestrator_passes_sampling_tolerance_and_reports_quality_warning(self):
        for tolerance, interval, comparable in ((3, 15, True), (2, 100, False)):
            calls = []
            def run(args):
                result = valid_result(10 if not calls else 1)
                result['sampling'] = sampling_fixture(5 if not calls else interval)
                calls.append(args)
                return result
            args = self.args()
            args.sampling_quality_tolerance = tolerance
            with self.subTest(tolerance=tolerance), mock.patch.object(b, 'execute_single', run):
                result = b.run_ab(args, self.active)
                quality = result['comparison']['sampling_quality']
                self.assertEqual(quality['tolerance'], tolerance)
                self.assertEqual(quality['comparable'], comparable)
                self.assertTrue(result['measurement_valid'])
                self.assertEqual(result['comparison']['metrics'][0]['status'], 'ok')
                self.assertTrue(any('sampling coverage is unverified' in w for w in result['warnings']))

    def test_orchestrator_restores_dirty_before_right_and_isolates_artifacts(self):
        (self.repo / 'source').write_text('staged\n')
        self.git(self.repo, 'add', 'source')
        (self.repo / 'source').write_text('worktree\n')
        original = b.source_snapshot(self.repo)
        calls = []
        def run(args):
            calls.append((b.git_head(args.repo_root), args.work_dir, args.output_dir))
            if len(calls) == 1:
                self.assertFalse(b.tracked_dirty(self.repo))
                self.assertEqual(b.git_head(self.module), self.sub_old)
            else:
                b.verify_original(self.repo, original)
            return valid_result(10 + len(calls))
        with mock.patch.object(b, 'execute_single', run):
            result = b.run_ab(self.args(), self.active)
        self.assertEqual([c[0] for c in calls], [self.old, self.current])
        self.assertNotEqual(calls[0][1:], calls[1][1:])
        self.assertTrue(result['measurement_valid'])
        b.verify_original(self.repo, original)


if __name__ == '__main__':
    unittest.main()
