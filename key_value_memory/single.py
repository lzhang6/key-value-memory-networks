"""Example running MemN2N on a single bAbI task.
Download tasks from facebook.ai/babi """
from __future__ import absolute_import
from __future__ import print_function

from data_utils import load_task, vectorize_data
from sklearn import cross_validation, metrics
from memn2n_kv import MemN2N_KV
from itertools import chain
from six.moves import range

import tensorflow as tf
import numpy as np
from memn2n_kv import zero_nil_slot, add_gradient_noise

tf.flags.DEFINE_float("epsilon", 1, "Epsilon value for Adam Optimizer.")
tf.flags.DEFINE_float("max_grad_norm", 40.0, "Clip gradients to this norm.")
tf.flags.DEFINE_integer("evaluation_interval", 50, "Evaluate and print results every x epochs")
tf.flags.DEFINE_integer("batch_size", 32, "Batch size for training.")
tf.flags.DEFINE_integer("feature_size", 30, "Feature size")
tf.flags.DEFINE_integer("hops", 3, "Number of hops in the Memory Network.")
tf.flags.DEFINE_integer("epochs", 200, "Number of epochs to train for.")
tf.flags.DEFINE_integer("embedding_size", 20, "Embedding size for embedding matrices.")
tf.flags.DEFINE_integer("memory_size", 30, "Maximum size of memory.")
tf.flags.DEFINE_integer("task_id", 1, "bAbI task id, 1 <= id <= 20")
tf.flags.DEFINE_integer("random_state", None, "Random state.")
tf.flags.DEFINE_string("data_dir", "data/tasks_1-20_v1-2/en/", "Directory containing bAbI tasks")
tf.flags.DEFINE_boolean("allow_soft_placement", True, "Allow device soft device placement")
tf.flags.DEFINE_boolean("log_device_placement", False, "Log placement of ops on devices")

FLAGS = tf.flags.FLAGS

print("Started Task:", FLAGS.task_id)

# task data
train, test = load_task(FLAGS.data_dir, FLAGS.task_id)
data = train + test

vocab = sorted(reduce(lambda x, y: x | y, (set(list(chain.from_iterable(s)) + q + a) for s, q, a in data)))
word_idx = dict((c, i + 1) for i, c in enumerate(vocab))

max_story_size = max(map(len, (s for s, _, _ in data)))
mean_story_size = int(np.mean(map(len, (s for s, _, _ in data))))
sentence_size = max(map(len, chain.from_iterable(s for s, _, _ in data)))
query_size = max(map(len, (q for _, q, _ in data)))
memory_size = min(FLAGS.memory_size, max_story_size)
vocab_size = len(word_idx) + 1 # +1 for nil word
sentence_size = max(query_size, sentence_size) # for the position

print("Longest sentence length", sentence_size)
print("Longest story length", max_story_size)
print("Average story length", mean_story_size)

# train/validation/test sets
S, Q, A = vectorize_data(train, word_idx, sentence_size, memory_size)
trainS, valS, trainQ, valQ, trainA, valA = cross_validation.train_test_split(S, Q, A, test_size=.1, random_state=FLAGS.random_state)
testS, testQ, testA = vectorize_data(test, word_idx, sentence_size, memory_size)

print("Training set shape", trainS.shape)

# params
n_train = trainS.shape[0]
n_test = testS.shape[0]
n_val = valS.shape[0]

print("Training Size", n_train)
print("Validation Size", n_val)
print("Testing Size", n_test)

train_labels = np.argmax(trainA, axis=1)
test_labels = np.argmax(testA, axis=1)
val_labels = np.argmax(valA, axis=1)

batch_size = FLAGS.batch_size
batches = zip(range(0, n_train-batch_size, batch_size), range(batch_size, n_train, batch_size))

with tf.Graph().as_default():
    session_conf = tf.ConfigProto(
        allow_soft_placement=FLAGS.allow_soft_placement,
        log_device_placement=FLAGS.log_device_placement)
    tf.set_random_seed(FLAGS.random_state)

    global_step = tf.Variable(0, name="global_step", trainable=False)
    # decay learning rate
    starter_learning_rate = 0.01
    learning_rate = tf.train.exponential_decay(starter_learning_rate, global_step, 2000, 0.96, staircase=True)

    optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate, epsilon=FLAGS.epsilon)

    with tf.Session() as sess:
        
        model = MemN2N_KV(batch_size=batch_size, vocab_size=vocab_size,
                          note_size=sentence_size, doc_size=sentence_size, memory_key_size=memory_size,
                          feature_size=FLAGS.feature_size, memory_value_size=memory_size, embedding_size=FLAGS.embedding_size, hops=FLAGS.hops)
        grads_and_vars = optimizer.compute_gradients(model.loss_op)

        grads_and_vars = [(tf.clip_by_norm(g, FLAGS.max_grad_norm), v)
                          for g, v in grads_and_vars if g is not None]
        grads_and_vars = [(add_gradient_noise(g), v) for g, v in grads_and_vars]
        nil_grads_and_vars = []
        for g, v in grads_and_vars:
            if v.name in model._nil_vars:
                nil_grads_and_vars.append((zero_nil_slot(g), v))
            else:
                nil_grads_and_vars.append((g, v))

        train_op = optimizer.apply_gradients(grads_and_vars,
                                             name="train_op",
                                             global_step=global_step)
        sess.run(tf.initialize_all_variables())

        for t in range(1, FLAGS.epochs+1):
            np.random.shuffle(batches)
            train_preds = []
            for start in range(0, n_train, batch_size):
                end = start + batch_size
                s = trainS[start:end]
                q = trainQ[start:end]
                a = trainA[start:end]
                feed_dict = {
                    model._memory_value: s,
                    model._query: q,
                    model._doc: s,
                    model._labels: a
                }
                _, step, predict_op = sess.run([train_op, global_step, model.predict_op], feed_dict)
                train_preds += list(predict_op)

                # total_cost += cost_t
            train_acc = metrics.accuracy_score(np.array(train_preds), train_labels)
            print('-----------------------')
            print('Epoch', t)
            print('Training Accuracy:', train_acc)
            print('-----------------------')
                
            if t % FLAGS.evaluation_interval == 0:
                feed_dict = {
                    model._query: valQ,
                    model._doc: valS,
                    model._memory_value: valS
                }
                val_preds = sess.run(model.predict_op, feed_dict)
                val_acc = metrics.accuracy_score(np.array(val_preds), val_labels)
                print (val_preds)
                print('-----------------------')
                print('Epoch', t)
                print('Validation Accuracy:', val_acc)
                print('-----------------------')
        feed_dict = {
            model._query: testQ,
            model._doc: testS,
            model._memory_value: testS
        }
        test_preds = sess.run(model.predict_op, feed_dict)
        # test_preds = model.predict(testS, testQ)
        test_acc = metrics.accuracy_score(test_preds, test_labels)
        print("Testing Accuracy:", test_acc)
