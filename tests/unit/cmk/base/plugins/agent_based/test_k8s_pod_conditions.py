#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2021 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# pylint: disable=comparison-with-callable,redefined-outer-name

import json

import pytest

from cmk.base.plugins.agent_based import k8s_pod_conditions
from cmk.base.plugins.agent_based.agent_based_api.v1 import render, State

READY = {
    "status": True,
    "reason": None,
    "detail": None,
}

NOT_READY = {
    "status": False,
    "reason": "MuchReason",
    "detail": "wow detail many detailed",
}

MINUTE = 60


@pytest.fixture
def status():
    return True


@pytest.fixture
def status_initialized(status):
    return status


@pytest.fixture
def status_scheduled(status):
    return status


@pytest.fixture
def status_containersready(status):
    return status


@pytest.fixture
def status_ready(status):
    return status


@pytest.fixture
def string_table_element(
    status_initialized, status_scheduled, status_containersready, status_ready
):
    status_scheduled = status_initialized and status_scheduled
    status_containersready = status_scheduled and status_containersready
    status_ready = status_containersready and status_ready
    return {
        "initialized": READY if status_initialized else NOT_READY,
        "scheduled": READY if status_scheduled else NOT_READY,
        "containersready": READY if status_containersready else NOT_READY,
        "ready": READY if status_ready else NOT_READY,
    }


@pytest.fixture
def string_table(string_table_element):
    return [[json.dumps(string_table_element)]]


@pytest.fixture
def section(string_table):
    return k8s_pod_conditions.parse(string_table)


def test_parse(string_table):
    section = k8s_pod_conditions.parse(string_table)
    assert section.initialized == READY
    assert section.scheduled == READY
    assert section.containersready == READY
    assert section.ready == READY


@pytest.mark.parametrize(
    """
        status_initialized,
        status_scheduled,
        status_containersready,
        status_ready,
        expected_initialized,
        expected_scheduled,
        expected_containersready,
        expected_ready,
    """,
    [
        (True, True, True, True, READY, READY, READY, READY),
        (True, True, True, False, READY, READY, READY, NOT_READY),
        (True, True, False, True, READY, READY, NOT_READY, NOT_READY),
        (True, False, True, True, READY, NOT_READY, NOT_READY, NOT_READY),
        (False, True, True, True, NOT_READY, NOT_READY, NOT_READY, NOT_READY),
    ],
)
def test_parse_multi(
    expected_initialized,
    expected_scheduled,
    expected_containersready,
    expected_ready,
    string_table,
):
    section = k8s_pod_conditions.parse(string_table)
    assert section.initialized == expected_initialized
    assert section.scheduled == expected_scheduled
    assert section.containersready == expected_containersready
    assert section.ready == expected_ready


def test_discovery_returns_an_iterable(string_table):
    parsed = k8s_pod_conditions.parse(string_table)
    assert list(k8s_pod_conditions.discovery(parsed))


OK = 0
WARN = 3
CRIT = 5


@pytest.fixture
def params():
    return {
        "initialized": (WARN * MINUTE, CRIT * MINUTE),
        "scheduled": (WARN * MINUTE, CRIT * MINUTE),
        "containersready": (WARN * MINUTE, CRIT * MINUTE),
        "ready": (WARN * MINUTE, CRIT * MINUTE),
    }


@pytest.fixture
def check_result(params, section):
    return k8s_pod_conditions.check(params, section)


@pytest.mark.parametrize("status", [True, False])
def test_check_yields_check_results(check_result, section):
    assert len(list(check_result)) == len(section.dict())


@pytest.mark.parametrize("status", [True, False])
def test_check_all_states_ok(check_result):
    assert all(r.state == State.OK for r in check_result)


@pytest.mark.parametrize(
    "status, suffix",
    [
        (True, "condition passed"),
        (
            False,
            f"condition not passed ({NOT_READY['reason']}: {NOT_READY['detail']}) for 0 seconds",
        ),
    ],
)
def test_check_all_results_with_summary(status, suffix, check_result, section):
    check_result = list(check_result)
    assert all(r.summary.endswith(suffix) for r in check_result)


TIMESTAMP = 359
RECENT = {"timestamp": TIMESTAMP - OK * MINUTE}
STALE_WARN = {"timestamp": TIMESTAMP - WARN * MINUTE}
STALE_CRIT = {"timestamp": TIMESTAMP - CRIT * MINUTE}


@pytest.fixture(autouse=True)
def time(mocker):
    def time_side_effect():
        timestamp = TIMESTAMP
        while True:
            yield timestamp
            timestamp += MINUTE

    time_mock = mocker.Mock(side_effect=time_side_effect())
    mocker.patch.object(k8s_pod_conditions, "time", time_mock)
    return time_mock


@pytest.fixture
def state():
    return OK


@pytest.fixture
def state_initialized(state):
    return state


@pytest.fixture
def state_scheduled(state):
    return state


@pytest.fixture
def state_containersready(state):
    return state


@pytest.fixture
def state_ready(state):
    return state


@pytest.fixture
def value_store(
    state_initialized,
    state_scheduled,
    state_containersready,
    state_ready,
):
    def state_to_value(state):
        if state == CRIT:
            return STALE_CRIT.copy()
        if state == WARN:
            return STALE_WARN.copy()
        return RECENT.copy()

    return {
        "k8s_pod_conditions_initialized": state_to_value(state_initialized),
        "k8s_pod_conditions_scheduled": state_to_value(state_scheduled),
        "k8s_pod_conditions_containersready": state_to_value(state_containersready),
        "k8s_pod_conditions_ready": state_to_value(state_ready),
    }


