import typing
from dataclasses import dataclass, fields

import numpy as np
from gym.spaces import Dict, Tuple

from ..args import args
from ..utils.misc import to_wall_time, mask_to_indices
from ..utils.spaces import discrete_i, time_space, stages_mask_space
from .job import Job, job_space
from .worker import Worker, worker_space
from .stage import Stage


sys_state_space = Dict({
    'wall_time': time_space,
    'jobs': Tuple(args.n_jobs * [job_space]),
    'n_jobs': discrete_i(args.n_jobs),
    'n_completed_jobs': discrete_i(args.n_jobs),
    'workers': Tuple(args.n_workers * [worker_space]),
    'frontier_stages_mask': stages_mask_space,
    'saturated_stages_mask': stages_mask_space
})


@dataclass
class SysState:
    wall_time: np.ndarray = to_wall_time(0.)

    n_jobs: int = 0

    n_completed_jobs: int = 0

    jobs: typing.Tuple[Job, ...] = \
        tuple([Job() for _ in range(args.n_jobs)])

    workers: typing.Tuple[Worker, ...] = \
        tuple([Worker() for _ in range(args.n_workers)])

    frontier_stages_mask: np.ndarray = \
        np.zeros(args.n_jobs * args.max_stages, np.int8)

    saturated_stages_mask: np.ndarray = \
        np.zeros(args.n_jobs * args.max_stages, np.int8)


    @property
    def all_jobs_complete(self):
        return self.n_completed_jobs == self.n_jobs


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
        return [worker for worker in self.workers if worker.available]


    def get_stage_indices(self, job_id, stage_ids):
        return job_id * args.max_stages + np.array(stage_ids, dtype=int)

    
    def get_stage_idx(self, job_id, stage_id):
        return self.get_stage_indices(job_id, np.array([stage_id]))


    def get_stage_from_idx(self, stage_idx):
        stage_id = stage_idx % args.max_stages
        job_id = (stage_idx - stage_id) // args.max_stages
        return self.jobs[job_id].stages[stage_id]


    def add_job(self, new_job):
        old_job = self.jobs[new_job.id_]
        for field in fields(old_job):
            setattr(old_job, field.name, getattr(new_job, field.name))
        
        self.add_src_nodes_to_frontier(new_job)

        self.n_jobs += 1


    def add_src_nodes_to_frontier(self, job):
        source_ids = job.find_src_nodes()
        source_ids = np.array(source_ids)
        indices = self.get_stage_indices(job.id_, source_ids)
        self.add_stages_to_frontier(indices)


    def get_workers_from_mask(self, workers_mask):
        worker_indices = mask_to_indices(workers_mask)
        workers = [self.workers[i] for i in worker_indices]
        return workers


    def is_action_valid(self, action):
        if action.job_id == Job.INVALID_ID or action.stage_id == Stage.INVALID_ID:
            return False

        job = self.jobs[action.job_id]
        stage = job.stages[action.stage_id]

        # n_requested_workers = action.worker_type_counts.sum()

        # # check that not too many workers are requested for the stage
        # if n_requested_workers > stage.n_remaining_tasks:
        #     return False

        # # check that not too many workers are requested for the job
        # if n_requested_workers > job.n_avail_worker_slots:
        #     return False

        # check that the selected stage is actually ready for scheduling
        stage_idx = self.get_stage_idx(action.job_id, action.stage_id)
        if not self.is_stage_in_frontier(stage_idx):
            return False

        # # check that there are enough workers of each type
        # # to fulfill the request
        # n_worker_types = len(action.worker_type_counts)
        # avail_worker_counts = self.get_avail_worker_counts(n_worker_types)
        # requested_counts = np.array(action.worker_type_counts)
        # if (requested_counts > avail_worker_counts).any():
        #     return False

        # # check that the requested types are actually 
        # # compatible with the stage's worker types
        # for worker_type in stage.incompatible_worker_types():
        #     if action.worker_type_counts[worker_type] > 0:
        #         return False

        return True


    def get_avail_worker_counts(self, n_worker_types):
        '''counts[i] = count of available workers of type i'''
        counts = np.zeros(n_worker_types)
        for worker_type in range(n_worker_types):
            for worker in self.workers:
                if worker.type_ == worker_type and worker.available:
                    counts[worker_type] += 1
        return counts


    def take_action(self, action):
        # retrieve selected stage object
        stage = self.jobs[action.job_id].stages[action.stage_id]

        task_ids = []

        # find workers that are closest to this stage's job
        for worker_type in stage.compatible_worker_types():
            if stage.saturated:
                break
            n_remaining_requests = action.n_workers - len(task_ids)
            for _ in range(n_remaining_requests):
                worker = self.find_closest_worker(stage, worker_type)
                if worker is None:
                    break
                task_id = self.schedule_worker(worker, stage)
                task_ids += [task_id]

        print(f'scheduled {len(task_ids)} tasks')

        # check if stage is now saturated; if so, remove from frontier
        stage_idx = self.get_stage_idx(action.job_id, action.stage_id)
        if stage.saturated:
            self.remove_stage_from_frontier(stage_idx)
            self.saturate_stage(stage_idx)

        return stage, task_ids


    def find_closest_worker(self, stage, worker_type):
        '''chooses an available worker for a stage's 
        next task, according to the following priority:
        1. worker is already at stage
        2. worker is not at stage but is at stage's job
        3. any other available worker
        if the stage is already saturated, or if no 
        worker is found, then `None` is returned
        '''
        if stage.saturated:
            return None

        # try to find available worker already at the stage
        for task in stage.tasks:
            if task.worker_id == Worker.INVALID_ID:
                continue
            worker = self.workers[task.worker_id]
            if worker.type_ == worker_type and worker.available:
                return worker

        # job_is_at_worker_capacity = \
        #     self.jobs[stage.job_id].is_at_worker_capacity

        # try to find available worker at stage's job;
        # if none is found then return any available worker
        avail_worker = None
        for worker in self.workers:
            if worker.type_ == worker_type and worker.available:
                if worker.job_id == stage.job_id:
                    return worker
                elif avail_worker == None: #and not job_is_at_worker_capacity:
                    avail_worker = worker
        return avail_worker


    def schedule_worker(self, worker, stage):
        # check if the worker is moving to a different job
        # if worker.job_id != stage.job_id:
        #     old_job_id = worker.job_id
        #     new_job_id = stage.job_id
        #     self.update_job_worker_counts(old_job_id, new_job_id)

        worker.assign_new_stage(stage)

        task_id = stage.add_worker(worker, self.wall_time.copy())
        return task_id


    def update_job_worker_counts(self, old_job_id, new_job_id):
        old_job = self.jobs[old_job_id] \
            if old_job_id != Job.INVALID_ID \
            else None
        new_job = self.jobs[new_job_id]

        if old_job is not None:
            print(old_job_id, new_job_id)
            assert old_job.n_workers > 0
            old_job.n_workers -= 1
        
        assert not new_job.is_at_worker_capacity
        new_job.n_workers += 1


    def process_task_completion(self, stage, task_id):
        worker_id = stage.tasks[task_id].worker_id
        worker = self.workers[worker_id]

        stage.add_task_completion(task_id, self.wall_time.copy())
        
        worker.make_available()

        if stage.is_complete:
            print('stage completion')
            self.process_stage_completion(stage)
        
        job = self.jobs[stage.job_id]
        if job.is_complete:
            print('job completion')
            self.process_job_completion(job)


    def process_stage_completion(self, stage):
        self.jobs[stage.job_id].add_stage_completion()

        stage_idx = self.get_stage_idx(stage.job_id, stage.id_)
        self.remove_stage_from_saturated(stage_idx)

        # add stage's decendents to the frontier, if their
        # other dependencies are also satisfied
        job = self.jobs[stage.job_id]
        new_stages_ids = job.find_new_frontiers(stage)
        new_stages_idxs = \
            self.get_stage_indices(job.id_, new_stages_ids)
        self.add_stages_to_frontier(new_stages_idxs)


    def process_job_completion(self, job):
        self.n_completed_jobs += 1
        job.t_completed = self.wall_time.copy()


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