from __future__ import annotations

import dataclasses
import functools
import itertools
import threading
import warnings
import weakref
from collections import defaultdict
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)
from weakref import ref as WeakRef

import torch.fx
from torch import Tensor

from torch._C import _cuda_CUDAAllocator_AllocatorState as AllocatorState
from torch._dynamo.mutation_guard import GenerationTracker

from torch._inductor.compile_fx import (
    get_expanded_dims,
    index_expanded_dims,
    remove_unaligned_input_idxs,
    static_input,
)
from torch._prims_common import check

from torch.utils import _pytree as pytree
from . import config


@dataclasses.dataclass(frozen=True)
class GraphID:
    "Unique counter of a cuda graph recording"
    id: int


@dataclasses.dataclass(frozen=True)
class FunctionID:
    "Unique counter of a function wrapped in cudagraphify_impl"
    id: int


@dataclasses.dataclass(frozen=True)
class WrappedFunction(object):
    model: Callable
    static_input_idxs: Sequence[int]
    id: FunctionID


class TreeManagerContainer(object):
    """
    Manages the lifetime of the tree manager. Like `PrivatePool` in cuda caching allocator,
    the tree and its corresponding memory pool should be kept alive as long as any outstanding
    graph or tensor which is an output of a graph remains alive.
    """

    def __init__(self):
        # This class keeps a strong reference to tree_manager,
        # but upon all other strong references to the tree_manager will reset it to None.
        # We need a strong reference so that we can still access its attributes upon cleanup.
        self.tree_manager: Optional[CUDAGraphTreeManager] = None

        # Number of outstanding references to the current tree manager
        self.live_cudagraphify_fns = 0

        # Following two objects are only set in the case that Tensor outputs outlive
        # the cudagraphify_fns. Reference to the Graph is needed to keep the private pool from
        # deallocation.
        self.live_tensors_count = 0
        self.graph: Optional[torch.cuda.CUDAGraph] = None

        self.lock = threading.Lock()

    def finalize_tensor(self):
        with self.lock:
            self.live_tensors_count -= 1
            if self.live_tensors_count == 0:
                self.graph = None

                # manager was used again after existing cleanup,
                # we shouldnt set it to None
                if self.live_cudagraphify_fns == 0:
                    self.tree_manager = None

    def finalize_reference(self):
        with self.lock:
            self.live_cudagraphify_fns -= 1
            if self.live_cudagraphify_fns == 0:
                self.finalize_tree_manager()

    def add_strong_reference(self, fn: Callable):
        with self.lock:
            self.live_cudagraphify_fns += 1

        weakref.finalize(fn, self.finalize_reference)

    def finalize_tree_manager(self):
        assert self.lock.locked()
        tree_manager = self.tree_manager

        live_tensors = list(
            tree_manager.live_cudagraph_pool_tensors_in_curr_execution()
        )
        if not live_tensors:
            self.tree_manager = None
            return

        # Maintain reference to graph to keep tensors alive
        assert len(tree_manager.roots) > 0, "expected at least one use"
        root = next(tree_manager.get_roots())
        self.graph = root.graph
        for t in live_tensors:
            self.live_tensors_count += 1
            weakref.finalize(t, self.finalize_tensor)

    def get_tree_manager(self) -> CUDAGraphTreeManager:
        if self.tree_manager is None:
            self.tree_manager = CUDAGraphTreeManager()
        return self.tree_manager


local = threading.local()
local.tree_manager_container = TreeManagerContainer()

# We need to register this as an object that will be copied over as TLS when new
# threads are created in autograd
torch._C._stash_obj_in_tls("tree_manager_container", local.tree_manager_container)


def get_container():
    if hasattr(local, "tree_manager_container"):
        return local.tree_manager_container
    assert torch._C._is_key_in_tls("tree_manager_container")
    return torch._C._get_obj_in_tls("tree_manager_container")


