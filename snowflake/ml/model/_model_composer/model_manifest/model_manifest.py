import collections
import pathlib
from typing import List, Optional, cast

import yaml

from snowflake.ml.model import type_hints
from snowflake.ml.model._model_composer.model_manifest import model_manifest_schema
from snowflake.ml.model._model_composer.model_method import (
    function_generator,
    model_method,
)
from snowflake.ml.model._model_composer.model_runtime import model_runtime
from snowflake.ml.model._packager.model_meta import model_meta as model_meta_api
from snowflake.snowpark import Session


class ModelManifest:
    """Class to construct MANIFEST.yml file for Model

    Attributes:
        workspace_path: A local path where model related files should be dumped to.
        runtimes: A list of ModelRuntime objects managing the runtimes and environment in the MODEL object.
        methods: A list of ModelMethod objects managing the method we registered to the MODEL object.
    """

    MANIFEST_FILE_REL_PATH = "MANIFEST.yml"
    _DEFAULT_RUNTIME_NAME = "python_runtime"

    def __init__(self, workspace_path: pathlib.Path) -> None:
        self.workspace_path = workspace_path

    def save(
        self,
        session: Session,
        model_meta: model_meta_api.ModelMetadata,
        model_file_rel_path: pathlib.PurePosixPath,
        options: Optional[type_hints.ModelSaveOption] = None,
    ) -> None:
        if options is None:
            options = {}
        self.runtimes = [
            model_runtime.ModelRuntime(
                session=session,
                name=ModelManifest._DEFAULT_RUNTIME_NAME,
                model_meta=model_meta,
                imports=[model_file_rel_path],
            )
        ]
        self.function_generator = function_generator.FunctionGenerator(model_file_rel_path=model_file_rel_path)
        self.methods: List[model_method.ModelMethod] = []
        for target_method in model_meta.signatures.keys():
            method = model_method.ModelMethod(
                model_meta=model_meta,
                target_method=target_method,
                runtime_name=self.runtimes[0].name,
                function_generator=self.function_generator,
                options=model_method.get_model_method_options_from_options(options, target_method),
            )

            self.methods.append(method)

        method_name_counter = collections.Counter([method.method_name for method in self.methods])
        dup_method_names = [k for k, v in method_name_counter.items() if v > 1]
        if dup_method_names:
            raise ValueError(
                f"Found duplicate method named resolved as {', '.join(dup_method_names)} in the model. "
                "This might because you have methods with same letters but different cases. "
                "In this case, set case_sensitive as True for those methods to distinguish them."
            )

        manifest_dict = model_manifest_schema.ModelManifestDict(
            manifest_version=model_manifest_schema.MODEL_MANIFEST_VERSION,
            runtimes={runtime.name: runtime.save(self.workspace_path) for runtime in self.runtimes},
            methods=[
                method.save(
                    self.workspace_path,
                    options=function_generator.get_function_generate_options_from_options(
                        options, method.target_method
                    ),
                )
                for method in self.methods
            ],
        )

        with (self.workspace_path / ModelManifest.MANIFEST_FILE_REL_PATH).open("w", encoding="utf-8") as f:
            yaml.safe_dump(manifest_dict, f)

    def load(self) -> model_manifest_schema.ModelManifestDict:
        with (self.workspace_path / ModelManifest.MANIFEST_FILE_REL_PATH).open("r", encoding="utf-8") as f:
            raw_input = yaml.safe_load(f)
        if not isinstance(raw_input, dict):
            raise ValueError(f"Read ill-formatted model MANIFEST, should be a dict, received {type(raw_input)}")

        original_loaded_manifest_version = raw_input.get("manifest_version", None)
        if not original_loaded_manifest_version:
            raise ValueError("Unable to get the version of the MANIFEST file.")

        res = cast(model_manifest_schema.ModelManifestDict, raw_input)

        return res
