# Copyright IRT Antoine de Saint Exupéry et Université Paul Sabatier Toulouse III - All
# rights reserved. DEEL is a research program operated by IVADO, IRT Saint Exupéry,
# CRIAQ and ANITI - https://www.deel.ai/
# =====================================================================================
"""
First order Influence module
"""

import tensorflow as tf

from .influence_calculator import BaseInfluenceCalculator, IHVPCalculator

from ..types import Optional, Union, Tuple
from ..common import assert_batched_dataset
from deel.influenciae.common.sorted_dict import BatchedSortedDict

from .inverse_hessian_vector_product import (
    InverseHessianVectorProduct,
    ExactIHVP
)

from ..common import InfluenceModel


class FirstOrderInfluenceCalculator(BaseInfluenceCalculator):
    """
    A class implementing the necessary methods to compute the different influence quantities
    using the first-order approximation.

    The methods currently implemented are available to evaluate one or a group of point(s):
    - Influence function vectors: the weights difference when removing point(s)
    - Influence values/Cook's distance: a measure of reliance of the model on the individual
      point(s)

    Parameters
    ----------
    model
        The TF2.X model implementing the InfluenceModel interface.
    dataset
        A batched TF dataset containing the training dataset over which we will estimate the
        inverse-hessian-vector product.
    ihvp_calculator
        Either a string containing the IHVP method ('exact' or 'cgd'), an IHVPCalculator
        object or an InverseHessianVectorProduct object.
    n_samples_for_hessian
        An integer indicating the amount of samples to take from the provided train dataset.
    shuffle_buffer_size
        An integer indicating the buffer size of the train dataset's shuffle operation -- when
        choosing the amount of samples for the hessian.
    normalize
        Implement "RelatIF: Identifying Explanatory Training Examples via Relative Influence"
        https://arxiv.org/pdf/2003.11630.pdf
        if True, compute the relative influence by normalizing the influence function.
    """

    def __init__(
            self,
            model: InfluenceModel,
            dataset: tf.data.Dataset,
            ihvp_calculator: Union[str, InverseHessianVectorProduct, IHVPCalculator] = ExactIHVP,
            n_samples_for_hessian: Optional[int] = None,
            shuffle_buffer_size: Optional[int] = 10000,
            normalize=False
    ):
        super(FirstOrderInfluenceCalculator, self).__init__(model, dataset, ihvp_calculator, n_samples_for_hessian,
                                                            shuffle_buffer_size)
        self.normalize = normalize

    def compute_influence(self, dataset: tf.data.Dataset) -> tf.Tensor:
        """
        Computes the influence function vector -- an estimation of the weights difference when
        removing point(s) -- one vector for each point.

        Parameters
        ----------
        dataset
            A batched Tensorflow dataset containing the points from which we aim to compute the
            influence of removal.

        Returns
        -------
        influence_vectors
            A tensor containing one vector per input point

        """
        assert_batched_dataset(dataset)

        influence_vectors = self.ihvp_calculator.compute_ihvp(dataset)

        influence_vectors = self.__normalize_if_needed(influence_vectors)

        influence_vectors = tf.transpose(influence_vectors)

        return influence_vectors

    def __normalize_if_needed(self, v):
        """
        Normalize the input vector if the normalize property is True. If False, do nothing
        :param v: the vector to normalize of shape [Features_Space, Batch_Size]
        :return: the normalized vector if the normalize property is True, otherwise the input vector
        """
        if self.normalize:
            v = v / tf.norm(v, axis=0, keepdims=True)
        return v

    def top_k(self,
              sample_to_evaluate: Tuple[tf.Tensor, tf.Tensor],
              dataset_train: tf.data.Dataset,
              k: int = 5) -> Tuple[tf.Tensor, tf.Tensor]:
        """
        Find the top-k closest elements of the training dataset for each sample to evaluate

        The Cook's distance is evaluate for each point(s) provided individually, giving measure of the
        influence that each point carries on the model's weights.

        Parameters
        ----------
        sample_to_evaluate
            A batched tensor containing the samples which will be compare to the training dataset
        dataset_train
            A batched TF dataset containing the samples used during the training procedure
        k
            the number of most influence samples to retain in training datatse
        Returns
        -------
        influence_values
            Top-k influence values for each sample to evaluate.
        training_samples
            Top-k training sample for each sample to evaluate.
        """
        grads_to_evaluate = self.model.batch_jacobian_tensor(*sample_to_evaluate)
        batch_size = tf.shape(grads_to_evaluate)[0]
        grads_to_evaluate = tf.reshape(grads_to_evaluate, (batch_size, -1))

        batched_sorted_dic = BatchedSortedDict(batch_size, k)
        for batch in dataset_train:
            # TODO - improve: API IHVP shall accept tensor
            ihvp = self.ihvp_calculator.compute_ihvp(
                tf.data.Dataset.from_tensor_slices(batch).batch(int(tf.shape(batch[0])[0])))

            ihvp = self.__normalize_if_needed(ihvp)

            influence_values = tf.matmul(grads_to_evaluate, ihvp)
            batched_sorted_dic.add_all(influence_values,
                                       tf.repeat(tf.expand_dims(batch[0], axis=0), batch_size, axis=0))

        influences_values, training_samples = batched_sorted_dic.get()

        return influences_values, training_samples

    def compute_influence_values(
            self,
            dataset_train: tf.data.Dataset,
            dataset_to_evaluate: Optional[tf.data.Dataset] = None
    ) -> tf.Tensor:
        """
        Computes Cook's distance of each point(s) provided individually, giving measure of the
        influence that each point carries on the model's weights.

        The dataset_train contains the points we will be removing and dataset_to_evaluate,
        those with respect to whom we will be measuring the influence.
        As we will be performing the same operation in batches, we consider that each point
        from one dataset corresponds to one from the other. As such, both datasets must contain
        the same amount of points. In case the dataset_to_evaluate is not given, use by default the
        dataset_train: compute the self influence.

        Parameters
        ----------
        dataset_train
            A batched TF dataset containing the points we wish to remove.
        dataset_to_evaluate
            A batched TF dataset containing the points with respect to whom we wish to measure
            the influence of removing the training points. Default as dataset_train (self
            influence).

        Returns
        -------
        influence_values
            A tensor containing one influence value per pair of input values (one coming from
            each dataset).
        """
        if dataset_to_evaluate is None:
            # default to self influence
            dataset_to_evaluate = dataset_train

        dataset_size = self.assert_compatible_datasets(dataset_train, dataset_to_evaluate)

        grads = self.model.batch_jacobian(dataset_to_evaluate)
        grads = tf.reshape(grads, (dataset_size, -1))

        ihvp = self.ihvp_calculator.compute_ihvp(dataset_train)

        ihvp = self.__normalize_if_needed(ihvp)

        influence_values = tf.reduce_sum(
            tf.math.multiply(grads, tf.transpose(ihvp)), axis=1, keepdims=True)

        return influence_values

    def compute_influence_group(
            self,
            group: tf.data.Dataset
    ) -> tf.Tensor:
        """
        Computes the influence function vector -- an estimation of the weights difference when
        removing the points -- of the whole group of points.

        Parameters
        ----------
        group
            A batched TF dataset containing the group of points of which we wish to compute the
            influence of removal.

        Returns
        -------
        influence_group
            A tensor containing one vector for the whole group.
        """
        assert_batched_dataset(group)

        ihvp = self.ihvp_calculator.compute_ihvp(group)
        reduced_ihvp = tf.reduce_sum(ihvp, axis=1)

        reduced_ihvp = self.__normalize_if_needed(reduced_ihvp)

        influence_group = tf.reshape(reduced_ihvp, (1, -1))

        return influence_group

    def compute_influence_values_group(
            self,
            group_train: tf.data.Dataset,
            group_to_evaluate: Optional[tf.data.Dataset] = None
    ) -> tf.Tensor:
        """
        Computes Cook's distance of the whole group of points provided, giving measure of the
        influence that the group carries on the model's weights.

        The dataset_train contains the points we will be removing and dataset_to_evaluate,
        those with respect to whom we will be measuring the influence. As we will be performing
        the same operation in batches, we consider that each point from one dataset corresponds
        to one from the other. As such, both datasets must contain the same amount of points.
        In case the group_to_evaluate is not given, use by default the
        group_to_train: compute the self influence of the group.


        Parameters
        ----------
        group_train
            A batched TF dataset containing the group of points we wish to remove.
        group_to_evaluate
            A batched TF dataset containing the group of points with respect to whom we wish to
            measure the influence of removing the training points.

        Returns
        -------
        influence_values_group
            A tensor containing one influence value for the whole group.
        """
        if group_to_evaluate is None:
            # default to self influence
            group_to_evaluate = group_train

        dataset_size = self.assert_compatible_datasets(group_train, group_to_evaluate)

        reduced_grads = tf.reduce_sum(tf.reshape(self.model.batch_jacobian(group_to_evaluate),
                                                 (dataset_size, -1)), axis=0, keepdims=True)

        reduced_ihvp = tf.reduce_sum(self.ihvp_calculator.compute_ihvp(group_train), axis=1, keepdims=True)

        reduced_ihvp = self.__normalize_if_needed(reduced_ihvp)

        influence_values_group = tf.matmul(reduced_grads, reduced_ihvp)

        return influence_values_group
