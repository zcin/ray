import json
import logging
import math
import os
import random
import time
import traceback
from collections import defaultdict
from copy import copy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import ray
from ray import ObjectRef, cloudpickle
from ray.actor import ActorHandle
from ray.exceptions import RayActorError, RayError, RayTaskError, RuntimeEnvSetupError
from ray.serve import metrics
from ray.serve._private import default_impl
from ray.serve._private.autoscaling_policy import AutoscalingPolicyManager
from ray.serve._private.cluster_node_info_cache import ClusterNodeInfoCache
from ray.serve._private.common import (
    DeploymentHandleSource,
    DeploymentID,
    DeploymentStatus,
    DeploymentStatusInfo,
    DeploymentStatusInternalTrigger,
    DeploymentStatusTrigger,
    Duration,
    MultiplexedReplicaInfo,
    ReplicaID,
    ReplicaState,
    RunningReplicaInfo,
)
from ray.serve._private.config import DeploymentConfig
from ray.serve._private.constants import (
    MAX_DEPLOYMENT_CONSTRUCTOR_RETRY_COUNT,
    RAY_SERVE_COLLECT_AUTOSCALING_METRICS_ON_HANDLE,
    RAY_SERVE_EAGERLY_START_REPLACEMENT_REPLICAS,
    RAY_SERVE_ENABLE_TASK_EVENTS,
    RAY_SERVE_FORCE_STOP_UNHEALTHY_REPLICAS,
    RAY_SERVE_MIN_HANDLE_METRICS_TIMEOUT_S,
    RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY,
    REPLICA_HEALTH_CHECK_UNHEALTHY_THRESHOLD,
    SERVE_LOGGER_NAME,
    SERVE_NAMESPACE,
)
from ray.serve._private.deployment_info import DeploymentInfo
from ray.serve._private.deployment_scheduler import (
    DeploymentDownscaleRequest,
    DeploymentScheduler,
    ReplicaSchedulingRequest,
    ReplicaSchedulingRequestStatus,
    SpreadDeploymentSchedulingPolicy,
)
from ray.serve._private.long_poll import LongPollHost, LongPollNamespace
from ray.serve._private.storage.kv_store import KVStoreBase
from ray.serve._private.usage import ServeUsageTag
from ray.serve._private.utils import (
    JavaActorHandleProxy,
    check_obj_ref_ready_nowait,
    get_capacity_adjusted_num_replicas,
    get_random_string,
    msgpack_deserialize,
    msgpack_serialize,
)
from ray.serve._private.version import DeploymentVersion, VersionedReplica
from ray.serve.generated.serve_pb2 import DeploymentLanguage
from ray.serve.schema import (
    DeploymentDetails,
    ReplicaDetails,
    _deployment_info_to_schema,
)
from ray.util.placement_group import PlacementGroup

logger = logging.getLogger(SERVE_LOGGER_NAME)


@dataclass
class HandleRequestMetric:
    actor_id: str
    handle_source: DeploymentHandleSource
    queued_requests: float
    running_requests: Dict[ReplicaID, float]
    timestamp: float

    @property
    def total_requests(self) -> float:
        return self.queued_requests + sum(self.running_requests.values())

    @property
    def is_serve_component_source(self) -> bool:
        return self.handle_source in [
            DeploymentHandleSource.PROXY,
            DeploymentHandleSource.REPLICA,
        ]


