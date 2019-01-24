"""========================================================================
This file inherits the encoder, decoder, and the channel modules used for joint source-channel coding of text data. Also incldues the system and the train file.
It expands the channel coding aspect of the network by passing the outputs of the recurrent nets in the JSC architecture through a multi-layered residual network.


@author: Nariman Farsad, Milind Rao (original authors) and Avoy Datta (channel coding aspect)
@copyright: Copyright 2018
========================================================================"""
import os
import tensorflow as tf
print(tf.__version__)
import numpy as np
import pickle
import time
#from tensorflow.python.layers import core as layers_core
#from tensorflow.contrib.seq2seq import ScheduledEmbeddingTrainingHelper
#from tensorflow.contrib.seq2seq import AttentionWrapper, AttentionWrapperState
#from tensorflow.python.ops import random_ops
#from tensorflow.python.ops import array_ops
#from tensorflow.python.ops import math_ops
#from tensorflow.python.ops import control_flow_ops
#from tensorflow.python.ops import check_ops
#from tensorflow.python.framework import ops
from tensorflow.python.framework import dtypes
from functools import partial
from preprocess_library import SentenceBatchGenerator, Word2Numb, bin_batch_create
import bisect
from tqdm import tqdm
import argparse
from threading import Thread


#Sets which GPU to use
gpu_num = 1
os.environ["CUDA_VISIBLE_DEVICES"]=str(gpu_num)

class Config(object):
    """The model configuration
    """

    PAD = 0
    EOS = 1
    SOS = 2
    UNK = 3

    def __init__(self,
                 src_model_path = None,
                 chan_model_path = None,
                 full_model_path = None,
                 channel={'type':'erasure','chan_param':0.90},
                 numb_epochs=10,
                 lr=0.001,
                 numb_tx_bits=400, # Length of output of old source encoder (after scale down) 
                 
                 chan_code_rate = 0.90,
                 enc_hidden_act = tf.nn.relu,
                 dec_hidden_act = tf.nn.relu, 
                 
                 load_mech = None, #Mechanism with which to load data. Set from input optional arguments at runtime 
                    
                 vocab_size=19158, # including special <PAD> <EOS> <SOS> <UNK>
                 embedding_size=200,
                 enc_hidden_units=256,
                 numb_enc_layers=2,
                 numb_dec_layers=2,
                 batch_size=512,
                 batch_size_test = 128,
                 length_from=4,
                 length_to=30,
                 bin_len = 4,
                 bits_per_bin = [200,250,300,350,400,450,500],
                 variable_encoding = None,
                 deep_encoding = False, #Never works
                 deep_encoding_params = [1000,800,600],
                 peephole = True,
                 dataset = 'euro',
                 w2n_path="../data/w2n_n2w_TopEuro.pickle",
#                 traindata_path="../../data/training_euro_wordlist20.pickle",
                 traindata_path="../data/corpora/europarl-v7.en/europarl-v7.en",
#                 testdata_path="../../data/testing_euro_wordlist20.pickle",
                 testdata_path="../data/corpora/europarl-v7.en/europarl-v7.en",
                 embed_path="../data/200_embed_large_TopEuro.pickle",
                 print_every = 1,
                 max_test_counter = int(1e6),
                 max_validate_counter = 10000,
                 max_batch_in_epoch = int(1e9),
                 save_every = int(1e5),
                 summary_every = 20,
                 qcap = 200,

                 chan_enc_layers = [4096, 2048, 1024, 512],
                 chan_dec_layers = [4096, 2048, 1600, 1024, 512],

                 **kwargs):
        """
        Args:
            src_model_path - path where the source encoder model is saved (trained on its own)
            chan_model_path - path where the channel coder network (pre-trained) is saved
            full_model_path - path where the entire network is saved (both parts run together)
            channel - dict with keys type [erasure,awgn] and chan_param
            bits_per_bin - ensure this is even and of the same size as the number of batches 
        """
        if isinstance(chan_enc_layers, list):
            self.enc_layers_dims = chan_enc_layers
            self.num_chan_enc_layers = len(chan_enc_layers)
        else:
            self.num_enc_layers = chan_enc_layers
            self.enc_layers_dims = None

        if isinstance(chan_dec_layers, list):
            self.dec_layers_dims = chan_dec_layers
            self.num_chan_dec_layers = len(chan_dec_layers)
        else:
            self.num_dec_layers = chan_dec_layers
            self.dec_layers_dims = None

        self.mod_chan_coding = True #Set in main func, but true by default
        
        self.load_mech = load_mech        

        self.enc_hidden_act = enc_hidden_act
        self.dec_hidden_act = dec_hidden_act
        self.src_model_path = src_model_path
        self.chan_model_path = chan_model_path
        self.full_model_path = full_model_path
        self.model_save_path = full_model_path #Default path where trained models are saved. Also set manually at main function(switched to src_model_path if src coder trained alone).

        self.epochs = numb_epochs
        self.lr = lr
        self.vocab_size = vocab_size
        self.embedding_size = embedding_size # length of embeddings
        self.enc_hidden_units = enc_hidden_units
        self.numb_enc_layers = numb_enc_layers
        self.numb_dec_layers = numb_dec_layers
        self.dec_hidden_units = enc_hidden_units * 2
        #batch properties
        self.batch_size = batch_size
        self.batch_size_test = batch_size_test
        self.length_from = length_from
        self.length_to = length_to
        self.bin_len = bin_len
        if not bits_per_bin:
            self.bits_per_bin = [numb_tx_bits for _ in range(length_from,length_to,bin_len)] 
        else:
            self.bits_per_bin = bits_per_bin
        self.variable_encoding = None #variable_encoding
        self.deep_encoding = deep_encoding
        self.deep_encoding_params = deep_encoding_params
        
        self.peephole = peephole
        self.channel = channel
        self.numb_tx_bits = numb_tx_bits
        self.chan_code_rate = chan_code_rate

        self.num_chan_bits = np.ceil(numb_tx_bits / chan_code_rate)

        self.w2n_path = w2n_path
        self.traindata_path = traindata_path
        self.testdata_path = testdata_path
        self.embed_path = embed_path
        self.dataset=dataset
        
        self.queue_limits = list(range(length_from,length_to,bin_len))
        self.print_every = print_every
        self.max_test_counter = max_test_counter
        self.max_batch_in_epoch = max_batch_in_epoch
        self.save_every = save_every
        self.summary_every = summary_every
        self.max_validate_counter = max_validate_counter
        
        self.qcap = qcap
        self.kwargs=kwargs
     
def generate_tb_filename(config):
    """Generate the file name for the model
    """
    tb_name = ""
    if config.channel["type"] == "none":
        tb_name = "Ch-" + config.channel["type"]
    else:
        tb_name = "Ch-" + config.channel["type"] + '{:0.2f}'.format(config.channel["chan_param"])
    
    if config.channel["chan_param_max"] is not None:
        print("Variable channel params in effect.")
        tb_name += "-var-"

    tb_name +=  "-txbits-" + str(config.numb_tx_bits) #+ "-voc-" + str(config.vocab_size)
    if config.deep_encoding:
        tb_name+= '-de'
        
    if config.variable_encoding:
        tb_name += ('-var2' if config.variable_encoding==2 else '-var')
        tb_name+='-{}-{}'.format(config.kwargs.get('bits_per_bin_gen',['const'])[0],
                                  config.kwargs.get('bits_per_bin_gen',[None,None])[1])
    tb_name+='-'+config.dataset+'-'
    return tb_name



class Embedding(object):
    """The word embeddings used in the encoder and decoder
    """
    def __init__(self,config):
        self.vocab_size = config.vocab_size
        self.embedding_size = config.embedding_size
        if config.embed_path == None:
            self.embeddings = tf.Variable(tf.random_uniform([self.vocab_size, self.embedding_size], -1.0, 1.0),
                                                            dtype=tf.float32)
        else:
            with open(config.embed_path, 'rb') as fop:
                embeddings = pickle.load(fop)
                self.embeddings = tf.Variable(embeddings, dtype=tf.float32)
        self.curr_embeds = None

    def get_embeddings(self,inputs):    # this thing could get huge in a real world application
        self.curr_embeds = tf.nn.embedding_lookup(self.embeddings, inputs)
        return self.curr_embeds


