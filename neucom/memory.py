import torch
from torch.autograd import Variable
import  torch.nn.functional as F
import torch.nn as nn

import numpy as np
from collections import namedtuple
from neucom.utils import *

class Memory(nn.Module):
    def __init__(self,mem_slot=256, mem_size=64, read_heads=4, batch_size=1):
        """
        constructs a memory matrix with read heads and a write head as described
        in the DNC paper
        http://www.nature.com/nature/journal/vaop/ncurrent/full/nature20101.html

        Parameters:
        ----------
        words_num: int
            the maximum number of words that can be stored in the memory at the
            same time
        mem_size: int
            the size of the individual word in the memory
        read_heads: int
            the number of read heads that can read simultaneously from the memory
        batch_size: int
            the size of input data batch
        """

        self.__dict__.update(locals())
        self.I = Variable(torch.eye(mem_slot))

        self.memory_tuple = namedtuple('mem_tuple', 'mem_mat, mem_usage, pre_vec, \
                                        link_mat, write_weight, read_weight, read_vec')

    def init_memory(self):
        """
        return a tuple of the intial values pertinetn to 
        the memorys
        Returns: namedtuple('mem_tuple', 'mem_mat, mem_usage, pre_vec, \
                            link_mat, write_weight, read_weight, read_vec')
        """
        mem_list = [Variable(torch.zeros(self.batch_size, self.mem_slot, self.mem_size).fill_(1e-6)), #initial memory matrix
            Variable(torch.zeros(self.batch_size, self.mem_slot)), #initial memory usage vector
            Variable(torch.zeros(self.batch_size, self.mem_slot)), #initial precedence vector
            Variable(torch.zeros(self.batch_size, self.mem_slot, self.mem_slot)), #initial link matrix
            
            Variable(torch.zeros(self.batch_size, self.mem_slot).fill_(1e-6)), #initial write weighting
            Variable(torch.zeros(self.batch_size, self.mem_slot, self.read_heads).fill_(1e-6)), #initial read weighting
            Variable(torch.zeros(self.batch_size, self.mem_size, self.read_heads).fill_(1e-6))] #initial read vector
        return self.memory_tuple._make(mem_list)

    def get_content_address(self, memory_matrix, keys, strengths):
        """
        retrives a content-based adderssing weights given the keys

        Parameters:
        ----------
        memory_matrix: Tensor (batch_size, mem_slot, mem_size)
            the memory matrix to lookup in
        keys: Tensor (batch_size, mem_size, number_of_keys)
            the keys to query the memory with
        strengths: Tensor (batch_size, number_of_keys, )
            the list of strengths for each lookup key
        
        Returns: Tensor (batch_size, mem_slot, number_of_keys)
            The list of lookup weightings for each provided key
        """
        # cos_dist is (batch_size, mem_slot, number_of_keys)
        cos_dist = cosine_distance(memory_matrix, keys)
        
        strengths = expand_dims(strengths, 1)

        return softmax(cos_dist*strengths, 1)

    def update_usage_vector(self, usage_vector, read_weights, write_weight, free_gates):
        """
        updates and returns the usgae vector given the values of the free gates
        and the usage_vector, read_weights, write_weight from previous step

        Parameters:
        ----------
        usage_vector: Tensor (batch_size, mem_slot)
        read_weights: Tensor (batch_size, mem_slot, read_heads)
        write_weight: Tensor (batch_size, mem_slot)
        free_gates: Tensor (batch_size, read_heads, )

        Returns: Tensor (batch_size, mem_slot, )
            the updated usage vector
        """
        free_gates = expand_dims(free_gates,1)
        retention_vector = torch.prod(2- read_weights * free_gates, 2)
        updated_usage = (usage_vector + write_weight - usage_vector * write_weight)  * retention_vector

        return updated_usage

    def get_allocation_weight(self, sorted_usage, free_list):
        """
        retreives the writing allocation weight based on the usage free list

        Parameters:
        ----------
        sorted_usage: Tensor (batch_size, mem_slot, )
            the usage vector sorted ascendly
        free_list: Tensor (batch, mem_slot, )
            the original indecies of the sorted usage vector

        Returns: Tensor (batch_size, mem_slot, )
            the allocation weight for each word in memory
        """

        shifted_cumprod =  torch.cumprod(sorted_usage, axis = 1) / sorted_usage[0]
        shifted_cumprod[-1] = shifted_cumprod[-1]/sorted_usage[-1]
        
        unordered_allocation_weight = (1 - sorted_usage) * shifted_cumprod

        mapped_free_list = free_list + self.index_mapper
        flat_unordered_allocation_weight = unordered_allocation_weight.view(-1)
        flat_mapped_free_list = mapped_free_list.view(-1)
        flat_container = torch.zeros(self.batch_size * self.mem_slot)
        #flat_ordered_weights = flat_container.scatter(
        #    flat_mapped_free_list,
        #    flat_unordered_allocation_weight
        #)
        flat_ordered_weights = flat_container.scatter_(
            flat_mapped_free_list,
            flat_unordered_allocation_weight
        )
        return flat_ordered_weights.view(self.batch_size, self.mem_slot)

    def update_write_weight(self, lookup_weight, allocation_weight, write_gate, allocation_gate):
        """
        updates and returns the current write_weight

        Parameters:
        ----------
        lookup_weight: Tensor (batch_size, mem_slot, 1)
            the weight of the lookup operation in writing
        allocation_weight: Tensor (batch_size, mem_slot)
            the weight of the allocation operation in writing
        write_gate: (batch_size, 1)
            the fraction of writing to be done
        allocation_gate: (batch_size, 1)
            the fraction of allocation to be done

        Returns: Tensor (batch_size, mem_slot)
            the updated write_weight
        """

        # remove the dimension of 1 from the lookup_weight
        first_2_size = lookup_weight.size()[0:2]
        lookup_weight = lookup_weight.view(*first_2_size)
        updated_write_weight = write_gate * (allocation_gate * allocation_weight + (1 - allocation_gate) * lookup_weight)

        return updated_write_weight

    def update_memory(self, memory_matrix, write_weight, write_vector, erase_vector):
        """
        updates and returns the memory matrix given the weight, write and erase vectors
        and the memory matrix from previous step

        Parameters:
        ----------
        memory_matrix: Tensor (batch_size, mem_slot, mem_size)
            the memory matrix from previous step
        write_weight: Tensor (batch_size, mem_slot)
            the weight of writing at each memory location
        write_vector: Tensor (batch_size, mem_size)
            a vector specifying what to write
        erase_vector: Tensor (batch_size, mem_size)
            a vector specifying what to erase from memory

        Returns: Tensor (batch_size, mem_slot, mem_size)
            the updated memory matrix
        """

        # expand data with a dimension of 1 at multiplication-adjacent location
        # to force matmul to behave as an outer product
        write_weight = expand_dims(write_weight, 2)
        write_vector = expand_dims(write_vector, 1)
        erase_vector = expand_dims(erase_vector, 1)

        erasing = memory_matrix * (1 - torch.mm(write_weight, erase_vector))
        writing = torch.mm(write_weight, write_vector)
        updated_memory = erasing + writing

        return updated_memory

    def update_precedence_vector(self, precedence_vector, write_weight):
        """
        updates the precedence vector given the latest write weight
        and the precedence_vector from last step

        Parameters:
        ----------
        precedence_vector: Tensor (batch_size. mem_slot)
            the precedence vector from the last time step
        write_weight: Tensor (batch_size,mem_slot)
            the latest write weight for the memory

        Returns: Tensor (batch_size, mem_slot)
            the updated precedence vector
        """

        reset_factor = 1 - reduce_sum(write_weight, 1, keep_dims=True)
        updated_precedence_vector = reset_factor * precedence_vector + write_weight

        return updated_precedence_vector
    
    def update_link_matrix(self, precedence_vector, link_matrix, write_weight):
        """
        updates and returns the temporal link matrix for the latest write
        given the precedence vector and the link matrix from previous step

        Parameters:
        ----------
        precedence_vector: Tensor (batch_size, mem_slot)
            the precedence vector from the last time step
        link_matrix: Tensor (batch_size, mem_slot, mem_slot)
            the link matrix form the last step
        write_weight: Tensor (batch_size, mem_slot)
            the latest write_weight for the memory

        Returns: Tensor (batch_size, mem_slot, mem_slot)
            the updated temporal link matrix
        """

        write_weight = expand_dims(write_weight, 2)
        precedence_vector = expand_dims(precedence_vector, 1)

        reset_factor = 1 - pairwise_add(write_weight, is_batch=True)
        updated_link_matrix = reset_factor * link_matrix + torch.mm(write_weight, precedence_vector)
        updated_link_matrix = (1 - self.I) * updated_link_matrix  # eliminates self-links

        return updated_link_matrix
    
    def get_directional_weights(self, read_weights, link_matrix):
        """
        computes and returns the forward and backward reading weights
        given the read_weights from the previous step

        Parameters:
        ----------
        read_weights: Tensor (batch_size, mem_slot, read_heads)
            the read weights from the last time step
        link_matrix: Tensor (batch_size, mem_slot, mem_slot)
            the temporal link matrix

        Returns: Tuple
            forward weight: Tensor (batch_size, mem_slot, read_heads),
            backward weight: Tensor (batch_size, mem_slot, read_heads)
        """

        forward_weight = torch.mm(link_matrix, read_weights)
        backward_weight = torch.mm(link_matrix.transpose(0,1), read_weights)


        return forward_weight, backward_weight

    def update_read_weights(self, lookup_weights, forward_weight, backward_weight, read_mode):
        """
        updates and returns the current read_weights

        Parameters:
        ----------
        lookup_weights: Tensor (batch_size, mem_slot, read_heads)
            the content-based read weight
        forward_weight: Tensor (batch_size, mem_slot, read_heads)
            the forward direction read weight
        backward_weight: Tensor (batch_size, mem_slot, read_heads)
            the backward direction read weight
        read_mode: Tesnor (batch_size, 3, read_heads)
            the softmax distribution between the three read modes

        Returns: Tensor (batch_size, mem_slot, read_heads)
        """

        backward_mode = expand_dims(read_mode[:, 0, :], 1) * backward_weight
        lookup_mode = expand_dims(read_mode[:, 1, :], 1) * lookup_weights
        forward_mode = expand_dims(read_mode[:, 2, :], 1) * forward_weight
        updated_read_weights = backward_mode + lookup_mode + forward_mode

        return updated_read_weights

    def update_read_vectors(self, memory_matrix, read_weights):
        """
        reads, updates, and returns the read vectors of the recently updated memory

        Parameters:
        ----------
        memory_matrix: Tensor (batch_size, mem_slot, mem_size)
            the recently updated memory matrix
        read_weights: Tensor (batch_size, mem_slot, read_heads)
            the amount of info to read from each memory location by each read head

        Returns: Tensor (mem_size, read_heads)
        """

        updated_read_vectors = torch.mm(memory_matrix.transpose(0,1), read_weights)

        return updated_read_vectors

    
    def write(self, memory_matrix, usage_vector, read_weights, write_weight,
              precedence_vector, link_matrix,  key, strength, free_gates,
              allocation_gate, write_gate, write_vector, erase_vector):
        """
        defines the complete pipeline of writing to memory gievn the write variables
        and the memory_matrix, usage_vector, link_matrix, and precedence_vector from
        previous step

        Parameters:
        ----------
        memory_matrix: Tensor (batch_size, mem_slot, mem_size)
            the memory matrix from previous step
        usage_vector: Tensor (batch_size, mem_slot)
            the usage_vector from the last time step
        read_weights: Tensor (batch_size, mem_slot, read_heads)
            the read_weights from the last time step
        write_weight: Tensor (batch_size, mem_slot)
            the write_weight from the last time step
        precedence_vector: Tensor (batch_size, mem_slot)
            the precedence vector from the last time step
        link_matrix: Tensor (batch_size, mem_slot, mem_slot)
            the link_matrix from previous step
        key: Tensor (batch_size, mem_size, 1)
            the key to query the memory location with
        strength: (batch_size, 1)
            the strength of the query key
        free_gates: Tensor (batch_size, read_heads)
            the degree to which location at read haeds will be freed
        allocation_gate: (batch_size, 1)
            the fraction of writing that is being allocated in a new locatio
        write_gate: (batch_size, 1)
            the amount of information to be written to memory
        write_vector: Tensor (batch_size, mem_size)
            specifications of what to write to memory
        erase_vector: Tensor(batch_size, mem_size)
            specifications of what to erase from memory

        Returns : Tuple
            the updated usage vector: Tensor (batch_size, mem_slot)
            the updated write_weight: Tensor(batch_size, mem_slot)
            the updated memory_matrix: Tensor (batch_size, mem_slot, words_size)
            the updated link matrix: Tensor(batch_size, mem_slot, mem_slot)
            the updated precedence vector: Tensor (batch_size, mem_slot)
        """

        lookup_weight = self.get_content_address(memory_matrix, key, strength)
        new_usage_vector = self.update_usage_vector(usage_vector, read_weights, write_weight, free_gates)

        np_new_usage_vec = new_usage_vector.numpy()
        sort_list = np.argsort(np_new_usage_vec, axis = -1)

        #TODO: make free_list to the same device to input
        free_list = Variable(torch.LongStorage(sort_list))
        free_list = Variable(new_usage_vector.input(*(sort_list.shape))).long()
        sorted_usage = torch.gather(new_usage_vector,1,  free_list)
        
        #sorted_usage, free_list = top_k(-1 * new_usage_vector, self.mem_slot)
        #sorted_usage = -1 * sorted_usage

        allocation_weight = self.get_allocation_weight(sorted_usage, free_list)
        new_write_weight = self.update_write_weight(lookup_weight, allocation_weight, write_gate, allocation_gate)
        new_memory_matrix = self.update_memory(memory_matrix, new_write_weight, write_vector, erase_vector)
        new_link_matrix = self.update_link_matrix(precedence_vector, link_matrix, new_write_weight)
        new_precedence_vector = self.update_precedence_vector(precedence_vector, new_write_weight)

        return new_usage_vector, new_write_weight, new_memory_matrix, new_link_matrix, new_precedence_vector


    def read(self, memory_matrix, read_weights, keys, strengths, link_matrix, read_modes):
        """
        defines the complete pipeline for reading from memory

        Parameters:
        ----------
        memory_matrix: Tensor (batch_size, mem_slot, mem_size)
            the updated memory matrix from the last writing
        read_weights: Tensor (batch_size, mem_slot, read_heads)
            the read weights form the last time step
        keys: Tensor (batch_size, mem_size, read_heads)
            the kyes to query the memory locations with
        strengths: Tensor (batch_size, read_heads)
            the strength of each read key
        link_matrix: Tensor (batch_size, mem_slot, mem_slot)
            the updated link matrix from the last writing
        read_modes: Tensor (batch_size, 3, read_heads)
            the softmax distribution between the three read modes

        Returns: Tuple
            the updated read_weights: Tensor(batch_size, mem_slot, read_heads)
            the recently read vectors: Tensor (batch_size, mem_size, read_heads)
        """

        lookup_weight = self.get_content_address(memory_matrix, keys, strengths)
        forward_weight, backward_weight = self.get_directional_weights(read_weights, link_matrix)
        new_read_weights = self.update_read_weights(lookup_weight, forward_weight, backward_weight, read_modes)
        new_read_vectors = self.update_read_vectors(memory_matrix, new_read_weights)

        return new_read_weights, new_read_vectors