def cudagraphify_impl(model, inputs, static_input_idxs=()):
    manager = get_container().get_tree_manager()
    return manager.add_function(model, inputs, static_input_idxs)


def is_live(weak_ref):
    if weak_ref is None:
        return False
    return weak_ref() is not None


class TensorWeakRef(object):
    """
    Wrapper around a weak ref of a Tensor that handles the _fix_weakref() call required
    when unwarpping a Tensor weakref.
    """

    ref: WeakRef[Tensor]

    def __init__(self, tensor: Tensor):
        assert isinstance(tensor, Tensor)
        self.ref = weakref.ref(tensor)

    def __call__(self):
        out = self.ref()
        if out is None:
            return out
        assert isinstance(out, Tensor)
        out._fix_weakref()
        return out


def is_cuda_tensor(x):
    return isinstance(x, torch.Tensor) and x.device.type == "cuda"


class CUDAGraphNode(object):
    """
    A single recording of a function into a CUDA Graph. Recordings of CUDA Graphs share a single memory pool
    and are structured into a tree, where there is a single recording that can precede it (parent) and multiple
    subsequent recordings that may follow (children). A node will have no parent if it is the first recording
    in a tree; i.e., when it is first recorded, there are no live tensors from a previous recording which
    would force a dependency.

    On first recording, all of the live tensors in the current CUDA Graph Node path will be
    reflected in the corresponding private pool. On subsequent executions, the caching allocator
    is unaffected when the graph is replayed.

    In order to support recording a subsequent cuda graph recording after execution of this graph,
    we checkpoint the state of the memory pool so that it may be resumed.

    See [setCheckpointPoolState] for further explanation.
    """

    def __init__(
        self,
        wrapped_function: WrappedFunction,
        id: GraphID,
        parent: Optional[CUDAGraphNode],
        inputs: List[Tensor],
        cuda_graphs_pool: Tuple[int, int],
    ):
        assert isinstance(inputs, (list, tuple))

        self.wrapped_function = wrapped_function
        self.id = id

        # if this is a root parent will be None. use weakref to prevent reference cycle
        self._parent = weakref.ref(parent) if parent is not None else None
        self.cuda_graphs_pool = cuda_graphs_pool

        # A single wrapped function may be recorded multiple times if memory patterns or
        # invariants change from one execution to the next
        self.children: Dict[FunctionID, List[CUDAGraphNode]] = defaultdict(list)

        self.device: int = next(
            (x.device.index for x in inputs if is_cuda_tensor), None
        )

        # we preserve a single reference to executed outputs that is then referenced
        # in children to avoid children having to chase parent pointers in the hot path
        # DO NOT reassign output_weakrefs, only call `clear()`
        self.outputs_weakrefs: List[Optional[TensorWeakRef]] = []
        self.path_weakrefs: List[List[Optional[TensorWeakRef]]] = [
            node.outputs_weakrefs for node in self.path_from_root
        ]

        # tensors which are outputs of previous graphs in the tree
        self.cudagraph_managed_idxs: List[int] = [
            idx for idx, t in enumerate(inputs) if self.is_cuda_graph_recorded_tensor(t)
        ]

        self.static_input_idxs: List[int] = list(
            set(wrapped_function.static_input_idxs) | set(self.cudagraph_managed_idxs)
        )

        self.static_input_data_ptrs: List[int] = [
            (inputs[i].data_ptr() if i in self.static_input_idxs else None)
            for i in range(len(inputs))
        ]
        self.expanded_dims: List[List[int]] = [
            get_expanded_dims(x) if idx not in self.static_input_idxs else []
            for idx, x in enumerate(inputs)
        ]

        # For each node in path, which outputs were observed to be live
        # before invoking graph recording, and after graph recording
        self.recorded_liveness_before_graph: List[List[bool]] = []
        self.recorded_liveness_after_graph: List[List[bool]] = []

        # indices into node at level i for output j
        self.expected_dead_indices_before_graph: List[Tuple[int, int]] = []
        self.expected_dead_indices_after_graph: List[Tuple[int, int]] = []

        if self.parent is not None:
            previous_liveness = self.parent.recorded_liveness_after_graph
            curr_liveness = self.get_liveness(self.path_weakrefs)

            different_indices = self.get_different_indices(
                previous_liveness, curr_liveness
            )

            self.recorded_liveness_before_graph = curr_liveness
            self.expected_dead_indices_before_graph = different_indices

        # graph needs to be kept alive for the duration of the model
        # otherwise the memory pool would be freed on the first recording
        inps_alloc_graph = torch.cuda.CUDAGraph()
        recording_inputs = self.allocate_recording_inputs(inputs, inps_alloc_graph)

        # graph used for recording model invocation
        self.graph = torch.cuda.CUDAGraph()

        # we allocate non-static inputs within the same memory pool as the CUDAGraph
        # which we will record the model with. For memory efficiency, it is important
        # to reclaim the input memory when the inputs are no longer live. To accomplish this,
        # we record the metadata needed to reconstruct the inputs at their correct memory location,
        # but do not keep them live during the cuda graph recording.
        self.non_static_inputs_metadata = [
            self.tensor_metadata(x) if idx not in (self.static_input_idxs) else None
            for idx, x in enumerate(recording_inputs)
        ]

        stream = torch.cuda.Stream()
        self.warmup(wrapped_function.model, stream, recording_inputs)

        # on the first invocation, return the first recorded outputs, because their memory
        # is correctly accounted for in the CUDAGraphs caching allocator, so on subsequent cudagraph
        # recording we are tracing with a valid caching allocator state
        self.recording_outputs = self.record(
            wrapped_function.model, stream, recording_inputs
        )

        self.outputs_metadata = []

        # As with inputs, we do not want to keep the outputs permanently alive because that would prevent
        # their memory being reclaimed in subsequent cuda graph recordings. We record the tensor metadata
        # needed to reconstruct instead.
        for out in self.recording_outputs:
            if isinstance(out, torch.Tensor):
                self.device = (
                    self.device if self.device is not None else out.device.index
                )
                self.outputs_metadata.append(
                    self.tensor_metadata(out, ignore_storage_offset=False)
                )
            else:
                assert out is None
                self.outputs_metadata.append(None)

        # initialized on first run
        self.checkpointed_caching_state: Optional[AllocatorState] = None

    def run(self, new_inputs):
        assert len(self.static_input_data_ptrs) == len(new_inputs)

        storage_cache = {}
        for idx, data_ptr in enumerate(self.static_input_data_ptrs):
            if idx in self.cudagraph_managed_idxs:
                continue
            if data_ptr is not None:
                assert data_ptr == new_inputs[idx].data_ptr()
            else:
                dst = self.reconstruct_from_tensor_metadata(
                    self.non_static_inputs_metadata[idx], storage_cache
                )
                src = new_inputs[idx]
                expanded_dims = self.expanded_dims[idx]

                dst = index_expanded_dims(dst, expanded_dims)
                src = index_expanded_dims(src, expanded_dims)
                # TODO - one jit kernel across multiple inputs
                dst.copy_(src)

        new_inputs.clear()
        self.graph.replay()

        # outputs is not None on first execution
        if self.recording_outputs is not None:
            outputs = self.recording_outputs
            self.recording_outputs = None
            self.add_first_outputs(outputs)

            return outputs

        outputs = [
            self.reconstruct_from_tensor_metadata(metadata, storage_cache)
            for metadata in self.outputs_metadata
        ]

        self.add_replayed_outputs(outputs)

        return outputs

    def all_outputs_are_dead(self):
        "All outputs of the path from this node to its root are dead"
        for i, j in self.live_indices_after_graph:
            if is_live(self.path_weakrefs[i][j]):
                return False

        return True

    def warmup(self, model, stream, inps):
        "Warmup the model"
        # TODO - optimize memory of warmup (deallocate previous inputs, re-use existing memory for running kernels)
        torch.cuda.synchronize()
        stream.wait_stream(torch.cuda.current_stream())
        # copy inputs because list will get cleared in model invocation
        with torch.cuda.stream(stream):
            model(list(inps))
        stream.synchronize()
        torch.cuda.current_stream().wait_stream(stream)
        torch.cuda.synchronize()

    def record(self, model, stream, inputs):
        "Record the model"
        with torch.cuda.graph(self.graph, stream=stream, pool=self.cuda_graphs_pool):
            static_outputs = model(inputs)

        # running model should reclaim memory
        assert len(inputs) == 0

        if not isinstance(static_outputs, (list, tuple)):
            static_outputs = (static_outputs,)

        return static_outputs

    def add_first_outputs(self, outputs):
        "Add the outputs from the first invocation of the node and set up metadata"
        prev_liveness = self.recorded_liveness_before_graph
        curr_liveness = self.get_liveness(self.path_weakrefs)

        delta = self.get_different_indices(prev_liveness, curr_liveness)
        self.expected_dead_indices_after_graph = delta

        assert len(self.outputs_weakrefs) == 0
        weak_refs = [self.map_to_ref(o) for o in outputs]
        self.outputs_weakrefs.extend(weak_refs)
        self.recorded_liveness_after_graph = self.get_liveness(self.path_weakrefs)

        self.checkpointed_caching_state = torch._C._cuda_getCheckpointState(
            self.device, self.cuda_graphs_pool
        )

        self.live_indices_after_graph = []
        for i in range(len(self.path_weakrefs)):
            for j in range(len(self.path_weakrefs[i])):
                self.live_indices_after_graph.append((i, j))

    def add_replayed_outputs(self, outputs):
        self.outputs_weakrefs.clear()
        for out in outputs:
            self.outputs_weakrefs.append(self.map_to_ref(out))

    @staticmethod
    def map_to_ref(t: Optional[Tensor]) -> Optional[TensorWeakRef]:
        if not isinstance(t, torch.Tensor):
            assert t is None
            return None
        return TensorWeakRef(t)

    @property
    def parent(self):
        "unwraps the weakref to _parent"
        return self._parent() if self._parent is not None else None

    @property
    def path_to_root(self):
        "Returns all nodes in the path starting at se;f and ending at root"
        node = self
        while node:
            yield node
            node = node.parent

    @property
    def path_from_root(self):
        "Returns all nodes in the path starting at the rppt and ending at self"
        nodes = reversed(list(self.path_to_root))
        for node in nodes:
            yield node

    def is_cuda_graph_recorded_tensor(self, t: torch.Tensor):
        "Is this tensor an output of a node in this path"
        for output_refs in self.path_weakrefs:
            for tensor_ref in output_refs:
                tensor = tensor_ref()
                if tensor is None:
                    continue
                if (
                    tensor.untyped_storage().data_ptr()
                    == t.untyped_storage().data_ptr()
                ):
                    return True

        return False

    @staticmethod
    def check_liveness(indices: List[Tuple[int, int]], output_refs: List[List[bool]]):
        "Check that all of the indices specified are dead references"
        for i, j in indices:
            if output_refs[i][j]() is not None:
                return False
        return True

    def add_child(self, function_id: FunctionID, node: CUDAGraphNode):
        "Adds node as a a child of self"
        self.children[function_id].append(node)

    @staticmethod
    def get_different_indices(
        prev: List[List[bool]], curr: List[List[bool]]
    ) -> List[Tuple[int, int]]:
        "Find indices where the two lists differ."
        dead_indices = []
        assert len(prev) <= len(curr)
        for i, (outputs1, outputs2) in enumerate(zip(prev, curr)):
            assert len(outputs1) == len(outputs2)
            for j, (output1, output2) in enumerate(zip(outputs1, outputs2)):
                if output1 != output2:
                    dead_indices.append((i, j))

        return dead_indices

    @staticmethod
    def get_liveness(weakrefs: List[List[Optional[TensorWeakRef]]]) -> List[List[bool]]:
        "Maps weakrefs to true if the reference is alive and false otherwise"
        if len(weakrefs) == 0:
            return []

        return [pytree.tree_map(is_live, outputs) for outputs in weakrefs]

    def data_ptrs_dead_since_invocation(self) -> List[int]:
        """
        Since this node was invoked, return data ptrs of all tensor outputs that have died
        in the current executing tree path.
        """
        curr_liveness = self.get_liveness(self.path_weakrefs)
        get_different_indices = self.get_different_indices(
            self.recorded_liveness_after_graph, curr_liveness
        )

        path = list(self.path_from_root)
        ptrs_to_deallocate = []
        for (i, j) in get_different_indices:
            ptrs_to_deallocate.append(path[i].outputs_metadata[j]["data_ptr"])

        return ptrs_to_deallocate

    def path_live_weakrefs(self) -> Iterable[WeakRef[Tensor]]:
        for outputs in self.path_weakrefs:
            for out in outputs:
                if is_live(out):
                    yield out

    def clear_path_outputs(self):
        "Clear the output lists of all nodes in the path"
        for li in self.path_weakrefs:
            li.clear()

    @staticmethod
    def tensor_metadata(x, ignore_storage_offset=True):
        assert isinstance(x, torch.Tensor)
        # We ignore the storage offset for inputs, but not for outputs
        # TODO: - should we make the storage resizable ?
        return {
            "nbytes": x.untyped_storage().nbytes(),
            "data_ptr": x.untyped_storage().data_ptr(),
            "size": x.shape,
            "stride": x.stride(),
            "dtype": x.dtype,
            "device": x.device,
            "storage_offset": x.storage_offset() if not ignore_storage_offset else 0,
        }

    @staticmethod
    def reconstruct_from_tensor_metadata(
        metadata: Dict[str, Any], storage_cache: Dict[int, torch.Storage]
    ) -> Tensor:
        s = storage_cache.get(metadata["data_ptr"], None)
        if s is None:
            s = torch._C._construct_storage_from_data_pointer(
                metadata["data_ptr"], metadata["device"], metadata["nbytes"]
            )
        t = torch.empty([0], device=metadata["device"], dtype=metadata["dtype"])
        t.set_(
            source=s,
            storage_offset=metadata["storage_offset"],
            size=metadata["size"],
            stride=metadata["stride"],
        )
        return t

    def allocate_recording_inputs(self, inputs, inps_alloc_graph):
        torch.cuda.synchronize()
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        recording_inputs = []

        # inputs should be allocated in the cuda graph memory pool
        with warnings.catch_warnings(record=True) as w:
            with torch.cuda.graph(
                inps_alloc_graph,
                pool=self.cuda_graphs_pool,
                stream=stream,
            ):
                for i, inp in enumerate(inputs):
                    if i not in self.static_input_idxs:
                        recording_inputs.append(static_input(inp))
                    else:
                        recording_inputs.append(inp)

        assert len(w) == 1 and "The CUDA Graph is empty" in str(w[0])

        # TODO: more memory efficient to allocate new input and deallocate
        # old input, one by one

        # Now that the Graph is no longer recording, zero out inputs
        # since they may be used in indexing in graph warmup
        for i, inp in enumerate(recording_inputs):
            if i not in self.static_input_idxs:
                inp.zero_()

        return recording_inputs

    def check_invariants(self, inputs):
        """
        Checks if this node can be run. The same pattern of tensor liveness and tensors
        managed in the cudagraph private pool must remain stable.
        """

        # previously managed data pointers remain stable
        for idx in self.cudagraph_managed_idxs:
            if inputs[idx].data_ptr() != self.static_input_data_ptrs[idx]:
                return False

        if not self.check_liveness(
            self.expected_dead_indices_before_graph, self.path_weakrefs
        ):
            return False

        # the cudagraph managed tensors which died upon recording must also die upon
        # this invocation. it is too late to check after we've replayed the graph,
        # because we would have already written over their memory.
        for idx in self.cudagraph_managed_idxs:
            inputs[idx] = None

        check(
            self.check_liveness(
                self.expected_dead_indices_after_graph, self.path_weakrefs
            ),
            lambda: "TODO: graph recording observed an input tensor deallocate during graph "
            " recording that did not occur during replay. Please file an issue.",
        )
        return True

    def num_descendants(self):
        "Total number of descendents of this node"
        num_desc = 0
        for children in self.children.values():
            for child in children:
                num_desc += 1
                num_desc += child.num_descendants()
        return num_desc