class VariableEncoder(object):
    """A variable encoder that includes binarization
    """
    def __init__(self,encoder_input, encoder_input_len, batch_id,isTrain, embedding,config):
        """ 
        Args:
            encoder_input - the sentences (padded) that are the inputs to the encoder unit
            encoder_input_len - the length of the sentences
            batch_id - For variable length, each batch of a different size is 
             mapped to a sentence of a different length. This gives the id of the batch
            isTrain - whether it is the training operation for binarization. 
            embedding - the embedding matrix. 
            config - includes configuration of length of encoding for each batch_id
        """
        self.enc_input = encoder_input
        self.enc_input_len = encoder_input_len
        self.numb_enc_layers = config.numb_enc_layers
        self.enc_hidden_units = config.enc_hidden_units
        self.embedding = embedding
        self.peephole = config.peephole
        self.batch_size = config.batch_size
        self.isTrain = isTrain
        self.batch_id = batch_id 
        self.config = config
        
        self.enc_state_c, self.enc_state_h = self.build_enc_network() #Only handles source coding aspect

        if (self.config.mod_chan_coding == False):
            self.state_bits = self.reduce_size_and_binarize()
            self.enc_output = self.state_bits #renaming for use
            print('enc_state_c',self.enc_state_c,'state_bits',self.state_bits)
        else:
            print('enc_state_c',self.enc_state_c)
        
    def build_enc_network(self):
        """Build the LSTM encoder
        """
        embedded = self.embedding.get_embeddings(self.enc_input)

        lstm_fw_cells = [tf.contrib.rnn.LSTMCell(num_units=self.enc_hidden_units,
                                                 use_peepholes=self.peephole,
                                                 state_is_tuple=True,
                                                 name = "src_enc_LSTM_fw_" + str(i + 1))
                         for i in range(self.numb_enc_layers)]
        lstm_bw_cells = [tf.contrib.rnn.LSTMCell(num_units=self.enc_hidden_units,
                                                 use_peepholes=self.peephole,
                                                 state_is_tuple=True,
                                                 name = "src_enc_LSTM_bw_" + str(i + 1))
                         for i in range(self.numb_enc_layers)]

        (_,encoder_fw_final_state,
          encoder_bw_final_state) = tf.contrib.rnn.stack_bidirectional_dynamic_rnn(cells_fw=lstm_fw_cells,
                                                                                    cells_bw=lstm_bw_cells,
                                                                                    inputs=embedded,
                                                                                    dtype=tf.float32,
                                                                                    sequence_length=self.enc_input_len)
      
        encoder_fw_final_state_com = tf.concat(encoder_fw_final_state,axis=-1)
        encoder_bw_final_state_com = tf.concat(encoder_bw_final_state,axis=-1)
        encoder_final_state = tf.concat([encoder_fw_final_state_com,encoder_bw_final_state_com],axis=-1) 
            # layers x 2[c,h] x bat x emb_size.2
        
        [encoder_final_state_c,encoder_final_state_h] = tf.unstack(encoder_final_state,axis=0)
        return (encoder_final_state_c, encoder_final_state_h)
    
    def training_binarizer(self, input_layer):
        """Binarizer function used at training
        """
        prob = tf.truediv(tf.add(1.0, input_layer), 2.0)
        bernoulli = tf.contrib.distributions.Bernoulli(probs=prob, dtype=tf.float32)
        return 2 * bernoulli.sample() - 1

    def test_binarizer(self, input_layer):
        """Binarizer function used during testing
        """
        ones = tf.ones_like(input_layer,dtype=tf.float32)
        neg_ones = tf.scalar_mul(-1.0, ones)
        return tf.where(tf.less(input_layer,0.0), neg_ones, ones)

    def binarize(self,input_layer):
        """This part of the code binarizes the reduced states. The last line ensure the
        backpropagation gradients pass through the binarizer unchanged
        """
        binarized = tf.cond(self.isTrain,
                            partial(self.training_binarizer, input_layer),
                            partial(self.test_binarizer, input_layer))

        pass_through = tf.identity(input_layer) # this is used for pass through gradient back prop
        return pass_through + tf.stop_gradient(binarized - pass_through )
    
    def scale_down(self,input_layer,output_dim, name='',**kwargs):
        
        scaled_down_pre = tf.layers.dense(input_layer, 
                                          output_dim,
                                          activation = tf.nn.tanh,
                                          kernel_initializer = tf.contrib.layers.xavier_initializer(),
                                          name = 'src_'+name)
       
        scaled_down_bin = self.binarize(scaled_down_pre)
        if self.config.variable_encoding == 1: #Pad to the size of the maximum 
            paddings = tf.constant([[0,0],[0,int(self.config.bits_per_bin[-1]-output_dim)]])
            scaled_down_pad = tf.pad(scaled_down_bin,paddings,'CONSTANT')
        elif self.config.variable_encoding == 2: #use only 1 matrix
            paddings = tf.constant([[0,0],[0,int(self.config.bits_per_bin[-1]-kwargs['enc_dim'])]])
            scaled_down_sel = tf.slice(scaled_down_bin,[0,0],[-1,kwargs['enc_dim']])
            scaled_down_pad = tf.pad(scaled_down_sel,paddings,'CONSTANT')            
        elif self.config.variable_encoding is None: #Constant embeddings
            scaled_down_pad = scaled_down_bin
        return scaled_down_pad


        # W = tf.get_variable('scale_W_'+name,
        #                       shape=[input_dim,output_dim],
        #                       dtype=tf.float32,
        #                       initializer=tf.contrib.layers.xavier_initializer())
        # b = tf.get_variable('scale_b_'+name,
        #                       shape=[1,output_dim],
        #                       dtype = tf.float32,
        #                       initializer=tf.zeros_initializer())
        
        # scaled_down_pre = tf.nn.tanh(tf.matmul(input_layer,W) + b,name='tanh_'+name)
                          
    def reduce_size_and_binarize (self):
        """reduces the size of the state according to the
        number of bits and binarizes
        """
        enc_state_conc = tf.concat([self.enc_state_c,self.enc_state_h],axis=1) # bat x hidden_u . 2[fw,bw] . 2 [c,h] . enc_layers
        enc_state_conc_dim = self.enc_hidden_units*self.numb_enc_layers*4

        enc_state = enc_state_conc
        enc_state_dim = enc_state_conc_dim
            
        if self.config.variable_encoding==1:
            dict_pred_fun_state = {}
            state_enc_bat = {}
            for ind,_ in enumerate(self.config.queue_limits):
                
                state_enc_bat[ind] = self.scale_down(
                        enc_state,
                        enc_state_dim,self.config.bits_per_bin[ind],
                        name="enc_state_to_bits_bat_%d"%ind)
                dict_pred_fun_state[tf.equal(self.batch_id,ind)] = lambda ind_=ind: state_enc_bat[ind_]
                    
            print('encoder compressor',state_enc_bat)       
            def_op = lambda: tf.zeros([],dtype=tf.float32)
            self.state_reduc = tf.case(dict_pred_fun_state,default=def_op,exclusive=True,name='enc_state_to_bits_var')
        
        elif self.config.variable_encoding == 2:
            dict_pred_fun_state = {}
            state_enc_bat = {}
            with tf.variable_scope('variable_enc_const') as scope:
                for ind,_ in enumerate(self.config.queue_limits):
                    state_enc_bat[ind] = self.scale_down(enc_state,
                            enc_state_dim,self.config.bits_per_bin[-1],
                            enc_dim = self.config.bits_per_bin[ind])
                    dict_pred_fun_state[tf.equal(self.batch_id,ind)] = lambda ind_=ind: state_enc_bat[ind_]
                    scope.reuse_variables()
                    
            print('encoder compressor',state_enc_bat)       
            def_op = lambda: tf.zeros([],dtype=tf.float32)
            self.state_reduc = tf.case(dict_pred_fun_state,default=def_op,exclusive=True,name='enc_state_to_bits_var')
        
        elif self.config.variable_encoding is None:

            self.state_reduc = self.scale_down(enc_state,
                                               self.config.numb_tx_bits,
                                               name="enc_state_to_bits_fix")


        return self.state_reduc

"""
Modified VariableEncoder to accomodate the new channel coding mechanism.
The namespace for the variables from the old network must match the new.
"""
class VariableEncoder_mod(VariableEncoder):

    def __init__(self,encoder_input, encoder_input_len, batch_id,isTrain, embedding,config):

        #VariableEncoder.__init__(encoder_input, encoder_input_len, batch_id,isTrain, embedding,config)

        super().__init__(encoder_input, encoder_input_len, batch_id,isTrain, embedding, config) #Initializes parent
        print("States before redux:", self.enc_state_c, self.enc_state_h)
        self.input = self.mod_reduce_size()
        print("States after reduction: ", self.input)
        self.state_bits = self.binarize(self.input) #Done for the sake of tf.summary
        self.input_len = config.numb_tx_bits
        self.batch_size = config.batch_size
        self.isTrain = isTrain
        
        self.reduced_states = self.build_chan_enc_net()
        print("reduced states:", self.reduced_states)
        self.enc_output = self.binarize(self.reduced_states)
        
        print("Output from channel encoder: ", self.enc_output)

    #Modified version of scale_down. Doesn't binarize at the end
    def scale_down_to_real(self,input_layer,output_dim, name='',**kwargs):
        scaled_down_pre = tf.layers.dense(input_layer, 
                                          output_dim,
                                          activation = tf.nn.tanh,
                                          kernel_initializer = tf.contrib.layers.xavier_initializer(),
                                          name = name)

        if self.config.variable_encoding == 1: #Pad to the size of the maximum 
            paddings = tf.constant([[0,0],[0,int(self.config.bits_per_bin[-1]-output_dim)]])
            scaled_down_pad = tf.pad(scaled_down_pre,paddings,'CONSTANT')
        elif self.config.variable_encoding == 2: #use only 1 matrix
            paddings = tf.constant([[0,0],[0,int(self.config.bits_per_bin[-1]-kwargs['enc_dim'])]])
            scaled_down_sel = tf.slice(scaled_down_pre,[0,0],[-1,kwargs['enc_dim']])
            scaled_down_pad = tf.pad(scaled_down_sel,paddings,'CONSTANT')    

        elif self.config.variable_encoding is None: #Constant embeddings
            scaled_down_pad = scaled_down_pre
        return scaled_down_pad

    #Reduces size of encoded states from parent class
    def mod_reduce_size(self):
        """reduces the size of the state according to the
        number of bits and binarizes
        """
        enc_state_conc = tf.concat([self.enc_state_c,self.enc_state_h],axis=1) # bat x hidden_u . 2[fw,bw] . 2 [c,h] . enc_layers
        enc_state_conc_dim = self.enc_hidden_units*self.numb_enc_layers*4

        enc_state = enc_state_conc
        enc_state_dim = enc_state_conc_dim
            
        if self.config.variable_encoding==1:
            dict_pred_fun_state = {}
            state_enc_bat = {}
            for ind,_ in enumerate(self.config.queue_limits):
                
                state_enc_bat[ind] = self.scale_down(
                        enc_state,
                        enc_state_dim,self.config.bits_per_bin[ind],
                        name="enc_state_to_bits_bat_%d"%ind)
                dict_pred_fun_state[tf.equal(self.batch_id,ind)] = lambda ind_=ind: state_enc_bat[ind_]
                    
            print('encoder compressor',state_enc_bat)       
            def_op = lambda: tf.zeros([],dtype=tf.float32)
            self.state_reduc = tf.case(dict_pred_fun_state,default=def_op,exclusive=True,name='enc_state_to_bits_var')
        
        elif self.config.variable_encoding == 2:
            dict_pred_fun_state = {}
            state_enc_bat = {}
            with tf.variable_scope('variable_enc_const') as scope:
                for ind,_ in enumerate(self.config.queue_limits):
                    state_enc_bat[ind] = self.scale_down(enc_state,
                            enc_state_dim,self.config.bits_per_bin[-1],
                            enc_dim = self.config.bits_per_bin[ind])
                    dict_pred_fun_state[tf.equal(self.batch_id,ind)] = lambda ind_=ind: state_enc_bat[ind_]
                    scope.reuse_variables()
                    
            print('encoder compressor',state_enc_bat)       
            def_op = lambda: tf.zeros([],dtype=tf.float32)
            self.state_reduc = tf.case(dict_pred_fun_state,default=def_op,exclusive=True,name='src_enc_state_to_bits_var')
        
        elif self.config.variable_encoding is None:
            print("States before scale down: ", enc_state)
            self.state_reduc = self.scale_down_to_real(enc_state,
                                                       self.config.numb_tx_bits,
                                                       name="src_enc_state_to_bits_fix") #Same name used as parent class   
            
        
        print("After scale down: ", self.state_reduc)
        return self.state_reduc        

    def get_net_dims(self, n_in, n_out):
        
        if (self.config.enc_layers_dims != None):
            return self.config.enc_layers_dims
        
        num_hidden_layers = self.config.num_chan_enc_layers #num_hidden >= 2
        hidden_dims = []
        
        """
        Uses ideas from D.Stathakis' paper to determine the number of neurons for first 2 hidden layers
        """
        n_h1 = int(math.sqrt((n_out + 2) * n_in) + 2 * math.sqrt(n_in / (n_out + 2)))
        n_h2 = int(n_out * math.sqrt(n_in / (n_out + 2)))

        hidden_dims.append(n_h1)
        if (self.config.num_enc_layers > 1):
            hidden_dims.append(n_h2)
        
        #For remaining layers
        
        for _ in range(num_hidden_layers - 2):
            
            hidden_dims.append(int(hidden_dims[-1] * 0.8))
            
        
        return hidden_dims
    
    def build_chan_enc_net(self):
        """
        Builds the channel encoder
        """
        print(self.input_len, self.config.numb_tx_bits)
        hidden_dims = self.get_net_dims(self.input_len, self.config.numb_tx_bits) #length >= 1
   
        print("Hidden encoder dimensions: ", hidden_dims)

        hidden_layers = [] #list of tensors for storing activations from each hidden layer
        
        print("Input dims:", self.input)
        
        hidden_act1 = tf.layers.dense(self.input, hidden_dims[0], 
                                      activation = self.config.enc_hidden_act,
                                      kernel_initializer = tf.contrib.layers.xavier_initializer(),
                                      name = "chan_enc_h1") 
        
        print("hidden1: ", hidden_act1)
        hidden_layers.append(hidden_act1)
        
        for i in range(1, len(hidden_dims)):
        
