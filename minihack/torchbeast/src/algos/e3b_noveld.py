# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import logging
import os
import sys
import threading
import time
import timeit
import pprint
import json
import pdb
import contextlib

import numpy as np
import random
import copy

import torch
from torch import multiprocessing as mp
from torch import nn
from torch.nn import functional as F

from src.core import file_writer
from src.core import prof
from src.core import vtrace

import src.models as models
import src.losses as losses

from src.env_utils import FrameStack, Environment
from src.utils import get_batch, log, create_env, create_buffers, act, create_heatmap_buffers



NetHackStateEmbeddingNet = models.NetHackStateEmbeddingNet
MinigridInverseDynamicsNet = models.MinigridInverseDynamicsNet

MinigridMLPEmbeddingNet = models.MinigridMLPEmbeddingNet
MinigridMLPTargetEmbeddingNet = models.MinigridMLPTargetEmbeddingNet


def learn(actor_model,
          model,
          inverse_dynamics_model,
          random_target_network,
          predictor_network,
          random_encoder,
          actor_elliptical_encoder, 
          elliptical_encoder, 
          batch,
          initial_agent_state, 
          optimizer,
          elliptical_encoder_optimizer,
          inverse_dynamics_optimizer,
          predictor_optimizer, 
          scheduler,
          flags,
          frames=None,
          lock=threading.Lock()):
    """Performs a learning (optimization) step."""
    with lock:
        timings = prof.Timings()
#        timings.reset()

        episodic_bonus = batch['bonus_reward'][1:]
        intrinsic_rewards = episodic_bonus
        

        # NovelD/RND bonus
        with torch.no_grad():
            encoded_states, unused_state = random_encoder(batch, tuple())
            
        random_embedding_next, unused_state = random_target_network(encoded_states[1:].detach(), initial_agent_state)
        predicted_embedding_next, unused_state = predictor_network(encoded_states[1:].detach(), initial_agent_state)
        random_embedding, unused_state = random_target_network(encoded_states[:-1].detach(), initial_agent_state)
        predicted_embedding, unused_state = predictor_network(encoded_states[:-1].detach(), initial_agent_state)

        intrinsic_rewards_noveld_next = torch.norm(predicted_embedding_next.detach() - random_embedding_next.detach(), dim=2, p=2)
        intrinsic_rewards_noveld = torch.norm(predicted_embedding.detach() - random_embedding.detach(), dim=2, p=2)
        intrinsic_rewards_noveld = torch.clamp(intrinsic_rewards_noveld_next - flags.scale_fac * intrinsic_rewards_noveld, min=0)

        if flags.bonus_combine == 'mult':
            intrinsic_rewards *= intrinsic_rewards_noveld
        elif flags.bonus_combine == 'add':
            intrinsic_rewards += flags.global_bonus_coeff*intrinsic_rewards_noveld

        rnd_loss = flags.rnd_loss_coef * \
                losses.compute_rnd_loss(predicted_embedding_next, random_embedding_next.detach())
        


        # ICM loss
        elliptical_encoder.train()
        inverse_dynamics_model.train()
        icm_state_emb_all, _ = elliptical_encoder(batch, tuple())
        icm_state_emb = icm_state_emb_all[:-1]
        icm_next_state_emb = icm_state_emb_all[1:]
        pred_actions = inverse_dynamics_model(icm_state_emb, icm_next_state_emb)
        inverse_dynamics_loss = losses.compute_inverse_dynamics_loss(pred_actions, batch['action'][1:])
        
            
        learner_outputs, unused_state = model(batch, initial_agent_state)
        bootstrap_value = learner_outputs['baseline'][-1]

        batch = {key: tensor[1:] for key, tensor in batch.items()}
        learner_outputs = {
            key: tensor[:-1]
            for key, tensor in learner_outputs.items()
        }
        
        rewards = batch['reward']

        if flags.reward_norm == 'int':
            model.update_running_moments(intrinsic_rewards)
            std = model.get_running_std()
            if std > 0:
                intrinsic_rewards /= std
        elif flags.reward_norm == 'ext':
            model.update_running_moments(rewards)
            std = model.get_running_std()
            if std > 0:
                rewards /= std
        
                    
        if flags.no_reward:
            total_rewards = intrinsic_rewards * flags.intrinsic_reward_coef
        else:            
            total_rewards = rewards + intrinsic_rewards * flags.intrinsic_reward_coef

        if flags.reward_norm == 'all':
            model.update_running_moments(total_rewards)
            std = model.get_running_std()
            if std > 0:
                total_rewards /= std
            

        if flags.clip_rewards == 1:
            clipped_rewards = torch.clamp(total_rewards, -1, 1)
        else:
            clipped_rewards = total_rewards
        
        discounts = (~batch['done']).float() * flags.discounting

        vtrace_returns = vtrace.from_logits(
            behavior_policy_logits=batch['policy_logits'],
            target_policy_logits=learner_outputs['policy_logits'],
            actions=batch['action'],
            discounts=discounts,
            rewards=clipped_rewards,
            values=learner_outputs['baseline'],
            bootstrap_value=bootstrap_value)

        pg_loss = losses.compute_policy_gradient_loss(learner_outputs['policy_logits'],
                                               batch['action'],
                                               vtrace_returns.pg_advantages)
        baseline_loss = flags.baseline_cost * losses.compute_baseline_loss(
            vtrace_returns.vs - learner_outputs['baseline'])
        entropy_loss = flags.entropy_cost * losses.compute_entropy_loss(
            learner_outputs['policy_logits'])

        total_loss = pg_loss + baseline_loss + entropy_loss + inverse_dynamics_loss + rnd_loss


        
        episode_returns = batch['episode_return'][batch['done']]
        stats = {
            'mean_episode_return': torch.mean(episode_returns).item(),
            'total_loss': total_loss.item(),
            'pg_loss': pg_loss.item(),
            'baseline_loss': baseline_loss.item(),
            'entropy_loss': entropy_loss.item(),
            'inverse_dynamics_loss': inverse_dynamics_loss.item(),
            'rnd_loss': rnd_loss.item(),
            'mean_rewards': torch.mean(rewards).item(),
            'episodic_rewards': torch.mean(episodic_bonus).item(),
            'noveld_rewards': torch.mean(intrinsic_rewards_noveld).item(),
            'mean_intrinsic_rewards': torch.mean(intrinsic_rewards).item(),
            'mean_total_rewards': torch.mean(total_rewards).item(),
        }

        
        optimizer.zero_grad()
        predictor_optimizer.zero_grad()
        elliptical_encoder_optimizer.zero_grad()
        inverse_dynamics_optimizer.zero_grad()
        total_loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), flags.max_grad_norm)
        nn.utils.clip_grad_norm_(inverse_dynamics_model.parameters(), flags.max_grad_norm)
        nn.utils.clip_grad_norm_(elliptical_encoder.parameters(), flags.max_grad_norm)
        nn.utils.clip_grad_norm_(predictor_network.parameters(), flags.max_grad_norm)

        optimizer.step()
        predictor_optimizer.step()
        elliptical_encoder_optimizer.step()
        inverse_dynamics_optimizer.step()
        

        actor_model.load_state_dict(model.state_dict())
        actor_elliptical_encoder.load_state_dict(elliptical_encoder.state_dict())
        return stats, None



    
    