def get_cudagraph_segments(pool_id):
    segments = torch.cuda.memory_snapshot()
    return [segment for segment in segments if segment["segment_pool_id"] == pool_id]


def check_memory_pool(pool_id, live_tensors):
    unique_storages = {t.untyped_storage().data_ptr() for t in live_tensors}
    segments = get_cudagraph_segments(pool_id)

    for segment in segments:
        addr = segment["address"]
        for block in segment["blocks"]:
            if block["state"] == "active_allocated":
                check(
                    addr in unique_storages,
                    lambda: f"{addr} allocated but not in live storages",
                )
                unique_storages.remove(addr)

            addr += block["size"]

    check(
        len(unique_storages) == 0,
        f"These storage data ptrs are not allocated but should be {unique_storages}",
    )


class CUDAGraphTreeManager(object):
    """
    Groups individual recordings or executions of cuda graphs into a tree of recordings,
    and checks required invariants.

    When graphs are recorded in the same tree, it enforces subsequent execution
    to follow the same order and have the same output tensor livespans. To remove
    unnecessary coupling of cuda graphs (and additional imposed invariants),
    the tree manager will end a currently recording tree whenever it is valid - when
    the memory pool no longer has any live allocations.

    We ignore outputs from a previous generation that correspond to prior model outputs.
    Currently this is hardcoded `GenerationTracker.generation` tracked in torch dynamo.
    # TODO: make generation increment configurable, warn on overwrite
    """

    def __init__(self):
        self.roots: Dict[FunctionID, List[CUDAGraphNode]] = defaultdict(list)
        self.ids_to_funcs: Dict[FunctionID, WrappedFunction] = {}

        self.cuda_graphs_thread_pool = torch.cuda.graph_pool_handle()

        self.graph_counter = itertools.count(0)
        self.func_counter = itertools.count(0)

        self.current_node: Optional[CUDAGraphNode] = None
        self.current_gen: int = -1

        self.in_recording = False
        self.previous_recording_outputs: List[TensorWeakRef] = []

        self.debug_fail_counter = 0
        self.debug_checkpointing_counter = 0

    def run(self, new_inputs: List[Tensor], function_id: FunctionID):
        # we will try to end the current execution when , since
        # we dont want to do unnecessary checking of the existing outputs
        # on the hot path
        if self.in_recording:
            self.try_end_curr_recording()

        child_nodes = (
            self.roots if self.current_node is None else self.current_node.children
        )

        if not self.in_recording:
            for child in child_nodes[function_id]:
                # here we are checking memory consistency between recording and execution,
                # as well as things like stability of tensor locations, etc
                # and other
                if child.check_invariants(new_inputs):
                    self.current_gen = self.get_curr_generation()
                    return self.execute_node(child, new_inputs)

            if self.current_node is not None and function_id in self.roots:
                self.try_end_curr_execution()
                if self.current_node is None:
                    return self.run(new_inputs, function_id)

            self.debug_fail_counter += 1

            self.try_end_curr_execution()
            if self.current_node is not None:
                self.convert_execution_to_recording()

        # now, we are in a recording state !
        self.current_gen = self.get_curr_generation()
        return self.record_function(new_inputs, function_id)

    def record_function(self, new_inputs, function_id):
        node = CUDAGraphNode(
            self.ids_to_funcs[function_id],
            self.new_graph_id(),
            self.current_node,
            new_inputs,
            self.cuda_graphs_thread_pool,
        )
        if self.current_node is None:
            self.roots[function_id].append(node)
        else:
            self.current_node.add_child(function_id, node)
        self.current_node = node
        self.in_recording = True
        return node.run(new_inputs)

    def execute_node(self, node: CUDAGraphNode, new_inputs):
        self.current_node = node
        return node.run(new_inputs)

    def new_graph_id(self):
        return GraphID(next(self.graph_counter))

    def new_func_id(self):
        return FunctionID(next(self.func_counter))

    def add_function(self, model, inputs, static_input_idxs) -> Callable:
        id = self.new_func_id()
        self.ids_to_funcs[id] = WrappedFunction(
            model, remove_unaligned_input_idxs(inputs, static_input_idxs), id
        )
        fn = functools.partial(self.run, function_id=id)

        # container needs to set clean up when fn dies
        get_container().add_strong_reference(fn)

        return fn

    def get_roots(self) -> Generator[CUDAGraphNode]:
        for nodes in self.roots.values():
            for node in nodes:
                yield node

    @staticmethod
    def get_curr_generation():
        return GenerationTracker.generation

    def try_end_curr_recording(self):
        assert self.in_recording

        if self.current_node is None:
            self.in_recording = False
            return

        # multiple invocations, allow overwriting the previous generation
        if self.current_gen != self.get_curr_generation():
            # we need to keep track of the previous live outputs if they were in recording
            # in case we need to record again
            self.previous_recording_outputs.extend(
                self.current_node.path_live_weakrefs()
            )

            self.clear_current_node_outputs_and_set_to_none()
            self.in_recording = False
            return

        if self.current_node.all_outputs_are_dead():
            self.clear_current_node_outputs_and_set_to_none()
            self.in_recording = False
            return

    def try_end_curr_execution(self):
        assert not self.in_recording
        if self.current_node is None:
            return

        if self.current_gen != self.get_curr_generation():
            self.clear_current_node_outputs_and_set_to_none()
            return

        if self.current_node.all_outputs_are_dead():
            self.clear_current_node_outputs_and_set_to_none()

    def clear_current_node_outputs_and_set_to_none(self):
        self.current_node.clear_path_outputs()
        self.current_node = None

    def convert_execution_to_recording(self):
        self.debug_checkpointing_counter += 1
        state = self.current_node.checkpointed_caching_state
        device = self.current_node.device
        assert state is not None and device is not None

        stale_tensors = [t() for t in self.previous_recording_outputs if is_live(t)]
        self.previous_recording_outputs = []

        live_tensors = [t() for t in self.current_node.path_live_weakrefs()]
        ptrs_to_deallocate = self.current_node.data_ptrs_dead_since_invocation()
        torch._C._cuda_setCheckpointPoolState(
            device, state, stale_tensors, live_tensors
        )

        for ptr in ptrs_to_deallocate:
            torch._C._cuda_cudaCachingAllocator_raw_delete(ptr)

        # Now the live blocks should be exactly equal to the live storages in private pool
        if config.triton.debug_cudagraph_trees:
            check_memory_pool(self.cuda_graphs_thread_pool, live_tensors)

    def live_cudagraph_pool_tensors_in_curr_execution(self) -> List[Tensor]:
        if self.current_node is None:
            return []
        # explicitly ignoring previous recorded outputs from past path
        return [t() for t in self.current_node.path_live_weakrefs()]
