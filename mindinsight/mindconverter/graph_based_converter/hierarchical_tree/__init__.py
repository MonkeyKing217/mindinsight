# Copyright 2020 Huawei Technologies Co., Ltd.All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Hierarchical tree module."""
from mindinsight.mindconverter.common.log import logger as log
from .hierarchical_tree import HierarchicalTree

__all__ = [
    "HierarchicalTreeFactory"
]


class HierarchicalTreeFactory:
    """Hierarchical tree factory."""

    @classmethod
    def create(cls, graph):
        """
        Factory method of hierarchical tree.

        Args:
            graph: Graph obj.

        Returns:
            HierarchicalTree, tree.
        """
        tree = HierarchicalTree()
        for _, node_name in enumerate(graph.nodes_in_topological_order):
            node_inst = graph.get_node(node_name)
            node_input = graph.get_input_shape(node_name)
            node_output = graph.get_output_shape(node_name)
            if not node_input:
                err_msg = f"This model is not supported now. " \
                          f"Cannot find {node_name}'s input shape."
                log.error(err_msg)

            tree.insert(node_inst, node_name, node_input, node_output)
        return tree