def train(flags):

    xpid = ''
    xpid += f'env_{flags.env}'
    xpid += f'-eb_{flags.episodic_bonus_type}'
    xpid += f'-cb_{flags.bonus_combine}'
    xpid += f'-gbc_{flags.global_bonus_coeff}'
    xpid += f'-lr_{flags.learning_rate}'
    xpid += f'-plr_{flags.predictor_learning_rate}'
    xpid += f'-sf_{flags.scale_fac}'
    xpid += f'-entropy_{flags.entropy_cost}'
    xpid += f'-intweight_{flags.intrinsic_reward_coef}'
    xpid += f'-ridge_{flags.ridge}'
    xpid += f'-cr_{flags.clip_rewards}'
    xpid += f'-rn_{flags.reward_norm}'
    xpid += f'-seed_{flags.seed}'

        
    flags.xpid = xpid
        
    plogger = file_writer.FileWriter(
        xpid=flags.xpid,
        xp_args=flags.__dict__,
        rootdir=flags.savedir,
    )

    checkpointpath = os.path.expandvars(
        os.path.expanduser('%s/%s/%s' % (flags.savedir, flags.xpid,
                                         'model.tar')))

    T = flags.unroll_length
    B = flags.batch_size

    if not flags.disable_cuda and torch.cuda.is_available():
        log.info('Using CUDA.')
        flags.device = torch.device(f'cuda:{flags.device}')
    else:
        log.info('Not using CUDA.')
        flags.device = torch.device('cpu')

    env = create_env(flags)
    if flags.num_input_frames > 1:
        env = FrameStack(env, flags.num_input_frames)  
        
    if 'MiniHack' in flags.env or 'NetHack' in flags.env:
        model = models.NetHackPolicyNet(env.observation_space, env.action_space.n, flags.use_lstm)
        elliptical_encoder = NetHackStateEmbeddingNet(env.observation_space, False) # do not use LSTM for encoder
        inverse_dynamics_model = MinigridInverseDynamicsNet(env.action_space.n, emb_size=1024)\
            .to(device=flags.device)
        random_target_network = MinigridMLPTargetEmbeddingNet(hidden_dim=flags.hidden_dim).to(device=flags.device) 
        predictor_network = MinigridMLPEmbeddingNet(hidden_dim=flags.hidden_dim).to(device=flags.device) 
        
    elif 'Vizdoom' in flags.env:
        model = models.MarioDoomPolicyNet(env.observation_space.shape, env.action_space.n)
        encoder = models.MarioDoomStateEmbeddingNet(env.observation_space.shape)
        elliptical_encoder = models.MarioDoomStateEmbeddingNet(env.observation_space.shape)
        inverse_dynamics_model = models.MarioDoomInverseDynamicsNet(env.action_space.n)\
            .to(device=flags.device) 
        heatmap_buffers = create_heatmap_buffers(env.observation_space.shape)
        
    else:
        raise Exception('Only MiniHack and Vizdoom are suppported Now!')


    buffers = create_buffers(env.observation_space, model.num_actions, flags)
    model.share_memory()
    elliptical_encoder.share_memory()
    
    initial_agent_state_buffers = []
    for _ in range(flags.num_buffers):
        state = model.initial_state(batch_size=1)
        for t in state:
            t.share_memory_()
        initial_agent_state_buffers.append(state)

    actor_processes = []
    ctx = mp.get_context('fork')
    free_queue = ctx.Queue()
    full_queue = ctx.Queue()

    episode_state_count_dict = dict()
    
    for i in range(flags.num_actors):
        actor = ctx.Process(
            target=act,
            args=(i, free_queue, full_queue, model, elliptical_encoder, buffers, 
                  episode_state_count_dict, None, initial_agent_state_buffers, flags))
        actor.start()
        actor_processes.append(actor)

    if 'MiniHack' in flags.env or 'NetHack' in flags.env:
        learner_model = models.NetHackPolicyNet(env.observation_space, env.action_space.n, flags.use_lstm, hidden_dim=flags.hidden_dim).to(flags.device)
        random_encoder = NetHackStateEmbeddingNet(env.observation_space, False, hidden_dim=flags.hidden_dim).to(flags.device)
        elliptical_learner_encoder = NetHackStateEmbeddingNet(env.observation_space, False, hidden_dim=flags.hidden_dim).to(flags.device)
        elliptical_learner_encoder.load_state_dict(elliptical_encoder.state_dict())
        

    elif 'Vizdoom' in flags.env:
        learner_model = models.MarioDoomPolicyNet(env.observation_space.shape, env.action_space.n).to(flags.device)
        learner_encoder = models.MarioDoomStateEmbeddingNet(env.observation_space.shape).to(flags.device)
        elliptical_learner_encoder = models.MarioDoomStateEmbeddingNet(env.observation_space.shape).to(flags.device)
        elliptical_learner_encoder.load_state_dict(elliptical_encoder.state_dict())

    optimizer = torch.optim.RMSprop(
        learner_model.parameters(),
        lr=flags.learning_rate,
        momentum=flags.momentum,
        eps=flags.epsilon,
        alpha=flags.alpha)

    elliptical_encoder_optimizer = torch.optim.Adam(
        elliptical_learner_encoder.parameters(), 
        lr=flags.predictor_learning_rate)
    
    inverse_dynamics_optimizer = torch.optim.Adam(
        inverse_dynamics_model.parameters(), 
        lr=flags.predictor_learning_rate)

    predictor_optimizer = torch.optim.Adam(
        predictor_network.parameters(), 
        lr=flags.predictor_learning_rate)
    
    

    def lr_lambda(epoch):
        return 1 - min(epoch * T * B, flags.total_frames) / flags.total_frames

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)

    logger = logging.getLogger('logfile')
    stat_keys = [
        'total_loss',
        'mean_episode_return',
        'pg_loss',
        'baseline_loss',
        'entropy_loss',
        'inverse_dynamics_loss',
        'mean_rewards',
        'episodic_rewards',
        'noveld_rewards',
#        'global_rewards',
        'mean_intrinsic_rewards',
        'mean_total_rewards',
    ]

    if flags.episodic_bonus_type == 'elliptical-icm-global':
        stat_keys += ['global_elliptical_rewards']
        
    logger.info('# Step\t%s', '\t'.join(stat_keys))

    frames, stats = 0, {}


    if os.path.exists(checkpointpath):
        print(f'[loading checkpoint: {checkpointpath}]')
        checkpoint = torch.load(checkpointpath)
        model.load_state_dict(checkpoint['model_state_dict'])
        learner_model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        encoder.load_state_dict(checkpoint['encoder'])
        learner_encoder.load_state_dict(checkpoint['encoder'])
        elliptical_encoder.load_state_dict(checkpoint['elliptical_encoder_state_dict'])
        elliptical_learner_encoder.load_state_dict(checkpoint['elliptical_encoder_state_dict'])
        inverse_dynamics_model.load_state_dict(checkpoint['inverse_dynamics_model_state_dict'])
        predictor_network.load_state_dict(checkpoint['predictor_network_state_dict'])
        predictor_optimizer.load_state_dict(checkpoint['predictor_optimizer_state_dict'])
        random_target_network.load_state_dict(checkpoint['random_target_network_state_dict'])
        elliptical_encoder_optimizer.load_state_dict(checkpoint['elliptical_encoder_optimizer_state_dict'])
        inverse_dynamics_optimizer.load_state_dict(checkpoint['inverse_dynamics_optimizer_state_dict'])
        
        frames = checkpoint['frames']
    

    def batch_and_learn(i, lock=threading.Lock()):
        """Thread target for the learning process."""
        nonlocal frames, stats
        timings = prof.Timings()
        batches = []
        
        while frames < flags.total_frames:
            timings.reset()
            batch, agent_state = get_batch(free_queue, full_queue, buffers, 
                initial_agent_state_buffers, flags, timings)
            stats, decoder_logits = learn(model, learner_model,
                                          inverse_dynamics_model,
                                          random_target_network, predictor_network,
                                          random_encoder,
                                          elliptical_encoder,
                                          elliptical_learner_encoder, batch, agent_state, optimizer, 
                                          elliptical_encoder_optimizer, inverse_dynamics_optimizer,
                                          predictor_optimizer, scheduler,
                                          flags, frames=frames)
            timings.time('learn')
            with lock:
                to_log = dict(frames=frames)
                to_log.update({k: stats[k] for k in stat_keys})
                plogger.log(to_log)
                frames += T * B
                
        if i == 0:
            log.info('Batch and learn: %s', timings.summary())

    for m in range(flags.num_buffers):
        free_queue.put(m)

    threads = []    
    for i in range(flags.num_threads):
        thread = threading.Thread(
            target=batch_and_learn, name='batch-and-learn-%d' % i, args=(i,))
        thread.start()
        threads.append(thread)


    def checkpoint(frames):
        checkpointpath = os.path.expandvars(os.path.expanduser(
            '%s/%s/%s' % (flags.savedir, flags.xpid,'model.tar')))
        log.info('Saving checkpoint to %s', checkpointpath)
        torch.save({
            'frames': frames,
            'model_state_dict': model.state_dict(),
            'random_target_network_state_dict': random_target_network.state_dict(),
            'predictor_network_state_dict': predictor_network.state_dict(),
            'elliptical_encoder_state_dict': elliptical_encoder.state_dict(),
            'inverse_dynamics_model_state_dict': inverse_dynamics_model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'elliptical_encoder_optimizer_state_dict': elliptical_encoder_optimizer.state_dict(),
            'inverse_dynamics_optimizer_state_dict': inverse_dynamics_optimizer.state_dict(),
            'predictor_optimizer_state_dict': predictor_optimizer.state_dict(),
            'scheduler_state_dict': scheduler.state_dict(),
            'flags': vars(flags),
        }, checkpointpath)

    timer = timeit.default_timer
    try:
        last_checkpoint_time = timer()
        while frames < flags.total_frames:
            start_frames = frames
            start_time = timer()
            time.sleep(5)

            if timer() - last_checkpoint_time > flags.save_interval * 60:  
                checkpoint(frames)
                last_checkpoint_time = timer()

            fps = (frames - start_frames) / (timer() - start_time)
            
            if stats.get('episode_returns', None):
                mean_return = 'Return per episode: %.1f. ' % stats[
                    'mean_episode_return']
            else:
                mean_return = ''

            total_loss = stats.get('total_loss', float('inf'))
            if stats:
                log.info('After %i frames: loss %f @ %.1f fps. Mean Return %.1f. \n Stats \n %s', \
                        frames, total_loss, fps, stats['mean_episode_return'], pprint.pformat(stats))

    except KeyboardInterrupt:
        return  
    else:
        for thread in threads:
            thread.join()
        log.info('Learning finished after %d frames.', frames)
    finally:
        for _ in range(flags.num_actors):
            free_queue.put(None)
        for actor in actor_processes:
            actor.join(timeout=1)

    checkpoint(frames)
    plogger.close()