#             hidden_act = tf.contrib.layers.fully_connected(hidden_layers[-1], 
#                                                            hidden_dims[i], 
#                                                            activation_fn = self.config.enc_hidden_act) 
            hidden_act = tf.layers.dense(hidden_layers[-1], hidden_dims[i], 
                                         activation = self.config.enc_hidden_act,
                                         kernel_initializer = tf.contrib.layers.xavier_initializer(),
                                         name = "chan_enc_h" + str(i + 1)) 
            hidden_layers.append(hidden_act)
        
        if (len(hidden_dims) > 1):
            #Skip connection between h1 and output feed
            output_feed = tf.concat([hidden_act1, hidden_layers[-1]], axis = 1)
            print("Encoder skip test:", output_feed)
        else:
            output_feed = hidden_layers[-1]
            
        b_norm_output_feed = tf.contrib.layers.batch_norm(output_feed)
        
        out_states = tf.layers.dense(b_norm_output_feed, self.config.num_chan_bits, activation = tf.tanh,
                                     #kernel_initializer = tf.contrib.layers.xavier_initializer(),
                                     name = "chan_enc_out") 
        return out_states
        

class Channel(object):
    """The binarization layer of the encoder plus the channel model.
       Currently the channel model is either error free, erasure channel,or is
       the AWGN channel.
    """
    def __init__(self, channel_in, chan_param, config):
        self.channel_in = channel_in
        self.numb_dec_layers = config.numb_dec_layers
        self.config = config
        self.channel = config.channel
        self.chan_param = chan_param
        self.channel_out = self.build_channel()
        print("chan out", self.channel_out)


    def gaussian_noise_layer(self, input_layer, std, name=None):
        noise = tf.random_normal(shape=tf.shape(input_layer), mean=0.0,
                                 stddev=std, dtype=tf.float32, name=name)
        return input_layer + noise

    def test_binarizer(self, input_layer):
        """Binarizer function used during testing
        """
        ones = tf.ones_like(input_layer,dtype=tf.float32)
        neg_ones = tf.scalar_mul(-1.0, ones)
        return tf.where(tf.less(input_layer,0.0), neg_ones, ones)
    
    def build_channel(self):
        """Build the final binarization layer and the channel
        """
        # if no channel, just output the encoder states
        if self.channel['type'] == "none":
            chan_output = self.channel_in
        elif self.channel['type'] == "erasure":
            chan_output = tf.nn.dropout(self.channel_in,
                                         keep_prob=self.chan_param,
                                         name="erasure_chan_dropout_ch")
                            
        elif self.channel['type'] == "awgn":
            chan_output = self.gaussian_noise_layer(self.channel_in,
                                             std=self.chan_param,
                                             name="awgn_chan_noise")
            
        elif self.channel['type'] == 'bsc':
            
#            chan_out_g = self.gaussian_noise_layer(self.channel_in,
#                                             std=self.chan_param,
#                                             name="bsc_chan_noise")
            chan_output = tf.where(tf.greater(tf.random_uniform(shape=tf.shape(self.channel_in)),
                                              self.chan_param),
                                   self.channel_in,
                                   -self.channel_in)
            
        else:
            raise NameError('Channel type is not known.')

        return chan_output



class VariableDecoder(object):
    '''This is a simple decoder that does not use attention and uses raw_rnn for decoding.
    During training the the estimated bit is fed-back as the next input. Crucially
    different channel_outputs result in different length embeddings
    '''
    def __init__(self, enc_inputs, encoder_input_len, chan_output, embeddings,
                 batch_id,dec_inputs, prob_corr_input, config):
        
        
        self.enc_input = enc_inputs
        self.enc_input_len = encoder_input_len
        self.batch_id = batch_id
        self.decoder_lengths = self.enc_input_len + 3
        self.batch_size = config.batch_size
        self.peephole = config.peephole
        self.prob_corr_input = prob_corr_input
        self.decoder_input = dec_inputs
        self.numb_dec_layers = config.numb_dec_layers
        self.dec_hidden_units = config.dec_hidden_units
        self.vocab_size = config.vocab_size
        self.config = config
        self.init_state = self.expand_chann_out(chan_output)

        self.eos_step_embedded = None
        self.sos_step_embedded = None
        self.pad_step_embedded = None
        self.embeddings = embeddings

        # weights and bias for output projection
        self.W = tf.Variable(tf.random_uniform([self.dec_hidden_units, self.vocab_size], -1, 1), dtype=tf.float32, name = "src_dec_final_W")
        self.b = tf.Variable(tf.zeros([self.vocab_size]), dtype=tf.float32, name = "src_dec_final_b")

        self.dec_logits, self.dec_pred = self.build_dec_network
        
    def scale_up(self,input_layer,input_dim,output_dim,name=None):
        """ Takes an encoding number of bits and scales it up to create an init 
        state for the decoder
        """

        # layers_pair = [tf.layers.dense(input_layer,  output_dim, activation = tf.nn.tanh, kernel_initializer = tf.contrib.layers.xavier_initializer()) 
        #                 for j in range(2)]

        # return layers_pair
        W = [tf.get_variable('{}_scale_W_{}'.format(name, j),
                              shape=[input_dim,output_dim],
                              dtype=tf.float32,
                              initializer=tf.contrib.layers.xavier_initializer())
            for j in range(2)]
        if self.config.variable_encoding == 1:
            W2 = [tf.concat([W_each,tf.zeros(
                    [self.config.bits_per_bin[-1]-input_dim,output_dim],tf.float32)],axis=0)
                    for W_each in W]
        else:
            W2 = W
        b = [tf.get_variable('{}_scale_b_{}'.format(name, j),
                              shape=[1,output_dim],
                              dtype = tf.float32,
                              initializer=tf.contrib.layers.xavier_initializer())
            for j in range(2)]
        return [tf.nn.tanh(tf.matmul(input_layer,W2[j]) + b[j],
                           name='{}_tanh_{}'.format(name,j))
                for j in range(2)]
                           

    def expand_chann_out(self, channel_out):
        '''Expand the channel output (first layer of the decoder)
        '''
        self.state_c_out = []
        self.state_h_out = []
        
        
        init_state = []
        bits_with_erasure = channel_out
        bits_with_erasure_dim = self.config.bits_per_bin[-1] if self.config.variable_encoding else self.config.numb_tx_bits
        
           
        #Deep encoding functionality removed from previous version
           
        for i in range(self.numb_dec_layers):
            
            dict_pred_fun_stateCH = {}
            stateCH_dec_bat = {}
            if self.config.variable_encoding==1:                  
                for ind,_ in enumerate(self.config.queue_limits):
                    
                    stateCH_dec_bat[ind] = self.scale_up(
                            bits_with_erasure,
                            self.config.bits_per_bin[ind],self.dec_hidden_units,
                            name="src_dec_state_out_var_L{}_bat{}".format(i,ind))
                    dict_pred_fun_stateCH[tf.equal(self.batch_id,ind)] = lambda ind_=ind: stateCH_dec_bat[ind_]
                    
                def_op = lambda: [tf.zeros_like(x) for x in stateCH_dec_bat[ind]]
                state_c_out,state_h_out = tf.case(dict_pred_fun_stateCH,default=def_op,exclusive=True,
                                                   name="src_dec_var_CH_expand_L{}".format(i))
            elif self.config.variable_encoding == 2:
                state_c_out,state_h_out = self.scale_up(
                            bits_with_erasure,
                            bits_with_erasure_dim,self.dec_hidden_units,
                            name='src_dec_varfix_CH_expand_L{}'.format(i))


            elif self.config.variable_encoding is None:
                state_c_out,state_h_out = self.scale_up(
                            bits_with_erasure,
                            bits_with_erasure_dim,self.dec_hidden_units,
                            name='src_dec_fix_CH_expand_L{}'.format(i))

            
            self.state_c_out.append(state_c_out)
            self.state_h_out.append(state_h_out)
            init_state.append(tf.contrib.rnn.LSTMStateTuple(c=state_c_out, h=state_h_out))
            
        self.state_c_out = tf.concat(self.state_c_out,0)
        self.state_h_out = tf.concat(self.state_h_out,0)
        if self.numb_dec_layers==1:
            return init_state[0]
        else:
            return tuple(init_state)

    @property
    def build_dec_network(self):
        '''Build the decoder network
        '''
        if self.numb_dec_layers == 1:

            cell = tf.contrib.rnn.LSTMCell(num_units=self.dec_hidden_units,
                                       use_peepholes=self.peephole,
                                       state_is_tuple=True,
                                       name = "src_dec_LSTM_cell")
        elif self.numb_dec_layers > 1:
            cells = [tf.contrib.rnn.LSTMCell(num_units=self.dec_hidden_units,
                                             use_peepholes=self.peephole,
                                             state_is_tuple=True,
                                             name = "src_dec_LSTM_cell_" + str(i + 1))
                     for i in range(self.numb_dec_layers)]
            cell = tf.contrib.rnn.MultiRNNCell(cells)

        batch_size, encoder_max_time = tf.unstack(tf.shape(self.enc_input))

        eos_time_slice = tf.ones([batch_size], dtype=tf.int32, name='EOS')
        sos_time_slice = tf.add(tf.ones([batch_size], dtype=tf.int32, name='SOS'), 1)
        pad_time_slice = tf.zeros([batch_size], dtype=tf.int32, name='PAD')

        self.eos_step_embedded = self.embeddings.get_embeddings(eos_time_slice)
        self.sos_step_embedded = self.embeddings.get_embeddings(sos_time_slice)
        self.pad_step_embedded = self.embeddings.get_embeddings(pad_time_slice)

        decoder_outputs_ta, decoder_final_state, _ = tf.nn.raw_rnn(cell, self.loop_fn)

        decoder_outputs = decoder_outputs_ta.stack()
        decoder_outputs = tf.transpose(decoder_outputs, perm=[1, 0, 2])
        print(decoder_outputs)


        # Unpacks the given dimension of a rank-R tensor into rank-(R-1) tensors.
        # reduces dimensionality
        decoder_batch_size, decoder_max_steps, decoder_dim = tf.unstack(tf.shape(decoder_outputs))
        # flattened output tensor
        decoder_outputs_flat = tf.reshape(decoder_outputs, (-1, decoder_dim))
        print(decoder_outputs)

        # pass flattened tensor through decoder
        decoder_logits_flat = tf.add(tf.matmul(decoder_outputs_flat, self.W), self.b)
        print(decoder_logits_flat)

        # prediction vals
        decoder_logits = tf.reshape(decoder_logits_flat, (decoder_batch_size, decoder_max_steps, self.vocab_size))
        print(decoder_logits)

        # final prediction
        decoder_prediction = tf.argmax(decoder_logits, 2)
        print(decoder_prediction)

        return (decoder_logits, decoder_prediction)



    # we define and return these values, no operations occur here
    def loop_fn_initial(self):
        '''
        The initial condition used to setup the decoder loop in raw_rnn
        '''
        initial_elements_finished = (0 >= self.decoder_lengths)  # all False at the initial step
        # The fist input is the start of sentence <sos> special word
        initial_input = self.sos_step_embedded
        # Set the initial cell state
        initial_cell_state = self.init_state
        initial_cell_output = None
        initial_loop_state = None # we don't need to pass any additional information
        return (initial_elements_finished,
                initial_input,
                initial_cell_state,
                initial_cell_output,
                initial_loop_state)


    # attention mechanism --choose which previously generated word token to pass as input in the next timestep
    def loop_fn_transition(self,time, previous_output, previous_state, previous_loop_state):
        '''
        The main body loop function used in the raw_rnn decoder
        '''
        W = self.W
        b = self.b
        embeddings = self.embeddings
        bernoulli = tf.contrib.distributions.Bernoulli(probs=self.prob_corr_input, dtype=tf.float32)
        choose_correct = bernoulli.sample()
        correct_token = self.decoder_input[:, time-1]
        correct_input = embeddings.get_embeddings(correct_token)

        cell_output = previous_output

        print("cell_out!", cell_output)
        def get_next_input():
            # dot product between previous ouput and weights, then + biases
            output_logits = tf.add(tf.matmul(cell_output, W), b)

            # Returns the index with the largest value across axes of a tensor.
            prediction = tf.argmax(output_logits, axis=1)
            # embed prediction for the next input
            next_input = embeddings.get_embeddings(prediction)
            return tf.cond(tf.equal(choose_correct, 1.0), lambda: correct_input, lambda: next_input)

        # defining if corresponding sequence has ended
        elements_finished = (time >= self.decoder_lengths)  # this operation produces boolean tensor of [batch_size]

        # Computes the "logical and" of elements across dimensions of a tensor.
        finished = tf.reduce_all(elements_finished)  # -> boolean scalar
        # Return either fn1() or fn2() based on the boolean predicate pred.
        input = tf.cond(finished, lambda: self.pad_step_embedded, get_next_input)

        state = previous_state
        output = cell_output
        loop_state = None

        return (elements_finished,
                input,
                state,
                output,
                loop_state)

    def loop_fn(self, time, previous_output, previous_state, previous_loop_state):
        '''
        The complete loop function to be used in the raw_rnn decoder
        '''
        if previous_state is None:  # time == 0
            assert previous_output is None and previous_state is None
            return self.loop_fn_initial()
        else:
            return self.loop_fn_transition(time, previous_output, previous_state, previous_loop_state)

