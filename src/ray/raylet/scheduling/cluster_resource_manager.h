// Copyright 2017 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include <gtest/gtest_prod.h>

#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "absl/container/flat_hash_map.h"
#include "ray/common/bundle_location_index.h"
#include "ray/common/scheduling/cluster_resource_data.h"
#include "ray/common/scheduling/fixed_point.h"
#include "ray/raylet/scheduling/local_resource_manager.h"
#include "ray/util/container_util.h"
#include "ray/util/logging.h"
#include "src/ray/protobuf/gcs.pb.h"

namespace ray {
namespace raylet {
class ClusterTaskManagerTest;
class SchedulingPolicyTest;
}  // namespace raylet
namespace raylet_scheduling_policy {
class HybridSchedulingPolicyTest;
}
namespace gcs {
class GcsActorSchedulerTest;
}  // namespace gcs

/// Class manages the resources view of the entire cluster.
/// This class is not thread safe.
class ClusterResourceManager {
 public:
  explicit ClusterResourceManager(instrumented_io_context &io_service);

  /// Get the resource view of the cluster.
  const absl::flat_hash_map<scheduling::NodeID, Node> &GetResourceView() const;

  /// Update node resources. This happens when a node resource usage updated.
  ///
  /// \param node_id ID of the node which resources need to be updated.
  /// \param resource_view_sync_message The node resource usage data.
  bool UpdateNode(scheduling::NodeID node_id,
                  const syncer::ResourceViewSyncMessage &resource_view_sync_message);

  /// Remove node from the cluster data structure. This happens
  /// when a node fails or it is removed from the cluster.
  ///
  /// \param node_id ID of the node to be removed.
  bool RemoveNode(scheduling::NodeID node_id);

  /// Get number of nodes in the cluster.
  int64_t NumNodes() const;

  /// Update total capacity of a given resource of a given node.
  ///
  /// \param node_id: Node whose resource we want to update.
  /// \param resource_id: Resource which we want to update.
  /// \param resource_total: New capacity of the resource.
  void UpdateResourceCapacity(scheduling::NodeID node_id,
                              scheduling::ResourceID resource_id,
                              double resource_total);

  /// Delete a given resource from a given node.
  ///
  /// \param node_id: Node whose resource we want to delete.
  /// \param resource_ids: Resource id list we want to delete
  /// \return True if the node exist, else False.
  bool DeleteResources(scheduling::NodeID node_id,
                       const std::vector<scheduling::ResourceID> &resource_ids);

  /// Return local resources in human-readable string form.
  std::string GetNodeResourceViewString(scheduling::NodeID node_id) const;

  /// Get local resource.
  const NodeResources &GetNodeResources(scheduling::NodeID node_id) const;

  /// Subtract available resource from a given node.
  /// Return false if such node doesn't exist.
  bool SubtractNodeAvailableResources(scheduling::NodeID node_id,
                                      const ResourceRequest &resource_request);

  /// Check if we have available resources to fullfill resource request for an given node.
  ///
  /// \param node_id: the id of the node.
  /// \param resource_request: the request we want to check.
  /// \param ignore_object_store_memory_requirement: if true, we will ignore the
  ///  require_object_store_memory in the resource_request.
  bool HasAvailableResources(scheduling::NodeID node_id,
                             const ResourceRequest &resource_request,
                             bool ignore_object_store_memory_requirement) const;

  bool HasFeasibleResources(scheduling::NodeID node_id,
                            const ResourceRequest &resource_request) const;

  /// Add available resource to a given node.
  /// Return false if such node doesn't exist.
  bool AddNodeAvailableResources(scheduling::NodeID node_id,
                                 const ResourceSet &resource_set);

  /// Update node normal task resources.
  /// Return false if such node doesn't exist.
  /// TODO(Shanly): Integrated this method into `UpdateNode` later.
  bool UpdateNodeNormalTaskResources(scheduling::NodeID node_id,
                                     const rpc::ResourcesData &resource_data);

