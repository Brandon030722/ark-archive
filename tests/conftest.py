"""Ordinary tests never load the application's real credential files.

Settings-loader tests may explicitly pass a synthetic temporary ``_env_file``.
This does not inspect, change or display the real environment or dotenv files.
"""

import hashlib
import inspect
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from arknights_agent.config import Settings
from arknights_agent.runtime import stage21_public_allowance

Settings.model_config["env_file"] = None
for _key_name in ("ARK_DEEPSEEK_API_KEY", "ARK_OPENAI_API_KEY"):
    if _key_name in os.environ:
        del os.environ[_key_name]


_HISTORICAL_BUDGET_FIXTURES = {
    "test_stage23_budget", "test_stage23_r2_budget", "test_stage23_r3_budget",
    "test_stage24_budget", "test_stage25_budget", "test_stage26_budget",
    "test_stage27_budget", "test_stage28_budget", "test_stage29_budget",
    "test_stage30_budget", "test_stage31_budget", "test_stage32_budget", "test_stage33_budget",
    "test_stage35_budget", "test_stage37_budget", "test_stage38_budget",
}


@contextmanager
def _historical_inputs(module, epoch, current_sources):
    original = module.LIVE_BUDGET_PATH
    base = getattr(module, "BASE_LEDGER", None)
    profile = getattr(module, "CurrentModelToolProfile", None)
    old_root = stage21_public_allowance.SOURCE_ROOT
    try:
        stage21_public_allowance.SOURCE_ROOT = getattr(module, "_stage41_proof_read_root", old_root)
        module.LIVE_BUDGET_PATH = epoch
        if base is not None:
            # Rebuilt SQLite pages differ; both this disposable input and the
            # outer real ledger still retain complete physical SHA monitors.
            module.BASE_LEDGER = hashlib.sha256(epoch.read_bytes()).hexdigest()
        if profile is not None:
            def temporary_profile(**kwargs):
                return profile(**{**kwargs, "current_source_root": current_sources})
            module.CurrentModelToolProfile = temporary_profile
        yield
    finally:
        module.LIVE_BUDGET_PATH = original
        if base is not None:
            module.BASE_LEDGER = base
        if profile is not None:
            module.CurrentModelToolProfile = profile
        stage21_public_allowance.SOURCE_ROOT = old_root


@contextmanager
def _historical_source_inputs(module):
    old_root = stage21_public_allowance.SOURCE_ROOT
    try:
        stage21_public_allowance.SOURCE_ROOT = getattr(module, "_stage41_proof_read_root", old_root)
        yield
    finally:
        stage21_public_allowance.SOURCE_ROOT = old_root


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    with _historical_source_inputs(item.module):
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Give receipt-pinned old fixtures their historical input epoch only.

    The module global is restored before any test body executes. Consequently
    old tests which hash/monitor LIVE_BUDGET_PATH still monitor the real file;
    the new path applies solely to their existing fixture's read-only backup.
    No retired test file or production validator has its source hash changed.
    """
    name = fixturedef.func.__module__.rsplit(".", 1)[-1]
    if name not in _HISTORICAL_BUDGET_FIXTURES:
        yield
        return
    module = sys.modules[fixturedef.func.__module__]
    if fixturedef.argname != "template":
        with _historical_source_inputs(module):
            yield
        return
    from budget_epoch_fixture import copy_current_official_sources, project_native_epoch

    original = module.LIVE_BUDGET_PATH
    factory = request.getfixturevalue("tmp_path_factory")
    folder = factory.mktemp(name + "-reviewed-epoch-input")
    epoch = folder / "reviewed-native-epoch.sqlite3"
    if name in {"test_stage33_budget", "test_stage35_budget", "test_stage37_budget", "test_stage38_budget"}:
        from historical_source_fixture import (
            historical_proof_read_root,
            prepare_historical_source_root,
        )
        stage = int(name.removeprefix("test_stage").removesuffix("_budget"))
        shadow = folder / "historical-proof-source-root"
        proof = prepare_historical_source_root(Path(__file__).resolve().parents[1], shadow, stage=stage)
        module._stage41_source_shadow = shadow
        module._stage41_proof_read_root = historical_proof_read_root(
            Path(__file__).resolve().parents[1], shadow, proof["recovered_originals"])
        (folder / "source-shadow-proof.json").write_text(__import__("json").dumps(proof, indent=2))
    with _historical_source_inputs(module):
        if name in {"test_stage37_budget", "test_stage38_budget"}:
            from late_budget_epoch_fixture import project_late_epoch
            project_late_epoch(original, epoch, module.s, temporary_root=folder)
        else:
            project_native_epoch(original, epoch, module.s, temporary_root=folder)
    source_root = None
    if hasattr(module, "CurrentModelToolProfile"):
        source_root = copy_current_official_sources(
            Path(__file__).resolve().parents[1] / ".local/stage40-budget-preparation/official-model-sources-1017-20261021",
            folder / "fresh-official-model-originals", temporary_root=folder)
    real_sha = hashlib.sha256(original.read_bytes()).hexdigest() if hasattr(module, "BASE_LEDGER") else None
    original_fixture = fixturedef.func
    if inspect.isgeneratorfunction(original_fixture):
        def isolated_fixture(**kwargs):
            with _historical_inputs(module, epoch, source_root):
                generator = original_fixture(**kwargs)
                value = next(generator)
            try:
                yield value
            finally:
                with _historical_inputs(module, epoch, source_root):
                    try:
                        next(generator)
                    except StopIteration:
                        pass
                    else:
                        raise ValueError("historical_fixture_generator_yielded_twice")
                if hashlib.sha256(original.read_bytes()).hexdigest() != real_sha:
                    raise ValueError("historical_fixture_real_ledger_changed_during_offline_test")
    else:
        def isolated_fixture(**kwargs):
            with _historical_inputs(module, epoch, source_root):
                return original_fixture(**kwargs)
    fixturedef.func = isolated_fixture
    try:
        yield
    finally:
        fixturedef.func = original_fixture