class VariableDecoder_mod(VariableDecoder):
    '''Daughter class of the source decoder. Incorporates a neural network to convert the bits transmitted through channel to a state suitable for decoding.
    The parent source decoder is a simple decoder that does not use attention and uses raw_rnn for decoding. During training the the estimated bit is fed-back as the next input. Crucially
    different channel_outputs result in different length embeddings
    '''
    def __init__(self, enc_inputs, encoder_input_len, chan_output, embeddings,
                 batch_id,dec_inputs, prob_corr_input, config, create_parent = True): #dec_inputs are actually the targets
        
         
        self.config = config
        self.chan_dec_input = chan_output
        self.chan_dec_in_len = config.num_chan_bits
        self.output_len = config.numb_tx_bits
        #self.batch_size = config.batch_size
        self.num_hidden_layers = config.num_chan_dec_layers
        #self.isTrain = isTrain
        self.dec_network_out = self.build_chan_dec_net()
      
        if (create_parent == True): #Only false for beam decoder

            super().__init__(enc_inputs, encoder_input_len, self.dec_network_out, embeddings, batch_id, dec_inputs, prob_corr_input, config) #Initializes parent
            #super(),__init__(enc_inputs, encoder_input_len, chan_output, embeddings, batch_id, dec_inputs, prob_corr_input, config)
    def get_net_dims(self, n_in, n_out):
        
        layer_drop_const = 0.95
        
        if (self.config.dec_layers_dims != None):
            return self.config.dec_layers_dims
        
        num_hidden_layers = self.config.num_chan_dec_layers #num_hidden >= 1
        hidden_dims = []
        
        """
        Uses ideas from D.Stathakis' paper to determine the number of neurons for first 2 hidden layers
        """
        n_h1 = int(math.sqrt((n_out + 2) * n_in) + 2 * math.sqrt(n_in / (n_out + 2)))
        n_h2 = int(n_out * math.sqrt(n_in / (n_out + 2)))

        hidden_dims.append(n_h1)
        if (self.config.num_dec_layers > 1):
            hidden_dims.append(n_h2)
        
        #For remaining layers
        
        for _ in range(num_hidden_layers - 2):
            
            hidden_dims.append(int(hidden_dims[-1] * layer_drop_const))
            
        
        return hidden_dims
    
    
    def build_chan_dec_net(self):
        """
        Builds the channel encoder
        """
 
        hidden_dims = self.get_net_dims(self.chan_dec_in_len, self.output_len) 
       
        print("Hidden decoder dimensions: ", hidden_dims)
        hidden_layers = [] #list of tensors for storing activations from each hidden layer
        
        hidden_act1 = tf.layers.dense(self.chan_dec_input, hidden_dims[0], 
                                      activation = self.config.dec_hidden_act, 
                                      kernel_initializer = tf.contrib.layers.xavier_initializer(), 
                                      name = "chan_dec_h1") 

        hidden_layers.append(hidden_act1)
        
        for i in range(1, len(hidden_dims)):
        
            hidden_act = tf.layers.dense(hidden_layers[-1],
                                         hidden_dims[i], 
                                         activation = self.config.dec_hidden_act,
                                         kernel_initializer = tf.contrib.layers.xavier_initializer(),
                                         name = "chan_dec_h" + str(i + 1)) 

            hidden_layers.append(hidden_act)
        
        if (len(hidden_dims) > 1):
            #Skip connection between h1 and output feed
            output_feed = tf.concat([hidden_act1, hidden_layers[-1]], axis = 1)
            print("Channel decoder skip-connection test:", output_feed)
        else:
            output_feed = hidden_layers[-1]    

        out_states = tf.layers.dense(output_feed, 
                                     self.output_len, 
                                     activation = tf.nn.tanh, #Used since hinge loss no longer used
                                     kernel_initializer = tf.contrib.layers.xavier_initializer(),
                                     name = "chan_dec_out")
    
        print("Channel Decoder out: ", out_states)                                               
        return out_states
        
    #Used in decoder only for purposes of beam search
    def test_binarizer(self, input_layer):
        """Binarizer function used during testing
        """
        ones = tf.ones_like(input_layer,dtype=tf.float32)
        neg_ones = tf.scalar_mul(-1.0, ones)
        return tf.where(tf.less(input_layer,0.0), neg_ones, ones)


class VariableSystem(object):
    """This generates an end-to-end model that includes the sentence encoder,
    the channel, and the decoder. It also trains the models. variable length 
    embedding. Also has a queue for rapid processing. 
    """

    def __init__(self, config, train_data, test_data, word2numb, mod_chan_coding):
        self.config = config
        self.training_counter = 1
        self.test_counter = 1
        self.train_data = train_data
        self.test_data = test_data
        self.word2numb = word2numb

        #Stores values of params to be updated with each batch. Starts with default values of these params
        self.updated_params = {'lr':self.config.lr,
                               'chan_param':self.config.channel["chan_param"]}

        # ==== reset graph ====
        tf.reset_default_graph()
        
        # ==== Queue setup ====
        name_dtype_init_shape =[('isTrain', tf.bool,tf.ones([],dtype=tf.bool),[]),
                        ('enc_inputs',tf.int32,tf.zeros((config.batch_size,1),dtype=tf.int32),(config.batch_size, None)),
                        ('enc_inputs_len',tf.int32,tf.zeros((config.batch_size),dtype=tf.int32),(None)),
                        ('dec_inputs',tf.int32,tf.zeros((config.batch_size,1),dtype=tf.int32),(config.batch_size, None)),
                        ('dec_targets',tf.int64,tf.zeros((config.batch_size,1),dtype=tf.int64),(config.batch_size, None)),
                        ('helper_prob',tf.float32,tf.ones([]),[]),
                        ('chan_param',tf.float32,tf.ones([]),[]),
                        ('lr',tf.float32,tf.ones([])*self.config.lr,[]),
                        ('batch_id',tf.int32,tf.zeros([],dtype=tf.int32),[])]
        names_q,dtype_q,init_q,shape_q = zip(*name_dtype_init_shape)
        self.queue_vars = dict((name,tf.placeholder_with_default(init,shape))
                                for name,_,init,shape in name_dtype_init_shape)
        self.queue = tf.RandomShuffleQueue(self.config.qcap,2,dtype_q,names=names_q)
        self.enqueue_op = self.queue.enqueue(self.queue_vars)
        self.close_queue = self.queue.close(cancel_pending_enqueues=True) 
        queue_vars = self.queue.dequeue()     
        self.epochs = 0

            
        # ==== Placeholders ====
        self.isTrain = tf.placeholder_with_default(queue_vars['isTrain'],shape=(), name='isTrain')
        self.enc_inputs = tf.placeholder_with_default(queue_vars['enc_inputs'],shape=(config.batch_size, None), name='encoder_inputs')
        self.enc_inputs_len = tf.placeholder_with_default(queue_vars['enc_inputs_len'],shape=(None,), name='encoder_inputs_length')
        self.dec_inputs = tf.placeholder_with_default(queue_vars['dec_inputs'],shape=(config.batch_size, None), name='decoder_targets')
        self.dec_targets = tf.placeholder_with_default(queue_vars['dec_targets'],shape=(config.batch_size, None), name='decoder_targets')
        self.helper_prob = tf.placeholder_with_default(queue_vars['helper_prob'],shape=[], name='helper_prob')
        self.chan_param = tf.placeholder_with_default(queue_vars['chan_param'],shape=[], name='chan_param')
        self.lr = tf.placeholder_with_default(queue_vars['lr'],shape=[], name='lr')
        self.batch_id = tf.placeholder_with_default(queue_vars['batch_id'],shape=[],name='batch_id')        

#        # ==== Placeholders ====
#        self.isTrain = tf.placeholder(tf.bool, shape=(), name='isTrain')
#        self.enc_inputs = tf.placeholder(shape=(config.batch_size, None), dtype=tf.int32, name='encoder_inputs')
#        self.enc_inputs_len = tf.placeholder(shape=(None,), dtype=tf.int32, name='encoder_inputs_length')
#        self.dec_inputs = tf.placeholder(shape=(config.batch_size, None), dtype=tf.int32, name='decoder_targets')
#        self.dec_targets = tf.placeholder(shape=(config.batch_size, None), dtype=tf.int64, name='decoder_targets')
#        self.helper_prob = tf.placeholder(shape=[], dtype=tf.float32, name='helper_prob')
#        self.chan_param = tf.placeholder(shape=[], dtype=tf.float32, name='chan_param')
#        self.lr = tf.placeholder(shape=[], dtype=tf.float32, name='lr')
#        self.batch_id = tf.placeholder(shape=[],dtype=tf.int32,name='batch_id')
        
        # ==== Building neural network graph ====

        self.embeddings = Embedding(self.config)


