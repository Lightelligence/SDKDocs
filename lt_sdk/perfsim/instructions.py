import collections
import copy
import math

from lt_sdk.graph.transform_graph.node_transformers.generic_transforms import (
    base_transform,
)
from lt_sdk.perfsim import perfsim_logging
from lt_sdk.proto import lgf_pb2, sim_params_pb2, subgraph_binary_pb2


class ExecutionInfo(object):
    """Holds info about the instruction and resources allocated to it."""

    def __init__(self, instr):
        self.instr = instr

        # A map from MosaicParams.ResourceType to number required/freed.
        self.requires = collections.defaultdict(int)
        self.frees = collections.defaultdict(int)

    def __str__(self):
        return "req[{0}] free[{1}]".format(
            ", ".join("{0}: {1}".format(k,
                                        v) for k,
                      v in self.requires.items()),
            ", ".join("{0}: {1}".format(k,
                                        v) for k,
                      v in self.frees.items()))


class ExecutionBehavior(object):

    def __init__(self, lat, calc):
        # The latency of getting through the logic
        self.latency = lat
        # The time to actually perform the calculation
        self.calc_time = calc

    def total_clks(self):
        return self.latency + self.calc_time


class InstructionModel(object):
    PERF_SIM_PARAMS = None
    ARCH = None

    def __init__(self, instr):
        self.instr = instr

    def model(self):
        """Should return a ExecutionBehavior."""
        raise NotImplementedError()

    def requires(self):
        """Should return a ExecutionInfo."""
        raise NotImplementedError()

    def can_pipeline(self):
        """Should return a bool."""
        raise NotImplementedError()

    def accesses_umem(self):
        """Should return a bool."""
        raise NotImplementedError()

    def get_total_vector_count(self, alloc, tile):
        if tile is None or tile < 0:
            return alloc.physical_rows
        else:
            return math.ceil(float(alloc.physical_rows) / alloc.num_tiles)

    def alloc_access_clks(self, alloc, tile=None):
        vecs = self.get_total_vector_count(alloc, tile)
        if alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.UMEM:
            clks = vecs
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.TMEM:
            clks = InstructionModel.ARCH.tmem_transfer_clks(vecs)
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.HOSTMEM:
            clks = InstructionModel.ARCH.hostmem_transfer_clks(vecs)
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.ACCUMULATORS:
            clks = vecs
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.BYPASS:
            clks = vecs
        else:
            raise NotImplementedError("Unsupported memory type: {0}".format(
                alloc.mem_type))

        return clks

    def alloc_access_latency(self, alloc):
        if alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.UMEM:
            return 1
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.TMEM:
            return InstructionModel.PERF_SIM_PARAMS.mosaic.off_chip_move_latency
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.HOSTMEM:
            return InstructionModel.PERF_SIM_PARAMS.mosaic.off_chip_move_latency
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.ACCUMULATORS:
            return 1
        elif alloc.mem_type == subgraph_binary_pb2.MemoryAllocation.BYPASS:
            return 1
        else:
            raise NotImplementedError("Unsupported memory type: {0}".format(
                alloc.mem_type))

    def issue_latency(self, state):
        return InstructionModel.PERF_SIM_PARAMS.mosaic.issue_clocks

    def is_opu_node(self):
        """Indicicates if the instruction is a part of an operation
        that loads weights to the dot products, and potentially uses
        the broadcast network to get other cores' data."""
        return False


class Unary(InstructionModel):

    def __init__(self, *args):
        super().__init__(*args)
        self.src_alloc = self.instr.src_addr[0]
        self.dest_alloc = self.instr.dest_addr[0]

        if self.instr.HasField("generic_tile"):
            self.src_tile = self.instr.generic_tile.tile
            self.dest_tile = self.instr.generic_tile.tile
        elif self.instr.HasField("opu_tile"):
            self.src_tile = self.instr.opu_tile.tile_x
            if self.instr.opu_tile.tile_x != self.instr.opu_tile.num_tiles_x - 1:
                self.dest_alloc = self.instr.opu_tile.acc_addr
            self.dest_tile = self.instr.opu_tile.tile_y
        else:
            self.src_tile = -1
            self.dest_tile = -1

    def model(self):
        lat = self.alloc_access_latency(self.src_alloc)

        src_clks = self.alloc_access_clks(self.src_alloc, tile=self.src_tile)
        dest_clks = 0
        if self.dest_alloc:
            lat = max(lat, self.alloc_access_latency(self.dest_alloc))
            dest_clks = self.alloc_access_clks(self.dest_alloc, tile=self.dest_tile)

        calc_time = max(src_clks, dest_clks)
        perfsim_logging.log("calc time: {0}".format(calc_time),
                            perfsim_logging.LogLevel.INSTRUCTION_MODEL)
        return ExecutionBehavior(lat, calc_time)

    def requires(self):
        ret = ExecutionInfo(self.instr)
        ret.requires[sim_params_pb2.MosaicParams.RSC_UMEM_RD] = 1
        ret.requires[sim_params_pb2.MosaicParams.RSC_UMEM_WR] = 1
        ret.requires[sim_params_pb2.MosaicParams.RSC_ALU] = 1

        ret.frees = copy.copy(ret.requires)
        return ret

    def can_pipeline(self):
        return True

    def accesses_umem(self):
        return True


