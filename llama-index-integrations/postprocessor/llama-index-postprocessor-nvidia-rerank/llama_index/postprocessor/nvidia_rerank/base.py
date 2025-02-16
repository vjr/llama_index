from typing import Any, List, Optional, Generator, Literal
import os
from urllib.parse import urlparse, urlunparse

from llama_index.core.bridge.pydantic import Field, PrivateAttr, ConfigDict
from llama_index.core.callbacks import CBEventType, EventPayload
from llama_index.core.instrumentation import get_dispatcher
from llama_index.core.instrumentation.events.rerank import (
    ReRankEndEvent,
    ReRankStartEvent,
)
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import MetadataMode, NodeWithScore, QueryBundle
import requests
import warnings
from llama_index.core.base.llms.generic_utils import get_from_param_or_env

from .utils import (
    RANKING_MODEL_TABLE,
    BASE_URL,
    DEFAULT_MODEL,
    Model,
    determine_model,
)

dispatcher = get_dispatcher(__name__)
KNOWN_URLS = [item.endpoint for item in RANKING_MODEL_TABLE.values()]


class NVIDIARerank(BaseNodePostprocessor):
    """NVIDIA's API Catalog Reranker Connector."""

    model_config = ConfigDict(validate_assignment=True)
    model: Optional[str] = Field(
        description="The NVIDIA API Catalog reranker to use.",
    )
    top_n: Optional[int] = Field(
        default=5,
        ge=0,
        description="The number of nodes to return.",
    )
    max_batch_size: Optional[int] = Field(
        default=64,
        ge=1,
        description="The maximum batch size supported by the inference server.",
    )
    truncate: Optional[Literal["NONE", "END"]] = Field(
        description=(
            "Truncate input text if it exceeds the model's maximum token length. "
            "Default is model dependent and is likely to raise error if an "
            "input is too long."
        ),
        default=None,
    )
    _api_key: str = PrivateAttr("NO_API_KEY_PROVIDED")  # TODO: should be SecretStr
    _mode: str = PrivateAttr("nvidia")
    _is_hosted: bool = PrivateAttr(True)
    base_url: Optional[str] = None

    def __init__(
        self,
        model: Optional[str] = None,
        nvidia_api_key: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = os.getenv("NVIDIA_BASE_URL", BASE_URL),
        **kwargs: Any,
    ):
        """
        Initialize a NVIDIARerank instance.

        This class provides access to a NVIDIA NIM for reranking. By default, it connects to a hosted NIM, but can be configured to connect to an on-premises NIM using the `base_url` parameter. An API key is required for hosted NIM.

        Args:
            model (str): The model to use for reranking.
            nvidia_api_key (str, optional): The NVIDIA API key. Defaults to None.
            api_key (str, optional): The API key. Defaults to None.
            base_url (str, optional): The base URL of the on-premises NIM. Defaults to None.
            truncate (str): "NONE", "END", truncate input text if it exceeds
                            the model's context length. Default is model dependent and
                            is likely to raise an error if an input is too long.
            **kwargs: Additional keyword arguments.

        API Key:
        - The recommended way to provide the API key is through the `NVIDIA_API_KEY` environment variable.
        """
        if not base_url or (base_url in KNOWN_URLS and not model):
            model = model or DEFAULT_MODEL
        super().__init__(model=model, **kwargs)

        self._is_hosted = base_url in KNOWN_URLS
        self.base_url = base_url
        self._api_key = get_from_param_or_env(
            "api_key",
            nvidia_api_key or api_key,
            "NVIDIA_API_KEY",
            "NO_API_KEY_PROVIDED",
        )

        if self._is_hosted:  # hosted on API Catalog (build.nvidia.com)
            if (not self._api_key) or (self._api_key == "NO_API_KEY_PROVIDED"):
                raise ValueError("An API key is required for hosted NIM.")
        else:  # not hosted
            self.base_url = self._validate_url(base_url)

        self.model = model
        if not self.model:
            if self._is_hosted:
                self.model = DEFAULT_MODEL
            else:
                self.__get_default_model()

        if not self.model.startswith("nvdev/"):
            # allow internal models
            # TODO: add test case for this
            self._validate_model(self.model)  ## validate model
        self.base_url = base_url

    def __get_default_model(self):
        """Set default model."""
        if not self._is_hosted:
            valid_models = [
                model.id
                for model in self.available_models
                if not model.base_model or model.base_model == model.id
            ]
            self.model = next(iter(valid_models), None)
            if self.model:
                warnings.warn(
                    f"Default model is set as: {self.model}. \n"
                    "Set model using model parameter. \n"
                    "To get available models use available_models property.",
                    UserWarning,
                )
            else:
                raise ValueError("No locally hosted model was found.")
        else:
            self.model = DEFAULT_MODEL

    def _get_models(self) -> List[Model]:
        session = requests.Session()
        self.base_url = self.base_url.rstrip("/") + "/"
        if self._is_hosted:
            _headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            }
        else:
            _headers = {
                "Accept": "application/json",
            }
        url = (
            "https://integrate.api.nvidia.com/v1/models"
            if self._is_hosted
            else self.base_url.rstrip("/") + "/models"
        )
        response = session.get(url, headers=_headers)
        response.raise_for_status()

        assert (
            "data" in response.json()
        ), "Response does not contain expected 'data' key"
        assert isinstance(
            response.json()["data"], list
        ), "Response 'data' is not a list"
        assert all(
            isinstance(result, dict) for result in response.json()["data"]
        ), "Response 'data' is not a list of dictionaries"
        assert all(
            "id" in result for result in response.json()["data"]
        ), "Response 'rankings' is not a list of dictionaries with 'id'"

        # TODO: hosted now has a model listing, need to merge known and listed models
        # TODO: parse model config for local models
        if not self._is_hosted:
            return [
                Model(
                    id=model["id"],
                    base_model=getattr(model, "params", {}).get("root", None),
                )
                for model in response.json()["data"]
            ]
        else:
            return RANKING_MODEL_TABLE

    def _validate_url(self, base_url):
        """
        validate the base_url.
        if the base_url is not a url, raise an error
        if the base_url does not end in /v1, e.g. /embeddings
        emit a warning. old documentation told users to pass in the full
        inference url, which is incorrect and prevents model listing from working.
        normalize base_url to end in /v1.
        """
        if base_url is not None:
            parsed = urlparse(base_url)

            # Ensure scheme and netloc (domain name) are present
            if not (parsed.scheme and parsed.netloc):
                expected_format = "Expected format is: http://host:port"
                raise ValueError(
                    f"Invalid base_url format. {expected_format} Got: {base_url}"
                )

            normalized_path = parsed.path.rstrip("/")
            if not normalized_path.endswith("/v1"):
                warnings.warn(
                    f"{base_url} does not end in /v1, you may "
                    "have inference and listing issues"
                )
                normalized_path += "/v1"

                base_url = urlunparse(
                    (parsed.scheme, parsed.netloc, normalized_path, None, None, None)
                )
        return base_url

    def _validate_model(self, model_name: str) -> None:
        """
        Validates compatibility of the hosted model with the client.
        Skipping the client validation for non-catalogue requests.

        Args:
            model_name (str): The name of the model.

        Raises:
            ValueError: If the model is incompatible with the client.
        """
        model = determine_model(model_name)
        available_model_ids = [model.id for model in self.available_models]

        if not model:
            if self._is_hosted:
                warnings.warn(f"Unable to determine validity of {model_name}")
            else:
                if model_name not in available_model_ids:
                    raise ValueError(f"No locally hosted {model_name} was found.")

        if model and model.endpoint:
            self.base_url = model.endpoint

    @property
    def available_models(self) -> List[Model]:
        """Get available models."""
        # all available models are in the map
        ids = RANKING_MODEL_TABLE.keys()
        if not self._is_hosted:
            return self._get_models()
        else:
            return [Model(id=id) for id in ids]

    @classmethod
    def class_name(cls) -> str:
        return "NVIDIARerank"

    def _postprocess_nodes(
        self,
        nodes: List[NodeWithScore],
        query_bundle: Optional[QueryBundle] = None,
    ) -> List[NodeWithScore]:
        dispatcher.event(
            ReRankStartEvent(
                query=query_bundle,
                nodes=nodes,
                top_n=self.top_n,
                model_name=self.model,
            )
        )

        if query_bundle is None:
            raise ValueError(
                "Missing query bundle in extra info. Please do not give empty query!"
            )
        if len(nodes) == 0:
            return []

        session = requests.Session()

        _headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

        # TODO: replace with itertools.batched in python 3.12
        def batched(ls: list, size: int) -> Generator[List[NodeWithScore], None, None]:
            for i in range(0, len(ls), size):
                yield ls[i : i + size]

        with self.callback_manager.event(
            CBEventType.RERANKING,
            payload={
                EventPayload.NODES: nodes,
                EventPayload.MODEL_NAME: self.model,
                EventPayload.QUERY_STR: query_bundle.query_str,
                EventPayload.TOP_K: self.top_n,
            },
        ) as event:
            results = []
            for batch in batched(nodes, self.max_batch_size):
                payloads = {
                    "model": self.model,
                    **({"truncate": self.truncate} if self.truncate else {}),
                    "query": {"text": query_bundle.query_str},
                    "passages": [
                        {"text": n.get_content(metadata_mode=MetadataMode.EMBED)}
                        for n in batch
                    ],
                }
                response = session.post(self.base_url, headers=_headers, json=payloads)
                response.raise_for_status()
                # expected response format:
                # {
                #     "rankings": [
                #         {
                #             "index": 0,
                #             "logit": 0.0
                #         },
                #         ...
                #     ]
                # }
                assert (
                    "rankings" in response.json()
                ), "Response does not contain expected 'rankings' key"
                assert isinstance(
                    response.json()["rankings"], list
                ), "Response 'rankings' is not a list"
                assert all(
                    isinstance(result, dict) for result in response.json()["rankings"]
                ), "Response 'rankings' is not a list of dictionaries"
                assert all(
                    "index" in result and "logit" in result
                    for result in response.json()["rankings"]
                ), "Response 'rankings' is not a list of dictionaries with 'index' and 'logit' keys"
                for result in response.json()["rankings"][: self.top_n]:
                    results.append(
                        NodeWithScore(
                            node=batch[result["index"]].node, score=result["logit"]
                        )
                    )
            if len(nodes) > self.max_batch_size:
                results.sort(key=lambda x: x.score, reverse=True)
            results = results[: self.top_n]
            event.on_end(payload={EventPayload.NODES: results})

        dispatcher.event(ReRankEndEvent(nodes=results))
        return results
