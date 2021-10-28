"""Behavioural Cloning (BC).

Trains policy by applying supervised learning to a fixed dataset of (observation,
action) pairs generated by some expert demonstrator.
"""

import os
import sys
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple, Type, Union

import gym
import numpy as np
import torch as th
import tqdm.autonotebook as tqdm
from stable_baselines3.common import policies, preprocessing, utils, vec_env

from imitation.algorithms import base as algo_base
from imitation.data import rollout, types
from imitation.policies import base as policy_base
from imitation.util import logger, util


def reconstruct_policy(
    policy_path: str,
    device: Union[th.device, str] = "auto",
) -> policies.BasePolicy:
    """Reconstruct a saved policy.

    Args:
        policy_path: path where `.save_policy()` has been run.
        device: device on which to load the policy.

    Returns:
        policy: policy with reloaded weights.
    """
    policy = th.load(policy_path, map_location=utils.get_device(device))
    assert isinstance(policy, policies.BasePolicy)
    return policy


class ConstantLRSchedule:
    """A callable that returns a constant learning rate."""

    def __init__(self, lr: float = 1e-3):
        """Builds ConstantLRSchedule.

        Args:
            lr: the constant learning rate that calls to this object will return.
        """
        self.lr = lr

    def __call__(self, _):
        """Returns the constant learning rate."""
        return self.lr


class _NoopTqdm:
    """Dummy replacement for tqdm.tqdm() when we don't want a progress bar visible."""

    def close(self):
        pass

    def set_description(self, s):
        pass

    def update(self, n):
        pass


class EpochOrBatchIteratorWithProgress:
    """Wraps DataLoader so that all BC batches can be processed in one for-loop.

    Also uses `tqdm` to show progress in stdout.
    """

    def __init__(
        self,
        data_loader: Iterable[algo_base.TransitionMapping],
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        use_tqdm: Optional[bool] = None,
        on_epoch_end: Optional[Callable[[], None]] = None,
        on_batch_end: Optional[Callable[[], None]] = None,
        progress_bar_visible: bool = True,
    ):
        """Builds EpochOrBatchIteratorWithProgress.

        Args:
            data_loader: An iterable over data dicts, as used in `BC`.
            n_epochs: The number of epochs to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            n_batches: The number of batches to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            use_tqdm: Show a tqdm progress bar if True. True by default if stdout is a
                TTY.
            on_epoch_end: A callback function without parameters to be called at the
                end of every epoch.
            on_batch_end: A callback function without parameters to be called at the
                end of every batch.
            progress_bar_visible: If True, then show a tqdm progress bar.

        Raises:
            ValueError: If neither or both of `n_epochs` and `n_batches` are non-None.
        """
        if n_epochs is not None and n_batches is None:
            self.use_epochs = True
        elif n_epochs is None and n_batches is not None:
            self.use_epochs = False
        else:
            raise ValueError(
                "Must provide exactly one of `n_epochs` and `n_batches` arguments.",
            )

        self.data_loader = data_loader
        self.n_epochs = n_epochs
        self.n_batches = n_batches
        self.use_tqdm = os.isatty(sys.stdout.fileno()) if use_tqdm is None else use_tqdm
        self.on_epoch_end = on_epoch_end
        self.on_batch_end = on_batch_end
        self.progress_bar_visible = progress_bar_visible

    def __iter__(
        self,
    ) -> Iterable[Tuple[algo_base.TransitionMapping, Mapping[str, Any]]]:
        """Yields batches while updating tqdm display to display progress."""
        samples_so_far = 0
        epoch_num = 0
        batch_num = 0
        display = None
        batch_suffix = epoch_suffix = ""
        if self.progress_bar_visible:
            if self.use_epochs:
                display = tqdm.tqdm(total=self.n_epochs)
                epoch_suffix = f"/{self.n_epochs}"
            else:  # Use batches.
                display = tqdm.tqdm(total=self.n_batches)
                batch_suffix = f"/{self.n_batches}"
        else:
            display = _NoopTqdm()

        def update_desc():
            assert display is not None
            display.set_description(
                f"batch: {batch_num}{batch_suffix}  epoch: {epoch_num}{epoch_suffix}",
            )

        try:
            while True:
                if display is not None:
                    update_desc()
                got_data_on_epoch = False
                for batch in self.data_loader:
                    got_data_on_epoch = True
                    batch_num += 1
                    batch_size = len(batch["obs"])
                    assert batch_size > 0
                    samples_so_far += batch_size
                    stats = dict(
                        epoch_num=epoch_num,
                        batch_num=batch_num,
                        samples_so_far=samples_so_far,
                    )
                    yield batch, stats
                    if self.on_batch_end is not None:
                        self.on_batch_end()
                    if not self.use_epochs:
                        if display is not None:
                            update_desc()
                            display.update(1)
                        if batch_num >= self.n_batches:
                            return
                if not got_data_on_epoch:
                    raise AssertionError(
                        f"Data loader returned no data after "
                        f"{batch_num} batches, during epoch "
                        f"{epoch_num} -- did it reset correctly?",
                    )
                epoch_num += 1
                if self.on_epoch_end is not None:
                    self.on_epoch_end(
                        samples_so_far=samples_so_far, epoch_num=epoch_num,
                        batch_num=batch_num)

                if self.use_epochs:
                    if display is not None:
                        update_desc()
                        display.update(1)
                    if epoch_num >= self.n_epochs:
                        return

        finally:
            if display is not None:
                display.close()


