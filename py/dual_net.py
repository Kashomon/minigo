# Copyright 2018 Google LLC
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

"""
The policy and value networks share a majority of their architecture.
This helps the intermediate layers extract concepts that are relevant to both
move prediction and score estimation.
"""

import functools
import math
import os.path
import itertools
import sys
import tensorflow as tf
from tqdm import tqdm
from typing import Dict

import features
import preprocessing
import symmetries
import go

EXAMPLES_PER_GENERATION = 2000000
TRAIN_BATCH_SIZE = 16


class DualNetworkTrainer():
    def __init__(self, save_file=None, **hparams):
        self.hparams = get_default_hyperparams(**hparams)
        self.save_file = save_file
        self.sess = tf.Session(graph=tf.Graph())
        # TODO: eval stuff
        # self.eval_logdir = os.path.join(model_dir, 'logs', 'eval')

    def initialize_weights(self, init_from=None):
        '''Initialize weights from model checkpoint.

        If model checkpoint does not exist, fall back to init_from.
        If that doesn't exist either, bootstrap with random weights.

        This priority order prevents the mistake where the latest saved
        model exists, but you accidentally init from an older model checkpoint
        and then overwrite the newer model weights.
        '''
        if self.save_file is not None and os.path.exists(self.save_file + '.meta'):
            tf.train.Saver().restore(self.sess, self.save_file)
            return
        if init_from is not None:
            print("Initializing from {}".format(init_from), file=sys.stderr)
            tf.train.Saver().restore(self.sess, init_from)
        else:
            print("Bootstrapping with random weights", file=sys.stderr)
            self.sess.run(tf.global_variables_initializer())

    def save_weights(self):
        with self.sess.graph.as_default():
            tf.train.Saver().save(self.sess, self.save_file)

    def bootstrap(self):
        'Create a save file with random initial weights.'
        sess = tf.Session(graph=tf.Graph())
        with sess.graph.as_default():
            input_tensors = get_inference_input()
            output_tensors = dual_net(input_tensors, TRAIN_BATCH_SIZE,
                                      train_mode=True, **self.hparams)
            train_tensors = train_ops(
                input_tensors, output_tensors, **self.hparams)
            sess.run(tf.global_variables_initializer())
            tf.train.Saver().save(sess, self.save_file)

    def train(self, tf_records, init_from=None, logdir=None, num_steps=None):
        if num_steps is None:
            num_steps = EXAMPLES_PER_GENERATION // TRAIN_BATCH_SIZE
        with self.sess.graph.as_default():
            input_tensors = preprocessing.get_input_tensors(
                TRAIN_BATCH_SIZE, tf_records)
            output_tensors = dual_net(input_tensors, TRAIN_BATCH_SIZE,
                                      train_mode=True, **self.hparams)
            train_tensors = train_ops(
                input_tensors, output_tensors, **self.hparams)
            weight_tensors = logging_ops()
            self.initialize_weights(init_from)
            if logdir is not None:
                training_stats = StatisticsCollector()
                logger = tf.summary.FileWriter(logdir, self.sess.graph)
            for i in tqdm(range(num_steps)):
                try:
                    tensor_values = self.sess.run(train_tensors)
                except tf.errors.OutOfRangeError:
                    break
                if logdir is not None:
                    training_stats.report(
                        tensor_values['policy_cost'],
                        tensor_values['value_cost'],
                        tensor_values['l2_cost'],
                        tensor_values['combined_cost'])
                    if i % 100 == 0 and logdir is not None:
                        accuracy_summaries = training_stats.collect()
                        weight_summaries = self.sess.run(weight_tensors)
                        global_step = tensor_values['global_step']
                        logger.add_summary(accuracy_summaries, global_step)
                        logger.add_summary(weight_summaries, global_step)
            self.save_weights()