#        if (mod_chan_coding) == False: #Training/testing only src net

            #self.config.channel['type'] = 'bsc'
            #self.chan_param = 0.99 #Set channel to near-perfect
            #self.config.channel['chan_param'] =

        if (mod_chan_coding == True):
            print("Enc inputs: ", self.enc_inputs)
            self.encoder = VariableEncoder_mod(self.enc_inputs, 
                                               self.enc_inputs_len, 
                                               self.batch_id,
                                               self.isTrain,
                                               self.embeddings, 
                                               self.config)

        else:
            self.encoder = VariableEncoder(self.enc_inputs, 
                                           self.enc_inputs_len, 
                                           self.batch_id,
                                           self.isTrain,
                                           self.embeddings, 
                                           self.config)

        self.channel = Channel(self.encoder.enc_output,
                               self.chan_param,
                               self.config)
        
        
        if (mod_chan_coding == True):

            self.decoder = VariableDecoder_mod(self.enc_inputs,
                                             self.enc_inputs_len,
                                             self.channel.channel_out,
                                             self.embeddings,
                                             self.batch_id,
                                             self.dec_targets,
                                             self.helper_prob,
                                             self.config)
        else:

            self.decoder = VariableDecoder(self.enc_inputs,
                                         self.enc_inputs_len,
                                         self.channel.channel_out,
                                         self.embeddings,
                                         self.batch_id,
                                         self.dec_targets,
                                         self.helper_prob,
                                         self.config)

        
        # ==== define loss and training op and accuracy ====
        self.loss, self.train_op = self.define_loss()
        self.accuracy = self.define_accuracy()

        # ==== set up training/updating procedure ====
        self.saver = tf.train.Saver(max_to_keep = 3) #Responsible for saving file to model_save_path (saves everything for whatever model's being run)

        self.src_vars_to_load = self.get_vars('src')
        #print("Source variables being loaded: ", self.src_vars_to_load)

        self.src_loader =  tf.train.Saver() #Responsible for loading src coder

        if (mod_chan_coding == True):
            
            self.chan_vars_to_load = self.get_vars('chan')
            #print("Channel variables being loaded: ", self.chan_vars_to_load)
            self.chan_loader = tf.train.Saver(self.chan_vars_to_load)
            self.full_loader = tf.train.Saver() #Designed to load all vars into full model 

        tf.summary.scalar("CrossEntLoss", self.loss)
        tf.summary.histogram("enc_state_c", self.encoder.enc_state_c)
        tf.summary.histogram("enc_state_h", self.encoder.enc_state_h)
        if self.config.channel["type"] != "none":
            tf.summary.histogram("state_reduced", self.encoder.state_reduc)
            tf.summary.histogram("state_bits", self.encoder.state_bits)
        tf.summary.histogram("dec_state_c", self.decoder.state_c_out)
        tf.summary.histogram("dec_state_h", self.decoder.state_h_out)
        self.tb_summary = tf.summary.merge_all()
        self.tb_val_summ = tf.summary.scalar("Validation_Accuracy", self.accuracy)

    def get_vars(self, type):
        vars = []
        for var in tf.trainable_variables():
            if type == "src" and type in var.name:
                vars.append(var)
            elif type == "chan" and var.name.startswith(type): 
                vars.append(var)
        return vars
   
    def update_params(self):
        t_count = self.training_counter
        
        #Calculate current learn rate
        expected_runcount = self.config.epochs * 25000 #~25000 iters per epoch for news dataset
        max_lr = self.config.lr
        min_lr = max_lr / 100 #Can be tuned later
        if (self.training_counter < expected_runcount):
            curr_lr = min_lr + 0.5 * (max_lr - min_lr) * (1 + np.cos(self.training_counter * np.pi / expected_runcount))
        else:
            curr_lr = min_lr
        
        #Calculate current chan_param
        if self.config.channel['chan_param_max'] is None:
            curr_chan_param = self.config.channel['chan_param']
        else:
            curr_chan_param = np.random.uniform(low = self.config.channel['chan_param_min'], high = self.config.channel['chan_param_max'])
        
        return {'lr': curr_lr, 
                'chan_param': curr_chan_param}
        

    def load_model_helper(self, sess, trained_model_path, saver_to_load):

        trained_model_folder = os.path.split(trained_model_path)[0]
        ckpt = tf.train.get_checkpoint_state(trained_model_folder)
        v2_path = os.path.join(trained_model_folder, os.path.split(ckpt.model_checkpoint_path)[1] + ".index")
        norm_ckpt_path = os.path.join(trained_model_folder, os.path.split(ckpt.model_checkpoint_path)[1])
        #norm_ckpt_path = trained_model_path
        if ckpt and (tf.gfile.Exists(norm_ckpt_path) or
                         tf.gfile.Exists(v2_path)):
            print("Reading model parameters from %s" % norm_ckpt_path)
            saver_to_load.restore(sess, norm_ckpt_path)
        else:
            print("Error reading weights from %s" % norm_ckpt_path)

    def load_trained_model(self, sess):
        """
        Loads a trained model from what was saved. The load_mech (loading mechanism) determines whether the source and channel networks are loaded separately or together.
        If load_mech is set to individual_all, loads the two subnetworks from their own save paths. Alternatively, if load_mech is set to full, loads the whole network from its respective save path.
        """
        if (self.config.load_mech == None):

            print ("Loading mechanism unspecified")
            return

        if (self.config.load_mech == 'None'): #Implemented when using src_coder without loading any weights
            return

        if (self.config.load_mech == 'individual_all' or self.config.load_mech == 'src_only'): #Implemented when training combined model,  src_coder after initial training or testing ONLY src coder
            print("Path to load src from: ", self.config.src_model_path)
            print("Source variables being loaded: ", self.src_vars_to_load)
            self.load_model_helper(sess, self.config.src_model_path, self.src_loader)
            print("Finished loading weights for src coder.")

        if (self.config.load_mech == 'individual_all' or self.config.load_mech == 'chan_only'): #Implemented when training combined model
            print("Channel variables being loaded: ", self.chan_vars_to_load)
            self.load_model_helper(sess, self.config.chan_model_path, self.chan_loader)
            print("Finished loading weights for channel coder.")

        elif (self.config.load_mech == 'full'): #Implemented when TESTING full model

            self.load_model_helper(sess, self.config.full_model_path, self.full_loader)
            print("Finished loading weights for combined model.")


    def define_accuracy(self):
        eq_indicator = tf.cast(tf.equal(self.decoder.dec_pred, self.dec_targets), dtype=tf.float32)
        return tf.reduce_mean(eq_indicator)

    def define_loss(self):
        stepwise_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
            labels=tf.one_hot(self.dec_targets, depth=self.config.vocab_size, dtype=tf.float32),
            logits=self.decoder.dec_logits,
        )
        # loss function
        loss = tf.reduce_mean(stepwise_cross_entropy)
        # train it
        train_op = tf.train.AdamOptimizer(learning_rate=self.lr).minimize(loss)
        return loss, train_op

    def batch_to_feed(self, inputs, max_sequence_length=None, time_major=False):
        """
        Creates the next zero padded batch

        Args:
            inputs:
                list of sentences (integer lists)
            max_sequence_length:
                integer specifying how large should `max_time` dimension be.
                If None, maximum sequence length would be used

        Outputs:
            batch_out: zero padded batch
            sequence_lengths: sentence len
        """
        sequence_lengths = [len(seq) for seq in inputs]
        batch_size = len(inputs)

        if max_sequence_length is None:
            max_sequence_length = max(sequence_lengths)

        inputs_batch_major = np.zeros(shape=[batch_size, max_sequence_length], dtype=np.int32)  # == PAD

        for i, seq in enumerate(inputs):
            for j, element in enumerate(seq):
                inputs_batch_major[i, j] = element

        if time_major:
            # [batch_size, max_time] -> [max_time, batch_size]
            batch_out = inputs_batch_major.swapaxes(0, 1)
        else:
            batch_out = inputs_batch_major

        return batch_out, sequence_lengths

    def next_feed(self, batch, help_prob=1.0, isTrain=True):
        """
        Generate the data feed from the batch
        """
        
        encoder_inputs_, encoder_input_lengths_ = self.batch_to_feed(
            [(sequence) + [self.config.EOS] for sequence in batch])
        decoder_targets_, _ = self.batch_to_feed(
            [(sequence) + [self.config.EOS] + [self.config.PAD] * 3 for sequence in batch])
        decoder_inputs_, _ = self.batch_to_feed(
            [[self.config.SOS] + (sequence) + [self.config.EOS] + [self.config.PAD] * 2 for sequence in batch])
        
        if self.config.variable_encoding:
            batch_id_ =  bisect.bisect(self.config.queue_limits,encoder_input_lengths_[0])-1
        else:
            batch_id_ = 0
        
        return {self.isTrain: isTrain,
                self.enc_inputs: encoder_inputs_,
                self.enc_inputs_len: encoder_input_lengths_,
                self.dec_inputs: decoder_inputs_,
                self.dec_targets: decoder_targets_,
                self.helper_prob: help_prob,
                self.chan_param: self.config.channel['chan_param'],
                self.lr: self.config.lr,
                self.batch_id: batch_id_}


          
    def train(self, sess, tb_writer, grammar=None):
        """
        This trains the network
        """
        params = tf.trainable_variables()
        num_params = sum(
            map(lambda t: np.prod(tf.shape(t.value()).eval()), params))
        print('Total model parameters: ', num_params)
        help_prob = 1.0
        try:
            self.training_counter = 0
            self.test_counter = 0
            for i in range(self.config.epochs):
                if i > 5:
                    help_prob = max(0.0, help_prob - 0.05)

                # =============================  Train on Training Data ===============================
                self.train_data.prepare_batch_queues()
                batch = self.train_data.get_next_batch()
                print_every = self.config.print_every
                tic = time.time()
                master_tic = tic
                while batch != None and self.training_counter<self.config.max_batch_in_epoch:

                    fd = self.next_feed(batch, help_prob=help_prob)
                    _, loss, tb_summ = sess.run([self.train_op, self.loss, self.tb_summary], fd)
                    self.training_counter += 1                    

                    if self.training_counter% self.config.summary_every == 0:
                        tb_writer.add_summary(tb_summ, self.training_counter)
                    

                    batch = self.train_data.get_next_batch()

                    if self.training_counter % self.config.print_every == 0 or batch == None:
                        toc = time.time()
                        print("-- Epoch: ", i + 1,
                              "Numb Batches: ", print_every,
                              "Total Training Time: ", toc - master_tic,"s ", 
                              "Batch Training Time: ", toc - tic,"s ",
                              "Training Loss: ", loss)

                        if batch != None:
                            tic = time.time()
                    
                    if self.training_counter % self.config.save_every == 0:
                        self.saver.save(sess, self.config.model_save_path, global_step=self.training_counter // self.config.save_every)
                        print("Model saved in file: %s" % self.config.model_save_path)

                # =============================  Validate on Test Data ===============================
                self.test_data.prepare_batch_queues()
                batch = self.test_data.get_next_batch()
                acc_list = []
                tic = time.time()
                while batch != None and self.test_counter<self.config.max_validate_counter:
                    fd = self.next_feed(batch, isTrain=False, help_prob=0.0)
                    predict_, accu_, tb_summ = sess.run([self.decoder.dec_pred, self.accuracy, self.tb_val_summ], fd)
                    self.test_counter += 1
                    tb_writer.add_summary(tb_summ, self.test_counter)
                    acc_list.append(accu_)
                
                    batch = self.test_data.get_next_batch()
                    
                toc = time.time()
                print("-- Test Time: ", toc - tic, "s ",
                      "Average Accuracy: ", np.average(acc_list))
                for j,inp,pred in zip(range(10),fd[self.enc_inputs], predict_):
                    tx = " ".join(self.word2numb.convert_n2w(inp))
                    rx = " ".join(self.word2numb.convert_n2w(pred))
                    print('  sample {}:'.format(j + 1))
                    print('    input     > {}'.format(tx))
                    print('    predicted > {}'.format(rx))

                # =========================== Save the Model ==========================================
                self.saver.save(sess, self.config.model_save_path, global_step=i)
                print("Model saved in file: %s" % self.config.model_save_path)

        except KeyboardInterrupt:
            print('training interrupted')

        self.saver.save(sess, self.config.model_save_path)
        print("Model saved in file: %s" % self.config.model_save_path)

        return

    
    def enqueue_func(self,coord,sess):
        """ This is the function run by the thread responsible for filling in the 
        queue. Runs the enqueueing op that fills the queue with a placeholder that
        gets filled. 
        
        Args:
            coord - coordinator that does housekeeping on threads
        Returns:
            None
        """
        help_prob_ = 1 if self.epochs<=5 else max(0.0, 1 - 0.05*self.epochs)
        def next_feed_q(batch, help_prob=help_prob_):
            """
            Generate the data feed from the batch for the queue
            """
            encoder_inputs_, encoder_input_lengths_ = self.batch_to_feed(
                [(sequence) + [self.config.EOS] for sequence in batch])
            decoder_targets_, _ = self.batch_to_feed(
                [(sequence) + [self.config.EOS] + [self.config.PAD] * 3 for sequence in batch])
            decoder_inputs_, _ = self.batch_to_feed(
                [[self.config.SOS] + (sequence) + [self.config.EOS] + [self.config.PAD] * 2 for sequence in batch])
            if self.config.variable_encoding:
                batch_id_ =  bisect.bisect(self.config.queue_limits,encoder_input_lengths_[0])-1
            else:
                batch_id_ = 0
            
            self.updated_params = self.update_params()

            fd_pre = {'isTrain': True,
                    'enc_inputs': encoder_inputs_,
                    'enc_inputs_len': encoder_input_lengths_,
                    'dec_inputs': decoder_inputs_,
                    'dec_targets': decoder_targets_,
                    'helper_prob': help_prob,
                    'chan_param': self.updated_params['chan_param'],
                    'lr': self.updated_params['lr'],
                    'batch_id': batch_id_}
            fd = dict((self.queue_vars[name_v],val_v) for name_v,val_v in fd_pre.items())
            return fd
            
      # First setting up the queuerunner and loading the inputs
        self.train_data.prepare_batch_queues()
         
        try:
            while not coord.should_stop() and self.epochs < self.config.epochs:
                batch_ = self.train_data.get_next_batch()
                if batch_ is None:
                    self.epochs+=1
                    self.train_data.prepare_batch_queues()
                    continue
                
                fd = next_feed_q(batch_)
                sess.run(self.enqueue_op,feed_dict=fd)
        except Exception as e:
            print('ERROR in feeding queue',e)
            sess.run(self.close_queue)
            coord.request_stop()
            
    #Default training method.        
    def train_fast(self, sess, tb_writer, grammar=None):
        """
        This trains the network. it uses a queue mechanism to feed in placement
        """
        params = tf.trainable_variables()
        num_params = sum(
            map(lambda t: np.prod(tf.shape(t.value()).eval()), params))
        print('Total model parameters: ', num_params)
        
        self.training_counter = 0
        self.test_counter = 0

        # #load model
        # try:
        #     self.saver.restore(sess,self.config.model_save_path)
        #     print('Restored model from: ',self.config.model_save_path)
        # except:
        #     print('Could not restore. Starting from scratch')
        
        try:
            coord = tf.train.Coordinator()
            t = Thread(target=self.enqueue_func,args=(coord,sess,),name='enq_thread')
            t.daemon = True
            t.start()
        
            # =============================  Train on Training Data ===============================
            self.train_data.prepare_batch_queues()
            batch = self.train_data.get_next_batch()
            print_every = self.config.print_every
            tic = time.time()
            master_tic = tic
        

            while self.training_counter<self.config.max_batch_in_epoch*self.config.epochs and self.epochs<self.config.epochs:
                #self.updated_params = self.update_params()                

                if coord.should_stop():
                    print('Coord has requested a break in training')
                    break
                  
                (_, loss, tb_summ) = sess.run([self.train_op, self.loss, self.tb_summary])
                
                self.training_counter += 1
     
                if self.training_counter% self.config.summary_every == 0:
                    tb_writer.add_summary(tb_summ, self.training_counter)
                
                if self.training_counter % self.config.print_every == 0 or batch == None:
                    toc = time.time()
                    print("Training Counter:", self.training_counter, 
                          ",Epoch:", self.epochs + 1,
                          ",Time Interval:", toc - tic, "s  ", 
                          ",Total Training Time:", toc - master_tic, "s  ", 
                          ",Training Loss:", loss,
                          ",Learn Rate:", self.updated_params['lr'],
                          ",Keep_prob:", self.updated_params['chan_param'])

                    tic = time.time()
                
                if self.training_counter % self.config.save_every == 0:
                    self.saver.save(sess, self.config.model_save_path, global_step=self.training_counter)
                    print("Model saved in file: %s" % self.config.model_save_path)

        except KeyboardInterrupt:
            sess.run(self.close_queue)
            coord.request_stop()
            print('Training interrupted.')
        
        finally:
            print('Finished training')
            sess.run(self.close_queue)
            coord.request_stop()
            coord.join([t],stop_grace_period_secs=5)
            
        # =========================== Save the Model ==========================================
        self.saver.save(sess, self.config.model_save_path)
        print("Model saved in file: %s" % self.config.model_save_path)

        return
    
    
class SingleStepDecoder(object):
    '''The single step decoder is used for by BeamSearch call for 
    beam search decoding at test time. It simulates a single decode step.
    '''
    def __init__(self, embeddings, config, curr_input, state_PH):
        self.batch_size = config.batch_size
        self.peephole = config.peephole

        self.numb_dec_layers = config.numb_dec_layers
        self.dec_hidden_units = config.dec_hidden_units
        self.vocab_size = config.vocab_size

        self.embeddings = embeddings


        self.curr_input = curr_input
        self.curr_state = self.state_place_holder_to_tuple(state_PH)

        # weights and bias for output projection
        self.W = tf.Variable(tf.random_uniform([self.dec_hidden_units, self.vocab_size], -1, 1), dtype=tf.float32)
        self.b = tf.Variable(tf.zeros([self.vocab_size]), dtype=tf.float32)

        self.cell = self.build_cell()
        self.topk_ids, self.topk_probs, self.new_states = self.build_dec_network()



    def build_cell(self):
        '''Build the decoder cell
        '''
        cell = tf.contrib.rnn.LSTMCell(num_units=self.dec_hidden_units,
                                       use_peepholes=self.peephole,
                                       state_is_tuple=True)
        if self.numb_dec_layers>1:
            cells = [tf.contrib.rnn.LSTMCell(num_units=self.dec_hidden_units,
                                             use_peepholes=self.peephole,
                                             state_is_tuple=True)
                     for _ in range(self.numb_dec_layers)]
            cell = tf.contrib.rnn.MultiRNNCell(cells)
        return cell

    def state_place_holder_to_tuple(self, state_PH):
        '''Cell state place holder
        '''
        rnn_tuple_state = [tf.nn.rnn_cell.LSTMStateTuple(c=state_PH[idx][0], h=state_PH[idx][1])
             for idx in range(self.numb_dec_layers)]

        if self.numb_dec_layers == 1:
            return rnn_tuple_state[0]
        else:
            return tuple(rnn_tuple_state)


    def build_dec_network(self):
        '''Build the single step decoder
        '''
        input_emb = self.embeddings.get_embeddings(self.curr_input)

        outputs, states = tf.nn.static_rnn(cell=self.cell,
                                           inputs=[input_emb],
                                           initial_state=self.curr_state,
                                           dtype=tf.float32)

        decoder_outputs = outputs[0]


        decoder_batch_size, decoder_dim = tf.unstack(tf.shape(decoder_outputs))

        # pass flattened tensor through decoder
        decoder_logits = tf.add(tf.matmul(decoder_outputs, self.W), self.b)

        # final prediction
        topk_log_probs, topk_ids = tf.nn.top_k(tf.log(tf.nn.softmax(decoder_logits)), decoder_batch_size * 2)

        out_states = [(states[idx].c, states[idx].h)
                               for idx in range(self.numb_dec_layers)]

        return topk_ids, topk_log_probs, out_states




class Hypothesis(object):
    """Defines a hypothesis during beam search used in BeamSearch class.
    """

    def __init__(self, tokens, log_prob, state):
        self.tokens = tokens
        self.log_prob = log_prob
        self.state = state

    def Extend(self, token, log_prob, new_state):
        """Extend the hypothesis with result from latest step.
        """
        return Hypothesis(self.tokens + [token], self.log_prob + log_prob,
                          new_state)

    @property
    def latest_token(self):
        return self.tokens[-1]

    def __str__(self):
        return ('Hypothesis(log prob = %.4f, tokens = %s)' % (self.log_prob,
                                                              self.tokens))


class BeamSearchVariable(VariableDecoder_mod):
    """This implements a beam search decoder to be used during testing
    """
    #def __init__(self, enc_inputs, encoder_input_len, chan_output, embeddings,
    #             batch_id,dec_inputs, prob_corr_input, config):   
    def __init__(self, embeddings,
                 input_place_holder,
                 state_place_holder,
                 chanout_place_holder,
                 word2numb,
                 batch_id_place_holder,
                 beam_size, config):

        super().__init__(None, None, chanout_place_holder, embeddings, batch_id_place_holder, None, None, config, create_parent = False)

        self.word2numb = word2numb
        self._beam_size = beam_size
        self._start_token = config.SOS
        self._end_token = config.EOS
        self._max_steps = config.length_to+3
        self.numb_dec_layers = config.numb_dec_layers
        self.dec_hidden_units = config.dec_hidden_units
        self.input_PH = input_place_holder
        self.state_PH = state_place_holder
        self.chan_out_PH = chanout_place_holder
        self.batch_id = batch_id_place_holder
        self.config = config
        self.chan_coder_out = self.dec_network_out
        self.init_state = self.expand_chann_out(self.chan_coder_out)
        self.SingleStepDecoder = SingleStepDecoder(embeddings, config, self.input_PH, self.state_PH)


    def BeamSearch(self, sess, chan_output, batch_id, beam_size = None):
        """Performs beam search decoding
        """
        if beam_size!= None:
            self._beam_size = beam_size
        init_state = sess.run(self.init_state, feed_dict={self.chan_out_PH:chan_output,self.batch_id:batch_id})
        print(np.array(init_state).shape)

        # Replicate the initial states K times for the first step.
        hyps = [Hypothesis([self._start_token], 0.0, np.array(init_state))
                ] * self._beam_size
        results = []

        steps = 0
        while steps < self._max_steps and len(results) < self._beam_size:
            latest_tokens = [h.latest_token for h in hyps]
            curr_states = [h.state for h in hyps]
            curr_states = np.array(curr_states)


            if steps==0:
                curr_states = np.swapaxes(curr_states,0,3).squeeze(axis=0)
            else:
                curr_states = np.swapaxes(curr_states, 0, 1)
                curr_states = np.swapaxes(curr_states, 1, 2)


            fd = {self.input_PH: latest_tokens,
                  self.state_PH: curr_states}

            topk_ids, topk_log_probs, new_states = sess.run([self.SingleStepDecoder.topk_ids,
                                                             self.SingleStepDecoder.topk_probs,
                                                             self.SingleStepDecoder.new_states], feed_dict=fd)
            #for t in topk_ids:
            #    print(self.word2numb.convert_n2w(t.tolist()))
            #print(topk_log_probs)
            new_states = np.array(new_states)
            #print(new_states)
            #print(new_states.shape)
            #new_states=new_states.reshape((-1, self.numb_dec_layers, 2, self.dec_hidden_units))
            #print(new_states)
            #print(new_states.shape)


            # Extend each hypothesis.
            all_hyps = []
            # The first step takes the best K results from first hyps. Following
            # steps take the best K results from K*K hyps.
            num_beam_source = 1 if steps == 0 else len(hyps)
            for i in range(num_beam_source):
                h, ns = hyps[i], new_states[:,:,i,:]
                for j in range(self._beam_size*2):
                    all_hyps.append(h.Extend(topk_ids[i, j], topk_log_probs[i, j], ns))

            # Filter and collect any hypotheses that have the end token.
            hyps = []
            for h in self._BestHyps(all_hyps):
                if h.latest_token == self._end_token:
                    # Pull the hypothesis off the beam if the end token is reached.
                    results.append(h)
                else:
                    # Otherwise continue to the extend the hypothesis.
                    hyps.append(h)
                if len(hyps) == self._beam_size or len(results) == self._beam_size:
                    break

            steps += 1

        if steps == self._max_steps:
            results.extend(hyps)

        return self._BestHyps(results)

    def _BestHyps(self, hyps, norm_by_len=False):
        """Sort the hyps based on log probs and length.
        """
        # This length normalization is only effective for the final results.
        if norm_by_len:
            return sorted(hyps, key=lambda h: h.log_prob/len(h.tokens), reverse=True)
        else:
            return sorted(hyps, key=lambda h: h.log_prob, reverse=True)



class BeamSearchEncChanDecNetVar(object):
    """This generates an end-to-end model that includes the sentence encoder,
    the channel, and the beamsearch sentence decoder. It is used to test the system.
    Also, it is variable
    """

    def __init__(self, config, word2numb, beam_size=10):
        self.config = config

        self.word2numb = word2numb
        self.beam_size = beam_size

        # ==== reset graph ====
        tf.reset_default_graph()

        # ==== Placeholders ====
        self.isTrain = tf.placeholder(tf.bool, shape=(), name='isTrain')
        self.sentence = tf.placeholder(shape=(None, None), dtype=tf.int32, name='encoder_inputs')
        self.sentence_len = tf.placeholder(shape=(None,), dtype=tf.int32, name='encoder_inputs_length')
        self.dec_input = tf.placeholder(shape=(None,), dtype=tf.int32, name='dec_input')
        self.dec_state = tf.placeholder(shape=(config.numb_dec_layers, 2, None, config.dec_hidden_units),
                                        dtype=tf.float32, name='dec_state')
        chan_out_dim = config.bits_per_bin[-1] if config.variable_encoding else (np.ceil(config.numb_tx_bits/config.chan_code_rate))
        self.chan_out_PH = tf.placeholder(shape=(None, chan_out_dim),
                                          dtype=tf.float32, name='chan_out')
        self.batch_id = tf.placeholder(shape=[],dtype=tf.int32,name='batch_id')
        self.chan_param = tf.placeholder(shape=[], dtype=tf.float32, name='chan_param')

        # ==== Building neural network graph ====
        self.embeddings = Embedding(config)
        self.encoder = VariableEncoder_mod(self.sentence, 
                                       self.sentence_len, 
                                       self.batch_id,
                                       self.isTrain,
                                       self.embeddings, 
                                       self.config)

        self.channel = Channel(self.encoder.enc_output,
                               self.chan_param,
                               self.config)

        self.beam_search_dec = BeamSearchVariable(self.embeddings,
                                          self.dec_input,
                                          self.dec_state,
                                          self.chan_out_PH,
                                          self.word2numb,
                                          self.batch_id,
                                          beam_size=beam_size,
                                          config=config)

        self.saver = tf.train.Saver()

    def load_enc_dec_weights(self, sess):
        sess.run(tf.global_variables_initializer())
        vars_to_load = [var for var in tf.trainable_variables() if 'src' in var.name or var.name.startswith('chan')]
        print("All vars loaded: ", vars_to_load)
        saver_to_load = tf.train.Saver(vars_to_load)
        trained_model_path = self.config.model_save_path
        trained_model_folder = os.path.split(trained_model_path)[0]
        ckpt = tf.train.get_checkpoint_state(trained_model_folder)
        v2_path = os.path.join(trained_model_folder, os.path.split(ckpt.model_checkpoint_path)[1] + ".index")
        norm_ckpt_path = os.path.join(trained_model_folder, os.path.split(ckpt.model_checkpoint_path)[1])
        if ckpt and (tf.gfile.Exists(norm_ckpt_path) or
                         tf.gfile.Exists(v2_path)):
            print("Reading model parameters from %s" % norm_ckpt_path)
            saver_to_load.restore(sess, norm_ckpt_path)
        else:
            print("Error reading weights from %s" % norm_ckpt_path)
        

    def encode_Tx_sentence(self, sess, num_tokens, chan_param=None):
        chan_param_eval = chan_param or self.config.channel['chan_param']
        num_tokens.append(self.config.EOS)
        
        batch_id_ =  bisect.bisect(self.config.queue_limits,len(num_tokens)-1)-1
            
        fd = {self.isTrain: False,
              self.sentence: [num_tokens],
              self.sentence_len: [len(num_tokens)],
              self.chan_param: chan_param_eval,
              self.batch_id: batch_id_}
        
        chan_out = sess.run([self.channel.channel_out], fd)
        chan_out = np.array(chan_out)
        return (chan_out[0],batch_id_)

    def dec_Rx_bits(self, sess, chan_out_batch, beam_size=None):
        (chan_output,batch_id) = chan_out_batch
        beams = self.beam_search_dec.BeamSearch(sess, chan_output,batch_id, beam_size=beam_size)
        bestseq = " ".join(self.word2numb.convert_n2w(beams[0].tokens))
        bestseq_prob = beams[0].log_prob
        return bestseq, bestseq_prob, beams
    
def test_on_testset(sysNN,test_results_path):
    # =============================  Validate on Test Data ===============================
    sysNN.test_data.prepare_batch_queues(randomize=False)
    batch = sysNN.test_data.get_next_batch(randomize=False)
    acc_list = []
    pbar=tqdm(total=sysNN.config.max_test_counter)
    with open(test_results_path, 'w', newline='') as file:
        while batch != None and pbar.n<=sysNN.config.max_test_counter:

            fd = sysNN.next_feed(batch, isTrain=False, help_prob = 0.0)
            predict_, accu_, channel_out = sess.run([sysNN.decoder.dec_pred, sysNN.accuracy, sysNN.channel.channel_out], fd)
            acc_list.append(accu_)
            print("Channnel outputs: ", channel_out)

            for i, (inp, pred) in enumerate(zip(fd[sysNN.enc_inputs], predict_)):
                tx = " ".join(sysNN.word2numb.convert_n2w(inp))
                rx = " ".join(sysNN.word2numb.convert_n2w(pred))
                if i < 3 and pbar.n<3:
                    print('  sample {}:'.format(i + 1))
                    print('TX: {}'.format(tx))
                    print('RX: {}'.format(rx))
                    print('Accuracy, running avg: {}'.format(np.average(acc_list)))
                file.write('TX: {}\n'.format(tx))
                file.write('RX: {}\n'.format(rx))

            batch = sysNN.test_data.get_next_batch(randomize=False)
            pbar.update(1)
        pbar.close()
        print("Average Accuracy: ", np.average(acc_list))
        file.write("Average Accuracy: {}\n".format(np.average(acc_list)))
    return


def beam_test_on_testset(sess, beamNN, test_data, test_results_path,test_results_err_path,chan_param=None,bits_param = None):
    # =============================  Validate on Test Data ===============================
    bits_lim = bits_param or beamNN.config.bits_per_bin
    
    test_data.prepare_batch_queues(randomize=False)
    batches = test_data.get_next_batch(randomize=False)
    numb_errors = 0
    numb_words = 0
    pbar = tqdm(total = beamNN.config.max_test_counter)

    with open(test_results_path, 'w', newline='') as file:
        with open(test_results_err_path, 'w', newline='') as fileErr:
            while batches != None and pbar.n<beamNN.config.max_test_counter:
                batch = batches[0]
                chan_out = beamNN.encode_Tx_sentence(sess, batch,chan_param=chan_param)
                channel_out,batch_out = chan_out
#                print(channel_out[0,:10])
                channel_out[0,bits_lim[batch_out]:] = 0
                chan_out = (channel_out,batch_out)
#                print(channel_out[0,:10])
                bestseq, bestseq_prob, all_beams = beamNN.dec_Rx_bits(sess, chan_out)

                pred = all_beams[0].tokens
                pred = pred[1:]
                diff = [int(pred[i] != batch[i]) for i in range(min(len(pred), len(batch)))]
                numb_words += max(len(pred), len(batch))
                curr_errors = sum(diff) + abs(len(pred)-len(batch))
                numb_errors += curr_errors
                running_err_rate = numb_errors/numb_words
                print("Running word error rate: ", running_err_rate)
                tx = " ".join(beamNN.word2numb.convert_n2w(batch))
                rx = " ".join(beamNN.word2numb.convert_n2w(pred))
                    
                file.write('TX: {}\n'.format(tx))
                file.write('RX: {}\n'.format(rx))
                print('Batch: {}\n'.format(tx))
                print('Prediction: {}\n'.format(rx))
                if curr_errors > 0:
                    fileErr.write('TX: {}\n'.format(tx))
                    fileErr.write('RX: {}\n'.format(rx))
                pbar.update(1)
                batches = test_data.get_next_batch(randomize=False)

        WER = numb_errors/numb_words
        print("Average Word Error Rate: ", WER)
        file.write("Average Word Error Rate: {}\n".format(WER))
        pbar.close()
    return


def parse_args(arg_to_parse = None):
    """ Function parses args passed in command line
    """
    parent_dir, _ = os.path.split(os.getcwd())
    
    parser = argparse.ArgumentParser(description='Joint Source Channel Coding')

    parser.add_argument('--chan_code_rate','-ccr',default = 0.90,help='Code rate for channel coder')
    parser.add_argument('--src_model_path','-smp')
    parser.add_argument('--chan_model_path','-cmp') 
    parser.add_argument('--chan_model_file', '-cmf')
    parser.add_argument('--full_model_path','-fmp')
    parser.add_argument('--task','-t',default='train_full',choices=['train_full', 'train_src', 'test_src', 'test','beam','tfast'])
    parser.add_argument('--load_mechanism','-lm', default = 'None', choices=['None', 'individual_all', 'full', 'src_only','chan_only'])
    parser.add_argument('--chan_param_max','-cp_max', type=float,help='max keep rate or sig value of channel')
    parser.add_argument('--chan_param_min','-cp_min', type=float,help='min keep rate or sig value of channel')
    parser.add_argument('--performance_file_name', '-p_fname')
    parser.add_argument('--edit_distance_type', '-edt', default = 'ed_only', choices = ['ed_only', 'ed_WuP'])
    
    parser.add_argument('--variable_encoding','-v',action='count',help='Number of v indicate version. 0 is no variable, 1 is std method, 2 is exp method')
    parser.add_argument('--dataset','-d',default='news',choices=['wiki','news','euro','beta'])
    parser.add_argument('--channel','-c',choices=['erasure','awgn','bsc','none'])
    parser.add_argument('--chan_param','-cp',type=float,help='Keep rate or sig value of channel')
    parser.add_argument('--numb_epochs','-e',default=10,type=int)
    parser.add_argument('--deep_encoding','-de',action='store_true')
    parser.add_argument('--deep_encoding_params','-dp',nargs='+',type=int,default=[1000,800,600])
    parser.add_argument('--lr','-lr',default=0.001,type=float)
    parser.add_argument('--numb_tx_bits','-ntx',default=400,type=int)
    parser.add_argument('--vocab_size','-vs',default=20000,type=int)
    parser.add_argument('--embedding_size','-es',default=200,type=int)
    parser.add_argument('--enc_hidden_units','-eu',default=256,type=int)
    parser.add_argument('--numb_enc_layers','-nel',default=2,type=int)
    parser.add_argument('--numb_dec_layers','-ndl',default=2,type=int)
    parser.add_argument('--batch_size','-b',default=512,type=int)
    parser.add_argument('--batch_size_test','-bt',default=8,type=int)
    parser.add_argument('--length_from','-lf',default=4,type=int)
    parser.add_argument('--length_to','-lt',default=30,type=int)
    parser.add_argument('--bin_len','-bl',default=4,type=int)
    parser.add_argument('--bits_per_bin','-bb',nargs='+',type=int)
    parser.add_argument('--bits_per_bin_gen','-bg',nargs='+',default=['linear',250],help='Generates bits per bin. const linear or sqrt followed by low_lim on bits')
    parser.add_argument('--w2n_path','-wp')
    parser.add_argument('--traindata_path','-trp')
    parser.add_argument('--testdata_path','-tep')
    parser.add_argument('--embed_path','-ep')
    parser.add_argument('--summ_path','-sp')
    parser.add_argument('--test_results_path','-terp')
    parser.add_argument('--print_every','-pe',default=100,type=int)
    parser.add_argument('--max_test_counter','-mt',default=int(60000),type=int)
    parser.add_argument('--max_validate_counter','-mv',default=500,type=int)
    parser.add_argument('--max_batch_in_epoch','-mb',default=int(1e9),type=int)
    parser.add_argument('--save_every','-se',default = int(1e4), type=int)
    parser.add_argument('--summary_every','-sme',default=5,type=int)
    parser.add_argument('--peephole','-p',action='store_false')
    parser.add_argument('--beam_size','-bs',default=10,type=int)
    parser.add_argument('--test_param','-tp',default=None,type=float,help='Channel parameter value for testing')
    parser.add_argument('--test_param2','-tp2',nargs='+',type=int,help='Channel parameter to pass a different number of bits at test time')
    parser.add_argument('--add_name_results','-anr',default='')
    parser.add_argument('--unk_perc','-up',default=0.2,type=float)
    parser.add_argument('--qcap','-q',default=200,type=int)
    
    if arg_to_parse is None:
        conf_args = vars(parser.parse_args())
    else:
        conf_args = vars(parser.parse_args(arg_to_parse))
    
    if conf_args['deep_encoding'] and conf_args['variable_encoding']==1:
        raise ValueError('deep encoding and variable encoding of type 1 are not compatible')
        
    conf_args['channel'] = {'type':conf_args['channel'],'chan_param':conf_args['chan_param'], 'chan_param_max':conf_args['chan_param_max'], 'chan_param_min': conf_args['chan_param_min']}
    #if (conf_args['chan_param_max'] is not None): 
        #conf_args['channel']['chan_param_max'] = conf_args['chan_param_max'] 
        #conf_args['channel']['chan_param_min'] = conf_args['chan_param_min']
    
    if conf_args['variable_encoding'] and (conf_args['bits_per_bin'] is None):
        #Generating the bit allocation per bin
        type_bit_bin = conf_args['bits_per_bin_gen'][0]
        if len(conf_args['bits_per_bin_gen'])>1:
            low_lim = int(conf_args['bits_per_bin_gen'][1])
        else:
            low_lim=None
            conf_args['bits_per_bin_gen'].append(None)
        conf_args['bits_per_bin']= bin_batch_create(conf_args['numb_tx_bits'],
                         dataset=conf_args['dataset'],
                         type_bit_bin = type_bit_bin,low_lim=low_lim)
        
    conf_args['w2n_path'] = conf_args['w2n_path'] or os.path.join(parent_dir,'data',conf_args['dataset'],'w2n_n2w_'+conf_args['dataset']+'.pickle')
    conf_args['testdata_path'] = conf_args['testdata_path'] or os.path.join(parent_dir,'data',conf_args['dataset'],conf_args['dataset']+'_test.dat')
    conf_args['traindata_path'] = conf_args['traindata_path'] or os.path.join(parent_dir,'data',conf_args['dataset'],conf_args['dataset']+'_train.dat')
    conf_args['embed_path'] = conf_args['embed_path'] or os.path.join(parent_dir,'data',conf_args['dataset'],'{}_embed_{}.pickle'.format(conf_args['embedding_size'],conf_args['dataset']))
    fileName = generate_tb_filename(Config(**conf_args))
    print("File name generated: ", fileName)
    
    if conf_args['chan_model_file'] is None:
        conf_args['chan_model_file'] = os.path.join(conf_args['dataset'], fileName)
    #Sets the save paths
    conf_args['src_model_path'] = conf_args['src_model_path'] or os.path.join(parent_dir,'trained_models', 'src_model',conf_args['dataset'],fileName)
    #conf_args['chan_model_path'] = conf_args['chan_model_path'] or os.path.join(parent_dir,'trained_models', 'chan_model',conf_args['dataset'],fileName) 
    conf_args['chan_model_path'] = conf_args['chan_model_path'] or os.path.join(parent_dir,'trained_models', 'chan_model', conf_args['chan_model_file'])
    conf_args['full_model_path'] = conf_args['full_model_path'] or os.path.join(parent_dir,'trained_models', 'full_model',conf_args['dataset'],fileName) 
    
    mod_type = "src" if conf_args['task'] in ["train_src", "test_src"] else "full"
    conf_args['performance_file_name'] = conf_args['performance_file_name'] or "WER_" + mod_type + "_" + conf_args['channel']['type']

    conf_args['summ_path'] = conf_args['summ_path'] or os.path.join(parent_dir,'tensorboard',conf_args['dataset'],fileName)
    
    if conf_args['test_param']:
        conf_args['add_name_results']+='{:0.02f}'.format(conf_args['test_param'])
    default_test_results_path = os.path.join(parent_dir,'test_results',conf_args['dataset'],fileName+conf_args['task']+conf_args['add_name_results']+'.out')
    conf_args['test_results_path'] = conf_args['test_results_path'] or default_test_results_path
    return conf_args

if __name__ == '__main__':
    conf_args = parse_args()
    
    print('Init and Loading Data...')
    config = Config(**conf_args)

    word2numb = Word2Numb(config.w2n_path,vocab_size = config.vocab_size)
    config.vocab_size= min(len(word2numb.n2w),config.vocab_size)
    
    train_sentence_gen = SentenceBatchGenerator(config.traindata_path,
                                                word2numb,
                                                batch_size=config.batch_size,
                                                min_len=config.length_from,
                                                max_len=config.length_to,
                                                diff=config.bin_len,
                                                unk_perc = conf_args['unk_perc'])

    test_sentences = SentenceBatchGenerator(config.testdata_path,
                                            word2numb,
                                            batch_size=config.batch_size,
                                            min_len=config.length_from,
                                            max_len=config.length_to,
                                            diff=config.bin_len,
                                            unk_perc = conf_args['unk_perc'])

    
    summ_path = conf_args['summ_path']
    test_results_path = conf_args['test_results_path']

    train_test = conf_args['task'] # One of 'train_src','train_full', 'tfast', 'test', 'beam'
    src_list = ["train_src", "test_src"]
    config.model_save_path = config.src_model_path if (train_test in src_list) else config.full_model_path    
    config.mod_chan_coding = not(train_test in src_list)
    config.load_mech = conf_args['load_mechanism']    

    if train_test == "train_src":
        print('Building Network...')
        sysNN = VariableSystem(config, train_sentence_gen, test_sentences, word2numb, mod_chan_coding = config.mod_chan_coding)
        print('Start training src coder...')
        with tf.Session() as sess:
            sess.run(tf.global_variables_initializer())
            sysNN.load_trained_model(sess) #Loads nothing if config.load_mech == 'None'    
            writer = tf.summary.FileWriter(summ_path)
            writer.add_graph(sess.graph)
            sysNN.train_fast(sess, writer)

    elif train_test in ['train_full','tfast']:
        print('Building Network...')
        sysNN = VariableSystem(config, train_sentence_gen, test_sentences, word2numb, mod_chan_coding = config.mod_chan_coding)
        print('Start training...')
        with tf.Session() as sess:
            sess.run(tf.global_variables_initializer())
            
            sysNN.load_trained_model(sess) #Expect load_mech to be set to 'individual_all'

            #sysNN.src_saver.restore(sess, config.model_save_path)
            #print("Finished loading weights of src coder")

            #sysNN.chan_saver.restore(sess, config.chan_model_path)
            #print("Finsihed loadingg weights of channel coder")

            writer = tf.summary.FileWriter(summ_path)
            writer.add_graph(sess.graph)
            sysNN.train_fast(sess, writer)
            # if train_test=='train_full':
            #     sysNN.train(sess, writer)
            # else:
            #     sysNN.train_fast(sess,writer)
        
    elif train_test in ['test', 'test_src']:
        print('Building Network...')
        print("Mod_chan: ", config.mod_chan_coding)
        sysNN = VariableSystem(config, train_sentence_gen, test_sentences, word2numb, mod_chan_coding = config.mod_chan_coding)
        print("Trainable Variables in model: ", [var.name for var in tf.trainable_variables()])
        print('Start testing...')
        with tf.Session() as sess:
            sess.run(tf.global_variables_initializer())

            print("Mod_chan: ", config.mod_chan_coding)
            print("Channel: ", config.channel)
            sysNN.load_trained_model(sess) #Expect load_mech to be set to one of ['full',  'src_only']
            print("Done Loading")
            test_on_testset(sysNN, test_results_path)
        print('Finished testing...')
        
    elif train_test in ['beam', 'test_src']: #'beam' tests on full model, 'test_src' only tests src code
        test_results_path = "beam-" + "test_results_path"
        print("Saving results to: ",  test_results_path)
        test_sentences = SentenceBatchGenerator(config.testdata_path,
                                        word2numb,
                                        batch_size=config.batch_size_test,
                                        min_len=config.length_from,
                                        max_len=config.length_to,
                                        diff=config.bin_len,
                                        unk_perc = conf_args['unk_perc'])
        test_results_err_path = test_results_path + '.err'
        print('Building beam search network')
        beam_size = conf_args['beam_size']
        beam_sys = BeamSearchEncChanDecNetVar(config, word2numb, beam_size=beam_size)
        print('Beginning beam search processing')
        print('Start session...')
        with tf.Session() as sess:
            print("Mod encoding: ", config.mod_chan_coding)
            beam_sys.load_enc_dec_weights(sess)
            print("Beam weights initialized and loaded.")
            beam_test_on_testset(sess, beam_sys, test_sentences, test_results_path, 
                                 test_results_err_path,chan_param=conf_args['test_param'],
                                 bits_param = conf_args['test_param2'])

