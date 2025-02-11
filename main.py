#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import division
import argparse
import bz2
from datetime import datetime
import os
import pickle
import gym

import numpy as np
import torch
from tqdm import trange

from agent import Agent
from memory import PrioritizedReplayMemory
from utils import preprocess_frame
from test import test

import wimblepong

parser = argparse.ArgumentParser(description='Rainbow')
parser.add_argument('--id', type=str, default='Rainbow-5',
                    help='Experiment ID')
parser.add_argument('--seed', type=int, default=123, help='Random seed')
parser.add_argument('--disable-cuda', action='store_true',
                    help='Disable CUDA')
parser.add_argument('--env', type=str, default="WimblepongVisualSimpleAI-v0",
                    help='Choose Wimblepong environment')
parser.add_argument('--T-max', type=int, default=int(50e6),
                    metavar='STEPS',
                    help='Number of training steps'
                    )
parser.add_argument('--history-length', type=int, default=4,
                    metavar='T', help='Number of consecutive states processed')
parser.add_argument('--hidden-size', type=int, default=512,
                    metavar='SIZE', help='Network hidden size')
parser.add_argument('--noisy-std', type=float, default=0.1, metavar='σ',
                    help='Initial standard deviation of noisy linear layers'
                    )
parser.add_argument('--atoms', type=int, default=51, metavar='C',
                    help='Discretised size of value distribution')
parser.add_argument('--V-min', type=float, default=-10, metavar='V',
                    help='Minimum of value distribution support')
parser.add_argument('--V-max', type=float, default=10, metavar='V',
                    help='Maximum of value distribution support')
parser.add_argument('--model', type=str, metavar='PARAMS',
                    help='Pretrained model (state dict)')
parser.add_argument('--memory-capacity', type=int, default=int(1e6),
                    metavar='CAPACITY',
                    help='Experience replay memory capacity')
parser.add_argument('--replay-frequency', type=int, default=4,
                    metavar='k',
                    help='Frequency of sampling from memory')
parser.add_argument('--priority-exponent', type=float, default=0.5,
                    metavar='ω',
                    help='Prioritised experience replay exponent (originally denoted α)'
                    )
parser.add_argument('--priority-weight', type=float, default=0.4,
                    metavar='β',
                    help='Initial prioritised experience replay importance sampling weight'
                    )
parser.add_argument('--multi-step', type=int, default=3, metavar='n',
                    help='Number of steps for multi-step return')
parser.add_argument('--discount', type=float, default=0.99,
                    metavar='γ', help='Discount factor')
parser.add_argument('--target-update', type=int, default=int(8e3),
                    metavar='τ',
                    help='Number of steps after which to update target network'
                    )
parser.add_argument('--reward-clip', type=int, default=0,
                    metavar='VALUE',
                    help='Reward clipping (0 to disable)')
parser.add_argument('--learning-rate', type=float, default=0.0000625,
                    metavar='η', help='Learning rate')
parser.add_argument('--adam-eps', type=float, default=1.5e-4,
                    metavar='ε', help='Adam epsilon')
parser.add_argument('--batch-size', type=int, default=32,
                    metavar='SIZE', help='Batch size')
parser.add_argument('--learn-start', type=int, default=int(20e3),
                    metavar='STEPS',
                    help='Number of steps before starting training')
parser.add_argument('--evaluate', action='store_true',
                    help='Evaluate only')
parser.add_argument('--evaluation-interval', type=int, default=10000,
                    metavar='STEPS',
                    help='Number of training steps between evaluations')
parser.add_argument('--evaluation-episodes', type=int, default=10,
                    metavar='N',
                    help='Number of evaluation episodes to average over'
                    )
parser.add_argument('--hit-reward', type=float, default=0,
                    help='+reward if you hit the ball'
                    )
parser.add_argument('--crop-opponent', action='store_true',
                    help='True if you want the opponent paddle pixels to be black'
                    )
parser.add_argument('--evaluation-size', type=int, default=500,
                    metavar='N',
                    help='Number of transitions to use for validating Q'
                    )
parser.add_argument('--render', action='store_true',
                    help='Display screen (testing only)')
parser.add_argument('--enable-cudnn', action='store_true',
                    help='Enable cuDNN (faster but nondeterministic)')
parser.add_argument('--checkpoint-interval', default=10000,
                    help='How often to checkpoint the model, defaults to 0 (never checkpoint)'
                    )
parser.add_argument('--memory', default='results/Rainbow-5/memory', help='Path to save/load the memory from'
                    )
parser.add_argument('--disable-bzip-memory', action='store_true',
                    help='Don\'t zip the memory file. Not recommended (zipping is a bit slower and much, much smaller)'
                    )

# Setup

args = parser.parse_args()

print(' ' * 26 + 'Options')
for (k, v) in vars(args).items():
    print(' ' * 26 + k + ': ' + str(v))
results_dir = os.path.join('results', args.id)
if not os.path.exists(results_dir):
    os.makedirs(results_dir)
