#!/usr/bin/env python3

import argparse
import datetime
import os
import pprint
import env

import gym
import numpy as np
import torch
import pybullet_envs
from torch import nn
from torch.distributions import Independent, Normal
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.tensorboard import SummaryWriter

from tianshou.data import Collector, ReplayBuffer, VectorReplayBuffer
from tianshou.env import SubprocVectorEnv
from tianshou.policy import PPOPolicy
from tianshou.trainer import onpolicy_trainer
from tianshou.utils import TensorboardLogger
from tianshou.utils.net.common import Net

from env.exhx5_walkingmodule_env import Exhx5WalkEnv
from env.env_wrapper import RandomWrapper, MetaWrapper, RandomDynamicWrapper, \
    RandomCoMWrapper, RandomFrictionWrapper, RandomVelocityWrapper, RandomDampingWrapper, RandomStiffnessWrapper, \
    RandomIntervalWrapper
from env.make_random_env import make_random_env, make_random_adapt_env
from model.net import MQLRecurrentCritic, MQLRecurrentActorProb, MetaDynamic, ActorProb, Critic, WorldModel
from model.policy import MyPolicy


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='Exhx5WalkMod-v0')
    parser.add_argument('--subtask', type=str, default='friction')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--buffer-size', type=int, default=20000)
    parser.add_argument('--uni-buffer-size', type=int, default=1000000)
    parser.add_argument('--hidden-sizes', type=int, nargs='*', default=[64, 64])
    parser.add_argument('--dyn-hidden-sizes', type=int, nargs='*', default=[300, 300])
    parser.add_argument('--dyn-batch-size', type=int, default=128)
    parser.add_argument('--dyn-epoch', type=int, default=500)
    parser.add_argument('--recurrent', type=int, default=False)
    parser.add_argument('--universal-policy', type=int, default=True)
    parser.add_argument('--context-hidden-size', type=int, default=64)
    parser.add_argument('--context-size', type=int, default=8)
    parser.add_argument('--layer-num', type=int, default=1)
    parser.add_argument('--history-len', type=int, default=8)
    parser.add_argument('--lr', type=float, default=3e-4)
    parser.add_argument('--dyn-lr', type=float, default=1e-3)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--epoch', type=int, default=100)
    parser.add_argument('--step-per-epoch', type=int, default=1000)
    parser.add_argument('--step-per-collect', type=int, default=1000)
    parser.add_argument('--repeat-per-collect', type=int, default=10)
    parser.add_argument('--batch-size', type=int, default=64)
    parser.add_argument('--training-num', type=int, default=10)
    parser.add_argument('--test-num', type=int, default=10)
    # ppo special
    parser.add_argument('--rew-norm', type=int, default=False)
    # In theory, `vf-coef` will not make any difference if using Adam optimizer.
    parser.add_argument('--vf-coef', type=float, default=0.25)
    parser.add_argument('--ent-coef', type=float, default=0.0)
    parser.add_argument('--gae-lambda', type=float, default=0.95)
    parser.add_argument('--bound-action-method', type=str, default="clip")
    parser.add_argument('--lr-decay', type=int, default=False)
    parser.add_argument('--max-grad-norm', type=float, default=0.5)
    parser.add_argument('--eps-clip', type=float, default=0.2)
    parser.add_argument('--dual-clip', type=float, default=None)
    parser.add_argument('--value-clip', type=int, default=0)
    parser.add_argument('--norm-adv', type=int, default=0)
    parser.add_argument('--recompute-adv', type=int, default=0)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument('--render', type=float, default=0.)
    parser.add_argument(
        '--device', type=str, default='cuda' if torch.cuda.is_available() else 'cpu'
    )
    parser.add_argument('--resume', type=int, default=True)
    parser.add_argument('--resume-path', type=str,
                        default='log/Exhx5WalkMod-v0/ppo_pre_train_with_context_world_model_epoch_500/baseline_pre_train_world_model_with_context_epoch_500')
    parser.add_argument('--adapt', type=int, default=True)
    parser.add_argument('--augmentation-ratio', type=int, default=0)
    parser.add_argument(
        '--watch',
        default=False,
        action='store_true',
        help='watch the play of pre-trained policy only'
    )
    return parser.parse_args()


