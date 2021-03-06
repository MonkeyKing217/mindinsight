# Copyright 2020 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Implement the debugger grpc server."""
from functools import wraps

from mindinsight.debugger.common.log import logger as log
from mindinsight.debugger.common.utils import get_ack_reply, ServerStatus, \
    Streams
from mindinsight.debugger.proto import debug_grpc_pb2_grpc as grpc_server_base
from mindinsight.debugger.proto.ms_graph_pb2 import GraphProto


def debugger_wrap(func):
    """Wrapper for catch exception."""

    @wraps(func)
    def record_log(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as err:
            log.exception(err)
            raise err

    return record_log


class DebuggerGrpcServer(grpc_server_base.EventListenerServicer):
    """The grpc server used to interactive with grpc client."""

    def __init__(self, cache_store):
        """
        Initialize.

        Args:
            cache_store (DebuggerCache): Debugger cache store.
        """
        cache_store.initialize()
        self._cache_store = cache_store
        # the next position of command queue to be queried
        self._pos = None
        # the status of grpc server, the value is in ServerStatus
        self._status = None
        # the run command cache, used to deal with left continue steps or nodes
        self._old_run_cmd = None
        # the view command cache, used to update tensor history through data queue
        self._received_view_cmd = None
        # the flag of receiving watch point hit
        self._received_hit = None
        self.init()

    def init(self):
        """Init debugger grpc server."""
        self._pos = '0'
        self._status = ServerStatus.PENDING
        self._old_run_cmd = {}
        self._received_view_cmd = {}
        self._received_hit = False
        self._cache_store.clean()

    @debugger_wrap
    def WaitCMD(self, request, context):
        """Wait for a command in DebuggerCache."""
        # check if graph have already received.
        log.info("Received WaitCMD at %s-th step.", request.cur_step)
        if self._status == ServerStatus.PENDING:
            log.warning("No graph received before WaitCMD.")
            reply = get_ack_reply(1)
            return reply
        # send graph if it has not been sent before
        self._pre_process(request)
        # deal with old command
        reply = self._deal_with_old_command()
        # wait for next command
        if reply is None:
            reply = self._wait_for_next_command()
        # check the reply
        if reply is None:
            reply = get_ack_reply(1)
            log.warning("Failed to get command event.")
        else:
            log.info("Reply to WaitCMD: %s", reply)
        return reply

    def _pre_process(self, request):
        """Pre-process before dealing with command."""
        metadata_stream = self._cache_store.get_stream_handler(Streams.METADATA)
        is_new_step = metadata_stream.step < request.cur_step
        # clean cache data at the beginning of new step
        if is_new_step:
            self._cache_store.clean_data()
            self._cache_store.get_stream_handler(Streams.TENSOR).clean_tensors(request.cur_step)
        # receive graph at the beginning of the training
        if self._status == ServerStatus.RECEIVE_GRAPH:
            self._send_graph_flag(metadata_stream)
        # receive new metadata
        if is_new_step or metadata_stream.full_name != request.cur_node:
            self._update_metadata(metadata_stream, request)
        self._send_received_tensor_tag()
        self._send_watchpoint_hit_flag()

    def _send_graph_flag(self, metadata_stream):
        """
        Send graph and metadata to UI.

        Args:
            metadata_stream (MetadataHandler): Metadata handler stream.
        """
        self._cache_store.clean_command()
        # receive graph in the beginning of the training
        self._status = ServerStatus.WAITING
        metadata_stream.state = 'waiting'
        metadata = metadata_stream.get()
        res = self._cache_store.get_stream_handler(Streams.GRAPH).get()
        res.update(metadata)
        self._cache_store.put_data(res)
        log.debug("Put graph into data queue.")

    def _update_metadata(self, metadata_stream, metadata_proto):
        """
        Update metadata.

        Args:
            metadata_stream (MetadataHandler): Metadata handler stream.
            metadata_proto (MetadataProto): Metadata proto send by client.
        """
        # put new metadata into cache
        metadata_stream.put(metadata_proto)
        cur_node = self._cache_store.get_stream_handler(Streams.GRAPH).get_node_name_by_full_name(
            metadata_proto.cur_node) if metadata_proto.cur_node else ''
        metadata_stream.node_name = cur_node
        metadata = metadata_stream.get()
        self._cache_store.put_data(metadata)
        log.debug("Put new metadata into data queue.")

    def _send_received_tensor_tag(self):
        """Send received_finish_tag."""
        node_name = self._received_view_cmd.get('node_name')
        if not node_name or self._received_view_cmd.get('wait_for_tensor'):
            return
        metadata = self._cache_store.get_stream_handler(Streams.METADATA).get()
        ret = {'receive_tensor': {'node_name': node_name}}
        ret.update(metadata)
        self._cache_store.put_data(ret)
        self._received_view_cmd.clear()
        log.debug("Send receive tensor flag for %s", node_name)

    def _send_watchpoint_hit_flag(self):
        """Send Watchpoint hit flag."""
        watchpoint_hit_stream = self._cache_store.get_stream_handler(Streams.WATCHPOINT_HIT)
        if watchpoint_hit_stream.empty or not self._received_hit:
            return
        self._received_hit = False
        watchpoint_hits_info = watchpoint_hit_stream.get()
        self._cache_store.put_data(watchpoint_hits_info)
        log.debug("Send the watchpoint hits to DataQueue.\nSend the reply.")

    def _deal_with_old_command(self):
        """Deal with old command."""
        event = None
        while self._cache_store.has_command(self._pos) and event is None:
            event = self._get_next_command()
            log.debug("Deal with old %s-th command:\n%s.", self._pos, event)
        # deal with continue run command
        if event is None and self._old_run_cmd:
            left_step_count = self._old_run_cmd.get('left_step_count')
            node_name = self._old_run_cmd.get('node_name')
            # node_name and left_step_count should not set at the same time
            if not (left_step_count or node_name) or (left_step_count and node_name):
                log.warning("Invalid old run command. %s", self._old_run_cmd)
                self._old_run_cmd.clear()
                return None
            if left_step_count:
                event = self._deal_with_left_continue_step(left_step_count)
            else:
                event = self._deal_with_left_continue_node(node_name)
            self._cache_store.get_stream_handler(Streams.WATCHPOINT_HIT).clean()
            log.debug("Send old RunCMD. Clean watchpoint hit.")
        return event

    def _deal_with_left_continue_step(self, left_step_count):
        """
        Construct run command with left continue step count.

        Args:
            left_step_count (int): The count of left steps to be executed.

        Returns:
            Event, the run command event.
        """
        event = get_ack_reply()
        event.run_cmd.run_steps = 1
        event.run_cmd.run_level = 'step'
        left_step_count = left_step_count - 1 if left_step_count > 0 else -1
        if not left_step_count:
            self._old_run_cmd.clear()
        else:
            self._old_run_cmd['left_step_count'] = left_step_count
        log.debug("Send old step RunCMD. Left step count: %s", left_step_count)
        return event

    def _deal_with_left_continue_node(self, node_name):
        """
        Construct run command with left continue nodes.

        Args:
            node_name (str): The target node name.

        Returns:
            Union[None, Event], the run command event.
        """
        cur_full_name = self._cache_store.get_stream_handler(Streams.METADATA).full_name
        if cur_full_name == node_name:
            log.info("Execute to target node: %s", node_name)
            self._old_run_cmd.clear()
            return None
        event = get_ack_reply()
        event.run_cmd.run_level = 'node'
        event.run_cmd.node_name = ''
        log.debug("Send old node RunCMD, cur node: %s, target node: %s", cur_full_name, node_name)
        return event

    def _wait_for_next_command(self):
        """
        Wait for next command.

        Returns:
            EventReply, the command event.
        """
        log.info("Start to wait for command.")
        self._cache_store.get_stream_handler(Streams.METADATA).state = 'waiting'
        self._cache_store.put_data({'metadata': {'state': 'waiting'}})
        event = None
        while event is None and self._status == ServerStatus.WAITING:
            log.debug("Wait for %s-th command", self._pos)
            event = self._get_next_command()
        return event

    def _get_next_command(self):
        """Get next command."""
        self._pos, event = self._cache_store.get_command(self._pos)
        if event is None:
            return event
        if isinstance(event, dict):
            event = self._deal_with_view_cmd(event)
        elif event.HasField('run_cmd'):
            event = self._deal_with_run_cmd(event)
        elif event.HasField('exit'):
            self._cache_store.clean()
            log.info("Clean cache for exit cmd.")

        return event

    def _deal_with_view_cmd(self, event):
        """Deal with view cmd."""
        view_cmd = event.get('view_cmd')
        node_name = event.get('node_name')
        log.debug("Receive view cmd for node: %s.", node_name)
        if not (view_cmd and node_name):
            log.debug("Invalid view command. Ignore it.")
            return None
        self._received_view_cmd['node_name'] = node_name
        self._received_view_cmd['wait_for_tensor'] = True
        return view_cmd

    def _deal_with_run_cmd(self, event):
        """Deal with run cmd."""
        run_cmd = event.run_cmd
        # receive step command
        if run_cmd.run_level == 'step':
            # receive pause cmd
            if not run_cmd.run_steps:
                log.debug("Pause training and wait for next command.")
                self._old_run_cmd.clear()
                return None
            # receive step cmd
            left_steps = run_cmd.run_steps - 1
            event.run_cmd.run_steps = 1
            if left_steps:
                self._old_run_cmd['left_step_count'] = left_steps if left_steps > 0 else -1
        elif run_cmd.node_name:
            self._old_run_cmd['node_name'] = run_cmd.node_name
            run_cmd.node_name = ''
        self._cache_store.get_stream_handler(Streams.WATCHPOINT_HIT).clean()
        log.debug("Receive RunCMD. Clean watchpoint hit cache.")

        return event

    @debugger_wrap
    def SendMetadata(self, request, context):
        """Send metadata into DebuggerCache."""
        log.info("Received Metadata.")
        if self._status != ServerStatus.PENDING:
            log.info("Re-initialize cache store when new session comes.")
            self.init()

        client_ip = context.peer().split(':', 1)[-1]
        metadata_stream = self._cache_store.get_stream_handler(Streams.METADATA)
        if request.training_done:
            log.info("The training from %s has finished.", client_ip)
        else:
            metadata_stream.put(request)
            metadata_stream.client_ip = client_ip
            log.debug("Put new metadata from %s into cache.", client_ip)
        # put metadata into data queue
        metadata = metadata_stream.get()
        self._cache_store.put_data(metadata)
        reply = get_ack_reply()
        log.debug("Send the reply to %s.", client_ip)
        return reply

    @debugger_wrap
    def SendGraph(self, request_iterator, context):
        """Send graph into DebuggerCache."""
        log.info("Received graph.")
        serial_graph = b""
        for chunk in request_iterator:
            serial_graph += chunk.buffer
        graph = GraphProto.FromString(serial_graph)
        log.debug("Deserialize the graph. Receive %s nodes", len(graph.node))
        self._cache_store.get_stream_handler(Streams.GRAPH).put(graph)
        self._cache_store.get_stream_handler(Streams.TENSOR).put_const_vals(graph.const_vals)
        self._status = ServerStatus.RECEIVE_GRAPH
        reply = get_ack_reply()
        log.debug("Send the reply for graph.")
        return reply

    @debugger_wrap
    def SendTensors(self, request_iterator, context):
        """Send tensors into DebuggerCache."""
        log.info("Received tensor.")
        tensor_construct = []
        tensor_stream = self._cache_store.get_stream_handler(Streams.TENSOR)
        metadata_stream = self._cache_store.get_stream_handler(Streams.METADATA)
        tensor_names = []
        step = metadata_stream.step
        for tensor in request_iterator:
            tensor_construct.append(tensor)
            if tensor.finished:
                update_flag = tensor_stream.put({'step': step, 'tensor_protos': tensor_construct})
                if self._received_view_cmd.get('wait_for_tensor') and update_flag:
                    self._received_view_cmd['wait_for_tensor'] = False
                    log.debug("Set wait for tensor flag to False.")
                tensor_construct = []
                tensor_names.append(':'.join([tensor.node_name, tensor.slot]))
                continue
        reply = get_ack_reply()
        return reply

    @debugger_wrap
    def SendWatchpointHits(self, request_iterator, context):
        """Send watchpoint hits info DebuggerCache."""
        log.info("Received WatchpointHits. Left run cmd %s change to emtpy.", self._old_run_cmd)
        self._old_run_cmd.clear()
        self._received_hit = True
        watchpoint_hit_stream = self._cache_store.get_stream_handler(Streams.WATCHPOINT_HIT)
        watchpoint_stream = self._cache_store.get_stream_handler(Streams.WATCHPOINT)
        graph_stream = self._cache_store.get_stream_handler(Streams.GRAPH)
        for watchpoint_hit_proto in request_iterator:
            ui_node_name = graph_stream.get_node_name_by_full_name(
                watchpoint_hit_proto.tensor.node_name)
            log.debug("Receive watch point hit: %s", watchpoint_hit_proto)
            if not ui_node_name:
                log.info("Not support to show %s on graph.", watchpoint_hit_proto.tensor.node_name)
                continue
            watchpoint_hit = {
                'tensor_proto': watchpoint_hit_proto.tensor,
                'watchpoint': watchpoint_stream.get_watchpoint_by_id(watchpoint_hit_proto.id),
                'node_name': ui_node_name
            }
            watchpoint_hit_stream.put(watchpoint_hit)
        reply = get_ack_reply()
        return reply
