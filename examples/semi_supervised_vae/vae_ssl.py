#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division
import os
import time

import tensorflow as tf
from tensorflow.contrib import layers
from six.moves import range
import numpy as np
import zhusuan as zs

from examples import conf
from examples.utils import dataset


@zs.reuse('model')
def M2(observed, n, n_x, n_y, n_z, n_particles):
    with zs.BayesianNet(observed=observed) as model:
        z_mean = tf.zeros([n, n_z])
        z_logstd = tf.zeros([n, n_z])
        z = zs.Normal('z', z_mean, z_logstd, n_samples=n_particles,
                      group_event_ndims=1)
        y_logits = tf.zeros([n, n_y])
        y = zs.OnehotCategorical('y', y_logits, n_samples=n_particles)
        lx_zy = layers.fully_connected(tf.concat([z, tf.to_float(y)], 2), 500)
        lx_zy = layers.fully_connected(lx_zy, 500)
        x_logits = layers.fully_connected(lx_zy, n_x, activation_fn=None)
        x = zs.Bernoulli('x', x_logits, group_event_ndims=1)
    return model


@zs.reuse('variational')
def qz_xy(x, y, n_z, n_particles):
    with zs.BayesianNet() as variational:
        lz_xy = layers.fully_connected(tf.to_float(tf.concat([x, y], 1)), 500)
        lz_xy = layers.fully_connected(lz_xy, 500)
        lz_mean = layers.fully_connected(lz_xy, n_z, activation_fn=None)
        lz_logstd = layers.fully_connected(lz_xy, n_z, activation_fn=None)
        z = zs.Normal('z', lz_mean, lz_logstd, n_samples=n_particles,
                      group_event_ndims=1)
    return variational


@zs.reuse('classifier')
def qy_x(x, n_y):
    ly_x = layers.fully_connected(tf.to_float(x), 500)
    ly_x = layers.fully_connected(ly_x, 500)
    ly_x = layers.fully_connected(ly_x, n_y, activation_fn=None)
    return ly_x


