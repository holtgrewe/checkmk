import datetime
import json
from unittest import TestCase
from unittest.mock import Mock

from dateutil.tz import tzutc
from kubernetes import client  # type: ignore[import] # pylint: disable=import-error
from mocket import Mocketizer  # type: ignore[import]
from mocket.mockhttp import Entry  # type: ignore[import]

from cmk.special_agents.agent_kube import Pod
from cmk.special_agents.utils_kubernetes.schemata import api, section
from cmk.special_agents.utils_kubernetes.transform import (
    convert_to_timestamp,
    parse_pod_info,
    pod_conditions,
    pod_containers,
    pod_resources,
)


class TestAPIPod:
    def test_parse_conditions(self, core_client, dummy_host):
        node_with_conditions = {
            "items": [
                {
                    "status": {
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "False",
                                "reason": None,
                                "message": None,
                            },
                        ],
                    },
                },
            ],
        }
        Entry.single_register(
            Entry.GET,
            f"{dummy_host}/api/v1/pods",
            body=json.dumps(node_with_conditions),
            headers={"content-type": "application/json"},
        )
        with Mocketizer():
            pod = list(core_client.list_pod_for_all_namespaces().items)[0]
        condition = pod_conditions(pod.status.conditions)[0]
        assert condition.detail is None
        assert condition.status is False
        assert condition.detail is None
        assert condition.type == api.ConditionType.READY

    def test_parse_containers(self, core_client, dummy_host):
        mocked_pods = {
            "kind": "PodList",
            "apiVersion": "v1",
            "metadata": {"selfLink": "/api/v1/pods", "resourceVersion": "6605101"},
            "items": [
                {
                    "status": {
                        "containerStatuses": [
                            {
                                "name": "cadvisor",
                                "state": {"running": {"startedAt": "2021-10-08T07:39:10Z"}},
                                "lastState": {},
                                "ready": True,
                                "restartCount": 0,
                                "image": "some_image",
                                "imageID": "some_irrelevant_id",
                                "containerID": "some_container_id",
                                "started": True,
                            }
                        ],
                    },
                }
            ],
        }
        Entry.single_register(
            Entry.GET,
            f"{dummy_host}/api/v1/pods",
            body=json.dumps(mocked_pods),
            headers={"content-type": "application/json"},
        )
        with Mocketizer():
            pod = list(core_client.list_pod_for_all_namespaces().items)[0]
        containers = pod_containers(pod)
        assert len(containers) == 1
        assert containers[0].ready is True
        assert containers[0].state.type == "running"
        assert containers[0].image == "some_image"
        assert isinstance(containers[0].state.start_time, int)


class TestPodWithNoNode(TestCase):
    """If the cluster does not have any allocatable pods remaining, special client objects arise.
    For instance, these pods do not have a dedicated node.
    Below, there is one test for each affected function.
    """

    def test_pod_resources_pod_without_node(self) -> None:
        pod = client.V1Pod(
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="non_scheduled_container",
                        resources=client.V1ResourceRequirements(limits=None, requests=None),
                    ),
                ],
            ),
        )

        self.assertEqual(
            pod_resources(pod),
            api.PodUsageResources(
                cpu=api.Resources(limit=float("inf"), requests=0.0),
                memory=api.Resources(limit=float("inf"), requests=0.0),
            ),
        )

    def test_parse_pod_info_pod_without_node(self) -> None:
        pod = client.V1Pod(
            spec=client.V1PodSpec(
                host_network=None,
                node_name=None,
                containers=[
                    client.V1Container(
                        name="non_scheduled_container",
                    ),
                ],
            ),
            status=client.V1PodStatus(
                host_ip=None,
                pod_ip=None,
                qos_class="BestEffort",
            ),
        )
        pod_spec_api = parse_pod_info(pod)

        assert pod_spec_api.pod_ip is None
        assert pod_spec_api.node is None

    def test_pod_containers_pod_without_node(self) -> None:
        pod = client.V1Pod(
            status=client.V1PodStatus(
                container_statuses=None,
            )
        )

        container_info_api_list = pod_containers(pod)

        self.assertEqual(container_info_api_list, [])

    def test_pod_conditions_pod_without_node(self) -> None:
        pod_condition_list = [
            client.V1PodCondition(
                last_probe_time=None,
                last_transition_time=datetime.datetime(2021, 10, 29, 9, 5, 52, tzinfo=tzutc()),
                message="0/1 nodes are available: 1 Too many pods.",
                reason="Unschedulable",
                status="False",
                type="PodScheduled",
            )
        ]
        self.assertEqual(
            pod_conditions(pod_condition_list),
            [
                api.PodCondition(
                    status=False,
                    type=api.ConditionType.PODSCHEDULED,
                    custom_type=None,
                    reason="Unschedulable",
                    detail="0/1 nodes are available: 1 Too many pods.",
                )
            ],
        )


