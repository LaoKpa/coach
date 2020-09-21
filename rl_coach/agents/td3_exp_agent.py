#
# Copyright (c) 2019 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import copy
from typing import Union
from collections import OrderedDict
from random import shuffle
import os

import numpy as np

from rl_coach.agents.td3_agent import TD3Agent, TD3CriticNetworkParameters, TD3ActorNetworkParameters, \
    TD3AlgorithmParameters, TD3AgentExplorationParameters
from rl_coach.architectures.embedder_parameters import InputEmbedderParameters
from rl_coach.base_parameters import NetworkParameters, AlgorithmParameters, \
    AgentParameters, EmbedderScheme, MiddlewareScheme
from rl_coach.core_types import ActionInfo, TrainingSteps, Transition, Batch
from rl_coach.memories.episodic.episodic_experience_replay import EpisodicExperienceReplayParameters
from rl_coach.architectures.middleware_parameters import FCMiddlewareParameters
from rl_coach.architectures.head_parameters import RNDHeadParameters
from rl_coach.utilities.shared_running_stats import NumpySharedRunningStats
from rl_coach.logger import screen
from rl_coach.exploration_policies.e_greedy import EGreedyParameters
from rl_coach.schedules import LinearSchedule


class RNDNetworkParameters(NetworkParameters):
    def __init__(self):
        super().__init__()
        self.input_embedders_parameters = {'observation': InputEmbedderParameters(activation_function='leaky_relu',
                                                                                  input_rescaling={'image': 1.0})}
        self.middleware_parameters = FCMiddlewareParameters(scheme=MiddlewareScheme.Empty)
        self.heads_parameters = [RNDHeadParameters()]
        self.create_target_network = False
        self.optimizer_type = 'Adam'
        self.batch_size = 100
        self.learning_rate = 0.0001
        self.should_get_softmax_probabilities = False


class TD3EXPAlgorithmParameters(TD3AlgorithmParameters):
    def __init__(self):
        super().__init__()
        self.rnd_sample_size = 2000
        self.rnd_batch_size = 500
        self.rnd_optimization_epochs = 4


class TD3ExplorationAgentParameters(AgentParameters):
    def __init__(self):
        td3_exp_algorithm_params = TD3EXPAlgorithmParameters()
        super().__init__(algorithm=td3_exp_algorithm_params,
                         exploration=TD3AgentExplorationParameters(),
                         memory=EpisodicExperienceReplayParameters(),
                         networks=OrderedDict([("actor", TD3ActorNetworkParameters()),
                                               ("critic",
                                                TD3CriticNetworkParameters(td3_exp_algorithm_params.num_q_networks)),
                                               ("predictor", RNDNetworkParameters()),
                                               ("constant", RNDNetworkParameters())]))

    @property
    def path(self):
        return 'rl_coach.agents.td3_exp_agent:TD3ExplorationAgent'


