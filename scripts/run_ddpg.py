import os
import os.path as osp
import sys
import argparse
import operator
import tensorflow as tf
import numpy as np

sys.path.insert(1, osp.join(sys.path[0], '..'))
sys.path.insert(1, osp.join(sys.path[0], '../lib/ma_env'))

from lib import multiagent
from ddpg.model import DDPG
from settings import *

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
tf.logging.set_verbosity(tf.logging.ERROR)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=None)
    parser.add_argument('--name', type=str, default='ddpg', help='Naming for logging.')
    parser.add_argument('--scenario', type=str, default='simple_push.py',
                        help='Path of the scenario Python script (default=push_ball.py).')

    parser.add_argument('--n_agent', type=int, default=2, help='Set the number of agents (default=2')
    parser.add_argument('--len_episode', type=int, default=25, help='Time horizon limitation (default=25).')
    parser.add_argument('--n_train', type=int, default=10000, help='Training round.')
    parser.add_argument('--eval_interval', type=int, default=200, help='Evaluation episode interval (default=50).')
    parser.add_argument('--batch_size', type=int, default=64, help='Batch size (default=64).')
    parser.add_argument('--memory_size', type=int, default=10**6, help='Memory size (default=10**5).')
    parser.add_argument('--load', type=int, default=0, help='Load existed model.')

    parser.add_argument('--actor_lr', type=float, default=1e-4, help='Setting learning rate for Actor (default=1e-4).')
    parser.add_argument('--critic_lr', type=float, default=1e-3,
                        help='Setting learning rate for Critic (default=1e-3).')
    parser.add_argument('--tau', type=float, default=0.01, help='Hyper-parameter for soft update (default=0.01).')
    parser.add_argument('--gamma', type=float, default=0.98, help='Discount factor (default=0.98).')

    parser.add_argument('--render', action='store_true', help='Turn on render or not.')
    args = parser.parse_args()

    print('=== Configuration:\n', args)

    # =========================== initialize environment =========================== #
    step, steps_limit = 0, args.len_episode * args.n_train
    scenario = multiagent.scenarios.load(args.scenario).Scenario()
    world = scenario.make_world(num_agents=args.n_agent, world_dim_c=1, num_landmarks=args.n_agent // 2,
                                num_adversaries=args.n_agent // 2)

    env = multiagent.environment.MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation,
                                               info_callback=None, shared_viewer=True)

    # =========================== initialize model and summary =========================== #
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    sess = tf.Session(config=config)

    ddpg = [None for _ in range(env.n)]
    for i in range(env.n):
        with tf.variable_scope('ddpg_agent-{}'.format(i)):
            ddpg[i] = DDPG("{}_agent-{}".format(args.name, i), sess, env.observation_space[i].shape, (env.action_space[i].n,), args.gamma, args.actor_lr, args.critic_lr, args.memory_size, args.batch_size, args.tau)

    # initialize summary
    summary_r = [None for _ in range(env.n)]

    for i in range(env.n):
        summary_r[i] = tf.placeholder(tf.float32, None)
        tf.summary.scalar('Episode-Reward-{}'.format(i), summary_r[i])

    summary_dict = {'reward': summary_r}

    # if not args.render:
    summary_p_loss = [None for _ in range(env.n)]
    summary_q_loss = [None for _ in range(env.n)]

    for i in range(env.n):
        summary_p_loss[i] = tf.placeholder(tf.float32, None)
        summary_q_loss[i] = tf.placeholder(tf.float32, None)

        tf.summary.scalar('Actor-Loss-{}'.format(i), summary_p_loss[i])
        tf.summary.scalar('Critic-Loss-{}'.format(i), summary_q_loss[i])

    summary_dict['p_loss'] = summary_p_loss
    summary_dict['q_loss'] = summary_q_loss

    merged = tf.summary.merge_all()

    log_dir = os.path.join(LOG_DIR, args.name)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    else:
        tf.gfile.DeleteRecursively(log_dir)

    summary_writer = tf.summary.FileWriter(log_dir)

    sess.run(tf.global_variables_initializer())

    _ = [agent.sync_net() for agent in ddpg]

    if args.load > 0:
        _ = [agent.load(os.path.join(MODEL_BACK_UP, args.name), epoch=args.load) for agent in ddpg]

    # ======================================== main loop ======================================== #
    p_loss, q_loss = None, None
    is_evaluate = False

    if args.render and is_evaluate:
        env.render(mode=None)
    else:
        p_loss = [[] for _ in range(env.n)]
        q_loss = [[] for _ in range(env.n)]

    obs_n = env.reset()
    episode_r_n = [0. for _ in range(env.n)]

    # update this flag every `len_episode * eval_interval` steps, if it is true, then no training and data collection

    while step < steps_limit:
        act_n = [agent.act(obs) for agent, obs in zip(ddpg, obs_n)]
        next_obs_n, reward_n, done_n, info_n = env.step(act_n)

        if not is_evaluate:  # trigger for data collection
            _ = [agent.store_transition(o, a, next_o, r, done) for agent, o, a, next_o, r, done in zip(ddpg, obs_n, act_n, next_obs_n, reward_n, done_n)]

        obs_n = next_obs_n

        if args.render or is_evaluate:
            episode_r_n = map(operator.add, episode_r_n, reward_n)

        step += 1

        # =============================== render / record / model saving ===============================
        if args.render and is_evaluate:
            env.render(mode=None)

        if step % args.len_episode == 0 or np.any(done_n):
            obs_n = env.reset()

            feed_dict = dict()

            if args.render or is_evaluate:
                feed_dict.update(zip(summary_dict['reward'], episode_r_n))
                episode_r_n = [0. for _ in range(env.n)]

            if not is_evaluate:
                _loss = [agent.train() for agent in ddpg]

                if _loss[0] is not None:
                    p_loss = map(lambda x, y: y + [x[0]], _loss, p_loss)
                    q_loss = map(lambda x, y: y + [x[1]], _loss, q_loss)

            if is_evaluate:
                p_loss = list(map(lambda x: sum(x) / len(x), p_loss))
                q_loss = list(map(lambda x: sum(x) / len(x), q_loss))

                print("\n--- episode-{} [p-loss]: {} [q-loss]: {}".format(step // args.len_episode - 1, p_loss, q_loss))

                feed_dict.update(zip(summary_dict['p_loss'], p_loss))
                feed_dict.update(zip(summary_dict['q_loss'], q_loss))

                p_loss = [[] for _ in range(env.n)]
                q_loss = [[] for _ in range(env.n)]

                _ = [agent.save(MODEL_BACK_UP, step // args.len_episode - 1) for agent in ddpg]

            if is_evaluate:
                summary = sess.run(merged, feed_dict=feed_dict)
                summary_writer.add_summary(summary, (step - 1) // args.len_episode)

            is_evaluate = (step // args.len_episode % args.eval_interval == 0)
            # is_evaluate = not is_evaluate
            env.close()
            obs_n = env.reset()

