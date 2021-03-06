'''
Created on Oct 10, 2018
Tensorflow Implementation of Neural Graph Collaborative Filtering (NGCF) model in:
Wang Xiang et al. Neural Graph Collaborative Filtering. In SIGIR 2019.
@author: Xiang Wang (xiangwang@u.nus.edu)
version:
Parallelized sampling on CPU
C++ evaluation for top-k recommendation
'''

import os
import sys
import threading
import tensorflow as tf
import logging
from tensorflow.python.client import device_lib
from utility.helper import *
from utility.batch_test import *

os.environ['TF_CPP_MIN_LOG_LEVEL']='2'

cpus = [x.name for x in device_lib.list_local_devices() if x.device_type == 'CPU']

class LightGCN(object):
    def __init__(self, data_config, pretrain_data):
        # argument settings
        self.model_type = 'LightGCN'
        self.adj_type = args.adj_type
        self.alg_type = args.alg_type
        self.pretrain_data = pretrain_data
        self.n_users = data_config['n_users']
        self.n_items = data_config['n_items']
        self.n_cat = data_config['n_cat']
        self.n_price = data_config['n_price']
        self.n_fold = 100
        self.norm_adj = data_config['norm_adj']
        if args.adj_type == 'adj_with_cp':
            self.cat_and_price_adj = data_config['cat_and_price_adj']
        self.n_nonzero_elems = self.norm_adj.count_nonzero()        
        #TODO: If we reduce a adjacency matrix, n_nonzero_elem might need to be changed, as this has counted the wrong adjacency matrix
        self.lr = args.lr
        self.emb_dim = args.embed_size
        self.batch_size = args.batch_size
        self.weight_size = eval(args.layer_size)
        self.alpha_k = args.alpha_k
        self.n_layers = len(self.weight_size)
        if (self.alpha_k == 'leveled'):
            self.layer_effects = eval(args.layer_effect)
            self._validate_layer_effects()
        self.regs = eval(args.regs)
        self.decay = self.regs[0]
        self.verbose = args.verbose
        self.Ks = eval(args.Ks)
    
        self.log_dir=self.create_model_str()
        self.node_dim = data_config['node_dim']

        '''
        *********************************************************
        Create Placeholder for Input Data & Dropout.
        '''
        # placeholder definition
        self.users = tf.placeholder(tf.int32, shape=(None,))
        self.pos_items = tf.placeholder(tf.int32, shape=(None,))
        self.neg_items = tf.placeholder(tf.int32, shape=(None,))
        
        self.node_dropout_flag = args.node_dropout_flag
        self.node_dropout = tf.placeholder(tf.float32, shape=[None])
        self.mess_dropout = tf.placeholder(tf.float32, shape=[None])
        with tf.name_scope('TRAIN_LOSS'):
            self.train_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('train_loss', self.train_loss)
            self.train_mf_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('train_mf_loss', self.train_mf_loss)
            self.train_emb_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('train_emb_loss', self.train_emb_loss)
            self.train_reg_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('train_reg_loss', self.train_reg_loss)
        self.merged_train_loss = tf.summary.merge(tf.get_collection(tf.GraphKeys.SUMMARIES, 'TRAIN_LOSS'))
        
        
        with tf.name_scope('TRAIN_ACC'):
            self.train_rec_first = tf.placeholder(tf.float32)
            #record for top(Ks[0])
            tf.summary.scalar('train_rec_first', self.train_rec_first)
            self.train_rec_last = tf.placeholder(tf.float32)
            #record for top(Ks[-1])
            tf.summary.scalar('train_rec_last', self.train_rec_last)
            self.train_ndcg_first = tf.placeholder(tf.float32)
            tf.summary.scalar('train_ndcg_first', self.train_ndcg_first)
            self.train_ndcg_last = tf.placeholder(tf.float32)
            tf.summary.scalar('train_ndcg_last', self.train_ndcg_last)
        self.merged_train_acc = tf.summary.merge(tf.get_collection(tf.GraphKeys.SUMMARIES, 'TRAIN_ACC'))

        with tf.name_scope('TEST_LOSS'):
            self.test_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('test_loss', self.test_loss)
            self.test_mf_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('test_mf_loss', self.test_mf_loss)
            self.test_emb_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('test_emb_loss', self.test_emb_loss)
            self.test_reg_loss = tf.placeholder(tf.float32)
            tf.summary.scalar('test_reg_loss', self.test_reg_loss)
        self.merged_test_loss = tf.summary.merge(tf.get_collection(tf.GraphKeys.SUMMARIES, 'TEST_LOSS'))

        with tf.name_scope('TEST_ACC'):
            self.test_rec_first = tf.placeholder(tf.float32)
            tf.summary.scalar('test_rec_first', self.test_rec_first)
            self.test_rec_last = tf.placeholder(tf.float32)
            tf.summary.scalar('test_rec_last', self.test_rec_last)
            self.test_ndcg_first = tf.placeholder(tf.float32)
            tf.summary.scalar('test_ndcg_first', self.test_ndcg_first)
            self.test_ndcg_last = tf.placeholder(tf.float32)
            tf.summary.scalar('test_ndcg_last', self.test_ndcg_last)
        self.merged_test_acc = tf.summary.merge(tf.get_collection(tf.GraphKeys.SUMMARIES, 'TEST_ACC'))
        """
        *********************************************************
        Create Model Parameters (i.e., Initialize Weights).
        """
        # initialization of model parameters
        self.weights = self._init_weights()

        """
        *********************************************************
        Compute Graph-based Representations of all users & items via Message-Passing Mechanism of Graph Neural Networks.
        Different Convolutional Layers:
            1. ngcf: defined in 'Neural Graph Collaborative Filtering', SIGIR2019;
            2. gcn:  defined in 'Semi-Supervised Classification with Graph Convolutional Networks', ICLR2018;
            3. gcmc: defined in 'Graph Convolutional Matrix Completion', KDD2018;
        """
        if self.alg_type in ['lightgcn']:
            self.ua_embeddings, self.ia_embeddings = self._create_lightgcn_embed()

        elif self.alg_type in ['LightGCN-alpha-1']:
            self.ua_embeddings, self.ia_embeddings = self._create_lightgcn_alpha_k_equals_1_embed()
        
        elif self.alg_type in ['ngcf']:
            self.ua_embeddings, self.ia_embeddings = self._create_ngcf_embed()

        elif self.alg_type in ['gcn']:
            self.ua_embeddings, self.ia_embeddings = self._create_gcn_embed()

        elif self.alg_type in ['gcmc']:
            self.ua_embeddings, self.ia_embeddings = self._create_gcmc_embed()

        elif self.alg_type in ['ngcfpas']:
            self.ua_embeddings, self.ia_embeddings = self._create_ngcf_pas_embed()

        elif self.alg_type in ['pas']:
            self.ua_embeddings, self.ia_embeddings = self._create_price_aware_simple_embed()
            
        elif self.alg_type in ['gcf']:
            self.ua_embeddings, self.ia_embeddings = self._create_gcf_embed()
        
        elif self.alg_type in ['gcf-only-ip']:
            self.ua_embeddings, self.ia_embeddings = self._create_gcf_only_ip_embed()

        elif self.alg_type in ['gcf-sum']:
            self.ua_embeddings, self.ia_embeddings = self._create_gcf_sum_embed()
        
        elif self.alg_type in ['gcf-sum-only-ip']:
            self.ua_embeddings, self.ia_embeddings = self._create_gcf_sum_only_ip_embed()
        
        elif self.alg_type in ['gcf-minus-ip']:
            self.ua_embeddings, self.ia_embeddings =self._create_gcf_minus_IP_embed()
        
        elif self.alg_type in ['LightGCN-concat']:
            self.ua_embeddings, self.ia_embeddings =self._create_lightgcn_concat_embed()
        

        """
        *********************************************************
        Establish the final representations for user-item pairs in batch.
        """
        self.u_g_embeddings = tf.nn.embedding_lookup(self.ua_embeddings, self.users)
        self.pos_i_g_embeddings = tf.nn.embedding_lookup(self.ia_embeddings, self.pos_items)
        self.neg_i_g_embeddings = tf.nn.embedding_lookup(self.ia_embeddings, self.neg_items)
        self.u_g_embeddings_pre = tf.nn.embedding_lookup(self.weights['user_embedding'], self.users)
        self.pos_i_g_embeddings_pre = tf.nn.embedding_lookup(self.weights['item_embedding'], self.pos_items)
        self.neg_i_g_embeddings_pre = tf.nn.embedding_lookup(self.weights['item_embedding'], self.neg_items)

        """
        *********************************************************
        Inference for the testing phase.
        """
        self.batch_ratings = tf.matmul(self.u_g_embeddings, self.pos_i_g_embeddings, transpose_a=False, transpose_b=True)

        """
        *********************************************************
        Generate Predictions & Optimize via BPR loss.
        """
        self.mf_loss, self.emb_loss, self.reg_loss = self.create_bpr_loss(self.u_g_embeddings,
                                                                          self.pos_i_g_embeddings,
                                                                          self.neg_i_g_embeddings)
        self.loss = self.mf_loss + self.emb_loss

        self.opt = tf.train.AdamOptimizer(learning_rate=self.lr).minimize(self.loss)
    
    def _validate_layer_effects(self):
        error = ''
        if len(self.layer_effects) != self.n_layers + 1:
            error += 'number of arguments for layer_effects does not match number of layers\n'
        
        assert len(error) == 0, error

    def create_model_str(self):
        log_dir = '/' + self.alg_type + '/alpha_k_' + self.alpha_k
        if self.alpha_k == 'leveled':
            log_dir += '/layer_effect_' + str(self.layer_effects)
        log_dir += '/adj_' + self.adj_type + '/layers_'+str(self.n_layers)+'/dim_'+str(self.emb_dim)
        log_dir+='/'+args.dataset+'/lr_' + str(self.lr) + '/reg_' + str(self.decay) + '/k_' + str(self.Ks[0])
        return log_dir


    def _init_weights(self):
        all_weights = dict()
        initializer = tf.random_normal_initializer(stddev=0.01) #tf.contrib.layers.xavier_initializer()
        if self.pretrain_data is None:
            all_weights['user_embedding'] = tf.Variable(initializer([self.n_users, self.emb_dim]), name='user_embedding')
            all_weights['item_embedding'] = tf.Variable(initializer([self.n_items, self.emb_dim]), name='item_embedding')
            all_weights['price_embedding'] = tf.Variable(initializer([self.n_price, self.emb_dim]), name='price_embedding')
            all_weights['cat_embedding'] = tf.Variable(initializer([self.n_cat, self.emb_dim]), name='cat_embedding')
            print('using random initialization')#print('using xavier initialization')
        else:
            all_weights['user_embedding'] = tf.Variable(initial_value=self.pretrain_data['user_embed'], trainable=True,
                                                        name='user_embedding', dtype=tf.float32)
            all_weights['item_embedding'] = tf.Variable(initial_value=self.pretrain_data['item_embed'], trainable=True,
                                                        name='item_embedding', dtype=tf.float32)
            all_weights['price_embedding'] = tf.Variable(initial_value=self.pretrain_data['price_embed'], trainable=True,
                                                        name='price_embedding', dtype=tf.float32)
            all_weights['cat_embedding'] = tf.Variable(initial_value=self.pretrain_data['cat_embed'], trainable=True,
                                                        name='cat_embedding', dtype=tf.float32)
            print('using pretrained initialization')
            
        self.weight_size_list = [self.emb_dim] + self.weight_size
        
        for k in range(self.n_layers):
            all_weights['W_gc_%d' %k] = tf.Variable(
                initializer([self.weight_size_list[k], self.weight_size_list[k+1]]), name='W_gc_%d' % k)
            all_weights['b_gc_%d' %k] = tf.Variable(
                initializer([1, self.weight_size_list[k+1]]), name='b_gc_%d' % k)

            all_weights['W_bi_%d' % k] = tf.Variable(
                initializer([self.weight_size_list[k], self.weight_size_list[k + 1]]), name='W_bi_%d' % k)
            all_weights['b_bi_%d' % k] = tf.Variable(
                initializer([1, self.weight_size_list[k + 1]]), name='b_bi_%d' % k)

            all_weights['W_mlp_%d' % k] = tf.Variable(
                initializer([self.weight_size_list[k], self.weight_size_list[k+1]]), name='W_mlp_%d' % k)
            all_weights['b_mlp_%d' % k] = tf.Variable(
                initializer([1, self.weight_size_list[k+1]]), name='b_mlp_%d' % k)

        return all_weights
    def _split_A_hat(self, X):
        A_fold_hat = []
        length_of_adj = (X._shape[0])
        fold_len = length_of_adj // self.n_fold
        for i_fold in range(self.n_fold):
            start = i_fold * fold_len
            if i_fold == self.n_fold -1:
                end = length_of_adj
            else:
                end = (i_fold + 1) * fold_len

            A_fold_hat.append(self._convert_sp_mat_to_sp_tensor(X[start:end]))
        return A_fold_hat

    def _split_A_hat_node_dropout(self, X):
        A_fold_hat = []
        length_of_adj = (X._shape[0])
        fold_len = (length_of_adj) // self.n_fold
        for i_fold in range(self.n_fold):
            start = i_fold * fold_len
            if i_fold == self.n_fold -1:
                end = length_of_adj
            else:
                end = (i_fold + 1) * fold_len

            temp = self._convert_sp_mat_to_sp_tensor(X[start:end])
            n_nonzero_temp = X[start:end].count_nonzero()
            A_fold_hat.append(self._dropout_sparse(temp, 1 - self.node_dropout[0], n_nonzero_temp))

        return A_fold_hat

    def _create_price_aware_simple_embed(self): #adj matrix of I x U,C,P (one category pr. item)
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.cat_and_price_adj) # performance reasons
        else:
            A_fold_hat = self._split_A_hat(self.cat_and_price_adj)
        
        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding'],self.weights['cat_embedding'], self.weights['price_embedding']], axis=0)
        all_embeddings = [ego_embeddings]
        print("Size of ego_embeddings: ", ego_embeddings._shape)
        print("Size of cat_and_price_adj: ", self.cat_and_price_adj._shape)
        print("Size of A_fold_hat: ", len(A_fold_hat))
        for k in range(0, self.n_layers):
            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))

            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = side_embeddings
            all_embeddings += [ego_embeddings]
        all_embeddings=tf.stack(all_embeddings,1)
        all_embeddings=tf.reduce_mean(all_embeddings,axis=1,keepdims=False)
        u_g_embeddings, i_g_embeddings, c_g_embeddings, p_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items, self.n_cat, self.n_price], 0)
        return u_g_embeddings, i_g_embeddings

    def _create_ngcf_pas_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.cat_and_price_adj)
        else:
            A_fold_hat = self._split_A_hat(self.cat_and_price_adj)

        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding'],self.weights['cat_embedding'], self.weights['price_embedding']], axis=0)

        all_embeddings = [ego_embeddings]

        for k in range(0, self.n_layers):

            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))

            side_embeddings = tf.concat(temp_embed, 0)
            sum_embeddings = tf.nn.leaky_relu(tf.matmul(side_embeddings, self.weights['W_gc_%d' % k]) + self.weights['b_gc_%d' % k])
            # bi messages of neighbors.
            bi_embeddings = tf.multiply(ego_embeddings, side_embeddings)
            # transformed bi messages of neighbors.
            bi_embeddings = tf.nn.leaky_relu(tf.matmul(bi_embeddings, self.weights['W_bi_%d' % k]) + self.weights['b_bi_%d' % k])
            # non-linear activation.
            ego_embeddings = sum_embeddings + bi_embeddings

            # message dropout.
            # ego_embeddings = tf.nn.dropout(ego_embeddings, 1 - self.mess_dropout[k])
            # normalize the distribution of embeddings.
            norm_embeddings = tf.nn.l2_normalize(ego_embeddings, axis=1)

            all_embeddings += [norm_embeddings]

        all_embeddings = tf.concat(all_embeddings, 1)
        u_g_embeddings, i_g_embeddings, c_g_embeddings, p_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items, self.n_cat, self.n_price], 0)
        return u_g_embeddings, i_g_embeddings
   
    # Original GCF 
    def _create_gcf_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj)
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)

        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]

        for k in range(0, self.n_layers):
            temp_embed = []

            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))
            
            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = side_embeddings + tf.multiply(ego_embeddings, side_embeddings)

            # normalize embeddings
            norm_embeddings = tf.nn.l2_normalize(ego_embeddings, axis=1)

            #concat
            all_embeddings += [norm_embeddings]
        all_embeddings = tf.concat(all_embeddings, 1)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings
    
    # Original GCF without inner product
    def _create_gcf_minus_IP_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj)
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)

        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]

        for k in range(0, self.n_layers):
            temp_embed = []

            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))
            
            side_embeddings = tf.concat(temp_embed, 0)

            # normalize embeddings
            norm_embeddings = tf.nn.l2_normalize(side_embeddings, axis=1)

            #concat
            all_embeddings += [norm_embeddings]

        all_embeddings = tf.concat(all_embeddings, 1)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings

    #GCF only with IP 
    def _create_gcf_only_ip_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj)
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)

        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]

        for k in range(0, self.n_layers):
            temp_embed = []

            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))
            
            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = tf.multiply(ego_embeddings, side_embeddings)

            # normalize embeddings
            norm_embeddings = tf.nn.l2_normalize(ego_embeddings, axis=1)

            #concat
            all_embeddings += [norm_embeddings]

        all_embeddings = tf.concat(all_embeddings, 1)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings
    
    # GCF with summing layers instead of concatenating layers
    def _create_gcf_sum_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj)
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)

        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]

        for k in range(0, self.n_layers):
            temp_embed = []

            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))
            
            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = side_embeddings + tf.multiply(ego_embeddings, side_embeddings)

            # normalize embeddings
            norm_embeddings = tf.nn.l2_normalize(ego_embeddings, axis=1)
            all_embeddings += [norm_embeddings]

        # Summing and averaging layers
        all_embeddings=tf.stack(all_embeddings,1)
        all_embeddings=tf.reduce_mean(all_embeddings,axis=1,keepdims=False)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings

    # GCF with summing layers instead of concatenating layers and only with IP
    def _create_gcf_sum_only_ip_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj)
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)

        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]

        for k in range(0, self.n_layers):
            temp_embed = []

            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))
            
            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = tf.multiply(ego_embeddings, side_embeddings)

            # normalize embeddings
            norm_embeddings = tf.nn.l2_normalize(ego_embeddings, axis=1)
            all_embeddings += [norm_embeddings]

        # Summing and averaging layers
        all_embeddings=tf.stack(all_embeddings,1)
        all_embeddings=tf.reduce_mean(all_embeddings,axis=1,keepdims=False)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings


    def _create_lightgcn_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj) # performance reasons
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)
        
        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]
        
        for k in range(0, self.n_layers):

            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))

            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = side_embeddings
            all_embeddings += [ego_embeddings]
        all_embeddings = tf.stack(all_embeddings,1)
        all_embeddings = self._calc_alpha_k(all_embeddings) 
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings
    
    def _calc_alpha_k(self, embeddings):
        if self.alpha_k == 'mean':
            embeddings = tf.reduce_mean(embeddings,axis=1,keepdims=False)
            return embeddings
        elif self.alpha_k == 'leveled':
            return self._leveled_alpha_k(embeddings)
    
    # Coefficient is an array of arrays, that contains the percentages of how much each layer should weigh.
    def _leveled_alpha_k(self, embeddings):
        # level_one, level_two, level_three = self._get_alpha_k_effect_levels()
        # pre_temp = [0.25 for i in range(self.emb_dim)]
        # temp = []
        # for i in range(4):
        #     temp.append(pre_temp)
        # coeficients = []
        # i = 0
        # for dim in self.node_dim:
        #     if i%100 == 0:
        #         print(i)

        #     if len(coeficients) == 0:
        #         coeficients = [temp]
        #     else:
        #         coeficients.append(temp)
        #     i += 1
        layer_effects = self.layer_effects
        for i in range(len(layer_effects)):
            layer_effects[i] = [layer_effects[i] for k in range(self.emb_dim)]

        coeficients = [layer_effects for k in range(len(self.node_dim))]
        coeficients = np.array(coeficients)    
        embeddings = tf.multiply(embeddings, coeficients)
        return tf.reduce_sum(embeddings,axis=1,keepdims=False)

    def _create_lightgcn_alpha_k_equals_1_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj) # performance reasons
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)
        
        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]
        
        for k in range(0, self.n_layers):
            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))

            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = side_embeddings
            all_embeddings += [ego_embeddings]
        all_embeddings=tf.stack(all_embeddings,1)
        all_embeddings=tf.reduce_sum(all_embeddings,axis=1,keepdims=False)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings

    def _create_lightgcn_concat_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj) # performance reasons
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)
        
        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)
        all_embeddings = [ego_embeddings]
        
        for k in range(0, self.n_layers):

            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))

            side_embeddings = tf.concat(temp_embed, 0)
            ego_embeddings = side_embeddings
            all_embeddings += [ego_embeddings]
        all_embeddings = tf.concat(all_embeddings, 1)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings

    def _create_ngcf_embed(self):
        if self.node_dropout_flag:
            A_fold_hat = self._split_A_hat_node_dropout(self.norm_adj)
        else:
            A_fold_hat = self._split_A_hat(self.norm_adj)

        ego_embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)

        all_embeddings = [ego_embeddings]

        for k in range(0, self.n_layers):

            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], ego_embeddings))

            side_embeddings = tf.concat(temp_embed, 0)
            sum_embeddings = tf.nn.leaky_relu(tf.matmul(side_embeddings, self.weights['W_gc_%d' % k]) + self.weights['b_gc_%d' % k])

            bi_embeddings = tf.multiply(ego_embeddings, side_embeddings)
            # transformed bi messages of neighbors.
            bi_embeddings = tf.nn.leaky_relu(tf.matmul(bi_embeddings, self.weights['W_bi_%d' % k]) + self.weights['b_bi_%d' % k])
            # non-linear activation.
            ego_embeddings = sum_embeddings + bi_embeddings

            # message dropout.
            # ego_embeddings = tf.nn.dropout(ego_embeddings, 1 - self.mess_dropout[k])

            # normalize the distribution of embeddings.
            norm_embeddings = tf.nn.l2_normalize(ego_embeddings, axis=1)

            all_embeddings += [norm_embeddings]

        all_embeddings = tf.concat(all_embeddings, 1)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings
    
    
    def _create_gcn_embed(self):
        A_fold_hat = self._split_A_hat(self.norm_adj)
        embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)


        all_embeddings = [embeddings]

        for k in range(0, self.n_layers):
            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], embeddings))

            embeddings = tf.concat(temp_embed, 0)
            embeddings = tf.nn.leaky_relu(tf.matmul(embeddings, self.weights['W_gc_%d' %k]) + self.weights['b_gc_%d' %k])
            # embeddings = tf.nn.dropout(embeddings, 1 - self.mess_dropout[k])

            all_embeddings += [embeddings]

        all_embeddings = tf.concat(all_embeddings, 1)
        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings
    
    def _create_gcmc_embed(self):
        A_fold_hat = self._split_A_hat(self.norm_adj)

        embeddings = tf.concat([self.weights['user_embedding'], self.weights['item_embedding']], axis=0)

        all_embeddings = []

        for k in range(0, self.n_layers):
            temp_embed = []
            for f in range(self.n_fold):
                temp_embed.append(tf.sparse_tensor_dense_matmul(A_fold_hat[f], embeddings))
            embeddings = tf.concat(temp_embed, 0)
            # convolutional layer.
            embeddings = tf.nn.leaky_relu(tf.matmul(embeddings, self.weights['W_gc_%d' % k]) + self.weights['b_gc_%d' % k])
            # dense layer.
            mlp_embeddings = tf.matmul(embeddings, self.weights['W_mlp_%d' %k]) + self.weights['b_mlp_%d' %k]
            # mlp_embeddings = tf.nn.dropout(mlp_embeddings, 1 - self.mess_dropout[k])

            all_embeddings += [mlp_embeddings]
        all_embeddings = tf.concat(all_embeddings, 1)
        

        u_g_embeddings, i_g_embeddings = tf.split(all_embeddings, [self.n_users, self.n_items], 0)
        return u_g_embeddings, i_g_embeddings

    def create_bpr_loss(self, users, pos_items, neg_items):
        pos_scores = tf.reduce_sum(tf.multiply(users, pos_items), axis=1)
        neg_scores = tf.reduce_sum(tf.multiply(users, neg_items), axis=1)
        
        regularizer = tf.nn.l2_loss(self.u_g_embeddings_pre) + tf.nn.l2_loss(
                self.pos_i_g_embeddings_pre) + tf.nn.l2_loss(self.neg_i_g_embeddings_pre)
        regularizer = regularizer / self.batch_size
        
        mf_loss = tf.reduce_mean(tf.nn.softplus(-(pos_scores - neg_scores)))
        

        emb_loss = self.decay * regularizer

        reg_loss = tf.constant(0.0, tf.float32, [1])

        return mf_loss, emb_loss, reg_loss
    
    def _convert_sp_mat_to_sp_tensor(self, X):
        coo = X.tocoo().astype(np.float32)
        indices = np.mat([coo.row, coo.col]).transpose()
        return tf.SparseTensor(indices, coo.data, coo.shape)
        
    def _dropout_sparse(self, X, keep_prob, n_nonzero_elems):
        """
        Dropout for sparse tensors.
        """
        noise_shape = [n_nonzero_elems]
        random_tensor = keep_prob
        random_tensor += tf.random_uniform(noise_shape)
        dropout_mask = tf.cast(tf.floor(random_tensor), dtype=tf.bool)
        pre_out = tf.sparse_retain(X, dropout_mask)

        return pre_out * tf.div(1., keep_prob)

