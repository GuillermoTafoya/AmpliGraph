from ampligraph.datasets import SQLiteAdapter
from ampligraph.datasets import GraphDataLoader
from ampligraph.datasets.graph_partitioner import PARTITION_ALGO_REGISTRY, AbstractGraphPartitioner
import numpy as np
import shelve   
import tensorflow as tf
import abc


PARTITION_MANAGER_REGISTRY = {}
def register_partitioning_manager(name):
    """Decorator responsible for registering partition manager in the partition manager registry.
       
       Parameters
       ----------
       name: name of the new partition manager.
 
       Example
       -------
       >>>@register_partitioning_manager("NewManagerName")
       >>>class NewManagerName(PartitionedDataManager):
       >>>... pass
    """
    def insert_in_registry(class_handle):
        """Checks if partition manager already exists and if not registers it."""
        if name in PARTITION_MANAGER_REGISTRY.keys():
            msg = "Partitioning Manager with name {} "
            logger.error(msg)
            raise Exception(msg)
        "already exists!".format(name)
        
        PARTITION_MANAGER_REGISTRY[name] = class_handle
        class_handle.name = name
        return class_handle

    return insert_in_registry

class PartitionedDataManager(abc.ABC):
    def __init__(self, dataset_loader, model, strategy='Bucket'):
        """Initializes the Partitioning Data Manager. 
        Uses/Creates partitioner and generates partition related params.
        
        Parameters
        ----------
        dataset_loader : 
            Either an instance of AbstractGraphPartitioner or GraphDataLoader.
        model: tf.keras.Model
            The model that is being trained
        strategy: string
            Type of partitioning strategy to use
        
        """
        self._model = model
        self.k = self._model.k
        self.eta = self._model.eta
        self.partitioner_k = 5
        
        if isinstance(dataset_loader, AbstractGraphPartitioner):
            self.partitioner = dataset_loader
            self.partitioner_k = self.partitioner._k
        else:
            print('Partitioning may take a while...')
            self.partitioner = PARTITION_ALGO_REGISTRY.get(strategy)(dataset_loader, k=self.partitioner_k)
            
            
        self.num_ents = self.partitioner._data.backend.mapper.ents_length
        self.num_rels = self.partitioner._data.backend.mapper.rels_length
        self.max_ent_size = 0
        for i in range(len(self.partitioner.partitions)):
            self.max_ent_size = max(self.max_ent_size, 
                                    self.partitioner.partitions[i].backend.mapper.ents_length)
        
        self._generate_partition_params()
        
    @property
    def max_entities(self):
        '''Returns the maximum entity size that can occur in a partition
        '''
        return self.max_ent_size

    @property
    def max_relations(self):
        '''Returns the maximum relation size that can occur in a partition
        '''
        return self.num_rels
        
    def _generate_partition_params(self):
        ''' Generates the metadata needed for persisting and loading partition embeddings and other params
        '''
        raise NotImplementedError('Abstract method not implemented')
        
    def _update_partion_embeddings(self, graph_data_loader, partition_number):
        '''Persists the embeddings and other params after a partition is trained
        
        Parameters
        ----------
        graph_data_loader : GraphDataLoader
            Data loader of the current partition that was trained
        partition_number: int
            Partition number of the current partition that was trained
        '''
        raise NotImplementedError('Abstract method not implemented')
        
    def _change_partition(self, graph_data_loader, partition_number):
        '''Gets a new partition to train and loads all the params of the partition
        
        Parameters
        ----------
        graph_data_loader : GraphDataLoader
            Data loader of the next partition that will be trained
        partition_number: int
            Partition number of the next partition will be trained
        '''
        raise NotImplementedError('Abstract method not implemented')
        
    def data_generator(self):
        '''Generates the data to be trained from the current partition. 
        Once the partition data is exhausted, the current params are persisted; the partition is changed 
        and model is notified.
        
        Returns:
        --------
        batch_data_from_current_partition: (n,3)
            A batch of triples from current partition being trained
        '''
        for i, partition_data in enumerate(self.partitioner):
            # partition_data is an object of graph data loader
            # Perform tasks related to change of partition
            self._change_partition(partition_data, i)
            try:
                while True:
                    # generate data from the current partition
                    batch_data_from_current_partition = next(partition_data)
                    yield batch_data_from_current_partition
            
            except StopIteration:
                # No more data in current partition (parsed fully once), so the partition is trained
                # Hence persist the params related to the current partition.
                self._update_partion_embeddings(partition_data, i)
                
    def get_tf_generator(self):
        return tf.data.Dataset.from_generator(
            self.data_generator,
            output_types=tf.dtypes.int32,
            output_shapes=(None,3)
        ).prefetch(0)
                
    def __iter__(self):
        """Function needed to be used as an itertor."""
        return self

    def __next__(self):
        """Function needed to be used as an itertor."""
        return next(self.batch_iterator)
    
    def reload(self):
        ''' reload the data for next epoch
        '''
        self.partitioner.reload()
        self.batch_iterator = iter(self.data_generator())
        
    def on_epoch_end(self):
        ''' Activities to be performed on epoch end
        '''
        pass
    
    def on_complete(self):
        ''' Activities to be performed on end of training.
            The manager persists the data (splits the entity partitions into individual embeddings)
        '''
        pass
    