metrics = {
    'steps': [],
    'rewards': [],
    'Qs': [],
    'best_avg_reward': -float('inf'),
}
np.random.seed(args.seed)
torch.manual_seed(np.random.randint(1, 10000))
if torch.cuda.is_available() and not args.disable_cuda:
    args.device = torch.device('cuda')
    torch.cuda.manual_seed(np.random.randint(1, 10000))
    torch.backends.cudnn.enabled = args.enable_cudnn
else:
    args.device = torch.device('cpu')


def log(s):
    print('[' + str(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
          + '] ' + s)


def load_memory(memory_path, disable_bzip):
    if disable_bzip:
        with open(memory_path, 'rb') as pickle_file:
            return pickle.load(pickle_file)
    else:
        with bz2.open(memory_path, 'rb') as zipped_pickle_file:
            return pickle.load(zipped_pickle_file)


def save_memory(memory, memory_path, disable_bzip):
    if disable_bzip:
        with open(memory_path, 'wb') as pickle_file:
            pickle.dump(memory, pickle_file)
    else:
        with bz2.open(memory_path, 'wb') as zipped_pickle_file:
            pickle.dump(memory, zipped_pickle_file)

args.reward_clip = args.V_max + 5*args.hit_reward
args.V_max += 5*args.hit_reward

# Environment
env = gym.make(args.env)
action_space = env.action_space.n

# Agent
agent = Agent(args, env)

# If a model is provided, and evaluate is fale, presumably we want to resume, so try to load memory

if args.model is not None and not args.evaluate:
    if not args.memory:
        raise ValueError('Cannot resume training without memory save path. Aborting...')
    elif not os.path.exists(args.memory):
        raise ValueError(
            'Could not find memory file at {path}. Aborting...'.format(path=args.memory))

    mem = load_memory(args.memory, args.disable_bzip_memory)
else:

    mem = PrioritizedReplayMemory(args, args.memory_capacity)

priority_weight_increase = (1 - args.priority_weight) / (args.T_max - args.learn_start)

# Construct validation memory
val_mem = PrioritizedReplayMemory(args, args.evaluation_size)
T, done = 0, True
while T < args.evaluation_size:
    if done:
        state, done = env.reset(), False
    state = preprocess_frame(frame=state, device=args.device, crop_opponent=args.crop_opponent)
    next_state, _, done, _ = env.step(np.random.randint(0, action_space))
    val_mem.append(state.unsqueeze(0), None, None, done)
    state = next_state
    T += 1

if args.evaluate:
    agent.eval()  # Set DQN (online network) to evaluation mode
    avg_reward, avg_Q, winrate = test(args.env, args, 0, agent, val_mem, metrics, results_dir, evaluate=True)  # Test
    print('Winrate: ' + str(winrate) + ' | Avg. reward: ' + str(avg_reward) + ' | Avg. Q: ' + str(avg_Q))
else:
  # Training loop
    agent.train()
    T, done, prev_last_touch = 0, True, 0
    for T in trange(1, args.T_max + 1, total=args.T_max):
        if done:
            state, done, prev_last_touch = env.reset(), False, 0

        if T % args.replay_frequency == 0:
            agent.reset_noise()  # Draw a new set of noisy weights
  
        # Choose an action greedily (with noisy weights)
        action = agent.get_action(state)
        next_state, reward, done, _ = env.step(action)  # Step
        if env.ball.last_touch == 1 and prev_last_touch != 1:
            reward += args.hit_reward
        prev_last_touch = env.ball.last_touch
        if args.reward_clip > 0:
            reward = max(min(reward, args.reward_clip), -args.reward_clip)  # Clip rewards
        mem.append(agent.last_stacked_obs, action, reward, done)  # Append transition to memory

        # Train and test
        if T >= args.learn_start:
    		# Anneal importance sampling weight β to 1
            mem.priority_weight = min(mem.priority_weight + priority_weight_increase, 1)

            if T % args.replay_frequency == 0:
                # Train with n-step distributional double-Q learning
                agent.train_step(mem)

            if T % args.evaluation_interval == 0:
                agent.eval()  # Set DQN (online network) to evaluation mode
                avg_reward, avg_Q, winrate = test(args.env, args, T, agent, val_mem, metrics, results_dir)  # Test
                log('T = ' + str(T) + ' / ' + str(args.T_max)
                    + ' | Winrate: ' + str(winrate)
                    + ' | Avg. reward: ' + str(avg_reward)
                    + ' | Avg. Q: ' + str(avg_Q))
                agent.train()  # Set DQN (online network) back to training mode

                # If memory path provided, save it
                if args.memory is not None:
                    save_memory(mem, args.memory,
                                args.disable_bzip_memory)

            # Update target network
            if T % args.target_update == 0:
                agent.update_target_net()

            # Checkpoint the network
            if args.checkpoint_interval != 0 and T \
                    % args.checkpoint_interval == 0:
                agent.save(results_dir, 'checkpoint.pth')

        state = next_state

env.close()
