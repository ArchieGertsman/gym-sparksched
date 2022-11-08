from collections import defaultdict
from copy import deepcopy as dcp
from copy import copy as cp
from time import time
from sys import getsizeof as sizeof

import numpy as np
import torch
import torch.nn.functional as F
from torch_geometric.utils.convert import from_networkx
from torch_geometric.data import Batch

from ..entities.timeline import JobArrival, TaskCompletion, WorkerArrival
from ..utils.device import device
from ..entities.operation import FeatureIdx


class DagSchedEnv:
    '''An OpenAI-Gym-style simulation environment for scheduling 
    streaming jobs consisting of interdependent operations. 
    
    What?
    "job": consists of "operations" that need to be completed by "workers"
    "streaming": arriving stochastically and continuously over time
    "scheduling": assigning workers to jobs
    "operation": can be futher split into "tasks" which are identical 
      and can be worked on in parallel. The number of tasks in a 
      operation is equal to the number of workers that can work on 
      the operation in parallel.
    "interdependent": some operations may depend on the results of others, 
      and therefore cannot begin execution until those dependencies are 
      satisfied. These dependencies are encoded in directed acyclic graphs 
      (dags) where an edge from operation (a) to operation (b) means that 
      (a) must complete before (b) can begin.

    Example: a cloud computing cluster is responsible for receiving workflows
        (i.e. jobs) and executing them using its resources. A  machine learning 
        workflow may consist of numerous operations, such as data prep, training/
        validation, hyperparameter tuning, testing, etc. and these operations 
        depend on each other, e.g. data prep comes before training. Data prep 
        can further be broken into tasks, where each task entails prepping a 
        subset of the data, and these tasks can easily be parallelized. 
        
    The process of assigning workers to jobs is crutial, as sophisticated 
    scheduling algorithms can significantly increase the system's efficiency.
    Yet, it turns out to be a very challenging problem.
    '''

    # multiplied with reward to control its magnitude
    REWARD_SCALE = 1e-5

    # expected time to move a worker between jobs
    # (mean of exponential distribution)
    MOVING_COST = 2000.


    def __init__(self, rank):
        self.rank = rank
        self.t_step = 0
        self.t_add_task_completion = 0
        self.t_schedule_worker = 0


    @property
    def all_jobs_complete(self):
        '''whether or not all the jobs in the system
        have been completed
        '''
        return len(self.active_job_ids) == 0


    @property
    def n_completed_jobs(self):
        return len(self.completed_job_ids)



    @property
    def n_active_jobs(self):
        return len(self.active_job_ids)



    @property
    def n_seen_jobs(self):
        return self.n_completed_jobs + self.n_active_jobs



    @property
    def are_actions_available(self):
        '''checks if there are any valid actions that can be
        taken by the scheduling agent.
        '''
        return len(self.avail_worker_ids) > 0 and len(self.frontier_ops) > 0



    def reset(self, initial_timeline, workers, x_ptrs):
        '''resets the simulation. should be called before
        each run (including first). all state data is found here.
        '''

        # a priority queue containing scheduling 
        # events indexed by wall time of occurance
        # self.timeline = cp(initial_timeline)
        # self.timeline.pq = cp(initial_timeline.pq)
        self.timeline = initial_timeline
        self.n_job_arrivals = len(initial_timeline.pq)
        # list of worker objects which are to be scheduled
        # to complete tasks within the simulation
        # self.workers = dcp(workers)
        self.workers = workers
        self.n_workers = len(workers)

        # wall clock time, keeps increasing throughout
        # the simulation
        self.wall_time = 0.

        # set of job objects within the system
        self.jobs = {}

        # list of ids of all active jobs
        self.active_job_ids = []

        # list of ids of all completed jobs
        self.completed_job_ids = []

        # operations in the system which are ready
        # to be executed by a worker because their
        # dependencies are satisfied
        self.frontier_ops = set()

        # operations in the system which have not 
        # yet completed but have all the resources
        # they need assigned to them
        self.saturated_ops = set()

        self.x_ptrs = x_ptrs

        self.avail_worker_ids = set([worker.id_ for worker in self.workers])

        self.max_ops = np.max([len(e.job.ops) for _,_,e in initial_timeline.pq])


        # if self.rank == 0:
        #     print(f'{self.t_step:.3f}, {self.t_add_task_completion:.3f}, {self.t_schedule_worker:.3f}')

        # print(f'{self.rank}: {self.t_step:.3f}')

        self.t_step = 0
        self.t_add_task_completion = 0
        self.t_schedule_worker = 0



    def step(self, job_id, op_id, n_workers):
        '''steps onto the next scheduling event on the timeline, 
        which can be one of the following:
        (1) new job arrival
        (2) task completion
        (3) "nudge," meaning that there are available actions,
            even though neither (1) nor (2) have occurred, so 
            the policy should consider taking one of them
        '''

        t_step = time()

        if job_id is not None:
            op = self.jobs[job_id].ops[op_id]
            assert op in self.frontier_ops
            self._take_action(op, n_workers)

        prev_time = self.wall_time
        
        # step through timeline until agent needs to be consulted

        needs_agent = False
        while not needs_agent and not self.timeline.empty:
            t, event = self.timeline.pop()
            self.wall_time = t

            needs_agent = self._process_scheduling_event(event)

        done = self.timeline.empty and len(self.avail_worker_ids) == len(self.workers)
        reward = self._calculate_reward(prev_time)

        self.t_step += time() - t_step

        return reward, done




    def construct_op_msk(self):
        '''returns a mask tensor indicating which operations
        can be scheduled, i.e. op_msk[i] = 1 if the
        i'th operation is in the frontier, 0 otherwise
        '''
        op_msk = torch.zeros((self.n_job_arrivals, self.max_ops), dtype=torch.bool)
        for j in self.active_job_ids:
            job = self.jobs[j]
            if job.n_avail_local > 0 or len(self.avail_worker_ids) > 0:
                for i,op in enumerate(job.ops):
                    if op in self.frontier_ops:
                        op_msk[j, i] = 1
        return op_msk



    def construct_prlvl_msk(self):
        '''returns a mask tensor indicating which parallelism
        levels are valid for each job dag, i.e. 
        prlvl_msk[i,l] = 1 if parallelism level `l` is
        valid for job `i`
        '''
        prlvl_msk = torch.zeros((self.n_job_arrivals, self.n_workers), dtype=torch.bool)
        # for i, job_id in enumerate(self.active_job_ids):
        #     job = self.jobs[job_id]
        #     n_local = len(job.local_workers)
        #     i_min = max(0, n_local)
        #     i_max = max(0, n_local + len(self.avail_worker_ids))
        #     prlvl_msk[i, i_min:i_max] = 1
        prlvl_msk[:, :len(self.avail_worker_ids)+1] = 1
        return prlvl_msk



    def _push_task_completion_events(self, op, tasks):
        '''Given a set of task ids and the operation they belong to,
        pushes each of their completions as events to the timeline
        '''
        while len(tasks) > 0:
            self._push_task_completion_event(op, tasks.pop())


    
    def _push_task_completion_event(self, op, task):
        '''pushes a single task completion event to the timeline'''
        assigned_worker_id = task.worker_id
        worker_type = self.workers[assigned_worker_id].type_
        t_completion = \
            task.t_accepted + op.task_duration[worker_type]
        event = TaskCompletion(op, task)
        self.timeline.push(t_completion, event)



    def _push_worker_arrival_event(self, worker, op):
        '''pushes the event of a worker arriving to a job
        to the timeline'''
        # moving_cost = np.random.exponential(self.MOVING_COST)
        t_arrival = self.wall_time + self.MOVING_COST
        event = WorkerArrival(worker, op)
        self.timeline.push(t_arrival, event)



    def _process_scheduling_event(self, event):
        '''handles a scheduling event from the timeline, 
        which can be a job arrival, a worker arrival, a 
        task completion, or a nudge
        '''
        if isinstance(event, JobArrival):
            return self._process_job_arrival(event.job)
        elif isinstance(event, WorkerArrival):
            return self._process_worker_arrival(event.worker, event.op)
        elif isinstance(event, TaskCompletion):
            return self._process_task_completion(event.op, event.task)
        else:
            print('invalid event')
            assert False



    def _process_job_arrival(self, job):
        '''adds a new job to the list of jobs, and adds all of
        its source operations to the frontier
        '''
        job.x_ptr = self.x_ptrs[job.id_]
        self.jobs[job.id_] = job
        self.active_job_ids += [job.id_]
        src_ops = job.find_src_ops()
        self.frontier_ops |= src_ops
        return True
        


    def _process_worker_arrival(self, worker, op):
        '''performs some bookkeeping when a worker arrives'''
        job = self.jobs[op.job_id]
        job.add_local_worker(worker.id_)
        worker.is_moving = False
        worker.job_id = job.id_


        if op.is_complete or op.saturated:
            self.avail_worker_ids.add(worker.id_)
            return True
        else:
            self._schedule_worker(worker, op)
            return False
            




    def _process_task_completion(self, op, task):
        '''performs some bookkeeping when a task completes'''

        # t_step = time()

        job = self.jobs[op.job_id]

        t = time()
        job.add_task_completion(op, task, self.wall_time)
        self.t_add_task_completion += time() - t
        
        
        worker = self.workers[task.worker_id]
        worker.task = None

        needs_agent = False
        
        if op.is_complete:
            self._process_op_completion(op, worker)
            needs_agent = True
        elif not op.saturated:
            t = time()
            self._schedule_worker(worker, op)
            self.t_schedule_worker += time() - t

        if job.is_complete:
            self._process_job_completion(job)

        # self.t_step += time() - t_step

        return needs_agent


        
    def _process_op_completion(self, op, worker):
        '''performs some bookkeeping when an operation completes'''
        self.avail_worker_ids.add(worker.id_)

        job = self.jobs[op.job_id]
        job.add_op_completion()
        
        self.saturated_ops.remove(op)

        # add stage's decendents to the frontier, if their
        # other dependencies are also satisfied
        new_ops = job.find_new_frontiers(op)
        self.frontier_ops |= new_ops



    def _process_job_completion(self, job):
        '''performs some bookkeeping when a job completes'''
        assert job.id_ in self.jobs
        self.active_job_ids.remove(job.id_)
        self.completed_job_ids += [job.id_]
        job.t_completed = self.wall_time



    def _take_action(self, op, prlvl):
        '''updates the state of the environment based on the
        provided action = (op, prlvl), where
        - op is an Operation object which shall receive work next, and
        - prlvl is the number of workers to allocate to `op`'s job.
            this must be at least the number of workers already
            local to the job, and if it's larger then more workers
            are sent to the job.
        returns a set of the Task objects which have been scheduled
        for processing
        '''
        self._send_more_workers(op, prlvl)
        tasks = self._schedule_workers(op)
        if len(tasks) > 0:
            self._push_task_completion_events(op, tasks)



    def _send_more_workers(self, op, prlvl):
        '''sends `min(n_workers_to_send, n_available_workers)` workers
        to `op`'s job, where `n_workers_to_send` is the difference
        between the requested `prlvl` and the number of workers already
        at `op`'s job.
        '''
        # n_workers_to_send = prlvl - len(job.local_workers)
        # assert n_workers_to_send >= 0
        n_workers_to_send = prlvl

        for worker_id in list(self.avail_worker_ids):
            if n_workers_to_send == 0:
                break
            worker = self.workers[worker_id]
            if worker.can_assign(op):
                self._send_worker(worker, op)    
                n_workers_to_send -= 1
            


    def _send_worker(self, worker, op):
        if worker.job_id is not None:
            old_job = self.jobs[worker.job_id]
            old_job.remove_local_worker(worker.id_)
        worker.is_moving = True
        self.avail_worker_ids.remove(worker.id_)
        self._push_worker_arrival_event(worker, op)



    def _schedule_workers(self, op):
        '''assigns all of the available workers at `op`'s job
        to start working on `op`. Returns the tasks in `op` which
        are schedule to receive work.'''
        tasks = set()
        job = self.jobs[op.job_id]

        for worker_id in job.local_workers:
            if op.saturated:
                break
            worker = self.workers[worker_id]
            if worker.can_assign(op):
                task = job.assign_worker(
                    worker, 
                    op,
                    self.wall_time)
                tasks.add(task)

        # check if stage is now saturated; if so, remove from frontier
        if op.saturated and op in self.frontier_ops:
            self.frontier_ops.remove(op)
            self.saturated_ops.add(op)

        return tasks


    def _schedule_worker(self, worker, op):
        # t = time()

        job = self.jobs[op.job_id]
        
        task = job.assign_worker(
                worker, 
                op,
                self.wall_time)

        self._push_task_completion_event(op, task)

        if op.saturated and op in self.frontier_ops:
            self.frontier_ops.remove(op)
            self.saturated_ops.add(op)

        # self.t_step += time() - t




    def _calculate_reward(self, prev_time):
        '''number of jobs in the system multiplied by the time
        that has passed since the previous scheduling event compleiton.
        minimizing this quantity is equivalent to minimizing the
        average job completion time, by Little's Law (see Decima paper)
        '''
        reward = 0.
        for job_id in self.active_job_ids:
            job = self.jobs[job_id]
            start = max(job.t_arrival, prev_time)
            end = min(job.t_completed, self.wall_time)
            reward -= (end - start)
        return reward * self.REWARD_SCALE


    def n_ops_per_job(self):
        return [len(self.jobs[j].ops) for j in self.active_job_ids]