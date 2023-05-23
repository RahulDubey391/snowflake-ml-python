import os
from typing import TYPE_CHECKING, Callable, Optional, Sequence, Type, cast

import cloudpickle
import numpy as np
import pandas as pd
from typing_extensions import TypeGuard, Unpack

from snowflake.ml._internal import type_utils
from snowflake.ml.model import (
    _model_meta as model_meta_api,
    custom_model,
    model_signature,
    type_hints as model_types,
)
from snowflake.ml.model._handlers import _base

if TYPE_CHECKING:
    from snowflake.ml.framework.base import BaseEstimator


class _SnowMLModelHandler(_base._ModelHandler["BaseEstimator"]):
    """Handler for SnowML based model.

    Currently snowflake.ml.framework.base.BaseEstimator
        and snowflake.ml.framework.pipeline.Pipeline based classes are supported.
    """

    handler_type = "snowml"
    DEFAULT_TARGET_METHODS = ["predict", "transform", "predict_proba", "predict_log_proba", "decision_function"]

    @staticmethod
    def can_handle(
        model: model_types.SupportedModelType,
    ) -> TypeGuard["BaseEstimator"]:
        return (
            type_utils.LazyType("snowflake.ml.framework.base.BaseEstimator").isinstance(model)
            # Pipeline is inherited from BaseEstimator, so no need to add one more check
        ) and any(
            (hasattr(model, method) and callable(getattr(model, method, None)))
            for method in _SnowMLModelHandler.DEFAULT_TARGET_METHODS
        )

    @staticmethod
    def cast_model(
        model: model_types.SupportedModelType,
    ) -> "BaseEstimator":
        from snowflake.ml.framework.base import BaseEstimator

        assert isinstance(model, BaseEstimator)
        # Pipeline is inherited from BaseEstimator, so no need to add one more check

        return cast("BaseEstimator", model)

    @staticmethod
    def _save_model(
        name: str,
        model: "BaseEstimator",
        model_meta: model_meta_api.ModelMetadata,
        model_blobs_dir_path: str,
        sample_input: Optional[model_types.SupportedDataType] = None,
        is_sub_model: Optional[bool] = False,
        **kwargs: Unpack[model_types.SNOWModelSaveOptions],
    ) -> None:
        from snowflake.ml.framework.base import BaseEstimator

        assert isinstance(model, BaseEstimator)
        # Pipeline is inherited from BaseEstimator, so no need to add one more check

        if not is_sub_model:
            # TODO(xjiang): get model signature from modeling.
            if model_meta._signatures is None:
                # In this case sample_input should be available, because of the check in save_model.
                assert sample_input is not None
                target_methods = kwargs.pop("target_methods", None)
                if target_methods is None:
                    target_methods = [
                        method
                        for method in _SnowMLModelHandler.DEFAULT_TARGET_METHODS
                        if hasattr(model, method) and callable(getattr(model, method, None))
                    ]
                else:
                    for method_name in target_methods:
                        if not callable(getattr(model, method_name, None)):
                            raise ValueError(f"Target method {method_name} is not callable.")
                        if method_name not in _SnowMLModelHandler.DEFAULT_TARGET_METHODS:
                            raise ValueError(f"Target method {method_name} is not supported.")

                model_meta._signatures = {}
                for method_name in target_methods:
                    target_method = getattr(model, method_name)
                    sig = model_signature.infer_signature(sample_input, target_method(sample_input))
                    model_meta._signatures[method_name] = sig
            else:
                for method_name in model_meta._signatures.keys():
                    if not callable(getattr(model, method_name, None)):
                        raise ValueError(f"Target method {method_name} is not callable.")
                    if method_name not in _SnowMLModelHandler.DEFAULT_TARGET_METHODS:
                        raise ValueError(f"Target method {method_name} is not supported.")

        model_blob_path = os.path.join(model_blobs_dir_path, name)
        os.makedirs(model_blob_path, exist_ok=True)
        with open(os.path.join(model_blob_path, _SnowMLModelHandler.MODEL_BLOB_FILE), "wb") as f:
            cloudpickle.dump(model, f)
        base_meta = model_meta_api._ModelBlobMetadata(
            name=name, model_type=_SnowMLModelHandler.handler_type, path=_SnowMLModelHandler.MODEL_BLOB_FILE
        )
        model_meta.models[name] = base_meta
        model_meta._include_if_absent(
            [("scikit-learn", "scikit-learn"), ("xgboost", "xgboost"), ("lightgbm", "lightgbm"), ("joblib", "joblib")]
        )

    @staticmethod
    def _load_model(name: str, model_meta: model_meta_api.ModelMetadata, model_blobs_dir_path: str) -> "BaseEstimator":
        model_blob_path = os.path.join(model_blobs_dir_path, name)
        if not hasattr(model_meta, "models"):
            raise ValueError("Ill model metadata found.")
        model_blobs_metadata = model_meta.models
        if name not in model_blobs_metadata:
            raise ValueError(f"Blob of model {name} does not exist.")
        model_blob_metadata = model_blobs_metadata[name]
        model_blob_filename = model_blob_metadata.path
        with open(os.path.join(model_blob_path, model_blob_filename), "rb") as f:
            m = cloudpickle.load(f)

        from snowflake.ml.framework.base import BaseEstimator

        assert isinstance(m, BaseEstimator)
        return m

    @staticmethod
    def _load_as_custom_model(
        name: str, model_meta: model_meta_api.ModelMetadata, model_blobs_dir_path: str
    ) -> custom_model.CustomModel:
        """Create a custom model class wrap for unified interface when being deployed. The predict method will be
        re-targeted based on target_method metadata.

        Args:
            name: Name of the model.
            model_meta: The model metadata.
            model_blobs_dir_path: Directory path to the whole model.

        Returns:
            The model object as a custom model.
        """
        from snowflake.ml.model import custom_model

        def _create_custom_model(
            raw_model: "BaseEstimator",
            model_meta: model_meta_api.ModelMetadata,
        ) -> Type[custom_model.CustomModel]:
            def fn_factory(
                raw_model: "BaseEstimator",
                output_col_names: Sequence[str],
                target_method: str,
            ) -> Callable[[custom_model.CustomModel, pd.DataFrame], pd.DataFrame]:
                @custom_model.inference_api
                def fn(self: custom_model.CustomModel, X: pd.DataFrame) -> pd.DataFrame:
                    res = getattr(raw_model, target_method)(X)

                    if isinstance(res, list) and len(res) > 0 and isinstance(res[0], np.ndarray):
                        # In case of multi-output estimators, predict_proba(), decision_function(), etc., functions
                        # return a list of ndarrays. We need to concatenate them.
                        res = np.concatenate(res, axis=1)
                    return pd.DataFrame(res, columns=output_col_names)

                return fn

            type_method_dict = {}
            for target_method_name, sig in model_meta.signatures.items():
                type_method_dict[target_method_name] = fn_factory(
                    raw_model, [spec.name for spec in sig.outputs], target_method_name
                )

            _SnowMLModel = type(
                "_SnowMLModel",
                (custom_model.CustomModel,),
                type_method_dict,
            )

            return _SnowMLModel

        raw_model = _SnowMLModelHandler._load_model(name, model_meta, model_blobs_dir_path)
        _SnowMLModel = _create_custom_model(raw_model, model_meta)
        snowml_model = _SnowMLModel(custom_model.ModelContext())

        return snowml_model