@register_partitioning_manager('GeneralPartitionedDataManager')
class GeneralPartitionedDataManager(PartitionedDataManager):
    ''' Manages the partitioning related controls. 
    Handles data generation and informs model about changes in partition.
    '''
    def __init__(self, dataset_loader, model, strategy='RandomEdges'):
        """Initializes the Partitioning Data Manager. 
        Uses/Creates partitioner and generates partition related params.
        
        Parameters
        ----------
        dataset_loader : 
            Either an instance of AbstractGraphPartitioner or GraphDataLoader.
        model: tf.keras.Model
            The model that is being trained
        strategy: string
            Type of partitioning strategy to use
        
        """
        super(GeneralPartitionedDataManager, self).__init__(dataset_loader, model, strategy)
        
    def _generate_partition_params(self):
        ''' Generates the metadata needed for persisting and loading partition embeddings and other params
        '''

        # create entity embeddings and optimizer hyperparams for all entities
        update_part_size = int(np.ceil(self.num_ents/self.partitioner_k))
        for part_num in range(self.partitioner_k):
            with shelve.open('ent_partition', writeback=True) as ent_partition:
                for i in range(update_part_size * part_num, 
                               min(update_part_size * (part_num + 1), self.num_ents)):
                    out_dict_key = str(i)
                    opt_param = np.zeros(shape=(1, 3, self.k), dtype=np.float32)
                    # ent_emb = xavier(self.num_ents, self.k, num_ents_bucket)
                    ent_emb = self._model.encoding_layer.ent_init(
                        shape=(1, self.k),
                        dtype=tf.float32).numpy()
                    ent_partition.update({out_dict_key: [opt_param, ent_emb]})

        # create relation embeddings and optimizer hyperparams for all relations
        # relations are not partitioned
        with shelve.open('rel_partition', writeback=True) as rel_partition:
            for i in range(self.num_rels):
                out_dict_key = str(i)
                # TODO change the hardcoding from 3 to actual hyperparam of optim
                opt_param = np.zeros(shape=(1, 3, self.k), dtype=np.float32)
                # rel_emb = xavier(self.num_rels, self.k, self.num_rels)
                rel_emb = self._model.encoding_layer.rel_init(
                    shape=(1, self.k), 
                    dtype=tf.float32).numpy()
                rel_partition.update({out_dict_key: [opt_param, rel_emb]})
                

    def _update_partion_embeddings(self, graph_data_loader, partition_number):
        '''Persists the embeddings and other params after a partition is trained
        
        Parameters
        ----------
        graph_data_loader : GraphDataLoader
            Data loader of the current partition that was trained
        partition_number: int
            Partition number of the current partition that was trained
        '''
        # set the trained params back for persisting (exclude paddings)
        self.all_ent_embs = self._model.encoding_layer.ent_emb.numpy()[:len(self.ent_original_ids), :]
        self.all_rel_embs = self._model.encoding_layer.rel_emb.numpy()[:len(self.rel_original_ids), :]

        # get the optimizer params related to the embeddings
        ent_opt_hyperparams, rel_opt_hyperparams = self._model.optimizer.get_entity_relation_hyperparams()

        # get the number of params that are created by the optimizer
        num_opt_hyperparams = self._model.optimizer.get_hyperparam_count()
        
        # depending on optimizer, you can have 0 or more params
        if num_opt_hyperparams > 0:
            # store the params
            original_ent_hyperparams = []
            original_rel_hyperparams = []
            
            # get all the different params related to entities and relations
            # eg: beta1, beta2 related to embeddings (when using adam)
            for i in range(num_opt_hyperparams):
                original_ent_hyperparams.append(ent_opt_hyperparams[i][:len(self.ent_original_ids)])
                original_rel_hyperparams.append(rel_opt_hyperparams[i][:len(self.rel_original_ids)])
                
            # store for persistance
            self.all_rel_opt_params = np.stack(original_rel_hyperparams, 1)
            self.all_ent_opt_params = np.stack(original_ent_hyperparams, 1)
            
        # Open the buckets related to the partition and concat
        
        try:
            # persist entity related embs and optim params
            ent_partition = shelve.open('ent_partition', writeback=True)
            for i, key in enumerate(self.ent_original_ids):
                ent_partition[str(key)] = [self.all_ent_opt_params[i : i + 1], self.all_ent_embs[i : i + 1]]
            
        finally:
            ent_partition.close()
            
        try:
            # persist relation related embs and optim params
            rel_partition = shelve.open('rel_partition', writeback=True)
            for i, key in enumerate(self.rel_original_ids):
                rel_partition[str(key)] = [self.all_rel_opt_params[i : i + 1], self.all_rel_embs[i : i + 1]]
            
        finally:
            rel_partition.close()
            


    def _change_partition(self, graph_data_loader, partition_number):
        '''Gets a new partition to train and loads all the params of the partition
        
        Parameters
        ----------
        graph_data_loader : GraphDataLoader
            Data loader of the next partition that will be trained
        partition_number: int
            Partition number of the next partition will be trained
        '''
        with shelve.open(graph_data_loader.backend.mapper.entities_dict) as partition:
            partition_keys = sorted([int(key) for key in partition.keys()])
            self.ent_original_ids = [partition[str(key)] for key in partition_keys]

        with shelve.open('ent_partition') as partition:
            self.all_ent_embs = []
            self.all_ent_opt_params = []
            for key in self.ent_original_ids:
                self.all_ent_opt_params.append(partition[key][0])
                self.all_ent_embs.append(partition[key][1])
            self.all_ent_embs = np.concatenate(self.all_ent_embs, 0)
            self.all_ent_opt_params = np.concatenate(self.all_ent_opt_params, 0)
            
            
        with shelve.open(graph_data_loader.backend.mapper.relations_dict) as partition:
            partition_keys = sorted([int(key) for key in partition.keys()])
            self.rel_original_ids = [partition[str(key)] for key in partition_keys]

        with shelve.open('rel_partition') as partition:
            self.all_rel_embs = []
            self.all_rel_opt_params = []
            for key in self.rel_original_ids:
                self.all_rel_opt_params.append(partition[key][0])
                self.all_rel_embs.append(partition[key][1])
            self.all_rel_embs = np.concatenate(self.all_rel_embs, 0)
            self.all_rel_opt_params = np.concatenate(self.all_rel_opt_params, 0)

        # notify the model about the partition change 
        self._model.partition_change_updates(len(self.ent_original_ids), 
                                             self.all_ent_embs, 
                                             self.all_rel_embs)
        
        # Optimizer params will exist only after it has been persisted once
        if self._model.current_epoch > 1:
            # TODO: needs to be better handled
            # get the optimizer params of the embs that will be trained
            rel_optim_hyperparams = []
            ent_optim_hyperparams = []
            
            num_opt_hyperparams = self._model.optimizer.get_hyperparam_count()
            for i in range(num_opt_hyperparams):
                rel_hyperparam_i = self.all_rel_opt_params[:, i, :]
                rel_hyperparam_i = np.pad(rel_hyperparam_i, 
                                          ((0, self.num_rels - rel_hyperparam_i.shape[0]), (0,0)), 
                                           'constant',
                                           constant_values=(0))
                rel_optim_hyperparams.append(rel_hyperparam_i)
                
                ent_hyperparam_i = self.all_ent_opt_params[:, i, :]
                ent_hyperparam_i = np.pad(ent_hyperparam_i, 
                                          ((0, self.max_ent_size - ent_hyperparam_i.shape[0]), (0,0)),
                                          'constant',
                                          constant_values=(0))
                ent_optim_hyperparams.append(ent_hyperparam_i)
            
            # notify the optimizer and update the optimizer hyperparams
            self._model.optimizer.set_entity_relation_hyperparams(ent_optim_hyperparams, 
                                                                  rel_optim_hyperparams)
    
    def on_complete(self):
        ''' Activities to be performed on end of training.
            The manager persists the data (splits the entity partitions into individual embeddings)
        '''
        update_part_size = int(np.ceil(self.num_ents/self.partitioner_k))
        for part_num in range(self.partitioner_k):
            with shelve.open('ent_partition', writeback=True) as ent_partition:
                for i in range(update_part_size * part_num, 
                               min(update_part_size * (part_num + 1), self.num_ents)):
                    ent_partition[str(i)] = ent_partition[str(i)][1][0]

        # create relation embeddings and optimizer hyperparams for all relations
        # relations are not partitioned
        with shelve.open('rel_partition', writeback=True) as rel_partition:
            for i in range(self.num_rels):
                rel_partition[str(i)] = rel_partition[str(i)][1][0]


