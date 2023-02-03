
import numpy as np
import networkx as nx

from .base_job_sequence import BaseJobSequenceGen
from ..entities.job import Job
from ..entities.operation import Operation


class TPCHJobSequenceGen(BaseJobSequenceGen):
    query_base_path = './gym_dagsched/datasets/tpch'
    tpch_sizes = ['2g','5g','10g','20g','50g','80g','100g']
    num_queries = 22


    def generate_job(self, job_id, t_arrival):
        query_size = self.np_random.choice(self.tpch_sizes)
        query_path = f'{self.query_base_path}/{query_size}'
        query_num = 1 + self.np_random.integers(self.num_queries)
        
        adj_matrix = \
            np.load(f'{query_path}/adj_mat_{query_num}.npy', 
                    allow_pickle=True)

        task_durations = \
            np.load(f'{query_path}/task_duration_{query_num}.npy', 
                    allow_pickle=True).item()
        
        assert adj_matrix.shape[0] == adj_matrix.shape[1]
        assert adj_matrix.shape[0] == len(task_durations)

        n_ops = adj_matrix.shape[0]
        ops = []
        for op_id in range(n_ops):
            task_duration_data = task_durations[op_id]
            e = next(iter(task_duration_data['first_wave']))

            num_tasks = len(task_duration_data['first_wave'][e]) + \
                        len(task_duration_data['rest_wave'][e])

            # remove fresh duration from first wave duration
            # drag nearest neighbor first wave duration to empty spots
            self._pre_process_task_duration(task_duration_data)

            # generate a node
            op = Operation(op_id, 
                           job_id, 
                           num_tasks, 
                           task_duration_data, 
                           self.np_random)
            ops += [op]

        # generate DAG
        dag = nx.convert_matrix.from_numpy_matrix(
            adj_matrix, create_using=nx.DiGraph)
        for _,_,d in dag.edges(data=True):
            d.clear()
        job = Job(id_=job_id, ops=ops, dag=dag, t_arrival=t_arrival)
        
        return job



    def _pre_process_task_duration(self, task_duration):
        # remove fresh durations from first wave
        clean_first_wave = {}
        for e in task_duration['first_wave']:
            clean_first_wave[e] = []
            fresh_durations = SetWithCount()
            # O(1) access
            for d in task_duration['fresh_durations'][e]:
                fresh_durations.add(d)
            for d in task_duration['first_wave'][e]:
                if d not in fresh_durations:
                    clean_first_wave[e].append(d)
                else:
                    # prevent duplicated fresh duration blocking first wave
                    fresh_durations.remove(d)

        # fill in nearest neighour first wave
        last_first_wave = []
        for e in sorted(clean_first_wave.keys()):
            if len(clean_first_wave[e]) == 0:
                clean_first_wave[e] = last_first_wave
            last_first_wave = clean_first_wave[e]

        # swap the first wave with fresh durations removed
        task_duration['first_wave'] = clean_first_wave




class SetWithCount(object):
    """
    allow duplication in set
    """
    def __init__(self):
        self.set = {}

    def __contains__(self, item):
        return item in self.set

    def add(self, item):
        if item in self.set:
            self.set[item] += 1
        else:
            self.set[item] = 1

    def clear(self):
        self.set.clear()

    def remove(self, item):
        self.set[item] -= 1
        if self.set[item] == 0:
            del self.set[item]