class NonPipelinedUnary(Unary):

    def __init__(self, *args):
        super().__init__(*args)

    def can_pipeline(self):
        """Should return a bool."""
        return False


class Matmul(Unary):

    ENC_LAT = {
        sim_params_pb2.MosaicParams.ENC_NONE: 3,
        sim_params_pb2.MosaicParams.ENC_PARITY: 13,
        sim_params_pb2.MosaicParams.ENC_FULL: 24
    }
    ENC_TIME_MULT = {
        sim_params_pb2.MosaicParams.ENC_NONE: 1,
        sim_params_pb2.MosaicParams.ENC_PARITY: 1.01564086939,  # 98.46%
        sim_params_pb2.MosaicParams.ENC_FULL: 1.06666666667  # 93.75
    }

    def __init__(self, *args):
        super().__init__(*args)

    def requires(self):
        ret = ExecutionInfo(self.instr)
        ret.requires[sim_params_pb2.MosaicParams.RSC_UMEM_RD] = 1
        if self.instr.opu_tile.mode == subgraph_binary_pb2.OPUTile.WRITE_BACK:
            ret.requires[sim_params_pb2.MosaicParams.RSC_UMEM_WR] = 1

        ret.frees = copy.copy(ret.requires)
        ret.frees[sim_params_pb2.MosaicParams.RSC_MATMUL] = 1
        return ret

    def model(self):
        ret = super().model()
        ret.latency += InstructionModel.PERF_SIM_PARAMS.mosaic.matmul_latency_clocks
        ret.latency += Matmul.ENC_LAT[InstructionModel.PERF_SIM_PARAMS.mosaic.encoding]
        ret.calc_time = ret.calc_time * Matmul.ENC_TIME_MULT[
            InstructionModel.PERF_SIM_PARAMS.mosaic.encoding]
        return ret

    def can_pipeline(self):
        return self.instr.opu_tile.mode == subgraph_binary_pb2.OPUTile.WRITE_BACK

    def issue_latency(self, state):
        # If we're only waiting on the APW, we don't need to get its
        # done signal, can issue with no latency
        dep_pcs = [self.instr.pc - x for x in self.instr.dependent_pcs_distance]

        # APW should be in flight
        if dep_pcs[0] not in state.in_flight_pcs:
            perfsim_logging.log("APW not in flight", perfsim_logging.LogLevel.ARCH_MODEL)
            return InstructionModel.PERF_SIM_PARAMS.mosaic.issue_clocks

        # Any other deps should be complete
        for pc in dep_pcs[1:]:
            if pc not in state.complete_pcs:
                perfsim_logging.log("dep {0} not complete".format(pc),
                                    perfsim_logging.LogLevel.ARCH_MODEL)
                return InstructionModel.PERF_SIM_PARAMS.mosaic.issue_clocks

        return 1

    def is_opu_node(self):
        return True


class Conv(Matmul):

    def model(self):
        ret = super().model()
        conv_image_latency = 2 * self.instr.node.inputs[
            lgf_pb2.MatMulNode.INPUT_INDEX].shape.d[
                base_transform.BaseTransform._get_height_and_width_indices(
                    self.instr.node.conv2d.image_attr)[1]]
        ret.latency += conv_image_latency
        ret.latency += InstructionModel.PERF_SIM_PARAMS.mosaic.matmul_latency_clocks
        return ret


class Bundle(InstructionModel):

    def __init__(self, *args):
        super().__init__(*args)

        self.sub_model = INSTR_MODEL_FNS[self.instr.sub_instr.instr[0].node.WhichOneof(
            "node")](self.instr.sub_instr.instr[0])

    def requires(self):
        ret = self.sub_model.requires()

        ret.requires[sim_params_pb2.MosaicParams.RSC_ALU] = 1
        ret.frees[sim_params_pb2.MosaicParams.RSC_ALU] = ret.requires[
            sim_params_pb2.MosaicParams.RSC_ALU]
        return ret

    def model(self):
        ret = self.sub_model.model()
        ret.latency += len(self.instr.sub_instr.instr) - 1
        return ret

    def can_pipeline(self):
        return self.sub_model.can_pipeline()

    def accesses_umem(self):
        return self.sub_model.accesses_umem()

    def is_opu_node(self):
        return self.sub_model.is_opu_node()

    def issue_latency(self, state):
        return self.sub_model.issue_latency(state)


