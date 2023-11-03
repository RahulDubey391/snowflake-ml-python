import os
from typing import Dict, Optional, Type, cast, final

import cloudpickle
import pandas as pd
from packaging import requirements
from typing_extensions import TypeGuard, Unpack

from snowflake.ml._internal import env_utils, file_utils
from snowflake.ml.model import custom_model, model_signature, type_hints as model_types
from snowflake.ml.model._packager.model_env import model_env
from snowflake.ml.model._packager.model_handlers import _base
from snowflake.ml.model._packager.model_handlers_migrator import base_migrator
from snowflake.ml.model._packager.model_meta import (
    model_blob_meta,
    model_meta as model_meta_api,
    model_meta_schema,
)
from snowflake.ml.model.models import llm


@final
class LLMHandler(_base.BaseModelHandler[llm.LLM]):
    HANDLER_TYPE = "llm"
    HANDLER_VERSION = "2023-12-01"
    _MIN_SNOWPARK_ML_VERSION = "1.0.12"
    _HANDLER_MIGRATOR_PLANS: Dict[str, Type[base_migrator.BaseModelHandlerMigrator]] = {}

    MODELE_BLOB_FILE_OR_DIR = "model"
    LLM_META = "llm_meta"
    IS_AUTO_SIGNATURE = True

    @classmethod
    def can_handle(
        cls,
        model: model_types.SupportedModelType,
    ) -> TypeGuard[llm.LLM]:
        return isinstance(model, llm.LLM)

    @classmethod
    def cast_model(
        cls,
        model: model_types.SupportedModelType,
    ) -> llm.LLM:
        assert isinstance(model, llm.LLM)
        return cast(llm.LLM, model)

    @classmethod
    def save_model(
        cls,
        name: str,
        model: llm.LLM,
        model_meta: model_meta_api.ModelMetadata,
        model_blobs_dir_path: str,
        sample_input: Optional[model_types.SupportedDataType] = None,
        is_sub_model: Optional[bool] = False,
        **kwargs: Unpack[model_types.LLMSaveOptions],
    ) -> None:
        assert not is_sub_model, "LLM can not be sub-model."
        model_blob_path = os.path.join(model_blobs_dir_path, name)
        os.makedirs(model_blob_path, exist_ok=True)
        model_blob_dir_path = os.path.join(model_blob_path, cls.MODELE_BLOB_FILE_OR_DIR)

        sig = model_signature.ModelSignature(
            inputs=[
                model_signature.FeatureSpec(name="input", dtype=model_signature.DataType.STRING),
            ],
            outputs=[
                model_signature.FeatureSpec(name="generated_text", dtype=model_signature.DataType.STRING),
            ],
        )
        model_meta.signatures = {"infer": sig}
        assert os.path.isdir(model.model_id_or_path), "Only model dir is supported for now."
        file_utils.copytree(model.model_id_or_path, model_blob_dir_path)
        with open(
            os.path.join(model_blob_dir_path, cls.LLM_META),
            "wb",
        ) as f:
            cloudpickle.dump(model, f)

        base_meta = model_blob_meta.ModelBlobMeta(
            name=name,
            model_type=cls.HANDLER_TYPE,
            handler_version=cls.HANDLER_VERSION,
            path=cls.MODELE_BLOB_FILE_OR_DIR,
            options=model_meta_schema.LLMModelBlobOptions(
                {
                    "batch_size": model.max_batch_size,
                }
            ),
        )
        model_meta.models[name] = base_meta
        model_meta.min_snowpark_ml_version = cls._MIN_SNOWPARK_ML_VERSION

        pkgs_requirements = [
            model_env.ModelDependency(requirement="transformers", pip_name="transformers"),
            model_env.ModelDependency(requirement="pytorch==2.0.1", pip_name="torch"),
        ]
        if model.model_type == llm.SupportedLLMType.LLAMA_MODEL_TYPE.value:
            pkgs_requirements = [
                model_env.ModelDependency(requirement="sentencepiece", pip_name="sentencepiece"),
                model_env.ModelDependency(requirement="protobuf", pip_name="protobuf"),
                *pkgs_requirements,
            ]
        model_meta.env.include_if_absent(pkgs_requirements)
        # Recent peft versions are only available in PYPI.
        env_utils.append_requirement_list(
            model_meta.env._pip_requirements,
            requirements.Requirement("peft==0.5.0"),
        )

        model_meta.env.cuda_version = kwargs.get("cuda_version", model_env.DEFAULT_CUDA_VERSION)

    @classmethod
    def load_model(
        cls,
        name: str,
        model_meta: model_meta_api.ModelMetadata,
        model_blobs_dir_path: str,
        **kwargs: Unpack[model_types.ModelLoadOption],
    ) -> llm.LLM:
        model_blob_path = os.path.join(model_blobs_dir_path, name)
        if not hasattr(model_meta, "models"):
            raise ValueError("Ill model metadata found.")
        model_blobs_metadata = model_meta.models
        if name not in model_blobs_metadata:
            raise ValueError(f"Blob of model {name} does not exist.")
        model_blob_metadata = model_blobs_metadata[name]
        model_blob_filename = model_blob_metadata.path
        model_blob_dir_path = os.path.join(model_blob_path, model_blob_filename)
        assert model_blob_dir_path, "It must be a directory."
        with open(os.path.join(model_blob_dir_path, cls.LLM_META), "rb") as f:
            m = cloudpickle.load(f)
        assert isinstance(m, llm.LLM)
        # Switch to local path
        m.model_id_or_path = model_blob_dir_path
        return m

    @classmethod
    def convert_as_custom_model(
        cls,
        raw_model: llm.LLM,
        model_meta: model_meta_api.ModelMetadata,
        **kwargs: Unpack[model_types.ModelLoadOption],
    ) -> custom_model.CustomModel:
        import peft
        import transformers

        hub_kwargs = {
            "revision": raw_model.revision,
            "token": raw_model.token,
        }
        model_dir_path = raw_model.model_id_or_path
        hf_model = peft.AutoPeftModelForCausalLM.from_pretrained(  # type: ignore[attr-defined]
            model_dir_path,
            device_map="auto",
            torch_dtype="auto",
            **hub_kwargs,
        )
        peft_config = peft.PeftConfig.from_pretrained(model_dir_path)  # type: ignore[attr-defined]
        base_model_path = peft_config.base_model_name_or_path
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            base_model_path,
            padding_side="right",
            use_fast=False,
            **hub_kwargs,
        )
        hf_model.eval()

        if not tokenizer.pad_token:
            tokenizer.pad_token = tokenizer.eos_token
        # TODO(lhw): migrate away from hf pipeline
        pipe = transformers.pipeline(
            task="text-generation",
            model=hf_model,
            tokenizer=tokenizer,
            batch_size=raw_model.max_batch_size,
        )

        class _LLMCustomModel(custom_model.CustomModel):
            @custom_model.inference_api
            def infer(self, X: pd.DataFrame) -> pd.DataFrame:
                input_data = X.to_dict("list")["input"]
                res = pipe(input_data, return_full_text=False)
                # TODO(lhw): Assume single beam only.
                return pd.DataFrame({"generated_text": [output[0]["generated_text"] for output in res]})

        llm_custom = _LLMCustomModel(custom_model.ModelContext())

        return llm_custom