def load_pretrained_data():
    pretrain_path = '%spretrain/%s/%s.npz' % (args.proj_path, args.dataset, 'embedding')
    try:
        pretrain_data = np.load(pretrain_path)
        print('load the pretrained embeddings.')
    except Exception:
        pretrain_data = None
    return pretrain_data

# parallelized sampling on CPU 
class sample_thread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
    def run(self):
        with tf.device(cpus[0]):
            self.data = data_generator.sample()

class sample_thread_test(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
    def run(self):
        with tf.device(cpus[0]):
            self.data = data_generator.sample_test()
            
# training on GPU
class train_thread(threading.Thread):
    def __init__(self,model, sess, sample):
        threading.Thread.__init__(self)
        self.model = model
        self.sess = sess
        self.sample = sample
    def run(self):

        users, pos_items, neg_items = self.sample.data
        self.data = sess.run([self.model.opt, self.model.loss, self.model.mf_loss, self.model.emb_loss, self.model.reg_loss],
                                feed_dict={model.users: users, model.pos_items: pos_items,
                                            model.node_dropout: eval(args.node_dropout),
                                            model.mess_dropout: eval(args.mess_dropout),
                                            model.neg_items: neg_items})

class train_thread_test(threading.Thread):
    def __init__(self,model, sess, sample):
        threading.Thread.__init__(self)
        self.model = model
        self.sess = sess
        self.sample = sample
    def run(self):
        
        users, pos_items, neg_items = self.sample.data
        self.data = sess.run([self.model.loss, self.model.mf_loss, self.model.emb_loss],
                                feed_dict={model.users: users, model.pos_items: pos_items,
                                        model.neg_items: neg_items,
                                        model.node_dropout: eval(args.node_dropout),
                                        model.mess_dropout: eval(args.mess_dropout)})       
def get_multi_split_train_writers(sess, tensorboard_model_path, splits):
    users_to_test = []
    train_writers = []
    splits_with_users = []
    for split in splits:
        path = tensorboard_model_path + '/dimensionality_' + split + '/'
        train_writer = tf.summary.FileWriter(path, sess.graph)
        index = 0
        if '-' in split:
            sub_splits = split.split('-')
            users = data_generator.test_set_range(int(sub_splits[0]), int(sub_splits[1]))
            if len(users) > 0:
                users_to_test.append(users)
                train_writers.append(train_writer)
                splits_with_users.append(split)
        else:
            last_index = len(split) - 1
            sub_splits = split[0:last_index]
            users = data_generator.test_set_range(int(sub_splits))
            if len(users) > 0:
                users_to_test.append(users)
                train_writers.append(train_writer)
                splits_with_users.append(split)
    
    
    return users_to_test, train_writers, splits_with_users

if __name__ == '__main__':
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu_id)
    f0 = time()
    _logger = logging.getLogger()
    _logger.setLevel(logging.DEBUG)
    logging_file_path = args.log_file
    if logging_file_path == '':
        logging_file_path = 'log_file.out'
    output_file_handler = logging.FileHandler(logging_file_path)
    stdout_handler = logging.StreamHandler(sys.stdout)

    _logger.addHandler(output_file_handler)
    _logger.addHandler(stdout_handler)
    running_method_settings = (f"running with settings:\n" 
                      f"dataset=            {args.dataset}\n" 
                      f"layer_size=         {args.layer_size}\n"
                      f"batch_size=         {args.batch_size}\n"
                      f"alg_type=           {args.alg_type}\n"
                      f"node_dropout_flag=  {args.node_dropout_flag}\n"
                      f"node_dropout=       {args.node_dropout}\n"
                      f"Ks=                 {args.Ks}\n"
                      f"alpha_k=            {args.alpha_k}\n"
                      f"evaluation=         {args.evaluation}\n")

    _logger.debug(running_method_settings)
    config = dict()
    config['n_users'] = data_generator.n_users
    config['n_items'] = data_generator.n_items
    config['n_cat'] = data_generator.n_cat
    config['n_price'] = data_generator.n_price

    """
    *********************************************************
    Generate the Laplacian matrix, where each entry defines the decay factor (e.g., p_ui) between two connected nodes.
    """
    plain_adj, norm_adj, mean_adj,pre_adj, adj_with_cp, node_dim = data_generator.get_adj_mat()

    config['node_dim'] = node_dim
    if args.adj_type == 'plain':
        config['norm_adj'] = plain_adj
        _logger.debug('use the plain adjacency matrix')
    elif args.adj_type == 'adj_with_cp':
        config['norm_adj'] = plain_adj
        config['cat_and_price_adj'] = adj_with_cp
        _logger.debug('use the adjacency matrix with categories and price')
    elif args.adj_type == 'norm': 
        # This is the adjacency matrix with self loops.
        # Skal ikke bruges af LightGCN.
        config['norm_adj'] = norm_adj
        _logger.debug('use the normalized adjacency matrix')
    elif args.adj_type == 'gcmc':
        config['norm_adj'] = mean_adj
        _logger.debug('use the gcmc adjacency matrix')
    elif args.adj_type=='pre':
        config['norm_adj']=pre_adj
        _logger.debug('use the pre adjacency matrix')
    else:
        config['norm_adj'] = mean_adj + sp.eye(mean_adj.shape[0])
        _logger.debug('use the mean adjacency matrix')
    t0 = time()
    if args.pretrain == -1:
        pretrain_data = load_pretrained_data()
    else:
        pretrain_data = None
    model = LightGCN(data_config=config, pretrain_data=pretrain_data)
    
    """
    *********************************************************
    Save the model parameters.
    """
    saver = tf.train.Saver()

    if args.save_flag == 1:
        layer = '-'.join([str(l) for l in eval(args.layer_size)])
        weights_save_path = '%sweights/%s/%s/%s/l%s_r%s' % (args.weights_path, args.dataset, model.model_type, layer,
                                                            str(args.lr), '-'.join([str(r) for r in eval(args.regs)]))
        ensureDir(weights_save_path)
        save_saver = tf.train.Saver(max_to_keep=1)

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    sess = tf.Session(config=config)

    """
    *********************************************************
    Reload the pretrained model parameters.
    """
    if args.pretrain == 1:
        layer = '-'.join([str(l) for l in eval(args.layer_size)])

        pretrain_path = '%sweights/%s/%s/%s/l%s_r%s' % (args.weights_path, args.dataset, model.model_type, layer,
                                                        str(args.lr), '-'.join([str(r) for r in eval(args.regs)]))


        ckpt = tf.train.get_checkpoint_state(os.path.dirname(pretrain_path + '/checkpoint'))
        if ckpt and ckpt.model_checkpoint_path:
            sess.run(tf.global_variables_initializer())
            saver.restore(sess, ckpt.model_checkpoint_path)
            _logger.debug('load the pretrained model parameters from: ', pretrain_path)

            # *********************************************************
            # get the performance from pretrained model.
            if args.report != 1:
                users_to_test = list(data_generator.test_set.keys())
                ret = test(sess, model, users_to_test, drop_flag=True)
                cur_best_pre_0 = ret['recall'][0]
                
                pretrain_ret = 'pretrained model recall=[%s], precision=[%s], '\
                               'ndcg=[%s]' % \
                               (', '.join(['%.5f' % r for r in ret['recall']]),
                                ', '.join(['%.5f' % r for r in ret['precision']]),
                                ', '.join(['%.5f' % r for r in ret['ndcg']]))
                _logger.debug(pretrain_ret)
        else:
            sess.run(tf.global_variables_initializer())
            cur_best_pre_0 = 0.
            _logger.debug('without pretraining.')

    else:
        sess.run(tf.global_variables_initializer())
        cur_best_pre_0 = 0.
        _logger.debug('without pretraining.')

    """
    *********************************************************
    Get the performance w.r.t. different sparsity levels.
    """
    if args.report == 1:
        assert args.test_flag == 'full'
        users_to_test_list, split_state = data_generator.get_sparsity_split()
        users_to_test_list.append(list(data_generator.test_set.keys()))
        split_state.append('all')
         
        report_path = '%sreport/%s/%s.result' % (args.proj_path, args.dataset, model.model_type)
        ensureDir(report_path)
        f = open(report_path, 'w')
        f.write(
            'embed_size=%d, lr=%.4f, layer_size=%s, keep_prob=%s, regs=%s, loss_type=%s, adj_type=%s\n'
            % (args.embed_size, args.lr, args.layer_size, args.keep_prob, args.regs, args.loss_type, args.adj_type))

        for i, users_to_test in enumerate(users_to_test_list):
            ret = test(sess, model, users_to_test, drop_flag=True)

            final_perf = "recall=[%s], precision=[%s], ndcg=[%s]" % \
                         (', '.join(['%.5f' % r for r in ret['recall']]),
                          ', '.join(['%.5f' % r for r in ret['precision']]),
                          ', '.join(['%.5f' % r for r in ret['ndcg']]))

            f.write('\t%s\n\t%s\n' % (split_state[i], final_perf))
        f.close()
        exit()

    """
    *********************************************************
    Train.
    """
    tensorboard_model_path = 'tensorboard/'
    if not os.path.exists(tensorboard_model_path):
        os.makedirs(tensorboard_model_path)
    # run_time = 1
    # while (True):
    #     if os.path.exists(tensorboard_model_path + model.log_dir +'/run_' + str(run_time)):
    #         run_time += 1
    #     else:
    #         break
    train_writer_splits = [
        '1-5',
        '6-10',
        '11-15',
        '16-20',
        '21-25',
        '26-30',
        '31-35',
        '36-40',
        '41-45',
        '46-50',    
        '51-60',
        '61-70',
        '71-80',
        '81-90',
        '91-100',
        '101-150',
        '151-200',
        '201-250',
        '251-300',
        '301+'
    ]

    if args.evaluation == 'multiple':
        users_to_test_multiple, train_writers, train_writer_splits = get_multi_split_train_writers(sess, tensorboard_model_path + model.log_dir, train_writer_splits)
    else:
        train_writer = tf.summary.FileWriter(tensorboard_model_path +model.log_dir, sess.graph)
    loss_loger, pre_loger, rec_loger, ndcg_loger, hit_loger = [], [], [], [], []
    stopping_step = 0
    should_stop = False
    
    
    for epoch in range(1, args.epoch + 1):
        t1 = time()
        loss, mf_loss, emb_loss, reg_loss = 0., 0., 0., 0.
        n_batch = data_generator.n_train // args.batch_size + 1
        loss_test,mf_loss_test,emb_loss_test,reg_loss_test=0.,0.,0.,0.
        '''
        *********************************************************
        parallelized sampling
        '''
        sample_last = sample_thread()
        sample_last.start()
        sample_last.join()
        for idx in range(n_batch):
            train_cur = train_thread(model, sess, sample_last)
            sample_next = sample_thread()
            
            train_cur.start()
            sample_next.start()
            
            sample_next.join()
            train_cur.join()
            
            users, pos_items, neg_items = sample_last.data
            _, batch_loss, batch_mf_loss, batch_emb_loss, batch_reg_loss = train_cur.data
            sample_last = sample_next
        
            loss += batch_loss/n_batch
            mf_loss += batch_mf_loss/n_batch
            emb_loss += batch_emb_loss/n_batch
            
        summary_train_loss= sess.run(model.merged_train_loss,
                                      feed_dict={model.train_loss: loss, model.train_mf_loss: mf_loss,
                                                 model.train_emb_loss: emb_loss, model.train_reg_loss: reg_loss})

        if args.evaluation == 'multiple':
            for i in range(len(train_writers)):
                train_writers[i].add_summary(summary_train_loss, epoch)
        else:
            train_writer.add_summary(summary_train_loss, epoch)
        

        if np.isnan(loss) == True:
            _logger.debug('ERROR: loss is nan.')
            sys.exit()
        
        if (epoch % 20) != 0:
            if args.verbose > 0 and epoch % args.verbose == 0:
                perf_str = 'Epoch %d [%.1fs]: train==[%.5f=%.5f + %.5f]' % (
                    epoch, time() - t1, loss, mf_loss, emb_loss)
                _logger.debug(perf_str)
            continue
        if args.evaluation == 'multiple':
            for i in range(len(train_writers)):
                users_to_test = users_to_test_multiple[i]
                ret = test(sess, model, users_to_test ,drop_flag=True,train_set_flag=1)
                perf_str = 'Epoch %d - Split %s: train==[%.5f=%.5f + %.5f + %.5f], recall=[%s], precision=[%s], ndcg=[%s]' % \
                        (epoch, train_writer_splits[i], loss, mf_loss, emb_loss, reg_loss, 
                            ', '.join(['%.5f' % r for r in ret['recall']]),
                            ', '.join(['%.5f' % r for r in ret['precision']]),
                            ', '.join(['%.5f' % r for r in ret['ndcg']]))
                _logger.debug(perf_str)
                summary_train_acc = sess.run(model.merged_train_acc, feed_dict={model.train_rec_first: ret['recall'][0],
                                                                                model.train_rec_last: ret['recall'][-1],
                                                                                model.train_ndcg_first: ret['ndcg'][0],
                                                                                model.train_ndcg_last: ret['ndcg'][-1]})
                train_writers[i].add_summary(summary_train_acc, epoch // 20)
        else:
            users_to_test = list(data_generator.train_items.keys())
            ret = test(sess, model, users_to_test ,drop_flag=True,train_set_flag=1)
            perf_str = 'Epoch %d: train==[%.5f=%.5f + %.5f + %.5f], recall=[%s], precision=[%s], ndcg=[%s]' % \
                    (epoch, loss, mf_loss, emb_loss, reg_loss, 
                        ', '.join(['%.5f' % r for r in ret['recall']]),
                        ', '.join(['%.5f' % r for r in ret['precision']]),
                        ', '.join(['%.5f' % r for r in ret['ndcg']]))
            _logger.debug(perf_str)
            summary_train_acc = sess.run(model.merged_train_acc, feed_dict={model.train_rec_first: ret['recall'][0],
                                                                            model.train_rec_last: ret['recall'][-1],
                                                                            model.train_ndcg_first: ret['ndcg'][0],
                                                                            model.train_ndcg_last: ret['ndcg'][-1]})
            train_writer.add_summary(summary_train_acc, epoch // 20)

        
        '''
        *********************************************************
        parallelized sampling
        '''
        sample_last= sample_thread_test()
        sample_last.start()
        sample_last.join()
        for idx in range(n_batch):
            train_cur = train_thread_test(model, sess, sample_last)
            sample_next = sample_thread_test()
            
            train_cur.start()
            sample_next.start()
            
            sample_next.join()
            train_cur.join()
            
            users, pos_items, neg_items = sample_last.data
            batch_loss_test, batch_mf_loss_test, batch_emb_loss_test = train_cur.data
            sample_last = sample_next
            
            loss_test += batch_loss_test / n_batch
            mf_loss_test += batch_mf_loss_test / n_batch
            emb_loss_test += batch_emb_loss_test / n_batch
            
        summary_test_loss = sess.run(model.merged_test_loss,
                                     feed_dict={model.test_loss: loss_test, model.test_mf_loss: mf_loss_test,
                                                model.test_emb_loss: emb_loss_test, model.test_reg_loss: reg_loss_test})
        if args.evaluation == 'multiple':
            for i in range(len(train_writers)):
                train_writers[i].add_summary(summary_test_loss, epoch // 20)
        else:
            train_writer.add_summary(summary_test_loss, epoch // 20)
        _logger.debug('\n' + model.log_dir)
        if args.evaluation == 'multiple':
            for i in range(len(train_writers)):
                t2 = time()
                users_to_test = users_to_test_multiple[i]
                ret = test(sess, model, users_to_test, drop_flag=True)
                summary_test_acc = sess.run(model.merged_test_acc,
                                            feed_dict={model.test_rec_first: ret['recall'][0], model.test_rec_last: ret['recall'][-1],
                                                    model.test_ndcg_first: ret['ndcg'][0], model.test_ndcg_last: ret['ndcg'][-1]})
                train_writers[i].add_summary(summary_test_acc, epoch // 20)
                
                                                  
                t3 = time()
                
                loss_loger.append(loss)
                rec_loger.append(ret['recall'])
                pre_loger.append(ret['precision'])
                ndcg_loger.append(ret['ndcg'])

                if args.verbose > 0:
                    perf_str = 'Epoch %d  - Split %s [%.1fs + %.1fs]: test==[%.5f=%.5f + %.5f + %.5f], recall=[%s], ' \
                            'precision=[%s], ndcg=[%s]' % \
                            (epoch, train_writer_splits[i], t2 - t1, t3 - t2, loss_test, mf_loss_test, emb_loss_test, reg_loss_test, 
                                ', '.join(['%.5f' % r for r in ret['recall']]),
                                ', '.join(['%.5f' % r for r in ret['precision']]),
                                ', '.join(['%.5f' % r for r in ret['ndcg']]))
                    _logger.debug(perf_str)
            
            stop_test_time = time()
            
            users_to_test = list(data_generator.test_set.keys())
            ret = test(sess, model, users_to_test, drop_flag=True)

            cur_best_pre_0, stopping_step, should_stop = early_stopping(ret['recall'][0], cur_best_pre_0,
                                                                        stopping_step, expected_order='acc', flag_step=5)
            if args.verbose > 0:
                perf_str = 'Epoch %d - Combined: [%.1fs + %.1fs]: test==[%.5f=%.5f + %.5f + %.5f], recall=[%s], ' \
                        'precision=[%s], ndcg=[%s]' % \
                        (epoch, t2 - t1, t3 - t2, loss_test, mf_loss_test, emb_loss_test, reg_loss_test, 
                            ', '.join(['%.5f' % r for r in ret['recall']]),
                            ', '.join(['%.5f' % r for r in ret['precision']]),
                            ', '.join(['%.5f' % r for r in ret['ndcg']]))
                _logger.debug(perf_str)

            after_test_time = time()
            # *********************************************************
            # early stopping when cur_best_pre_0 is decreasing for ten successive steps.
            _logger.debug('early stopping test took: %.1f seconds' % (after_test_time - stop_test_time))
            if should_stop == True:
                break

            # *********************************************************
            # save the user & item embeddings for pretraining.
            if ret['recall'][0] == cur_best_pre_0 and args.save_flag == 1:
                save_saver.save(sess, weights_save_path + '/weights', global_step=epoch)
                _logger.debug('save the weights in path: ', weights_save_path)
        else:    
            t2 = time()

            users_to_test = list(data_generator.test_set.keys())
            ret = test(sess, model, users_to_test, drop_flag=True)
            summary_test_acc = sess.run(model.merged_test_acc,
                                        feed_dict={model.test_rec_first: ret['recall'][0], model.test_rec_last: ret['recall'][-1],
                                                model.test_ndcg_first: ret['ndcg'][0], model.test_ndcg_last: ret['ndcg'][-1]})
            train_writer.add_summary(summary_test_acc, epoch // 20)
                                                                                                    
            t3 = time()
            
            loss_loger.append(loss)
            rec_loger.append(ret['recall'])
            pre_loger.append(ret['precision'])
            ndcg_loger.append(ret['ndcg'])

            if args.verbose > 0:
                perf_str = 'Epoch %d [%.1fs + %.1fs]: test==[%.5f=%.5f + %.5f + %.5f], recall=[%s], ' \
                        'precision=[%s], ndcg=[%s]' % \
                        (epoch, t2 - t1, t3 - t2, loss_test, mf_loss_test, emb_loss_test, reg_loss_test, 
                            ', '.join(['%.5f' % r for r in ret['recall']]),
                            ', '.join(['%.5f' % r for r in ret['precision']]),
                            ', '.join(['%.5f' % r for r in ret['ndcg']]))
                _logger.debug(perf_str)
                
            cur_best_pre_0, stopping_step, should_stop = early_stopping(ret['recall'][0], cur_best_pre_0,
                                                                        stopping_step, expected_order='acc', flag_step=5)

            # *********************************************************
            # early stopping when cur_best_pre_0 is decreasing for ten successive steps.
            if should_stop == True:
                break

            # *********************************************************
            # save the user & item embeddings for pretraining.
            if ret['recall'][0] == cur_best_pre_0 and args.save_flag == 1:
                save_saver.save(sess, weights_save_path + '/weights', global_step=epoch)
                _logger.debug('save the weights in path: ', weights_save_path)
    recs = np.array(rec_loger)
    pres = np.array(pre_loger)
    ndcgs = np.array(ndcg_loger)

    best_rec_0 = max(recs[:, 0])
    idx = list(recs[:, 0]).index(best_rec_0)

    final_perf = "Best Iter=[%d]@[%.1f]\trecall=[%s], precision=[%s], ndcg=[%s]" % \
                 (idx, time() - t0, '\t'.join(['%.5f' % r for r in recs[idx]]),
                  '\t'.join(['%.5f' % r for r in pres[idx]]),
                  '\t'.join(['%.5f' % r for r in ndcgs[idx]]))
    _logger.debug(final_perf)

    save_path = '%soutput/%s/%s.result' % (args.proj_path, args.dataset, model.model_type)
    ensureDir(save_path)
    f = open(save_path, 'a')

    f.write(
        'embed_size=%d, lr=%.4f, layer_size=%s, node_dropout=%s, mess_dropout=%s, regs=%s, adj_type=%s\n\t%s\n'
        % (args.embed_size, args.lr, args.layer_size, args.node_dropout, args.mess_dropout, args.regs,
           args.adj_type, final_perf))
    f.close()