class DualNetwork():
    def __init__(self, save_file, **hparams):
        self.save_file = save_file
        self.hparams = get_default_hyperparams(**hparams)
        self.inference_input = None
        self.inference_output = None
        self.sess = tf.Session(graph=tf.Graph())
        self.initialize_graph()

    def initialize_graph(self):
        with self.sess.graph.as_default():
            input_tensors = get_inference_input()
            output_tensors = dual_net(input_tensors, batch_size=-1,
                                      train_mode=False, **self.hparams)
            self.inference_input = input_tensors
            self.inference_output = output_tensors
            if self.save_file is not None:
                self.initialize_weights(self.save_file)
            else:
                self.sess.run(tf.global_variables_initializer())

    def initialize_weights(self, save_file):
        """Initialize the weights from the given save_file.
        Assumes that the graph has been constructed, and the
        save_file contains weights that match the graph. Used 
        to set the weights to a different version of the player
        without redifining the entire graph."""
        tf.train.Saver().restore(self.sess, save_file)

    def run(self, position, use_random_symmetry=True):
        probs, values = self.run_many([position],
                                      use_random_symmetry=use_random_symmetry)
        return probs[0], values[0]

    def run_many(self, positions, use_random_symmetry=True):
        processed = list(map(features.extract_features, positions))
        if use_random_symmetry:
            syms_used, processed = symmetries.randomize_symmetries_feat(
                processed)
        outputs = self.sess.run(self.inference_output,
                                feed_dict={self.inference_input['pos_tensor']: processed})
        probabilities, value = outputs['policy_output'], outputs['value_output']
        if use_random_symmetry:
            probabilities = symmetries.invert_symmetries_pi(
                syms_used, probabilities)
        return probabilities, value


def get_inference_input():
    return {
        'pos_tensor': tf.placeholder(tf.float32,
                                     [None, go.N, go.N, features.NEW_FEATURES_PLANES]),
        'pi_tensor': tf.placeholder(tf.float32,
                                    [None, go.N * go.N + 1]),
        'value_tensor': tf.placeholder(tf.float32, [None]),
    }


def _round_power_of_two(n):
    """Finds the nearest power of 2 to a number.

    Thus 84 -> 64, 120 -> 128, etc.
    """
    return 2 ** int(round(math.log(n, 2)))


def get_default_hyperparams(**overrides):
    """Returns the hyperparams for the neural net.

    In other words, returns a dict whose parameters come from the AGZ
    paper:
      k: number of filters (AlphaGoZero used 256). We use 128 by
        default for a 19x19 go board.
      fc_width: Dimensionality of the fully connected linear layer
      num_shared_layers: number of shared residual blocks.  AGZ used both 19
        and 39. Here we use 19 because it's faster to train.
      l2_strength: The L2 regularization parameter.
      momentum: The momentum parameter for training
    """
    k = _round_power_of_two(go.N ** 2 / 3)  # width of each layer
    hparams = {
        'k': k,  # Width of each conv layer
        'fc_width': 2 * k,  # Width of each fully connected layer
        'num_shared_layers': go.N,  # Number of shared trunk layers
        'l2_strength': 2e-4,  # Regularization strength
        'momentum': 0.9,  # Momentum used in SGD
    }
    hparams.update(**overrides)
    return hparams


def dual_net(input_tensors, batch_size, train_mode, **hparams):
    '''
    Given dict of batched tensors
        pos_tensor: [BATCH_SIZE, go.N, go.N, features.NEW_FEATURES_PLANES]
        pi_tensor: [BATCH_SIZE, go.N * go.N + 1]
        value_tensor: [BATCH_SIZE]
    return dict of tensors
        logits: [BATCH_SIZE, go.N * go.N + 1]
        policy: [BATCH_SIZE, go.N * go.N + 1]
        value: [BATCH_SIZE]
    '''
    my_batchn = functools.partial(tf.layers.batch_normalization,
                                  momentum=.997, epsilon=1e-5,
                                  fused=True, center=True, scale=True,
                                  training=train_mode)

    my_conv2d = functools.partial(tf.layers.conv2d,
                                  filters=hparams['k'], kernel_size=[3, 3], padding="same")

    def my_res_layer(inputs, train_mode):
        int_layer1 = my_batchn(my_conv2d(inputs))
        initial_output = tf.nn.relu(int_layer1)
        int_layer2 = my_batchn(my_conv2d(initial_output))
        output = tf.nn.relu(inputs + int_layer2)
        return output

    initial_output = tf.nn.relu(my_batchn(
        my_conv2d(input_tensors['pos_tensor'])))

    # the shared stack
    shared_output = initial_output
    for i in range(hparams['num_shared_layers']):
        shared_output = my_res_layer(shared_output, train_mode)

    # policy head
    policy_conv = tf.nn.relu(my_batchn(
        my_conv2d(shared_output, filters=2, kernel_size=[1, 1]),
        center=False, scale=False))
    logits = tf.layers.dense(
        tf.reshape(policy_conv, [batch_size, go.N * go.N * 2]),
        go.N * go.N + 1)

    policy_output = tf.nn.softmax(logits)

    # value head
    value_conv = tf.nn.relu(my_batchn(
        my_conv2d(shared_output, filters=1, kernel_size=[1, 1]),
        center=False, scale=False))
    value_fc_hidden = tf.nn.relu(tf.layers.dense(
        tf.reshape(value_conv, [batch_size, go.N * go.N]),
        hparams['fc_width']))
    value_output = value_output = tf.nn.tanh(
        tf.reshape(tf.layers.dense(value_fc_hidden, 1), [batch_size]))
    return {
        'logits': logits,
        'policy_output': policy_output,
        'value_output': value_output,
    }


