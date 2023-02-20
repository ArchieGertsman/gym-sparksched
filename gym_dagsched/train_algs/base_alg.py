from abc import ABC, abstractmethod
from typing import Optional, Iterable
import shutil
import os
import sys
from itertools import chain

import numpy as np
from gymnasium.core import ObsType, ActType
import torch
from torch import Tensor
from torch.utils.data import DataLoader
from torch.multiprocessing import Pipe, Process
from torch.utils.tensorboard import SummaryWriter

from ..agents.decima_agent import DecimaAgent
from ..utils.device import device
from ..utils.profiler import Profiler
from ..utils.returns_calculator import ReturnsCalculator
from .rollouts import RolloutBuffer, RolloutDataset, rollout_worker
from ..utils.baselines import compute_baselines
from ..utils.graph import ObsBatch, collate_obsns




class BaseAlg(ABC):
    '''Base class for training algorithms, which must
    implement the abstract `_compute_loss` method
    '''

    def __init__(
        self,
        env_kwargs: dict,
        num_iterations: int,
        num_epochs: int,
        batch_size: int,
        num_envs: int,
        seed: int,
        log_dir: str,
        summary_writer_dir: Optional[str],
        model_save_dir: str,
        model_save_freq: int,
        optim_class: torch.optim.Optimizer,
        optim_lr: float,
        max_grad_norm: float,
        gamma: float,
        max_time_mean_init: float,
        max_time_mean_growth: float,
        max_time_mean_clip_range: float,
        entropy_weight_init: float,
        entropy_weight_decay: float,
        entropy_weight_min: float
    ):  
        self.num_iterations = num_iterations
        self.num_epochs = num_epochs
        self.batch_size = batch_size
        self.num_envs = num_envs

        self.log_dir = log_dir
        self.summary_writer_path = summary_writer_dir
        self.model_save_path = model_save_dir
        self.model_save_freq = model_save_freq

        self.max_time_mean = max_time_mean_init
        self.max_time_mean_growth = max_time_mean_growth
        self.max_time_mean_clip_range = max_time_mean_clip_range

        self.entropy_weight = entropy_weight_init
        self.entropy_weight_decay = entropy_weight_decay
        self.entropy_weight_min = entropy_weight_min

        self.agent = \
            DecimaAgent(
                env_kwargs['num_workers'],
                optim_class=optim_class,
                optim_lr=optim_lr,
                max_grad_norm=max_grad_norm)

        # computes differential returns by default, which is
        # helpful for maximizing average returns
        self.return_calc = ReturnsCalculator(gamma)

        self.env_kwargs = env_kwargs

        torch.manual_seed(seed)
        self.np_random_max_time = np.random.RandomState(seed)
        self.dataloader_gen = torch.Generator()
        self.dataloader_gen.manual_seed(seed)

        self.procs = []
        self.conns = []



    def train(self) -> None:
        '''trains the model on different job arrival sequences. 
        For each job sequence, 
        - multiple rollouts are collected in parallel, asynchronously
        - the rollouts are gathered at the center, where model parameters
            are updated, and
        - new model parameters are scattered to the rollout workers
        '''

        self._setup()

        for iteration in range(self.num_iterations):
            max_time = self._sample_max_time()

            self._log_iteration_start(iteration, max_time)

            state_dict = self.agent.actor_network.state_dict()
            if (iteration+1) % self.model_save_freq == 0:
                torch.save(state_dict, f'{self.model_save_path}/model.pt')
            
            # scatter
            env_options = {'max_wall_time': max_time}
            [conn.send((state_dict, env_options)) for conn in self.conns]

            # gather
            (rollout_buffers,
             avg_job_durations,
             completed_job_counts) = \
                zip(*[conn.recv() for conn in self.conns])

            with Profiler():
                policy_loss, entropy_loss = \
                    self._learn_from_rollouts(rollout_buffers)
                torch.cuda.synchronize()

            if self.summary_writer:
                ep_lens = [len(buff) for buff in rollout_buffers]
                self._write_stats(
                    iteration,
                    policy_loss,
                    entropy_loss,
                    avg_job_durations,
                    completed_job_counts,
                    ep_lens,
                    max_time
                )

            self._update_vars()

        self._cleanup()



    ## internal methods

    @abstractmethod
    def _compute_loss(
        self,
        obsns: ObsBatch,
        actions: Tensor,
        advantages: Tensor,
        old_lgprobs: Tensor
    ) -> tuple[Tensor, float, float]:
        '''Loss calculation unique to each algorithm

        Returns: 
            tuple (total_loss, policy_loss, entropy_loss),
            where total_loss is differentiable and the other losses
            are just scalars for logging.
        '''
        pass



    def _learn_from_rollouts(
        self,
        rollout_buffers: Iterable[RolloutBuffer]
    ) -> tuple[float, float]:

        dataloader = self._make_dataloader(rollout_buffers)

        policy_losses = []
        entropy_losses = []

        # run multiple learning epochs with minibatching
        for _ in range(self.num_epochs):
            for obsns, actions, advantages, old_lgprobs in dataloader:
                total_loss, action_loss, entropy_loss = \
                    self._compute_loss(
                        obsns, 
                        actions, 
                        advantages,
                        old_lgprobs
                    )

                policy_losses += [action_loss]
                entropy_losses += [entropy_loss]

                self.agent.update_parameters(total_loss)

        return np.sum(policy_losses), np.sum(entropy_losses)



    def _make_dataloader(
        self,
        rollout_buffers: Iterable[RolloutBuffer]
    ) -> DataLoader:
        '''creates a dataset out of the new rollouts, and returns a 
        dataloader that loads minibatches from that dataset
        '''

        # separate the rollout data into lists
        obsns_list, actions_list, wall_times_list, rewards_list = \
            zip(*((buff.obsns, buff.actions, buff.wall_times, buff.rewards)
                  for buff in rollout_buffers)) 

        # flatten observations and actions into a dict for fast access time
        obsns = {i: obs for i, obs in enumerate(chain(*obsns_list))}
        actions = {i: act for i, act in enumerate(chain(*actions_list))}

        advantages = self._compute_advantages(rewards_list, wall_times_list)
        advantages = torch.from_numpy(advantages)

        old_lgprobs = self._compute_old_lgprobs(obsns.values(), actions.values())
        
        rollout_dataset = \
            RolloutDataset(
                obsns, 
                actions, 
                advantages, 
                old_lgprobs
            )

        dataloader = \
            DataLoader(
                dataset=rollout_dataset,
                batch_size=self.batch_size,
                shuffle=True,
                collate_fn=RolloutDataset.collate,
                generator=self.dataloader_gen
            )

        return dataloader



    @torch.no_grad()
    def _compute_old_lgprobs(
        self, 
        obsns: Iterable[ObsType], 
        actions: Iterable[ActType]
    ) -> Tensor:
        obsns = collate_obsns(obsns)
        actions = torch.tensor([list(act.values()) for act in actions])
        old_lgprobs, _ = self.agent.evaluate_actions(obsns, actions)
        return old_lgprobs



    def _compute_advantages(
        self,
        rewards_list: list[np.ndarray],
        wall_times_list: list[np.ndarray]
    ) -> np.ndarray:

        returns_list = self.return_calc(rewards_list, wall_times_list)
        baselines_list, stds_list = compute_baselines(wall_times_list, returns_list)

        returns = np.hstack(returns_list)
        baselines = np.hstack(baselines_list)
        stds = np.hstack(stds_list)

        advantages = (returns - baselines) / (stds + 1e-8)

        return advantages



    def _setup(self) -> None:
        shutil.rmtree(self.log_dir, ignore_errors=True)
        os.mkdir(self.log_dir)
        sys.stdout = open(f'{self.log_dir}/main.out', 'a')
        
        print('cuda available:', torch.cuda.is_available())

        torch.multiprocessing.set_start_method('forkserver')
        
        self.summary_writer = None
        if self.summary_writer_path:
            self.summary_writer = SummaryWriter(self.summary_writer_path)

        self.agent.build(device)

        self._start_rollout_workers()



    def _cleanup(self) -> None:
        self._terminate_rollout_workers()

        if self.summary_writer:
            self.summary_writer.close()



    @classmethod
    def _log_iteration_start(cls, i, max_time):
        print_str = f'training on sequence {i+1}'
        if max_time < np.inf:
            print_str += f' (max wall time = {max_time*1e-3:.1f}s)'
        print(print_str, flush=True)



    def _start_rollout_workers(self) -> None:
        self.procs = []
        self.conns = []

        for rank in range(self.num_envs):
            conn_main, conn_sub = Pipe()
            self.conns += [conn_main]

            proc = Process(
                target=rollout_worker, 
                args=(rank, conn_sub, self.env_kwargs)
            )

            self.procs += [proc]
            proc.start()



    def _terminate_rollout_workers(self) -> None:
        [conn.send(None) for conn in self.conns]
        [proc.join() for proc in self.procs]



    def _sample_max_time(self):
        max_time = self.np_random_max_time.exponential(self.max_time_mean)
        max_time = np.clip(
            max_time, 
            self.max_time_mean - self.max_time_mean_clip_range,
            self.max_time_mean + self.max_time_mean_clip_range
        )
        return max_time



    def _write_stats(
        self,
        epoch: int,
        policy_loss: float,
        entropy_loss: float,
        avg_job_durations: list[float],
        completed_job_counts: list[int],
        ep_lens: list[int],
        max_time: float
    ) -> None:

        episode_stats = {
            'avg job duration': np.mean(avg_job_durations),
            'max wall time': max_time * 1e-3,
            'completed jobs count': np.mean(completed_job_counts),
            'avg reward per sec': self.return_calc.avg_per_step_reward(),
            'policy loss': policy_loss,
            'entropy loss': entropy_loss,
            'episode length': np.mean(ep_lens)
        }

        for name, stat in episode_stats.items():
            self.summary_writer.add_scalar(name, stat, epoch)



    def _update_vars(self) -> None:
        # increase the mean episode duration
        self.max_time_mean += self.max_time_mean_growth

        # decrease the entropy weight
        self.entropy_weight = np.clip(
            self.entropy_weight - self.entropy_weight_decay,
            self.entropy_weight_min,
            None
        )