@pytest.fixture(autouse=True)
def get_value_store(value_store, mocker):
    get_value_store_mock = mocker.Mock(return_value=value_store)
    mocker.patch.object(k8s_pod_conditions, "get_value_store", get_value_store_mock)
    return get_value_store_mock


@pytest.mark.parametrize("status", [True])
@pytest.mark.parametrize("state", [0, WARN, CRIT])
def test_check_results_state_ok_when_status_true(check_result):
    assert all(r.state == State.OK for r in check_result)


@pytest.mark.parametrize("status", [False])
@pytest.mark.parametrize(
    "state, expected_state",
    [
        (OK, State.OK),
        (WARN, State.WARN),
        (CRIT, State.CRIT),
    ],
)
def test_check_results_sets_state_when_status_false(expected_state, check_result):
    assert all(r.state == expected_state for r in check_result)


@pytest.mark.parametrize("status", [False])
@pytest.mark.parametrize("state", [OK, WARN, CRIT])
def test_check_results_sets_summary_when_status_false(state, check_result):
    time_diff = render.timespan(state * MINUTE)
    expected_content = (
        f"condition not passed ({NOT_READY['reason']}: {NOT_READY['detail']}) for {time_diff}"
    )
    assert all(expected_content in r.summary for r in check_result)


@pytest.mark.parametrize("status", [False])
@pytest.mark.parametrize(
    """
        state_initialized,
        state_scheduled,
        state_containersready,
        state_ready,
        expected_state_initialized,
        expected_state_scheduled,
        expected_state_containersready,
        expected_state_ready,
    """,
    [
        (OK, OK, OK, OK, State.OK, State.OK, State.OK, State.OK),
        (OK, OK, OK, WARN, State.OK, State.OK, State.OK, State.WARN),
        (OK, OK, WARN, WARN, State.OK, State.OK, State.WARN, State.WARN),
        (OK, WARN, WARN, WARN, State.OK, State.WARN, State.WARN, State.WARN),
        (WARN, WARN, WARN, WARN, State.WARN, State.WARN, State.WARN, State.WARN),
        (WARN, WARN, WARN, WARN, State.WARN, State.WARN, State.WARN, State.WARN),
        (WARN, WARN, WARN, CRIT, State.WARN, State.WARN, State.WARN, State.CRIT),
        (WARN, WARN, CRIT, CRIT, State.WARN, State.WARN, State.CRIT, State.CRIT),
        (WARN, CRIT, CRIT, CRIT, State.WARN, State.CRIT, State.CRIT, State.CRIT),
        (CRIT, CRIT, CRIT, CRIT, State.CRIT, State.CRIT, State.CRIT, State.CRIT),
    ],
)
def test_check_results_state_multi_when_status_false(
    expected_state_initialized,
    expected_state_scheduled,
    expected_state_containersready,
    expected_state_ready,
    check_result,
):
    expected_states = [
        expected_state_initialized,
        expected_state_scheduled,
        expected_state_containersready,
        expected_state_ready,
    ]
    assert [r.state for r in check_result] == expected_states


@pytest.mark.parametrize("status", [True])
@pytest.mark.parametrize("state", [OK, WARN, CRIT])
def test_check_results_removes_value_store_timestamp(value_store, check_result):
    list(check_result)
    assert all("timestamp" not in value for value in value_store.values())


@pytest.mark.parametrize("status", [False])
@pytest.mark.parametrize("state", [OK, WARN, CRIT])
def test_check_results_updates_value_store_timestamp(state, value_store, check_result):
    list(check_result)
    expected_timestamp = TIMESTAMP - state * MINUTE
    assert all(value["timestamp"] == expected_timestamp for value in value_store.values())


@pytest.mark.parametrize("status", [True])
@pytest.mark.parametrize("value_store", [{}])
def test_check_results_does_not_set_value_store_timestamp(value_store, check_result):
    list(check_result)
    assert all("timestamp" not in value for value in value_store.values())


@pytest.mark.parametrize("status", [False])
@pytest.mark.parametrize("value_store", [{}])
def test_check_results_sets_value_store_timestamp(value_store, check_result):
    list(check_result)
    assert all(value["timestamp"] == TIMESTAMP for value in value_store.values())


@pytest.fixture
def agent_section(fix_register):
    for name, section in fix_register.agent_sections.items():
        if str(name) == "k8s_pod_conditions_v1":
            return section
    assert False, "Should be able to find the section"


def test_register_agent_section_calls(agent_section):
    assert str(agent_section.name) == "k8s_pod_conditions_v1"
    assert str(agent_section.parsed_section_name) == "k8s_pod_conditions"
    assert agent_section.parse_function == k8s_pod_conditions.parse


@pytest.fixture
def check_plugin(fix_register):
    for name, plugin in fix_register.check_plugins.items():
        if str(name) == "k8s_pod_conditions":
            return plugin
    assert False, "Should be able to find the plugin"


def test_register_check_plugin_calls(check_plugin):
    assert str(check_plugin.name) == "k8s_pod_conditions"
    assert check_plugin.service_name == "Pod Condition"
    assert check_plugin.discovery_function.__wrapped__ == k8s_pod_conditions.discovery
    assert check_plugin.check_function.__wrapped__ == k8s_pod_conditions.check
    assert check_plugin.check_default_parameters == {}
    assert str(check_plugin.check_ruleset_name) == "k8s_pod_conditions"