  /// Return if the node is tracked.
  bool HasNode(const scheduling::NodeID &node_id) const {
    return nodes_.count(node_id) > 0;
  }

  bool IsNodeDraining(const scheduling::NodeID &node_id) const {
    const auto &node = map_find_or_die(nodes_, node_id);
    return node.GetLocalView().is_draining;
  }

  /// @param max_num_nodes_to_include Max number of nodes to include in the debug string.
  ///   If not specified, all nodes will be included.
  std::string DebugString(
      std::optional<size_t> max_num_nodes_to_include = std::nullopt) const;

  BundleLocationIndex &GetBundleLocationIndex();

  void SetNodeLabels(const scheduling::NodeID &node_id,
                     const absl::flat_hash_map<std::string, std::string> &labels);

 private:
  friend class ClusterResourceScheduler;
  friend class gcs::GcsActorSchedulerTest;

  /// Return the timestamp when the resource of the node got updated by scheduler.
  ///
  /// \param node_id ID of the node to query
  /// \return The timestamp when the node resource got updated. If it's null, it means
  ///    there is no such node or the resource of the node never got updated.
  std::optional<absl::Time> GetNodeResourceModifiedTs(scheduling::NodeID node_id) const;

  /// Add a new node or overwrite the resources of an existing node.
  ///
  /// \param node_id: Node ID.
  /// \param node_resources: Up to date total and available resources of the node.
  void AddOrUpdateNode(scheduling::NodeID node_id, const NodeResources &node_resources);

  void AddOrUpdateNode(
      scheduling::NodeID node_id,
      const absl::flat_hash_map<std::string, double> &resource_map_total,
      const absl::flat_hash_map<std::string, double> &resource_map_available);

  /// Return resources associated to the given node_id in ret_resources.
  /// If node_id not found, return false; otherwise return true.
  bool GetNodeResources(scheduling::NodeID node_id, NodeResources *ret_resources) const;

  /// List of nodes in the clusters and their resources organized as a map.
  /// The key of the map is the node ID.
  absl::flat_hash_map<scheduling::NodeID, Node> nodes_;

  /// Resource message updated
  absl::flat_hash_map<scheduling::NodeID, NodeResources> received_node_resources_;

  BundleLocationIndex bundle_location_index_;

  /// Timer to revert local changes to the resources periodically.
  std::shared_ptr<PeriodicalRunner> timer_;

  friend class ClusterResourceSchedulerTest;
  friend struct ClusterResourceManagerTest;
  friend class raylet::ClusterTaskManagerTest;
  FRIEND_TEST(ClusterResourceSchedulerTest, SchedulingDeleteClusterNodeTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, SchedulingModifyClusterNodeTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, SchedulingUpdateAvailableResourcesTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, SchedulingAddOrUpdateNodeTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, NodeAffinitySchedulingStrategyTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, SpreadSchedulingStrategyTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, SchedulingWithPreferredNodeTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, SchedulingResourceRequestTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, SchedulingUpdateTotalResourcesTest);
  FRIEND_TEST(ClusterResourceSchedulerTest,
              UpdateLocalAvailableResourcesFromResourceInstancesTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, ResourceUsageReportTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, DeadNodeTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, TestAlwaysSpillInfeasibleTask);
  FRIEND_TEST(ClusterResourceSchedulerTest, ObjectStoreMemoryUsageTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, AvailableResourceInstancesOpsTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, DirtyLocalViewTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, DynamicResourceTest);
  FRIEND_TEST(ClusterTaskManagerTestWithGPUsAtHead, RleaseAndReturnWorkerCpuResources);
  FRIEND_TEST(ClusterResourceSchedulerTest, TestForceSpillback);
  FRIEND_TEST(ClusterResourceSchedulerTest, AffinityWithBundleScheduleTest);
  FRIEND_TEST(ClusterResourceSchedulerTest, LabelSelectorIsSchedulableOnNodeTest);

  friend class raylet::SchedulingPolicyTest;
  friend class raylet_scheduling_policy::HybridSchedulingPolicyTest;
};

}  // end namespace ray
