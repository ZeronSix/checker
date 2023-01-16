from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import random

from ...exceptions import (
    BuildFailedError,
    ExecutionFailedError,
    StylecheckFailedError,
    TestsFailedError,
    TimeoutExpiredError,
)
from ...utils.files import check_files_contains_regexp, copy_files
from ...utils.print import print_info
from ..tester import Tester

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
        check_files_contains_regexp(
            source_dir,
            regexps=test_config.forbidden_regexp,
            patterns=test_config.allow_change,
            raise_on_found=True,
        )
        self.reference_root = public_tests_dir.parent
        task_name = source_dir.name
        task_dir = self.reference_root / task_name
        executor(
            copy_files,
            source=source_dir,
            target=task_dir,
            patterns=test_config.allow_change,
            verbose=verbose,
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

        try:
            print_info('Running clang format...', color='orange')
            format_path = self.reference_root / 'run-clang-format.py'
            executor(
                [str(format_path), '-r', str(task_dir)],
                cwd=build_dir,
                verbose=verbose,
            )
            print_info('[No issues]')
        except ExecutionFailedError:
            raise StylecheckFailedError('Style error (clang format)')

        try:
            print_info('Running clang tidy...', color='orange')
            files = [str(file) for file in task_dir.rglob('*.cpp')]
            executor(
                ['clang-tidy', '-p', '.', *files],
                cwd=build_dir,
                verbose=verbose,
            )
            print_info('[No issues]')
        except ExecutionFailedError:
            raise StylecheckFailedError('Style error (clang tidy)')

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
        capture_output: bool,
    ):
        files = [str(f) for f in build_dir.rglob(regexp)]
        if not files:
            return
        executor(
            ['cat', *files],
            sandbox=True,
            cwd=build_dir,
            verbose=verbose,
            capture_output=capture_output,
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
        for test_binary, build_type in test_config.tests:
            build_dir = self.reference_root / f'build-{build_type.lower()}'
            r = random.randint(0, 10 ** 20)
            try:
                print_info(f'Running {test_binary} ({build_type})...', color='orange')
                executor([
                        str(build_dir / test_binary),
                        '-r', f'xml::out=report_{r}.xml',
                        '-r', f'console::out=report_{r}.txt::colour-mode=ansi',
                    ],
                    sandbox=True,
                    cwd=build_dir,
                    verbose=verbose,
                    capture_output=True,
                    timeout=test_config.timeout,
                    env={
                        'ASAN_OPTIONS': f'log_path=asan_{r},color=always',
                        'TSAN_OPTIONS': f'log_path=tsan_{r},color=always',
                    }
                )
            except TimeoutExpiredError:
                message = f'Your solution exceeded time limit: {test_config.timeout} seconds'
                raise TestsFailedError(message)
            except ExecutionFailedError:
                raise TestsFailedError('Test failed')
            finally:
                for file in [f'report_{r}.txt', f'asan_{r}.*', f'tsan_{r}.*']:
                    BenchStrategy._cat(file, executor, build_dir, verbose, normalize_output)
        print_info('OK', color='green')
        return 1.


class Cpp2Tester(Tester):

    @dataclass
    class TaskTestConfig(Tester.TaskTestConfig):
        task_type: str
        allow_change: list[str] = field(default_factory=list)
        forbidden_regexp: list[str] = field(default_factory=list)
        tests: list[tuple[str, str]] = field(default_factory=list)
        timeout: float = 60.

        def __post_init__(self) -> None:
            assert self.task_type in ['bench']
            if self.task_type == 'bench':
                self.strategy = BenchStrategy()
            else:
                self.strategy = BenchStrategy()
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
            normalize_output=False,
        )