class TD3ExplorationAgent(TD3Agent):
    def __init__(self, agent_parameters, parent: Union['LevelManager', 'CompositeAgent']=None):
        super().__init__(agent_parameters, parent)
        self.rnd_stats = NumpySharedRunningStats(name='RND_normalization', epsilon=1e-8)
        self.rnd_stats.set_params()
        self.rnd_obs_stats = NumpySharedRunningStats(name='RND_observation_normalization', epsilon=1e-8)
        self.intrinsic_returns_estimate = None

    def update_intrinsic_returns_estimate(self, rewards):
        returns = np.zeros_like(rewards)
        for i, r in enumerate(rewards):
            if self.intrinsic_returns_estimate is None:
                self.intrinsic_returns_estimate = r
            else:
                self.intrinsic_returns_estimate = \
                    self.intrinsic_returns_estimate * self.ap.algorithm.discount + r
            returns[i] = self.intrinsic_returns_estimate
        return returns

    def prepare_rnd_inputs(self, batch):
        return {'camera': self.rnd_obs_stats.normalize(batch.next_states(['camera'])['camera'])}

    def handle_self_supervised_reward(self, batch):
        """
        Allows agents to update the batch for self supervised learning

        :param batch: original training batch
        :return: updated traing batch
        """
        return batch

    def update_transition_before_adding_to_replay_buffer(self, transition: Transition) -> Transition:
        """
        Allows agents to update the transition just before adding it to the replay buffer.
        Can be useful for agents that want to tweak the reward, termination signal, etc.

        :param transition: the transition to update
        :return: the updated transition
        """
        transition = super().update_transition_before_adding_to_replay_buffer(transition)
        image = np.array(transition.state['camera'])
        if self.rnd_obs_stats.n < 1:
            self.rnd_obs_stats.set_params(shape=image.shape, clip_values=[-5, 5])
        self.rnd_obs_stats.push_val(np.expand_dims(image, 0))
        return transition

    def train_rnd(self):
        if self.memory.num_transitions() == 0:
            return

        transitions = self.memory.transitions[-self.ap.algorithm.rnd_sample_size:]
        dataset = Batch(transitions)
        dataset_order = list(range(dataset.size))
        batch_size = self.ap.algorithm.rnd_batch_size
        for epoch in range(self.ap.algorithm.rnd_optimization_epochs):
            shuffle(dataset_order)
            total_loss = 0
            total_grads = 0
            for i in range(int(dataset.size / batch_size)):
                start = i * batch_size
                end = (i + 1) * batch_size

                batch = Batch(list(np.array(dataset.transitions)[dataset_order[start:end]]))
                inputs = self.prepare_rnd_inputs(batch)

                const_embedding = self.networks['constant'].online_network.predict(inputs)

                res = self.networks['predictor'].train_and_sync_networks(inputs, [const_embedding])

                total_loss += res[0]
                total_grads += res[2]

            screen.log_dict(
                OrderedDict([
                    ("training epoch", epoch),
                    ("dataset size", dataset.size),
                    ("mean loss", total_loss / dataset.size),
                    ("mean gradients", total_grads / dataset.size)
                ]),
                prefix="RND Training"
            )

    def learn_from_batch(self, batch):
        batch = self.handle_self_supervised_reward(batch)
        return super().learn_from_batch(batch)

    def train(self):
        if (self.total_steps_counter + 1) % 10000 == 0:
            dir_path = '../../datasets'
            if not os.path.exists(dir_path):
                os.mkdir(dir_path)
            replay_buffer_path = os.path.join(dir_path, 'RB_{}.p'.format(type(self).__name__))
            self.memory.save(replay_buffer_path)
        if self.total_steps_counter % self.ap.algorithm.rnd_sample_size == 0:
            self.train_rnd()
        return super().train()


class TD3IntrinsicRewardAgentParameters(TD3ExplorationAgentParameters):
    @property
    def path(self):
        return 'rl_coach.agents.td3_exp_agent:TD3IntrinsicRewardAgent'


class TD3IntrinsicRewardAgent(TD3ExplorationAgent):
    def __init__(self, agent_parameters, parent: Union['LevelManager', 'CompositeAgent']=None):
        super().__init__(agent_parameters, parent)

    def handle_self_supervised_reward(self, batch):
        inputs = self.prepare_rnd_inputs(batch)
        embedding = self.networks['constant'].online_network.predict(inputs)
        prediction = self.networks['predictor'].online_network.predict(inputs)
        prediction_error = np.mean((embedding - prediction) ** 2, axis=1)
        self.rnd_stats.push_val(np.expand_dims(self.update_intrinsic_returns_estimate(prediction_error), -1))
        intrinsic_rewards = prediction_error

        for i, t in enumerate(batch.transitions):
            t.reward = intrinsic_rewards[i] / self.rnd_stats.std[0]

        return batch


class TD3RandomAgentParameters(TD3ExplorationAgentParameters):
    def __init__(self):
        super().__init__()
        self.exploration = EGreedyParameters()
        self.exploration.epsilon_schedule = LinearSchedule(1.0, 1.0, 50000)

    @property
    def path(self):
        return 'rl_coach.agents.td3_exp_agent:TD3RandomAgent'


