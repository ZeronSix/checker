from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import random
import xml.etree.ElementTree as ET

from ...exceptions import (
    RunFailedError,
    BuildFailedError,
    ExecutionFailedError,
    TestsFailedError,
    TimeoutExpiredError,
)
from ...utils.files import copy_files
from ...utils.print import print_info
from ..tester import Tester
from ...executors.sandbox import Sandbox


class CppStrategy:
    @abstractmethod
    def check_config(
        self,
        test_config: Cpp2Tester.TaskTestConfig,
    ) -> None:  # pragma: nocover
        pass

    @abstractmethod
    def gen_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            source_dir: Path,
            public_tests_dir: Path,
            private_tests_dir: Path,
            sandbox: bool = True,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> None:  # pragma: nocover
        pass

    @abstractmethod
    def clean_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            verbose: bool = False,
    ) -> None:  # pragma: nocover
        pass

    @abstractmethod
    def run_tests(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            sandbox: bool = False,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> float:  # pragma: nocover
        pass


class ComposeStrategy(CppStrategy):
    def __init__(self, strategies: list[CppStrategy]):
        assert strategies
        self.strategies = strategies

    @abstractmethod
    def check_config(
        self,
        test_config: Cpp2Tester.TaskTestConfig,
    ) -> None:  # pragma: nocover
        for s in self.strategies:
            s.check_config(test_config)

    @abstractmethod
    def gen_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            source_dir: Path,
            public_tests_dir: Path,
            private_tests_dir: Path,
            sandbox: bool = True,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> None:  # pragma: nocover
        for s in self.strategies:
            s.gen_build(
                executor=executor,
                test_config=test_config,
                build_dir=build_dir,
                source_dir=source_dir,
                public_tests_dir=public_tests_dir,
                private_tests_dir=private_tests_dir,
                sandbox=sandbox,
                verbose=verbose,
                normalize_output=normalize_output,
            )

    @abstractmethod
    def clean_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            verbose: bool = False,
    ) -> None:  # pragma: nocover
        for s in self.strategies:
            s.clean_build(
                executor=executor,
                test_config=test_config,
                build_dir=build_dir,
                verbose=verbose,
            )

    @abstractmethod
    def run_tests(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            sandbox: bool = False,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> float:  # pragma: nocover
        return min([s.run_tests(
            executor=executor,
            test_config=test_config,
            build_dir=build_dir,
            sandbox=sandbox,
            verbose=verbose,
            normalize_output=normalize_output,
        ) for s in self.strategies])


class FlagStrategy(CppStrategy):
    @abstractmethod
    def check_config(
        self,
        test_config: Cpp2Tester.TaskTestConfig,
    ) -> None:  # pragma: nocover
        assert test_config.answer

    @abstractmethod
    def gen_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            source_dir: Path,
            public_tests_dir: Path,
            private_tests_dir: Path,
            sandbox: bool = True,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> None:  # pragma: nocover
        path = source_dir / 'flag.txt'
        if not path.exists():
            raise RunFailedError('\nCan\'t find flag.txt')
        with open(path, 'r') as f:
            lines = f.read().splitlines()
        self.flag = "" if not lines else lines[0]

    @abstractmethod
    def clean_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            verbose: bool = False,
    ) -> None:  # pragma: nocover
        pass

    @abstractmethod
    def run_tests(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            sandbox: bool = False,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> float:  # pragma: nocover
        if self.flag == test_config.answer:
            print_info('Flag OK', color='green')
            return 1.
        raise TestsFailedError(f'\nWrong flag: {self.flag}')


class BenchStrategy(CppStrategy):
    def check_config(
        self,
        test_config: Cpp2Tester.TaskTestConfig,
    ) -> None:
        assert test_config.tests
        assert test_config.allow_change

    def gen_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            source_dir: Path,
            public_tests_dir: Path,
            private_tests_dir: Path,
            sandbox: bool = True,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> None:
        self.reference_root = public_tests_dir.parent
        task_dir = self.reference_root / source_dir.name

        print_info('Copying task files...', color='orange')
        executor(
            copy_files,
            source=source_dir,
            target=task_dir,
            patterns=test_config.allow_change,
            verbose=verbose,
            print_files=True,
        )

        for test_binary, build_type in test_config.tests:
            build_dir = self.reference_root / f'build-{build_type.lower()}'
            try:
                print_info(f'Building {test_binary} ({build_type})...', color='orange')
                executor(
                    ['ninja', '-v', test_binary],
                    cwd=build_dir,
                    verbose=verbose,
                )
            except ExecutionFailedError:
                raise BuildFailedError(f'Can\'t build {test_binary}')

        error_messages: list[str] = []

        files: list[str] = []
        for r in test_config.allow_change:
            files += list(map(str, task_dir.glob(r)))
        lint_files = []
        for f in test_config.lint_files:
            lint_files += list(map(str, task_dir.glob(f)))

        try:
            print_info('Running clang format...', color='orange')
            format_path = str(self.reference_root / 'run-clang-format.py')
            executor(
                [format_path, '-r', *lint_files],
                cwd=build_dir,
                verbose=verbose,
            )
            print_info('[No issues]')
        except ExecutionFailedError:
            error_messages.append('Style error (clang format)')

        if files:
            if not test_config.nolint:
                try:
                    print_info('Running clang tidy...', color='orange')
                    executor(
                        ['clang-tidy', '-p', '.', *lint_files],
                        cwd=build_dir,
                        verbose=verbose,
                    )
                    print_info('[No issues]')
                except ExecutionFailedError:
                    error_messages.append('Style error (clang tidy)')

            forbidden: list[str] = []
            for f in test_config.forbidden:
                forbidden += ['-f', f]
            for f in test_config.forbidden_files:
                forbidden += ['-ff', f]
            try:
                print_info('Checking prohibited features...', color='orange')
                executor(
                    ['./check_forbidden', '-p', '.', *(forbidden + files)],
                    cwd=self.reference_root / 'build-relwithdebinfo',
                    verbose=verbose,
                )
                print_info('[No issues]')
            except ExecutionFailedError:
                error_messages.append('Using of prohibited features')

        if not error_messages:
            return
        elif len(error_messages) > 1:
            error_messages = [str(i + 1) + ') ' + m for i, m in enumerate(error_messages)]
        raise RunFailedError('\n\n' + '\n'.join(error_messages))

    def clean_build(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            verbose: bool = False,
    ) -> None:
        executor(
            ['rm', '-rf', str(build_dir)],
            check=False,
            verbose=verbose,
        )

    @staticmethod
    def _cat(
        regexp: str,
        executor: Sandbox,
        build_dir: Path,
        verbose: bool,
    ):
        files = [str(f) for f in build_dir.rglob(regexp)]
        if not files:
            return
        executor(
            ['cat', *files],
            sandbox=True,
            cwd=build_dir,
            verbose=verbose,
            capture_output=False,
        )

    def run_tests(
            self,
            executor: Sandbox,
            test_config: Cpp2Tester.TaskTestConfig,
            build_dir: Path,
            sandbox: bool = False,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> float:
        report_path: Path = None
        for test_binary, build_type in test_config.tests:
            t = build_type.lower()
            build_dir = self.reference_root / f'build-{t}'
            r = random.randint(0, 10 ** 20)
            if t == 'relwithdebinfo':
                report_path = f'/tmp/report_{r}.xml'
            try:
                print_info(f'Running {test_binary} ({build_type})...', color='orange')
                executor([
                        str(build_dir / test_binary),
                        '-r', f'xml::out=/tmp/report_{r}.xml',
                        '-r', f'console::out=/tmp/report_{r}.txt::colour-mode=ansi',
                    ],
                    sandbox=sandbox,
                    cwd=build_dir,
                    verbose=verbose,
                    capture_output=True,
                    timeout=test_config.timeout,
                    env={
                        'UBSAN_OPTIONS': f'log_path=/tmp/ubsan_{r},color=always,print_stacktrace=1',
                        'ASAN_OPTIONS': f'log_path=/tmp/asan_{r},color=always',
                        'TSAN_OPTIONS': f'log_path=/tmp/tsan_{r},color=always',
                    }
                )
            except TimeoutExpiredError:
                message = f'\nYour solution exceeded time limit: {test_config.timeout} seconds'
                raise TestsFailedError(message)
            except ExecutionFailedError:
                raise TestsFailedError('\nTest failed')
            finally:
                for file in [f'report_{r}.txt', f'ubsan_{r}.*', f'asan_{r}.*', f'tsan_{r}.*']:
                    BenchStrategy._cat(file, executor, Path("/tmp"), verbose)

        if not test_config.bench:
            print_info('OK', color='green')
            return 1.
        elif report_path is None:
            raise RunFailedError('\nCannot find bench result')

        bench_results: dict[str, float] = {}
        for b in ET.parse(report_path).iter('BenchmarkResults'):
            bench_results[b.get('name')] = float(b.find('mean').get('value'))
        if set(bench_results.keys()) != set(test_config.bench):
            raise RunFailedError('\nCannot find bench result')

        error_messages = []
        for name, time in bench_results.items():
            time *= 1e-9
            threshold = test_config.bench[name]
            if threshold >= 0 and time > threshold:
                error_messages.append(f'Bench {name}: {time:g} > {threshold:g}')
            elif threshold < 0 and time < -threshold:
                error_messages.append(f'Bench {name}: {time:g} < {-threshold:g}')
        if error_messages:
            raise TestsFailedError('\n\n' + '\n'.join(error_messages))

        print_info('OK', color='green')
        return 1.


class Cpp2Tester(Tester):

    @dataclass
    class TaskTestConfig(Tester.TaskTestConfig):
        task_type: str
        allow_change: list[str] = field(default_factory=list)
        forbidden: list[str] = field(default_factory=list)
        forbidden_files: list[str] = field(default_factory=list)
        tests: list[tuple[str, str]] = field(default_factory=list)
        timeout: float = 180.
        bench: dict[str, float] = field(default_factory=dict)
        answer: str = ''
        nolint: bool = False
        lint_files: list[str] = field(default_factory=lambda: ['**/*.h', '**/*.cpp'])

        def _get_strategy(self, name: str) -> CppStrategy:
            if name == 'bench':
                return BenchStrategy()
            elif name == 'flag':
                return FlagStrategy()
            else:
                raise RunFailedError('Unknown strategy')

        def _get_compose(self, names: list[str]) -> ComposeStrategy:
            strategies = [self._get_strategy(name) for name in names]
            return ComposeStrategy(strategies)

        def __post_init__(self) -> None:
            names = self.task_type.split(',')
            if len(names) == 1:
                self.strategy = self._get_strategy(names[0])
            else:
                self.strategy = self._get_compose(names)
            self.strategy.check_config(self)

    def _gen_build(  # type: ignore[override]
            self,
            test_config: TaskTestConfig,
            build_dir: Path,
            source_dir: Path,
            public_tests_dir: Path,
            private_tests_dir: Path,
            sandbox: bool = True,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> None:
        test_config.strategy.gen_build(
            self._executor,
            test_config,
            build_dir,
            source_dir,
            public_tests_dir,
            private_tests_dir,
            sandbox=sandbox,
            verbose=verbose,
            normalize_output=normalize_output,
        )

    def _clean_build(  # type: ignore[override]
            self,
            test_config: TaskTestConfig,
            build_dir: Path,
            verbose: bool = False,
    ) -> None:
        test_config.strategy.clean_build(
            self._executor,
            test_config,
            build_dir,
            verbose=verbose,
        )

    def _run_tests(  # type: ignore[override]
            self,
            test_config: TaskTestConfig,
            build_dir: Path,
            sandbox: bool = False,
            verbose: bool = False,
            normalize_output: bool = False,
    ) -> float:
        return test_config.strategy.run_tests(
            self._executor,
            test_config,
            build_dir,
            sandbox=sandbox,
            verbose=verbose,
            normalize_output=normalize_output,
        )