def train_ops(input_tensors, output_tensors, **hparams):
    global_step = tf.Variable(0, name="global_step", trainable=False)
    policy_cost = tf.reduce_mean(
        tf.nn.softmax_cross_entropy_with_logits(
            logits=output_tensors['logits'],
            labels=input_tensors['pi_tensor']))
    value_cost = tf.reduce_mean(tf.square(
        output_tensors['value_output'] - input_tensors['value_tensor']))
    l2_cost = 1e-4 * tf.add_n([tf.nn.l2_loss(v)
                               for v in tf.trainable_variables() if not 'bias' in v.name])
    combined_cost = policy_cost + value_cost + l2_cost
    learning_rate = tf.train.exponential_decay(1e-2, global_step, 10 ** 7, 0.1)
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        train_op = tf.train.MomentumOptimizer(
            learning_rate, hparams['momentum']).minimize(
                combined_cost, global_step=global_step)

    return {
        'policy_cost': policy_cost,
        'value_cost': value_cost,
        'l2_cost': l2_cost,
        'combined_cost': combined_cost,
        'global_step': global_step,
        'train_op': train_op,
    }


def logging_ops():
    return tf.summary.merge([
        tf.summary.histogram(weight_var.name, weight_var)
        for weight_var in tf.trainable_variables()],
        name="weight_summaries")


class StatisticsCollector(object):
    """Collect statistics on the runs and create graphs.

    Accuracy and cost cannot be calculated with the full test dataset
    in one pass, so they must be computed in batches. Unfortunately,
    the built-in TF summary nodes cannot be told to aggregate multiple
    executions. Therefore, we aggregate the accuracy/cost ourselves at
    the python level, and then shove it through the accuracy/cost summary
    nodes to generate the appropriate summary protobufs for writing.
    """
    graph = tf.Graph()
    with tf.device("/cpu:0"), graph.as_default():
        policy_error = tf.placeholder(tf.float32, [])
        value_error = tf.placeholder(tf.float32, [])
        reg_error = tf.placeholder(tf.float32, [])
        cost = tf.placeholder(tf.float32, [])
        policy_summary = tf.summary.scalar("Policy error", policy_error)
        value_summary = tf.summary.scalar("Value error", value_error)
        reg_summary = tf.summary.scalar("Regularization error", reg_error)
        cost_summary = tf.summary.scalar("Combined cost", cost)
        accuracy_summaries = tf.summary.merge(
            [policy_summary, value_summary, reg_summary, cost_summary],
            name="accuracy_summaries")
    session = tf.Session(graph=graph)

    def __init__(self):
        self.policy_costs = []
        self.value_costs = []
        self.regularization_costs = []
        self.combined_costs = []

    def report(self, policy_cost, value_cost, regularization_cost, combined_cost):
        # TODO refactor this to take a dict, and do something like
        # self.accums = defaultdict(list) and do self.accums[thing].append(value)
        # so that it can handle an arbitrary number of values.
        self.policy_costs.append(policy_cost)
        self.value_costs.append(value_cost)
        self.regularization_costs.append(regularization_cost)
        self.combined_costs.append(combined_cost)

    def collect(self):
        avg_pol = sum(self.policy_costs) / len(self.policy_costs)
        avg_val = sum(self.value_costs) / len(self.value_costs)
        avg_reg = sum(self.regularization_costs) / \
            len(self.regularization_costs)
        avg_cost = sum(self.combined_costs) / len(self.combined_costs)
        self.policy_costs = []
        self.value_costs = []
        self.regularization_costs = []
        self.combined_costs = []
        summary = self.session.run(self.accuracy_summaries,
                                   feed_dict={self.policy_error: avg_pol,
                                              self.value_error: avg_val,
                                              self.reg_error: avg_reg,
                                              self.cost: avg_cost})
        return summary
