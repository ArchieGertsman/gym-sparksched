from dataclasses import dataclass
from typing import List
import time

import networkx as nx
import numpy as np
import torch
from torch_geometric.data import Data

from .operation import FeatureIdx



@dataclass
class Job:
    '''An object representing a job in the system, containing
    a set of operations with interdependencies, stored as a dag.
    '''

    # unique identifier of this job
    id_: int

    # list of `Operation` objects
    ops: List

    # networkx dag storing the operations' interdependencies
    dag: nx.DiGraph

    # time that this job arrived into the system
    t_arrival: float

    # number of operations that have completed executing
    completed_ops_count = 0

    # time that this job completed, i.e. when the last
    # operation completed executing
    t_completed = np.inf

    local_workers = set()

    x_ptr = None



    



    @property
    def is_complete(self):
        '''whether or not this job has completed'''
        return self.completed_ops_count == len(self.ops)



    def add_op_completion(self):
        '''increments the count of completed operations'''
        assert self.completed_ops_count < len(self.ops)
        self.completed_ops_count += 1



    def find_src_ops(self):
        '''returns a set containing all the operations which are
        source nodes in the dag, i.e. which have no dependencies
        '''
        sources = [self.ops[node] \
            for node,in_deg in self.dag.in_degree() \
                if in_deg==0]
        return set(sources)



    def find_new_frontiers(self, op):
        '''if `op` is completed, returns all of its
        successors whose other dependencies are also 
        completed, if any exist.
        '''
        if not op.is_complete:
            return set()

        new_frontiers = set()
        # search through successors of `stage`
        for suc_op_id in self.dag.successors(op.id_):
            # if all dependencies are completed, then
            # add this successor to the frontiers
            if self._check_dependencies(suc_op_id):
                new_frontiers.add(self.ops[suc_op_id])
        
        return new_frontiers



    def _check_dependencies(self, op_id):
        '''searches to see if all the dependencies of operation 
        with id `op_id` are satisfied.
        '''
        for dep_id in self.dag.predecessors(op_id):
            if not self.ops[dep_id].is_complete:
                return False

        return True



    def populate_remaining_times(self):
        '''populates the `remaining_time` field for each operation
        within this job via BFS. The remaining time of an operation
        is defined recursively as its expected duration plus the 
        remaining times of each of its children.
        '''
        def _populate_recursive(op):
            op.remaining_time = \
                op.task_duration[op.task_duration<np.inf].mean()

            if self.dag.out_degree(op.id_) == 0:
                return

            for child_op_id in self.dag.successors(op.id_):
                child_op = self.ops[child_op_id]
                _populate_recursive(child_op)
                op.remaining_time += child_op.remaining_time
            

        src_ops = self.find_src_ops()
        # populate each connected component of the dag
        while len(src_ops) > 0:
            op = src_ops.pop()
            _populate_recursive(op)



    def init_feature_vectors(self):
        return [self.init_feature_vector(op) for op in self.ops]



    def init_feature_vector(self, op):
        '''returns a feature vector for a single node in the dag'''
        n_remaining_tasks = len(op.remaining_tasks)
        n_processing_tasks = len(op.processing_tasks)
        mean_task_duration = op.task_duration.mean()

        return [
            n_remaining_tasks,
            n_processing_tasks,
            mean_task_duration,
            0, 0
        ] 


    def update_n_avail_local(self, n):
        self.x_ptr[:, FeatureIdx.N_AVAIL_LOCAL_WORKERS] += n
        assert (self.x_ptr[:, FeatureIdx.N_AVAIL_LOCAL_WORKERS] >= 0).all()


    def add_local_worker(self, worker_id, x_ptr):
        self.local_workers.add(worker_id)
        self.update_n_avail_local(x_ptr, 1)


    def remove_local_worker(self, worker_id, x_ptr):
        self.local_workers.remove(worker_id)
        self.update_n_avail_local(x_ptr, -1)


    def assign_worker(self, worker, op, wall_time):
        assert op.n_saturated_tasks < op.n_tasks
        assert worker.can_assign(op)

        task = op.remaining_tasks.pop()
        self.processing_tasks.add(task)

        self.x_ptr[op.id_, FeatureIdx.N_REMAINING_TASKS] -= 1
        self.x_ptr[op.id_, FeatureIdx.N_PROCESSING_TASKS] += 1
        self.update_n_avail_local(self.x_ptr, -1)

        worker.task = task
        task.worker_id = worker.id_
        task.t_accepted = wall_time
        return task


    def add_task_completion(self, op, task, wall_time):
        assert not op.is_complete
        assert task in op.processing_tasks

        op.processing_tasks.remove(task)
        op.completed_tasks.add(task)

        self.x_ptr[op.id_, FeatureIdx.N_PROCESSING_TASKS] -= 1
        self.update_n_avail_local(self.x_ptr, 1)

        task.t_completed = wall_time