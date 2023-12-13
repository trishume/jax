# Copyright 2020 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Defines a helper function for creating a SavedModel from a jax2tf trained model.

This has been tested with TensorFlow Hub, TensorFlow JavaScript,
and TensorFlow Serving.

Note that the code in this file is provided only as an example. The functions
generated by `jax2tf.convert` are standard TensorFlow functions and you can
save them in a SavedModel using standard TensorFlow code. This decoupling
of jax2tf from SavedModel is important, because it allows the user to have full
control over what metadata is saved in the SavedModel. Please copy and
customize this function as needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Callable, Optional, Union

from jax.experimental import jax2tf  # type: ignore[import]
import tensorflow as tf  # type: ignore[import]


def convert_and_save_model(
    jax_fn: Callable[[Any, Any], Any],
    params,
    model_dir: str,
    *,
    input_signatures: Sequence[tf.TensorSpec],
    polymorphic_shapes: str | jax2tf.PolyShape | None = None,
    with_gradient: bool = False,
    enable_xla: bool = True,
    compile_model: bool = True,
    saved_model_options: tf.saved_model.SaveOptions | None = None):
  """Convert a JAX function and saves a SavedModel.

  This is an example, we do not promise backwards compatibility for this code.
  For serious uses, please copy and expand it as needed (see note at the top
  of the module).

  Use this function if you have a trained ML model that has both a prediction
  function and trained parameters, which you want to save separately from the
  function graph as variables (e.g., to avoid limits on the size of the
  GraphDef, or to enable fine-tuning.) If you don't have such parameters,
  you can still use this library function but probably don't need it
  (see jax2tf/README.md for some simple examples).

  In order to use this wrapper you must first convert your model to a function
  with two arguments: the parameters and the input on which you want to do
  inference. Both arguments may be np.ndarray or (nested)
  tuples/lists/dictionaries thereof.

  See the README.md for a discussion of how to prepare Flax and Haiku models.

  Args:
    jax_fn: a JAX function taking two arguments, the parameters and the inputs.
      Both arguments may be (nested) tuples/lists/dictionaries of np.ndarray.
    params: the parameters, to be used as first argument for `jax_fn`. These
      must be (nested) tuples/lists/dictionaries of np.ndarray, and will be
      saved as the variables of the SavedModel.
    model_dir: the directory where the model should be saved.
    input_signatures: the input signatures for the second argument of `jax_fn`
      (the input). A signature must be a `tensorflow.TensorSpec` instance, or a
      (nested) tuple/list/dictionary thereof with a structure matching the
      second argument of `jax_fn`. The first input_signature will be saved as
      the default serving signature. The additional signatures will be used
      only to ensure that the `jax_fn` is traced and converted to TF for the
      corresponding input shapes.
    with_gradient: the value to use for the `with_gradient` parameter for
      `jax2tf.convert`.
    enable_xla: the value to use for the `enable_xla` parameter for
      `jax2tf.convert`.
    compile_model: use TensorFlow jit_compiler on the SavedModel. This
      is needed if the SavedModel will be used for TensorFlow serving.
    polymorphic_shapes: if given then it will be used as the
      `polymorphic_shapes` argument to jax2tf.convert for the second parameter of
      `jax_fn`. In this case, a single `input_signatures` is supported, and
      should have `None` in the polymorphic dimensions.
    saved_model_options: options to pass to savedmodel.save.
  """
  if not input_signatures:
    raise ValueError("At least one input_signature must be given")
  if polymorphic_shapes is not None:
    if len(input_signatures) > 1:
      raise ValueError("For shape-polymorphic conversion a single "
                       "input_signature is supported.")
  tf_fn = jax2tf.convert(
    jax_fn,
    with_gradient=with_gradient,
    polymorphic_shapes=[None, polymorphic_shapes],
    enable_xla=enable_xla)

  # Create tf.Variables for the parameters. If you want more useful variable
  # names, you can use `tree.map_structure_with_path` from the `dm-tree` package
  param_vars = tf.nest.map_structure(
    lambda param: tf.Variable(param, trainable=with_gradient),
    params)
  tf_graph = tf.function(lambda inputs: tf_fn(param_vars, inputs),
                         autograph=False,
                         jit_compile=compile_model)

  signatures = {}
  # This signature is needed for TensorFlow Serving use.
  signatures[tf.saved_model.DEFAULT_SERVING_SIGNATURE_DEF_KEY] = \
    tf_graph.get_concrete_function(input_signatures[0])
  for input_signature in input_signatures[1:]:
    # If there are more signatures, trace and cache a TF function for each one
    tf_graph.get_concrete_function(input_signature)
  wrapper = _ReusableSavedModelWrapper(tf_graph, param_vars)
  if with_gradient:
    if not saved_model_options:
      saved_model_options = tf.saved_model.SaveOptions(experimental_custom_gradients=True)
    else:
      saved_model_options.experimental_custom_gradients = True
  tf.saved_model.save(wrapper, model_dir, signatures=signatures,
                      options=saved_model_options)


class _ReusableSavedModelWrapper(tf.train.Checkpoint):
  """Wraps a function and its parameters for saving to a SavedModel.

  Implements the interface described at
  https://www.tensorflow.org/hub/reusable_saved_models.
  """

  def __init__(self, tf_graph, param_vars):
    """Args:

      tf_graph: a tf.function taking one argument (the inputs), which can be
         be tuples/lists/dictionaries of np.ndarray or tensors. The function
         may have references to the tf.Variables in `param_vars`.
      param_vars: the parameters, as tuples/lists/dictionaries of tf.Variable,
         to be saved as the variables of the SavedModel.
    """
    super().__init__()
    # Implement the interface from https://www.tensorflow.org/hub/reusable_saved_models
    self.variables = tf.nest.flatten(param_vars)
    self.trainable_variables = [v for v in self.variables if v.trainable]
    # If you intend to prescribe regularization terms for users of the model,
    # add them as @tf.functions with no inputs to this list. Else drop this.
    self.regularization_losses = []
    self.__call__ = tf_graph
