import typing
from dataclasses import dataclass, fields

import numpy as np

from .job import Job
from .worker import Worker
from ..utils import invalid_time, mask_to_indices

@dataclass
class DagSchedState:
    wall_time: np.ndarray

    job_count: int

    jobs: typing.Tuple[Job, ...]

    workers: typing.Tuple[Worker, ...]

    frontier_stages_mask: np.ndarray

    saturated_stages_mask: np.ndarray



    @property
    def max_stages(self):
        return len(self.jobs[0].stages)

    @property
    def max_jobs(self):
        return len(self.jobs)

    @property
    def n_workers(self):
        return len(self.workers)


    def is_stage_in_frontier(self, stage_idx):
        return self.frontier_stages_mask[stage_idx]

    def add_stages_to_frontier(self, stage_idxs):
        self.frontier_stages_mask[stage_idxs] = 1

    def remove_stage_from_frontier(self, stage_idx):
        assert self.is_stage_in_frontier(stage_idx)
        self.frontier_stages_mask[stage_idx] = 0


    def is_stage_saturated(self, stage_idx):
        return self.saturated_stages_mask[stage_idx]

    def saturate_stage(self, stage_idx):
        self.saturated_stages_mask[stage_idx] = 1

    def remove_stage_from_saturated(self, stage_idx):
        assert self.is_stage_saturated(stage_idx)
        self.frontier_stages_mask[stage_idx] = 0



    def get_frontier_stages(self):
        stage_indices = mask_to_indices(self.frontier_stages_mask)
        stages = [self.get_stage_from_idx(stage_idx) for stage_idx in stage_indices]
        return stages


    def find_available_workers(self):
        return [worker for worker in self.workers if worker.is_available]


    def get_stage_indices(self, job_id, stage_ids):
        return job_id * self.max_stages + np.array(stage_ids, dtype=int)

    
    def get_stage_idx(self, job_id, stage_id):
        return self.get_stage_indices(job_id, np.array([stage_id]))


    def get_stage_from_idx(self, stage_idx):
        stage_id = stage_idx % self.max_stages
        job_id = (stage_idx - stage_id) // self.max_stages
        return self.jobs[job_id].stages[stage_id]


    def add_job(self, new_job):
        old_job = self.jobs[new_job.id_]
        for field in fields(old_job):
            setattr(old_job, field.name, getattr(new_job, field.name))
        
        self.add_src_nodes_to_frontier(new_job)

        self.job_count += 1


    def add_src_nodes_to_frontier(self, job):
        source_ids = job.find_src_nodes()
        source_ids = np.array(source_ids)
        indices = self.get_stage_indices(job.id_, source_ids)
        self.add_stages_to_frontier(indices)


    def get_workers_from_mask(self, workers_mask):
        worker_indices = mask_to_indices(workers_mask)
        workers = [self.workers[i] for i in worker_indices]
        return workers


    def check_action_validity(self, action):
        # check that the selected stage is actually ready for scheduling
        stage_idx = self.get_stage_idx(action.job_id, action.stage_id)
        if not self.is_stage_in_frontier(stage_idx):
            return False

        # check that there are enough workers of each type
        # to fulfill the request
        n_worker_types = len(action.worker_type_counts)
        avail_worker_counts = self.get_avail_worker_counts(n_worker_types)
        requested_counts = np.array(action.worker_type_counts)
        if not requested_counts <= avail_worker_counts:
            return False

        # check that the requested types are actually 
        # compatible with the stage's worker types
        stage = self.jobs[action.job_id].stages[action.stage_id]
        for worker_type in stage.incompatible_worker_types():
            if action.worker_type_counts[worker_type] > 0:
                return False

        return True


    def get_avail_worker_counts(self, n_worker_types):
        '''counts[i] = count of available workers of type i'''
        counts = np.zeros(n_worker_types)
        for worker_type in range(n_worker_types):
            for worker in self.workers:
                if worker.type_ == worker_type and worker.is_available:
                    counts[worker_type] += 1
        return counts


    def take_action(self, action):
        # retrieve selected stage object
        stage = self.jobs[action.job_id].stages[action.stage_id]

        task_ids = []

        # find workers that are closest to this stage's job
        for worker_type in stage.compatible_worker_types():
            count = action.worker_type_counts[worker_type]
            for _ in range(count):
                worker = self.find_closest_worker(stage)
                task_id = self.schedule_worker(worker, stage)
                task_ids += [task_id]

        # check if stage is now saturated; if so, remove from frontier
        stage_idx = self.get_stage_idx(action.job_id, action.stage_id)
        if stage.is_saturated():
            self.remove_stage_from_frontier(stage_idx)
            self.saturate_stage(stage_idx)

        return stage, task_ids


    def find_closest_worker(self, stage):
        '''chooses an available worker for a stage's 
        next task, according to the following priority:
        1. worker is already at stage
        2. worker is not at stage but is at stage's job
        3. any other available worker
        '''

        # try to find available worker already at the stage
        for worker_id in stage.worker_ids:
            if worker_id == -1:
                continue
            worker = self.workers[worker_id]
            if worker.is_available:
                return worker

        # try to find available worker at stage's job;
        # if none is found then return any available worker
        avail_worker = None
        for worker in self.workers:
            if worker.is_available:
                if worker.job_id == stage.job_id:
                    return worker
                elif avail_worker == None:
                    avail_worker = worker
        return avail_worker


    def schedule_worker(self, worker, stage):
        if worker.job_id != -1 and worker.stage_id != -1:
            old_stage = self.jobs[worker.job_id].stages[worker.stage_id]
            old_stage.remove_worker(worker)

        worker.assign_new_stage(stage)

        task_id = stage.add_worker(worker)
        if stage.t_accepted == invalid_time():
            stage.t_accepted = self.wall_time.copy()
        return task_id


    def process_task_completion(self, stage, task_id):
        worker_id = stage.worker_ids[task_id]
        worker = self.workers[worker_id]
        worker.make_available()

        stage.add_task_completion(task_id)


    def process_stage_completion(self, stage):
        stage.complete(self.wall_time.copy())

        stage_idx = self.get_stage_idx(stage.job_id, stage.id_)
        self.remove_stage_from_saturated(stage_idx)

        # add stage's decendents to the frontier, if their
        # other dependencies are also satisfied
        job = self.jobs[stage.job_id]
        new_stages_ids = job.find_new_frontiers(stage)
        new_stages_idxs = \
            self.get_stage_indices(job.id_, new_stages_ids)
        self.add_stages_to_frontier(new_stages_idxs)


    def actions_available(self):
        frontier_stages = self.get_frontier_stages()
        avail_workers = self.find_available_workers()

        if len(avail_workers) == 0 or len(frontier_stages) == 0:
            return False

        for stage in frontier_stages:
            for worker in avail_workers:
                if worker.compatible_with(stage):
                    return True

        return False