class TD3RandomAgent(TD3ExplorationAgent):
    def __init__(self, agent_parameters, parent: Union['LevelManager', 'CompositeAgent']=None):
        super().__init__(agent_parameters, parent)

    def train(self):
        if (self.total_steps_counter + 1) % 10000 == 0:
            dir_path = '../../datasets'
            if not os.path.exists(dir_path):
                os.mkdir(dir_path)
            replay_buffer_path = os.path.join(dir_path, 'RB_{}.p'.format(type(self).__name__))
            self.memory.save(replay_buffer_path)
        return 0


class TD3GoalBasedAgentParameters(TD3ExplorationAgentParameters):
    @property
    def path(self):
        return 'rl_coach.agents.td3_exp_agent:TD3GoalBasedAgent'

import matplotlib.pyplot as plt

class TD3GoalBasedAgent(TD3ExplorationAgent):
    def __init__(self, agent_parameters, parent: Union['LevelManager', 'CompositeAgent']=None):
        super().__init__(agent_parameters, parent)
        self.goal = None

    @staticmethod
    def concat_goal(state, goal_state):
        ret = np.concatenate([state['camera'], goal_state['camera']], axis=2)
        return ret

    def handle_self_supervised_reward(self, batch):
        batch_size = self.ap.network_wrappers['actor'].batch_size
        episode_indices = np.random.randint(self.memory.num_complete_episodes(), size=batch_size)
        transitions = []
        for e_idx in episode_indices:
            episode = self.memory.get_all_complete_episodes()[e_idx]
            transition_idx = np.random.randint(episode.length())
            t = copy.copy(episode[transition_idx])
            if np.random.rand(1) < 0.02:
                t.state['obs-goal'] = self.concat_goal(t.state, t.state)
                t.next_state['obs-goal'] = self.concat_goal(t.next_state, t.state)
                t.game_over = True
                t.reward = 0
            else:
                if transition_idx == episode.length() - 1:
                    goal_state = t.next_state['camera']
                    t.state['obs-goal'] = self.concat_goal(t.state, t.next_state)
                    t.next_state['obs-goal'] = self.concat_goal(t.next_state, t.next_state)
                else:
                    goal_idx = np.random.randint(transition_idx, episode.length())
                    goal_state = episode.transitions[goal_idx].next_state['camera']
                    t.state['obs-goal'] = self.concat_goal(t.state, episode.transitions[goal_idx].next_state)
                    t.next_state['obs-goal'] = self.concat_goal(t.next_state,
                                                                        episode.transitions[goal_idx].next_state)
                state = t.next_state['camera']
                t.game_over = np.alltrue(np.equal(state, goal_state))
                t.reward = -1

            transitions.append(t)

        return Batch(transitions)

    def choose_action(self, curr_state):
        if self.goal:
            curr_state['obs-goal'] = self.concat_goal(curr_state, self.goal.next_state)
        else:
            curr_state['obs-goal'] = self.concat_goal(curr_state, curr_state)

        return super().choose_action(curr_state)

    def generate_goal(self):
        if self.memory.num_transitions() == 0:
            return

        transitions = list(np.random.choice(self.memory.transitions,
                                            min(self.ap.algorithm.rnd_sample_size,
                                                self.memory.num_transitions()),
                                            replace=False))
        dataset = Batch(transitions)
        batch_size = self.ap.algorithm.rnd_batch_size
        self.goal = dataset[0]

        max_pred_err = 0
        for i in range(int(dataset.size / batch_size)):
            start = i * batch_size
            end = (i + 1) * batch_size

            inputs = self.prepare_rnd_inputs(Batch(dataset[start:end]))

            embedding = self.networks['constant'].online_network.predict(inputs)
            prediction = self.networks['predictor'].online_network.predict(inputs)
            prediction_error = np.sum((embedding - prediction) ** 2, axis=1)

            curr_max = np.max(prediction_error)
            if curr_max > max_pred_err:
                max_pred_err = curr_max
                idx = start + np.argmax(prediction_error)
                self.goal = dataset[idx]

    def handle_episode_ended(self) -> None:
        super().handle_episode_ended()
        self.generate_goal()