class BC(algo_base.DemonstrationAlgorithm):
    """Behavioral cloning (BC).

    Recovers a policy via supervised learning from observation-action pairs.
    """

    def __init__(
        self,
        *,
        observation_space: gym.Space,
        action_space: gym.Space,
        policy: Optional[policies.BasePolicy] = None,
        demonstrations: Optional[algo_base.AnyTransitions] = None,
        batch_size: int = 32,
        optimizer_cls: Type[th.optim.Optimizer] = th.optim.Adam,
        optimizer_kwargs: Optional[Mapping[str, Any]] = None,
        ent_weight: float = 1e-3,
        l2_weight: float = 0.0,
        device: Union[str, th.device] = "auto",
        custom_logger: Optional[logger.HierarchicalLogger] = None,
        augmentation_fn: Optional[Callable[[th.Tensor], th.Tensor]] = None,
    ):
        """Builds BC.

        Args:
            observation_space: the observation space of the environment.
            action_space: the action space of the environment.
            policy: a Stable Baselines3 policy; if unspecified,
                defaults to `FeedForward32Policy`.
            demonstrations: Demonstrations from an expert (optional). Transitions
                expressed directly as a `types.TransitionsMinimal` object, a sequence
                of trajectories, or an iterable of transition batches (mappings from
                keywords to arrays containing observations, etc).
            batch_size: The number of samples in each batch of expert data.
            optimizer_cls: optimiser to use for supervised training.
            optimizer_kwargs: keyword arguments, excluding learning rate and
                weight decay, for optimiser construction.
            ent_weight: scaling applied to the policy's entropy regularization.
            l2_weight: scaling applied to the policy's L2 regularization.
            augmentation_fn: function to augment a batch of (on-device) images
                (default: identity).
            device: name/identity of device to place policy on.
            custom_logger: Where to log to; if None (default), creates a new logger.

        Raises:
            ValueError: If `weight_decay` is specified in `optimizer_kwargs` (use the
                parameter `l2_weight` instead.)
        """
        self.batch_size = batch_size
        super().__init__(
            demonstrations=demonstrations,
            custom_logger=custom_logger,
        )

        if optimizer_kwargs:
            if "weight_decay" in optimizer_kwargs:
                raise ValueError("Use the parameter l2_weight instead of weight_decay.")
        self.tensorboard_step = 0

        self.action_space = action_space
        self.observation_space = observation_space
        self.device = utils.get_device(device)

        if policy is None:
            policy = policy_base.FeedForward32Policy(
                observation_space=observation_space,
                action_space=action_space,
                # Set lr_schedule to max value to force error if policy.optimizer
                # is used by mistake (should use self.optimizer instead).
                lr_schedule=ConstantLRSchedule(th.finfo(th.float32).max),
            )
        self._policy = policy.to(self.device)
        # TODO(adam): make policy mandatory and delete observation/action space params?
        assert self.policy.observation_space == self.observation_space
        assert self.policy.action_space == self.action_space

        optimizer_kwargs = optimizer_kwargs or {}
        self.optimizer = optimizer_cls(
            self.policy.parameters(),
            **optimizer_kwargs,
        )

        self.ent_weight = ent_weight
        self.l2_weight = l2_weight
        if augmentation_fn is None:
            augmentation_fn = util.identity
        self.augmentation_fn = augmentation_fn

    @property
    def policy(self) -> policies.BasePolicy:
        return self._policy

    def set_demonstrations(self, demonstrations: algo_base.AnyTransitions) -> None:
        self._demo_data_loader = algo_base.make_data_loader(
            demonstrations,
            self.batch_size,
        )

    def _calculate_loss(
        self,
        obs: Union[th.Tensor, np.ndarray],
        acts: Union[th.Tensor, np.ndarray],
    ) -> Tuple[th.Tensor, Mapping[str, float]]:
        """Calculate the supervised learning loss used to train the behavioral clone.

        Args:
            obs: The observations seen by the expert. Gradients are detached
                first before loss is calculated.
            acts: The actions taken by the expert. Gradients are detached first
                before loss is calculated.

        Returns:
            loss: The supervised learning loss for the behavioral clone to optimize.
            stats_dict: Statistics about the learning process to be logged.
        """
        obs = obs.detach()
        acts = acts.detach()

        _, log_prob, entropy = self.policy.evaluate_actions(obs, acts)
        prob_true_act = th.exp(log_prob).mean()
        log_prob = log_prob.mean()
        ent_loss = entropy = entropy.mean()

        l2_norms = [th.sum(th.square(w)) for w in self.policy.parameters()]
        l2_loss_raw = sum(l2_norms) / 2  # divide by 2 to cancel grad of square

        ent_term = -self.ent_weight * ent_loss
        neglogp = -log_prob
        l2_term = self.l2_weight * l2_loss_raw
        loss = neglogp + ent_term + l2_term

        stats_dict = dict(
            neglogp=neglogp.item(),
            loss=loss.item(),
            prob_true_act=prob_true_act.item(),
            ent_loss_raw=entropy.item(),
            ent_loss_term=ent_term.item(),
            l2_loss_raw=l2_loss_raw.item(),
            l2_loss_term=l2_term.item(),
        )

        return loss, stats_dict

    def _calculate_policy_norms(
        self, norm_type: Union[int, float] = 2
    ) -> Tuple[th.Tensor, th.Tensor]:
        """
        Calculate the gradient norm and the weight norm of the policy network.

        Args:
            norm_type: order of the norm.

        Returns:
            gradient_norm: norm of the gradient of the policy network (stored in each
                parameter's .grad attribute)
            weight_norm: norm of the weights of the policy network
        """

        norm_type = float(norm_type)

        gradient_parameters = list(
            filter(lambda p: p.grad is not None, self.policy.parameters())
        )
        stacked_gradient_norms = th.stack(
            [
                th.norm(p.grad.detach(), norm_type).to(self.policy.device)
                for p in gradient_parameters
            ]
        )
        stacked_weight_norms = th.stack(
            [
                th.norm(p.detach(), norm_type).to(self.policy.device)
                for p in self.policy.parameters()
            ]
        )

        gradient_norm = th.norm(stacked_gradient_norms, norm_type)
        weight_norm = th.norm(stacked_weight_norms, norm_type)

        return gradient_norm, weight_norm

    def train(
        self,
        *,
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        on_epoch_end: Optional[Callable[[], None]] = None,
        on_batch_end: Optional[Callable[[], None]] = None,
        log_interval: int = 500,
        log_rollouts_venv: Optional[vec_env.VecEnv] = None,
        log_rollouts_n_episodes: int = 5,
        progress_bar: bool = True,
        reset_tensorboard: bool = False,
    ):
        """Train with supervised learning for some number of epochs.

        Here an 'epoch' is just a complete pass through the expert data loader,
        as set by `self.set_expert_data_loader()`.

        Args:
            n_epochs: Number of complete passes made through expert data before ending
                training. Provide exactly one of `n_epochs` and `n_batches`.
            n_batches: Number of batches loaded from dataset before ending training.
                Provide exactly one of `n_epochs` and `n_batches`.
            on_epoch_end: Optional callback with no parameters to run at the end of each
                epoch.
            on_batch_end: Optional callback with no parameters to run at the end of each
                batch.
            log_interval: Log stats after every log_interval batches.
            log_rollouts_venv: If not None, then this VecEnv (whose observation and
                actions spaces must match `self.observation_space` and
                `self.action_space`) is used to generate rollout stats, including
                average return and average episode length. If None, then no rollouts
                are generated.
            log_rollouts_n_episodes: Number of rollouts to generate when calculating
                rollout stats. Non-positive number disables rollouts.
            progress_bar: If True, then show a progress bar during training.
            reset_tensorboard: If True, then start plotting to Tensorboard from x=0
                even if `.train()` logged to Tensorboard previously. Has no practical
                effect if `.train()` is being called for the first time.
        """
        it = EpochOrBatchIteratorWithProgress(
            self._demo_data_loader,
            n_epochs=n_epochs,
            n_batches=n_batches,
            on_epoch_end=on_epoch_end,
            on_batch_end=on_batch_end,
            progress_bar_visible=progress_bar,
        )

        if reset_tensorboard:
            self.tensorboard_step = 0

        batch_num = 0
        self.policy.train()
        for batch, stats_dict_it in it:
            # some later code (e.g. augmentation, and RNNs if we use them)
            # require contiguous tensors, hence the .contiguous()
            acts_tensor = (
                th.as_tensor(batch["acts"]).contiguous().to(self.policy.device)
            )
            obs_tensor = th.as_tensor(batch["obs"]).contiguous().to(self.policy.device)
            obs_tensor = preprocessing.preprocess_obs(
                obs_tensor,
                self.observation_space,
                normalize_images=True,
            )
            # we always apply augmentations to observations
            obs_tensor = self.augmentation_fn(obs_tensor)
            # FIXME(sam): SB policies *always* apply preprocessing, so we
            # need to undo the preprocessing we did before applying
            # augmentations. The code below is the inverse of SB's
            # preprocessing.preprocess_obs, but only for Box spaces.
            if isinstance(self.observation_space, gym.spaces.Box):
                if preprocessing.is_image_space(self.observation_space):
                    obs_tensor = obs_tensor * 255.0

            loss, stats_dict_loss = self._calculate_loss(obs_tensor, acts_tensor)

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            gradient_norm, weight_norm = self._calculate_policy_norms()
            norm_stats_dict = {
                "grad_norm": gradient_norm.item(),
                "weight_norm": weight_norm.item(),
                "n_updates": batch_num,
                "batch_size": len(obs_tensor),
                "lr_gmean": util.optim_lr_gmean(self.optimizer),
            }

            # FIXME(sam): is this the right way to do this? Originally our ILR
            # code was doing record_mean() for everything instead of doing
            # occasionally record() calls. Unclear to me which is more
            # appropriate.
            for stats in [stats_dict_it, stats_dict_loss, norm_stats_dict]:
                for k, v in stats.items():
                    self.logger.record_mean(f"bc/{k}", v)

            if batch_num % log_interval == 0:
                for stats in [stats_dict_it, stats_dict_loss, norm_stats_dict]:
                    for k, v in stats.items():
                        self.logger.record(f"bc/{k}", v)
                # TODO(shwang): Maybe instead use a callback that can be shared between
                #   all algorithms' `.train()` for generating rollout stats.
                #   EvalCallback could be a good fit:
                #   https://stable-baselines3.readthedocs.io/en/master/guide/callbacks.html#evalcallback
                if log_rollouts_venv is not None and log_rollouts_n_episodes > 0:
                    trajs = rollout.generate_trajectories(
                        self.policy,
                        log_rollouts_venv,
                        rollout.make_min_episodes(log_rollouts_n_episodes),
                    )
                    stats = rollout.rollout_stats(trajs)
                    self.logger.record("batch_size", len(batch["obs"]))
                    for k, v in stats.items():
                        if "return" in k and "monitor" not in k:
                            self.logger.record("rollout/" + k, v)
                self.logger.dump(self.tensorboard_step)
            batch_num += 1
            self.tensorboard_step += 1

    def save_policy(self, policy_path: types.AnyPath) -> None:
        """Save policy to a path. Can be reloaded by `.reconstruct_policy()`.

        Args:
            policy_path: path to save policy to.
        """
        th.save(self.policy, policy_path)