if __name__ == "__main__":
    tf.set_random_seed(1234)

    # Load MNIST
    data_path = os.path.join(conf.data_dir, 'mnist.pkl.gz')
    np.random.seed(1234)
    x_labeled, t_labeled, x_unlabeled, x_test, t_test = \
        dataset.load_mnist_semi_supervised(data_path, one_hot=True)
    x_test = np.random.binomial(1, x_test, size=x_test.shape).astype('float32')
    n_labeled, n_x = x_labeled.shape
    n_y = 10

    # Define model parameters
    n_z = 100

    # Define training/evaluation parameters
    lb_samples = 10
    beta = 1200.
    epoches = 3000
    batch_size = 100
    test_batch_size = 100
    iters = x_unlabeled.shape[0] // batch_size
    test_iters = x_test.shape[0] // test_batch_size
    test_freq = 10
    learning_rate = 0.0003
    anneal_lr_freq = 200
    anneal_lr_rate = 0.75

    # Build the computation graph
    n_particles = tf.placeholder(tf.int32, shape=[], name='n_particles')
    x_orig = tf.placeholder(tf.float32, shape=[None, n_x], name='x')
    x_bin = tf.cast(tf.less(tf.random_uniform(tf.shape(x_orig), 0, 1), x_orig),
                    tf.int32)

    def log_joint(observed):
        n = tf.shape(observed['x'])[1]
        model = M2(observed, n, n_x, n_y, n_z, n_particles)
        log_px_zy, log_pz, log_py = model.local_log_prob(['x', 'z', 'y'])
        return log_px_zy + log_pz + log_py

    # Labeled
    x_labeled_ph = tf.placeholder(tf.int32, shape=[None, n_x], name='x_l')
    x_labeled_obs = tf.tile(tf.expand_dims(x_labeled_ph, 0),
                            [n_particles, 1, 1])
    y_labeled_ph = tf.placeholder(tf.int32, shape=[None, n_y], name='y_l')
    y_labeled_obs = tf.tile(tf.expand_dims(y_labeled_ph, 0),
                            [n_particles, 1, 1])
    variational = qz_xy(x_labeled_ph, y_labeled_ph, n_z, n_particles)
    qz_samples, log_qz = variational.query('z', outputs=True,
                                           local_log_prob=True)
    labeled_lower_bound = tf.reduce_mean(
        zs.sgvb(log_joint, {'x': x_labeled_obs, 'y': y_labeled_obs},
                {'z': [qz_samples, log_qz]}, axis=0))

    # Unlabeled
    x_unlabeled_ph = tf.placeholder(tf.int32, shape=[None, n_x], name='x_u')
    n = tf.shape(x_unlabeled_ph)[0]
    y_diag = tf.diag(tf.ones(n_y, dtype=tf.int32))
    y_u = tf.reshape(tf.tile(tf.expand_dims(y_diag, 0), [n, 1, 1]), [-1, n_y])
    x_u = tf.reshape(tf.tile(tf.expand_dims(x_unlabeled_ph, 1), [1, n_y, 1]),
                     [-1, n_x])
    x_unlabeled_obs = tf.tile(tf.expand_dims(x_u, 0), [n_particles, 1, 1])
    y_unlabeled_obs = tf.tile(tf.expand_dims(y_u, 0), [n_particles, 1, 1])
    variational = qz_xy(x_u, y_u, n_z, n_particles)
    qz_samples, log_qz = variational.query('z', outputs=True,
                                           local_log_prob=True)
    lb_z = zs.sgvb(log_joint, {'x': x_unlabeled_obs, 'y': y_unlabeled_obs},
                   {'z': [qz_samples, log_qz]}, axis=0)
    # sum over y
    lb_z = tf.reshape(lb_z, [-1, n_y])
    qy_logits_u = qy_x(x_unlabeled_ph, n_y)
    qy_u = tf.nn.softmax(qy_logits_u)
    qy_u += 1e-8
    qy_u /= tf.reduce_sum(qy_u, 1, keep_dims=True)
    log_qy_u = tf.log(qy_u)
    unlabeled_lower_bound = tf.reduce_mean(
        tf.reduce_sum(qy_u * (lb_z - log_qy_u), 1))

    # Build classifier
    qy_logits_l = qy_x(x_labeled_ph, n_y)
    qy_l = tf.nn.softmax(qy_logits_l)
    pred_y = tf.argmax(qy_l, 1)
    acc = tf.reduce_sum(
        tf.cast(tf.equal(pred_y, tf.argmax(y_labeled_ph, 1)), tf.float32) /
        tf.cast(tf.shape(x_labeled_ph)[0], tf.float32))
    onehot_cat = zs.distributions.OnehotCategorical(qy_logits_l)
    log_qy_x = onehot_cat.log_prob(y_labeled_ph)
    classifier_cost = -beta * tf.reduce_mean(log_qy_x)

    # Gather gradients
    cost = -(labeled_lower_bound + unlabeled_lower_bound -
             classifier_cost) / 2.
    learning_rate_ph = tf.placeholder(tf.float32, shape=[], name='lr')
    optimizer = tf.train.AdamOptimizer(learning_rate_ph)
    grads = optimizer.compute_gradients(cost)
    infer = optimizer.apply_gradients(grads)

    params = tf.trainable_variables()
    for i in params:
        print(i.name, i.get_shape())

    # Run the inference
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        for epoch in range(1, epoches + 1):
            time_epoch = -time.time()
            if epoch % anneal_lr_freq == 0:
                learning_rate *= anneal_lr_rate
            np.random.shuffle(x_unlabeled)
            lbs_labeled = []
            lbs_unlabeled = []
            train_accs = []
            for t in range(iters):
                labeled_indices = np.random.randint(0, n_labeled,
                                                    size=batch_size)
                x_labeled_batch = x_labeled[labeled_indices]
                y_labeled_batch = t_labeled[labeled_indices]
                x_unlabeled_batch = x_unlabeled[t * batch_size:
                                                (t + 1) * batch_size]
                x_labeled_batch_bin = sess.run(
                    x_bin, feed_dict={x_orig: x_labeled_batch})
                x_unlabeled_batch_bin = sess.run(
                    x_bin, feed_dict={x_orig: x_unlabeled_batch})
                _, lb_labeled, lb_unlabeled, train_acc = sess.run(
                    [infer, labeled_lower_bound, unlabeled_lower_bound, acc],
                    feed_dict={x_labeled_ph: x_labeled_batch_bin,
                               y_labeled_ph: y_labeled_batch,
                               x_unlabeled_ph: x_unlabeled_batch_bin,
                               learning_rate_ph: learning_rate,
                               n_particles: lb_samples})
                lbs_labeled.append(lb_labeled)
                lbs_unlabeled.append(lb_unlabeled)
                train_accs.append(train_acc)
            time_epoch += time.time()
            print('Epoch {} ({:.1f}s), Lower bound: labeled = {}, '
                  'unlabeled = {} Accuracy: {:.2f}%'.
                  format(epoch, time_epoch, np.mean(lbs_labeled),
                         np.mean(lbs_unlabeled), np.mean(train_accs) * 100.))
            if epoch % test_freq == 0:
                time_test = -time.time()
                test_lls_labeled = []
                test_lls_unlabeled = []
                test_accs = []
                for t in range(test_iters):
                    test_x_batch = x_test[
                        t * test_batch_size: (t + 1) * test_batch_size]
                    test_y_batch = t_test[
                        t * test_batch_size: (t + 1) * test_batch_size]
                    test_ll_labeled, test_ll_unlabeled, test_acc = sess.run(
                        [labeled_lower_bound, unlabeled_lower_bound, acc],
                        feed_dict={x_labeled_ph: test_x_batch,
                                   y_labeled_ph: test_y_batch,
                                   x_unlabeled_ph: test_x_batch,
                                   n_particles: lb_samples})
                    test_lls_labeled.append(test_ll_labeled)
                    test_lls_unlabeled.append(test_ll_unlabeled)
                    test_accs.append(test_acc)
                time_test += time.time()
                print('>>> TEST ({:.1f}s)'.format(time_test))
                print('>> Test lower bound: labeled = {}, unlabeled = {}'.
                      format(np.mean(test_lls_labeled),
                             np.mean(test_lls_unlabeled)))
                print('>> Test accuracy: {:.2f}%'.format(
                    100. * np.mean(test_accs)))