@register_partitioning_manager('BucketPartitionedDataManager')
class BucketPartitionedDataManager(PartitionedDataManager):
    ''' Manages the partitioning related controls. 
    Handles data generation and informs model about changes in partition.
    '''
    def __init__(self, dataset_loader, model, strategy='Bucket'):
        """Initializes the Partitioning Data Manager. 
        Uses/Creates partitioner and generates partition related params.
        
        Parameters
        ----------
        dataset_loader : 
            Either an instance of AbstractGraphPartitioner or GraphDataLoader.
        model: tf.keras.Model
            The model that is being trained
        strategy: string
            Type of partitioning strategy to use
        
        """
        super(BucketPartitionedDataManager, self).__init__(dataset_loader, model, strategy)
        
    def _generate_partition_params(self):
        ''' Generates the metadata needed for persisting and loading partition embeddings and other params
        '''

        # create entity embeddings and optimizer hyperparams for all entities
        for i in range(self.partitioner_k):
            with shelve.open('ent_partition', writeback=True) as ent_partition:
                with shelve.open(self.partitioner.files[i]) as bucket:
    
                    out_dict_key = str(i)
                    num_ents_bucket = bucket['indexes'].shape[0]
                    # TODO change the hardcoding from 3 to actual hyperparam of optim
                    opt_param = np.zeros(shape=(num_ents_bucket, 3, self.k), dtype=np.float32)
                    # ent_emb = xavier(self.num_ents, self.k, num_ents_bucket)
                    ent_emb = self._model.encoding_layer.ent_init(
                        shape=(num_ents_bucket, self.k),
                        dtype=tf.float32).numpy()
                    ent_partition.update({out_dict_key: [opt_param, ent_emb]})
         
        # create relation embeddings and optimizer hyperparams for all relations
        # relations are not partitioned
        with shelve.open('rel_partition', writeback=True) as rel_partition:
            out_dict_key = str(0)
            # TODO change the hardcoding from 3 to actual hyperparam of optim
            opt_param = np.zeros(shape=(self.num_rels, 3, self.k), dtype=np.float32)
            # rel_emb = xavier(self.num_rels, self.k, self.num_rels)
            rel_emb = self._model.encoding_layer.rel_init(
                shape=(self.num_rels, self.k), 
                dtype=tf.float32).numpy()
            rel_partition.update({out_dict_key: [opt_param, rel_emb]})
                
        # for every partition
        for i in range(len(self.partitioner.partitions)):
            # get the source and dest bucket
            splits = self.partitioner.partitions[i].backend.mapper.metadata['name'].split('-')
            source_bucket = splits[0][-1]
            dest_bucket = splits[1]
            all_keys_merged_buckets = []
            # get all the unique entities present in the buckets
            with shelve.open(self.partitioner.files[int(source_bucket)]) as bucket:
                all_keys_merged_buckets.extend(bucket['indexes'])
            if source_bucket != dest_bucket: 
                with shelve.open(self.partitioner.files[int(dest_bucket)]) as bucket:
                    all_keys_merged_buckets.extend(bucket['indexes'])


            # since we would be concatenating the bucket embeddings, let's find what 0, 1, 2 etc indices of 
            # embedding matrix means.
            # bucket entity value to ent_emb matrix index mappings eg: 2001 -> 0, 2002->1, 2003->2, ...
            merged_bucket_to_ent_mat_mappings = {}
            for key, val in zip(all_keys_merged_buckets, np.arange(0, len(all_keys_merged_buckets))):
                merged_bucket_to_ent_mat_mappings[key] = val
            #print(merged_bucket_to_ent_mat_mappings)
            emb_mat_order = []

            # partitions do not contain all entities of the bucket they belong to.
            # they will produce data from 0->n idx. So we need to remap the get position of the 
            # entities of the partition in the concatenated emb matrix
            # data_index -> original_ent_index -> ent_emb_matrix mappings (a->b->c) 0->2002->1, 1->2003->2 
            # (because 2001 may not exist in this partition)
            with shelve.open(self.partitioner.partitions[i].backend.mapper.metadata['entities_shelf']) as ent_sh:
                sorted_partition_keys = np.sort(np.array(list(ent_sh.keys())).astype(np.int32))
                # a : 0 to n
                for key in sorted_partition_keys:
                    # a->b mapping
                    a_to_b = int(ent_sh[str(key)])
                    # a->b->c mapping
                    emb_mat_order.append(merged_bucket_to_ent_mat_mappings[a_to_b])

            # store it 
            with shelve.open('ent_partition_metadata', writeback=True) as metadata:
                metadata[str(i)] = emb_mat_order      
                
            rel_mat_order = []
            with shelve.open(self.partitioner.partitions[i].backend.mapper.metadata['relations']) as rel_sh:
                sorted_partition_keys = np.sort(np.array(list(rel_sh.keys())).astype(np.int32))
                # a : 0 to n
                for key in sorted_partition_keys:
                    # a->b mapping
                    rel_mat_order.append(int(rel_sh[str(key)]))

            with shelve.open('rel_partition_metadata', writeback=True) as metadata:
                metadata[str(i)] = rel_mat_order      


    def _update_partion_embeddings(self, graph_data_loader, partition_number):
        '''Persists the embeddings and other params after a partition is trained
        
        Parameters
        ----------
        graph_data_loader : GraphDataLoader
            Data loader of the current partition that was trained
        partition_number: int
            Partition number of the current partition that was trained
        '''
        # set the trained params back for persisting (exclude paddings)
        self.all_ent_embs[self.ent_original_ids] = \
            self._model.encoding_layer.ent_emb.numpy()[:len(self.ent_original_ids), :]
        self.all_rel_embs[self.rel_original_ids] = \
            self._model.encoding_layer.rel_emb.numpy()[:len(self.rel_original_ids), :]

        # get the optimizer params related to the embeddings
        ent_opt_hyperparams, rel_opt_hyperparams = self._model.optimizer.get_entity_relation_hyperparams()

        # get the number of params that are created by the optimizer
        num_opt_hyperparams = self._model.optimizer.get_hyperparam_count()
        
        # depending on optimizer, you can have 0 or more params
        if num_opt_hyperparams > 0:
            # store the params
            original_ent_hyperparams = []
            original_rel_hyperparams = []
            
            # get all the different params related to entities and relations
            # eg: beta1, beta2 related to embeddings (when using adam)
            for i in range(num_opt_hyperparams):
                original_ent_hyperparams.append(ent_opt_hyperparams[i][:len(self.ent_original_ids)])
                original_rel_hyperparams.append(rel_opt_hyperparams[i][:len(self.rel_original_ids)])
                
            # store for persistance
            self.all_rel_opt_params[self.rel_original_ids, :, :] = np.stack(original_rel_hyperparams, 1)
            self.all_ent_opt_params[self.ent_original_ids, :, :] = np.stack(original_ent_hyperparams, 1)
            
        # Open the buckets related to the partition and concat
        splits = graph_data_loader.backend.mapper.metadata['name'].split('-')
        source_bucket = splits[0][-1]
        dest_bucket = splits[1]
        
        try:
            # persist entity related embs and optim params
            s = shelve.open('ent_partition', writeback=True)
            source_bucket_params = s[source_bucket]
            dest_source_bucket_params = s[dest_bucket]

            # split and save self.all_ent_opt_params and self.all_ent_embs into respective buckets

            opt_params = [self.all_ent_opt_params[:self.split_opt_idx],
                          self.all_ent_opt_params[self.split_opt_idx:]]
            emb_params = [self.all_ent_embs[:self.split_emb_idx],
                          self.all_ent_embs[self.split_emb_idx:]]
            
            s[source_bucket] = [opt_params[0], emb_params[0]]
            s[dest_bucket] = [opt_params[1], emb_params[1]]
            
        finally:
            s.close()
            
        try:
            # persist relation related embs and optim params
            s = shelve.open('rel_partition', writeback=True)
            s['0'] = [self.all_rel_opt_params, self.all_rel_embs]
            
        finally:
            s.close()

    def _change_partition(self, graph_data_loader, partition_number):
        '''Gets a new partition to train and loads all the params of the partition
        
        Parameters
        ----------
        graph_data_loader : GraphDataLoader
            Data loader of the next partition that will be trained
        partition_number: int
            Partition number of the next partition will be trained
        '''
        try:
            # open the meta data related to the partition
            s = shelve.open('ent_partition_metadata')
            # entities mapping ids
            self.ent_original_ids = s[str(partition_number)]
        finally:
            s.close()

        try:
            s = shelve.open('rel_partition_metadata')
            # entities mapping ids
            self.rel_original_ids = s[str(partition_number)]
            
        finally:
            s.close()
            
        # Open the buckets related to the partition and concat
        splits = graph_data_loader.backend.mapper.metadata['name'].split('-')
        source_bucket = splits[0][-1]
        dest_bucket = splits[1]
        
        try:
            s = shelve.open('ent_partition')
            source_bucket_params = s[source_bucket]
            dest_source_bucket_params = s[dest_bucket]
            # full ent embs
            self.all_ent_embs = np.concatenate([source_bucket_params[1], dest_source_bucket_params[1]])
            self.split_emb_idx = source_bucket_params[1].shape[0]
            
            self.all_ent_opt_params = np.concatenate([source_bucket_params[0], dest_source_bucket_params[0]])
            self.split_opt_idx = source_bucket_params[0].shape[0]
            
            # now select only partition embeddings
            ent_embs = self.all_ent_embs[self.ent_original_ids]
            ent_opt_params = self.all_ent_opt_params[self.ent_original_ids]
        finally:
            s.close()
            
        try:
            s = shelve.open('rel_partition')
            # full rel embs
            self.all_rel_embs = s['0'][1]
            self.all_rel_opt_params =s['0'][0]
            # now select only partition embeddings
            rel_embs = self.all_rel_embs[self.rel_original_ids]
            rel_opt_params = self.all_rel_opt_params[self.rel_original_ids]
        finally:
            s.close()

        # notify the model about the partition change 
        self._model.partition_change_updates(len(self.ent_original_ids), ent_embs, rel_embs)
        
        # Optimizer params will exist only after it has been persisted once
        if self._model.current_epoch > 1 or (self._model.current_epoch == 1 and 
                                            partition_number > self.partitioner_k):
            # TODO: needs to be better handled
            # get the optimizer params of the embs that will be trained
            rel_optim_hyperparams = []
            ent_optim_hyperparams = []
            
            num_opt_hyperparams = self._model.optimizer.get_hyperparam_count()
            for i in range(num_opt_hyperparams):
                rel_hyperparam_i = rel_opt_params[:, i, :]
                rel_hyperparam_i = np.pad(rel_hyperparam_i, 
                                          ((0, self.num_rels - rel_hyperparam_i.shape[0]), (0,0)), 
                                           'constant',
                                           constant_values=(0))
                rel_optim_hyperparams.append(rel_hyperparam_i)
                
                ent_hyperparam_i = ent_opt_params[:, i, :]
                ent_hyperparam_i = np.pad(ent_hyperparam_i, 
                                          ((0, self.max_ent_size - ent_hyperparam_i.shape[0]), (0,0)),
                                          'constant',
                                          constant_values=(0))
                ent_optim_hyperparams.append(ent_hyperparam_i)
            
            # notify the optimizer and update the optimizer hyperparams
            self._model.optimizer.set_entity_relation_hyperparams(ent_optim_hyperparams, 
                                                                  rel_optim_hyperparams)
    
    def on_complete(self):
        ''' Activities to be performed on end of training.
            The manager persists the data (splits the entity partitions into individual embeddings)
        '''
        for i in range(self.partitioner_k - 1, -1, -1):
            with shelve.open(self.partitioner.files[i]) as bucket:

                with shelve.open('ent_partition', writeback=True) as ent_partition:
                    # get the bucket embeddings
                    # split and store separately
                    for key, val in zip(bucket['indexes'], ent_partition[str(i)][1]):
                        ent_partition[str(key)] = val
                    if i!=0:
                        del ent_partition[str(i)]
        with shelve.open('rel_partition', writeback=True) as rel_partition:
            # get the bucket embeddings
            # split and store separately
            for key in range(rel_partition['0'][1].shape[0] - 1, -1, -1):
                rel_partition[str(key)] = rel_partition['0'][1][key]
        

def get_partition_adapter(dataset_loader, model, strategy='Bucket'):
    if isinstance(dataset_loader, AbstractGraphPartitioner):
        partitioner_manager = PARTITION_MANAGER_REGISTRY.get(dataset_loader.manager)(
            dataset_loader, model, dataset_loader.name)

    else:
        partitioner = PARTITION_ALGO_REGISTRY.get(strategy)(dataset_loader, k=3)
        partitioner_manager = PARTITION_MANAGER_REGISTRY.get(partitioner.manager)(
            partitioner, model, strategy)
        
    return partitioner_manager