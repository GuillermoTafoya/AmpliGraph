from functools import partial
import tensorflow as tf

def LP_regularizer(trainable_param, regularizer_parameters={}):
    '''
    LP regularizer
    
    Parameters:
    -----------
    trainable_param: tf.Variable
        Trainable parameters of the model that needs to be regularized
    regularizer_parameters: dict
        Parameters of the regularizer
        
        - **p**: (int). p for the LP regularizer. Eg when p=3 (default), it uses L3 regularizer
        - **lambda** : (float). Regularizer weight. default is 0.001
        
    Returns:
    --------
    regularizer: instance of tf.keras.regularizer
        Regularizer instance
        
    '''
    return regularizer_parameters.get('lambda', 0.001) * tf.reduce_sum(
        tf.pow(tf.abs(trainable_param), regularizer_parameters.get('p', 3)))
    
def get(identifier, hyperparams={}):
    '''
    Get the regularizer specified by the identifier
    
    Parameters:
    -----------
    identifier: string, tf.keras.regularizer instance or a callable
        Instance of tf.keras.regularizer or name of the regularizer to use (will use default parameters) or a 
        callable function
        
    Returns:
    --------
    regularizer: instance of tf.keras.regularizer
        Regularizer instance
        
    '''
    if isinstance(identifier, str) and identifier == 'LP':
        identifier = partial(LP_regularizer, regularizer_parameters=hyperparams)
        identifier = tf.keras.regularizers.get(identifier)
        identifier.__name__ = 'LP'
    return identifier