class Move(NonPipelinedUnary):

    def __init__(self, *args):
        super().__init__(*args)

    def requires(self):
        ret = super().requires()
        del ret.requires[sim_params_pb2.MosaicParams.RSC_ALU]
        del ret.frees[sim_params_pb2.MosaicParams.RSC_ALU]
        return ret


class Noop(InstructionModel):

    def __init__(self, *args):
        super().__init__(*args)

    def model(self):
        lat = 1
        calc_time = 0
        return ExecutionBehavior(lat, calc_time)

    def requires(self):
        ret = ExecutionInfo(self.instr)
        return ret

    def can_pipeline(self):
        return False

    def accesses_umem(self):
        return False


class LoadWeights(InstructionModel):

    def __init__(self, *args):
        super().__init__(*args)

    def model(self):
        lat = 1
        calc_time = self.alloc_access_clks(self.instr.src_addr[0])
        return ExecutionBehavior(lat, calc_time)

    def requires(self):
        ret = ExecutionInfo(self.instr)

        # Can make this use only number of read slots needed, but
        # currently set to 3 to be conservative.
        ret.requires[sim_params_pb2.MosaicParams.RSC_UMEM_RD] = 3
        ret.requires[sim_params_pb2.MosaicParams.RSC_WSU] = 1

        ret.frees = copy.copy(ret.requires)
        del ret.frees[sim_params_pb2.MosaicParams.RSC_WSU]
        return ret

    def can_pipeline(self):
        return False

    def accesses_umem(self):
        return True


class ApplyWeights(InstructionModel):

    def __init__(self, *args):
        super().__init__(*args)

    def model(self):
        lat = 1
        calc_time = InstructionModel.PERF_SIM_PARAMS.mosaic.apply_weights_clocks
        return ExecutionBehavior(lat, calc_time)

    def requires(self):
        ret = ExecutionInfo(self.instr)
        ret.requires[sim_params_pb2.MosaicParams.RSC_MATMUL] = 1

        # The weight-staging unit gets freed by APW
        ret.frees[sim_params_pb2.MosaicParams.RSC_WSU] = 1
        return ret

    def can_pipeline(self):
        return False

    def accesses_umem(self):
        return False


class Pool(Matmul):

    def requires(self):
        ret = ExecutionInfo(self.instr)
        ret.requires[sim_params_pb2.MosaicParams.RSC_UMEM_RD] = 1
        ret.requires[sim_params_pb2.MosaicParams.RSC_UMEM_WR] = 1
        ret.requires[sim_params_pb2.MosaicParams.RSC_MATMUL] = 1

        ret.frees = copy.copy(ret.requires)
        return ret

    def is_opu_node(self):
        return False


INSTR_MODEL_FNS = {
    # Matmul-type
    lgf_pb2.LNF.ldw.DESCRIPTOR.name: LoadWeights,
    lgf_pb2.LNF.apw.DESCRIPTOR.name: ApplyWeights,
    lgf_pb2.LNF.matmul.DESCRIPTOR.name: Matmul,

    # Matmul-type, untested
    lgf_pb2.LNF.conv2d.DESCRIPTOR.name: Conv,
    lgf_pb2.LNF.bundle.DESCRIPTOR.name: Bundle,
    lgf_pb2.LNF.distributed_depthwise_conv2d.DESCRIPTOR.name: Matmul,
    lgf_pb2.LNF.pool.DESCRIPTOR.name: Pool,

    # Unary instructions
    lgf_pb2.LNF.sv_add.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.sv_mul.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.sv_min.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.sv_max.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.vv_add.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.vv_mul.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.vv_div.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.vv_max.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.vv_min.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.vv_sub.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.exp.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.batchnorm.DESCRIPTOR.name: Unary,

    # Others, tested
    lgf_pb2.LNF.move.DESCRIPTOR.name: Move,

    # Others, not tested
    lgf_pb2.LNF.mean.DESCRIPTOR.name: Unary,
    lgf_pb2.LNF.noop.DESCRIPTOR.name: Noop,
    lgf_pb2.LNF.reduce_sum.DESCRIPTOR.name: Unary,
    # TODO: get rid of reshape.
    lgf_pb2.LNF.reshape.DESCRIPTOR.name: Unary,
}