class TestPodStartUp(TestCase):
    """During startup of a large number of pods, special pod conditions may arise, where most of the
    information is missing. Depending on timing, we obtain different client objects from the
    kubernetes api.
    """

    def test_pod_containers_start_up(self) -> None:
        """
        In this specific instance all of the fields expect for the scheduled field are missing.
        """
        pod = client.V1Pod(
            status=client.V1PodStatus(
                container_statuses=[
                    client.V1ContainerStatus(
                        name="unready_container",
                        ready=False,
                        restart_count=0,
                        container_id=None,
                        image="gcr.io/kuar-demo/kuard-amd64:blue",
                        image_id="",
                        state=client.V1ContainerState(
                            running=None,
                            terminated=None,
                            waiting=client.V1ContainerStateWaiting(
                                message=None, reason="ContainerCreating"
                            ),
                        ),
                    )
                ],
            ),
        )
        self.assertEqual(
            pod_containers(pod),
            [
                api.ContainerInfo(
                    id=None,
                    name="unready_container",
                    image="gcr.io/kuar-demo/kuard-amd64:blue",
                    ready=False,
                    state=api.ContainerWaitingState(
                        type="waiting", reason="ContainerCreating", detail=None
                    ),
                    restart_count=0,
                )
            ],
        )

    def test_pod_conditions_start_up(self) -> None:
        """
        It is possible that during startup of pods, also more complete information arises.
        """
        pod_status = api.PodStatus(
            start_time=int(
                convert_to_timestamp(datetime.datetime(2021, 11, 22, 16, 11, 38, 710257))
            ),
            conditions=[
                api.PodCondition(
                    status=True,
                    type=api.ConditionType.INITIALIZED,
                    custom_type=None,
                    reason=None,
                    detail=None,
                ),
                api.PodCondition(
                    status=False,
                    type=api.ConditionType.READY,
                    custom_type=None,
                    reason="ContainersNotReady",
                    detail="containers with unready status: [unready_container]",
                ),
                api.PodCondition(
                    status=False,
                    type=api.ConditionType.CONTAINERSREADY,
                    custom_type=None,
                    reason="ContainersNotReady",
                    detail="containers with unready status: [unready_container]",
                ),
                api.PodCondition(
                    status=True,
                    type=api.ConditionType.PODSCHEDULED,
                    custom_type=None,
                    reason=None,
                    detail=None,
                ),
            ],
            phase=api.Phase.PENDING,
        )
        pod = Pod(
            uid=Mock(),
            status=pod_status,
            metadata=Mock(),
            spec=Mock(),
            resources=Mock(),
            containers=Mock(),
        )
        self.assertEqual(
            pod.conditions(),
            section.PodConditions(
                initialized=section.PodCondition(status=True, reason=None, detail=None),
                scheduled=section.PodCondition(status=True, reason=None, detail=None),
                containersready=section.PodCondition(
                    status=False,
                    reason="ContainersNotReady",
                    detail="containers with unready status: [unready_container]",
                ),
                ready=section.PodCondition(
                    status=False,
                    reason="ContainersNotReady",
                    detail="containers with unready status: [unready_container]",
                ),
            ),
        )

    def test_pod_conditions_start_up_missing_fields(self) -> None:
        """
        In this specific instance all of the fields except for the scheduled field are missing.
        """
        pod = Pod(
            uid=Mock(),
            status=api.PodStatus(
                start_time=int(
                    convert_to_timestamp(datetime.datetime(2021, 11, 22, 16, 11, 38, 710257))
                ),
                conditions=[
                    api.PodCondition(
                        status=True,
                        type=api.ConditionType.PODSCHEDULED,
                        custom_type=None,
                        reason=None,
                        detail=None,
                    )
                ],
                phase=api.Phase.PENDING,
            ),
            metadata=Mock(),
            spec=Mock(),
            resources=Mock(),
            containers=Mock(),
        )

        self.assertEqual(
            pod.conditions(),
            section.PodConditions(
                initialized=None,
                scheduled=section.PodCondition(status=True, reason=None, detail=None),
                containersready=None,
                ready=None,
            ),
        )
