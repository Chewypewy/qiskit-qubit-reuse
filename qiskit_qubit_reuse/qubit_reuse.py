# This code is part of Qiskit.
#
# (C) Copyright IBM 2022.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""VF2Layout pass to find a layout using subgraph isomorphism"""
import logging

from qiskit.transpiler.basepasses import TransformationPass
from qiskit.circuit import *
from qiskit.dagcircuit import *
from collections import deque

logger = logging.getLogger(__name__)

class Greedy:
    def __init__(self, dag: DAGCircuit, dual: bool = False) -> None:
        # Public variables
        self.dag = DAGCircuit()

        # Private variables
        self.__dual = dual
        self.__dag: DAGCircuit = dag.reverse_ops() if self.__dual else dag
        self.__creg = ClassicalRegister(self.__dag.num_clbits())
        self.__causal_cones: dict[int, set[Qubit]] = self.__get_causal_cones()
        self.__qubit_indices: dict[Qubit, int] = {qubit : i for i, qubit in enumerate(self.__dag.qubits)}
        self.__cl_indices: dict[Clbit, int] = {clbit : i for i, clbit in enumerate(self.__dag.clbits)}
        self.__qubit_mapping: dict[int, int] = {}
        self.__measured_qubits: list[int] = []
        self.__visited_nodes: set[DAGOpNode] = set()
        self.__current_added_qubit: int = 0
        
        # Initialization
        self.dag.add_creg(self.__creg)
        for index, _ in self.__causal_cones.items():
            self.__create_subpath(qubit=index, until_node=None)

        if self.__dual:
            self.dag = self.dag.reverse_ops()

    def filter_out_in_nodes(node: DAGNode) -> bool: # Filters input and output nodes.
        return isinstance(node, DAGInNode) or isinstance(node, DAGOutNode)

    def filter_unsupported(node: DAGNode) -> bool: # Discriminated barriers.
        return node.op.name == "barrier"

    def get_qubit_input_node(dag: DAGCircuit, qubit_index: int) -> tuple[Qubit, DAGNode]: # Returns qubit and input node from a qubit index.
        input_nodes = dag.input_map
        qubit = list(input_nodes.keys())[qubit_index]
        return (qubit, input_nodes.get(qubit, None))

    def get_qubit_output_node(dag: DAGCircuit, qubit_index: int) -> tuple[Qubit, DAGNode]: # Returns qubit and output node from an index.
        output_nodes = dag.output_map
        qubit = list(output_nodes.keys())[qubit_index]
        return (qubit, output_nodes.get(qubit, None))
    
    def get_causal_cone(self, dag: DAGCircuit, qubit_index: int) -> set[Qubit]: 
        if qubit_index >= dag.num_qubits():
            raise IndexError(f"Qubit index {qubit_index} is out of range")
        qubit, output_node = self.get_qubit_output_node(dag, qubit_index)
        qubits_to_check = set({qubit})
        queue = deque(dag.predecessors(output_node))

        while queue:
            node_to_check = queue.popleft()
            if not self.filter_out_in_nodes(node_to_check):
                qubit_set = set(node_to_check.qargs)
                if qubit_set.intersection(qubits_to_check) and not self.filter_unsupported(node_to_check):
                    qubits_to_check = qubits_to_check.union(qubit_set)
                    
                for node in dag.predecessors(node_to_check):
                    if not self.filter_out_in_nodes(node):
                        if qubits_to_check.intersection(set(node.qargs)):
                            queue.append(node)
        return qubits_to_check

    def __get_causal_cones(self) -> dict[int, list[Qubit]]:
        """
        Returns a sorted dictionary with each qubit as key and their respective causal cone as value.
        """
        result = dict(
            sorted(
            list({
                index : self.get_causal_cone(self.__dag, index) for index in range(self.__dag.num_qubits())
            }.items()),
            key= lambda item : len(item[1])
            )
        )
        return result
    
    def op_count_per_qubits(self) -> dict: 
        """
        For debugging. Counts the gates affecting each qubit. Splits them by reset.
        """
        op_counts = {}
        for index, qubit in enumerate(self.dag.qubits):  # Get all qubits and their indices
            counts = {}
            for op in self.dag.nodes_on_wire(qubit, only_ops=True):  # Iterate operations on wire.
                if op.name == "reset":  # If reset, add the resulting counts and reset.
                    new_val = op_counts.get(index, [])
                    new_val.append(counts)
                    op_counts[index] = new_val
                    counts = {}
                else:   # Else, keep counting.
                    counts[op.name] = counts.get(op.name, 0) + 1
            new_val = op_counts.get(index, [])  # Obtan current value if existent. If not use empty list.
            new_val.append(counts)  # Append counts
            op_counts[index] = new_val  # Add to counts.
        return op_counts

    def __assign_qubit(self, index) -> None:
        """
        Check if a new qubit from the new graph needs to be assigned to a qubit from the old circuit.
        
        If so, it either picks from a measured qubit or adds a new one to the circuit.
        """
        if self.__qubit_mapping.get(index, None) == None: # In case the qubit hasn't been assigned
            # Case measure is available
            if len(self.__measured_qubits) > 0:
                # Collect from the measured qubits queue.
                new_index = self.__measured_qubits.pop(0)
                # Applies a reset operation to set qubit.
                self.dag.apply_operation_back(op=Reset(), qargs=(self.dag.qubits[new_index],), cargs=())
                # Map this qubit to the new one.
                self.__qubit_mapping[index] = new_index
            else: # Case no measured qubits available
                # Map the new qubit to the index from the current_added qubit
                self.__qubit_mapping[index] = self.__current_added_qubit
                # Increase latest added qubit index.
                self.__current_added_qubit +=  1
                # Add a new qubit to the dag.
                self.dag.add_qubits([Qubit()])
    
    def __create_subpath(self, qubit: Qubit | int, until_node: DAGOpNode) -> None:
        """
        Recursively creates a subpath for a qubit in the circuit, based on its causal cone.
        """
        # If the provided qubit is an instance of Qubit, proceed to assign
        if isinstance(qubit, Qubit):
            self.__assign_qubit(self.__qubit_indices[qubit])
        # Else assign by index and retrieve the qubit index
        else:
            self.__assign_qubit(qubit)
            qubit = list(self.__qubit_indices)[qubit]
        # # Retrieve the input node
        # _, input_node  = get_qubit_input_node(self.__dag, self.__qubit_indices[qubit])
        # # Start the queue with the input node
        # queue = [input_node]
        queue = list(self.__dag.nodes_on_wire(qubit))
        for current_node in queue:
            if current_node == until_node: # Stop if we have reached the until node.
                break
            # If the current node has not been visited and is an instance of OpNode
            if current_node not in self.__visited_nodes and isinstance(current_node, DAGOpNode):
                # Add to the set of visited nodes.
                self.__visited_nodes.add(current_node)
                # Check if any of the qubits in qargs has not been added to the reduced circuit.
                if not current_node.op.name == 'barrier':
                    for op_qubit in current_node.qargs:
                        # if self.__qubit_mapping.get(self.__qubit_indices[op_qubit], None) == None:
                        #     # If the qubit has not been added, make the recursion call, make it stop at current node.
                        self.__create_subpath(op_qubit, until_node=current_node)
                    # Apply the operation
                    self.dag.apply_operation_back(
                            op= current_node.op,
                            qargs= (self.dag.qubits[self.__qubit_mapping[self.__qubit_indices[qbit]]] for qbit in current_node.qargs),
                            cargs= (self.dag.clbits[self.__cl_indices[clbit]] for clbit in current_node.cargs)
                        )
                # If measuring, add the qubit to the list of available qubits.
                if not self.__dual and current_node.op.name == 'measure':
                    self.__measured_qubits.append(self.__qubit_mapping[self.__qubit_indices[current_node.qargs[0]]])
            elif self.__dual and isinstance(current_node, DAGOutNode) and isinstance(current_node.wire, Qubit):
                self.__measured_qubits.append(self.__qubit_mapping[self.__qubit_indices[current_node.wire]])


class QubitReuse(TransformationPass):
    """A qubit reuse via midcircuit measurement transformation pass

    Cite paper here
    """

    def __init__(self, target, type="default"):
        """Initialize a ``QubitReuse`` pass instance

        Args:
            target (Target): A target representing the backend device to run ``QubitReuse`` on.
        """
        super().__init__()
        self.target = target
        self.type = type

    def run(self, dag):
        """run the qubit reuse pass method"""
        if self.type == "dual":
            return Greedy(dag, True)
        elif self.type == "regular":
            return Greedy(dag)
        else:
            regular = Greedy(dag)
            dual = Greedy(dag, True)
            regular_qubits = regular.num_qubits()
            dual_qubits = dual.num_qubits()

            return regular if regular_qubits <= dual_qubits else dual