class AutoscalingState:
    def __init__(self):
        # Map from handle ID to (# requests recorded at handle, recording timestamp)
        self.handle_requests: Dict[str, HandleRequestMetric] = dict()
        self.requests_queued_at_handles: Dict[str, float] = dict()
        # Number of ongoing requests reported by replicas
        self.replica_average_ongoing_requests: Dict[str, float] = dict()

        self._autoscaling_policy_manager = None
        self._running_replicas: List[ReplicaID] = []

    @property
    def autoscaling_policy_manager(self) -> AutoscalingPolicyManager:
        return self._autoscaling_policy_manager

    def register(self, info: DeploymentInfo):
        self._autoscaling_policy_manager = info.autoscaling_policy_manager

    def update_running_replica_ids(self, running_replicas: List[ReplicaID]):
        self._running_replicas = running_replicas

    def record_request_metrics_for_replica(
        self, replica_id: ReplicaID, window_avg: float, send_timestamp: float
    ) -> None:
        """Records average ongoing requests at replicas."""

        if (
            replica_id not in self.replica_average_ongoing_requests
            or send_timestamp > self.replica_average_ongoing_requests[replica_id][0]
        ):
            self.replica_average_ongoing_requests[replica_id] = (
                send_timestamp,
                window_avg,
            )

    def record_request_metrics_for_handle(
        self,
        *,
        handle_id: str,
        actor_id: Optional[str],
        handle_source: DeploymentHandleSource,
        queued_requests: float,
        running_requests: Dict[ReplicaID, float],
        send_timestamp: float,
    ) -> None:
        """Update request metric for a specific handle."""

        if (
            handle_id not in self.handle_requests
            or send_timestamp > self.handle_requests[handle_id].timestamp
        ):
            self.handle_requests[handle_id] = HandleRequestMetric(
                actor_id=actor_id,
                handle_source=handle_source,
                queued_requests=queued_requests,
                running_requests=running_requests,
                timestamp=send_timestamp,
            )

    def drop_stale_handle_metrics(self, alive_serve_actor_ids: Set[str]) -> None:
        """Drops handle metrics that are no longer valid.

        This includes handles that live on Serve Proxy or replica actors
        that have died AND handles from which the controller hasn't
        received an update for too long.
        """

        timeout_s = max(
            2 * self.autoscaling_policy_manager.get_metrics_interval_s(),
            RAY_SERVE_MIN_HANDLE_METRICS_TIMEOUT_S,
        )
        for handle_id, handle_metric in list(self.handle_requests.items()):
            # Drop metrics for handles that are on Serve proxy/replica
            # actors that have died
            if (
                handle_metric.is_serve_component_source
                and handle_metric.actor_id not in alive_serve_actor_ids
            ):
                del self.handle_requests[handle_id]
                if handle_metric.total_requests > 0:
                    logger.debug(
                        f"Dropping metrics for handle '{handle_id}' because the Serve "
                        f"actor it was on ({handle_metric.actor_id}) is no longer "
                        f"alive. It had {handle_metric.total_requests} ongoing requests"
                    )
            # Drop metrics for handles that haven't sent an update in a while.
            # This is expected behavior for handles that were on replicas or
            # proxies that have been shut down.
            elif time.time() - handle_metric.timestamp >= timeout_s:
                del self.handle_requests[handle_id]
                if handle_metric.total_requests > 0:
                    logger.info(
                        f"Dropping stale metrics for handle '{handle_id}' "
                        f"because no update was received for {timeout_s:.1f}s. "
                        f"Ongoing requests was: {handle_metric.total_requests}."
                    )

    def get_total_num_requests(self) -> float:
        """Get average total number of requests aggregated over the past
        `look_back_period_s` number of seconds.

        If there are 0 running replicas, then returns the total number
        of requests queued at handles

        If the flag RAY_SERVE_COLLECT_AUTOSCALING_METRICS_ON_HANDLE is
        set to 1, the returned average includes both queued and ongoing
        requests. Otherwise, the returned average includes only ongoing
        requests.
        """

        total_requests = 0

        if (
            RAY_SERVE_COLLECT_AUTOSCALING_METRICS_ON_HANDLE
            or len(self._running_replicas) == 0
        ):
            for handle_metric in self.handle_requests.values():
                total_requests += handle_metric.queued_requests
                for id in self._running_replicas:
                    if id in handle_metric.running_requests:
                        total_requests += handle_metric.running_requests[id]
        else:
            for id in self._running_replicas:
                if id in self.replica_average_ongoing_requests:
                    total_requests += self.replica_average_ongoing_requests[id][1]

        return total_requests


class AutoscalingStateManager:
    def __init__(self):
        self._autoscaling_states: Dict[DeploymentID, AutoscalingState] = {}
        self._autoscaling_policy_manager = None

    def register(self, deployment_id: DeploymentID, info: DeploymentInfo):
        if info.deployment_config.autoscaling_config:
            if deployment_id not in self._autoscaling_states:
                self._autoscaling_states[deployment_id] = AutoscalingState()
            self._autoscaling_states[deployment_id].register(info)
        else:
            if deployment_id in self._autoscaling_states:
                del self._autoscaling_states[deployment_id]

    def update_running_replica_ids(
        self, deployment_id: DeploymentID, running_replicas: List[ReplicaID]
    ):
        self._autoscaling_states[deployment_id].update_running_replica_ids(
            running_replicas
        )

    def get_total_num_requests(self, deployment_id: DeploymentID) -> float:
        return self._autoscaling_states[deployment_id].get_total_num_requests()

    def get_metrics(self):
        return {
            deployment_id: self.get_total_num_requests(deployment_id)
            for deployment_id in self._autoscaling_states
        }

    def record_request_metrics_for_replica(
        self, replica_id: ReplicaID, window_avg: float, send_timestamp: float
    ) -> None:
        self._autoscaling_states[
            replica_id.deployment_id
        ].record_request_metrics_for_replica(
            replica_id=replica_id, window_avg=window_avg, send_timestamp=send_timestamp
        )

    def record_request_metrics_for_handle(
        self,
        *,
        deployment_id: str,
        handle_id: str,
        actor_id: Optional[str],
        handle_source: DeploymentHandleSource,
        queued_requests: float,
        running_requests: Dict[ReplicaID, float],
        send_timestamp: float,
    ) -> None:
        """Update request metric for a specific handle."""

        if deployment_id in self._autoscaling_states:
            self._autoscaling_states[deployment_id].record_request_metrics_for_handle(
                handle_id=handle_id,
                actor_id=actor_id,
                handle_source=handle_source,
                queued_requests=queued_requests,
                running_requests=running_requests,
                send_timestamp=send_timestamp,
            )

    def drop_stale_handle_metrics(self, alive_serve_actor_ids: Set[str]) -> None:
        """Drops handle metrics that are no longer valid.

        This includes handles that live on Serve Proxy or replica actors
        that have died AND handles from which the controller hasn't
        received an update for too long.
        """

        for autoscaling_state in self._autoscaling_states.values():
            # if autoscaling_state.should_autoscale():
            autoscaling_state.drop_stale_handle_metrics(alive_serve_actor_ids)