def test_ppo(args=get_args()):
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.recurrent:
        env_wrapper = MetaWrapper
    else:
        env_wrapper = RandomWrapper
    if args.subtask == 'friction':
        env_wrapper = RandomFrictionWrapper
    elif args.subtask == 'velocity':
        env_wrapper = RandomVelocityWrapper
    elif args.subtask == 'damping':
        env_wrapper = RandomDampingWrapper
    elif args.subtask == 'stiffness':
        env_wrapper = RandomStiffnessWrapper
    elif args.subtask == 'interval':
        env_wrapper = RandomIntervalWrapper
    elif args.subtask == 'CoM':
        env_wrapper = RandomCoMWrapper
    else:
        env_wrapper = None
    if args.task == 'Exhx5WalkMod-v0':
        env = env_wrapper(MetaWrapper(Exhx5WalkEnv(render=args.watch)))
        if args.watch:
            train_envs = env
            test_envs = env
        else:
            train_envs, test_envs = make_random_adapt_env(task=Exhx5WalkEnv, wrapper=env_wrapper,
                                                          adapt_task_num=args.training_num, seed=args.seed)
    elif args.task == 'Exhx5Gazebo-v0':
        env = RandomWrapper(MetaWrapper(Exhx5GazeboEnv()))
        train_envs = env
        test_envs = env
        args.training_num = 1
        args.test_num = 5
    else:
        env = gym.make(args.task)
        train_envs = env
        test_envs = env
        args.training_num = 1
        args.test_num = 5
    args.oarc_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    args.max_action = env.action_space.high[0]
    args.state_shape = (args.oarc_shape[0] - args.action_shape[0] - 1,)
    print("Observations shape:", args.state_shape)
    print("OARC shape:", args.oarc_shape)
    print("Actions shape:", args.action_shape)
    print("Action range:", np.min(env.action_space.low), np.max(env.action_space.high))

    # seed
    # train_envs.seed(args.seed)
    # test_envs.seed(args.seed)
    # model
    if args.recurrent:
        actor = MQLRecurrentActorProb(layer_num=args.layer_num, state_shape=args.state_shape,
                                      action_shape=args.action_shape,
                                      context_hidden_size=args.context_hidden_size,
                                      context_size=args.context_size,
                                      hidden_sizes=args.hidden_sizes,
                                      max_action=args.max_action, device=args.device, unbounded=True,
                                      conditioned_sigma=False).to(args.device)
        critic = MQLRecurrentCritic(layer_num=args.layer_num, state_shape=args.state_shape,
                                    device=args.device, context_hidden_size=args.context_hidden_size,
                                    context_size=args.context_size,
                                    hidden_sizes=args.hidden_sizes).to(args.device)
        if args.training_num > 1:
            buffer = VectorReplayBuffer(args.buffer_size, len(train_envs), stack_num=args.history_len)
        else:
            buffer = ReplayBuffer(args.buffer_size, stack_num=args.history_len)
    else:
        net_a = Net(
            args.state_shape,
            hidden_sizes=args.hidden_sizes,
            activation=nn.Tanh,
            device=args.device
        )
        actor = ActorProb(
            preprocess_net=net_a,
            state_shape=args.state_shape,
            action_shape=args.action_shape,
            context_size=args.context_size,
            max_action=args.max_action,
            unbounded=True,
            device=args.device
        ).to(args.device)
        net_c = Net(
            args.state_shape,
            hidden_sizes=args.hidden_sizes,
            activation=nn.Tanh,
            device=args.device
        )
        critic = Critic(state_shape=args.state_shape, context_size=args.context_size,
                        preprocess_net=net_c, device=args.device).to(args.device)
        if args.training_num > 1:
            buffer = VectorReplayBuffer(args.buffer_size, len(train_envs), stack_num=args.history_len)
        else:
            buffer = ReplayBuffer(args.buffer_size, stack_num=args.history_len)
    dynamic = WorldModel(args.oarc_shape, args.action_shape,
                         context_hidden_size=args.context_hidden_size,
                         context_size=args.context_size,
                         hidden_sizes=args.dyn_hidden_sizes, device=args.device).to(args.device)
    universal_buffer = ReplayBuffer(args.uni_buffer_size, stack_num=args.history_len)
    torch.nn.init.constant_(actor.sigma_param, -0.5)
    for m in list(actor.modules()) + list(critic.modules()):
        if isinstance(m, torch.nn.Linear):
            # orthogonal initialization
            torch.nn.init.orthogonal_(m.weight, gain=np.sqrt(2))
            torch.nn.init.zeros_(m.bias)
    # do last policy layer scaling, this will make initial actions have (close to)
    # 0 mean and std, and will help boost performances,
    # see https://arxiv.org/abs/2006.05990, Fig.24 for details
    for m in actor.mu.modules():
        if isinstance(m, torch.nn.Linear):
            torch.nn.init.zeros_(m.bias)
            m.weight.data.copy_(0.01 * m.weight.data)

    dynamic_optim = torch.optim.Adam(
        list(dynamic.parameters()), lr=args.dyn_lr
    )
    optim = torch.optim.Adam(
        list(actor.parameters()) + list(critic.parameters()), lr=args.lr
    )

    lr_scheduler = None
    if args.lr_decay:
        # decay learning rate to 0 linearly
        max_update_num = np.ceil(
            args.step_per_epoch / args.step_per_collect
        ) * args.epoch

        lr_scheduler = LambdaLR(
            optim, lr_lambda=lambda epoch: 1 - epoch / max_update_num
        )

    def dist(*logits):
        return Independent(Normal(*logits), 1)

    policy = MyPolicy(
        dynamic=dynamic,
        dynamic_optim=dynamic_optim,
        universal_buffer=universal_buffer,
        adapt=args.adapt,
        augmentation_ratio=args.augmentation_ratio,
        actor=actor,
        critic=critic,
        optim=optim,
        dist_fn=dist,
        discount_factor=args.gamma,
        gae_lambda=args.gae_lambda,
        max_grad_norm=args.max_grad_norm,
        vf_coef=args.vf_coef,
        ent_coef=args.ent_coef,
        reward_normalization=args.rew_norm,
        action_scaling=True,
        action_bound_method=args.bound_action_method,
        lr_scheduler=lr_scheduler,
        action_space=env.action_space,
        eps_clip=args.eps_clip,
        value_clip=args.value_clip,
        dual_clip=args.dual_clip,
        advantage_normalization=args.norm_adv,
        recompute_advantage=args.recompute_adv
    )

    # load a previous policy
    if args.resume:
        args.resume_path = args.resume_path + '_' + str(args.seed)
        ckpt_path = os.path.join(args.resume_path, 'checkpoint_99.pth')
        checkpoint = torch.load(ckpt_path, map_location=args.device)
        policy.load_state_dict(checkpoint['policy'])
        # optim.load_state_dict(checkpoint['optim'])
        dynamic_optim.load_state_dict(checkpoint['dyn_optim'])
        print("Loaded agent from: ", args.resume_path)

    # collector
    train_collector = Collector(policy, train_envs, buffer, exploration_noise=True)
    test_collector = Collector(policy, test_envs)
    # log
    t0 = datetime.datetime.now().strftime("%m%d_%H%M%S")
    log_file = f'seed_{args.seed}_{t0}-{args.task.replace("-", "_")}_ppo'
    log_path = os.path.join(args.logdir, args.task, 'ppo_dynamic_aware_unit_' + str(args.subtask), log_file)
    # if args.resume:
    #     log_path = args.resume_path
    writer = SummaryWriter(log_path)
    writer.add_text("args", str(args))
    logger = TensorboardLogger(writer, update_interval=100, train_interval=100, save_interval=50)

    def save_fn(policy):
        torch.save({'policy': policy.state_dict(),
                    'optim': optim.state_dict(),
                    'dyn_optim': dynamic_optim.state_dict()
                    },
                   os.path.join(log_path, 'model.pth'))

    def save_checkpoint_fn(epoch, env_step, gradient_step):
        torch.save(
            {'policy': policy.state_dict(),
             'optim': optim.state_dict(),
             'dyn_optim': dynamic_optim.state_dict()
             },
            os.path.join(log_path, 'checkpoint.pth')
        )

    policy.eval()
    policy.predict_dynamic(train_collector)
    train_collector.reset()

    if not args.watch:
        # trainer
        result = onpolicy_trainer(
            policy,
            train_collector,
            test_collector,
            args.epoch,
            args.step_per_epoch,
            args.repeat_per_collect,
            episode_per_test=10,
            batch_size=args.batch_size,
            step_per_collect=args.step_per_collect,
            save_fn=save_fn,
            logger=logger,
            test_in_train=False,
            save_checkpoint_fn=save_checkpoint_fn,
            resume_from_log=args.resume,
        )
        pprint.pprint(result)

    # Let's watch its performance!
    policy.eval()
    test_envs.seed(args.seed)
    test_collector.reset()
    result = test_collector.collect(n_episode=args.test_num, render=args.render)
    print(f'Final reward: {result["rews"].mean()}, length: {result["lens"].mean()}')


if __name__ == '__main__':
    test_ppo()
